#!/usr/bin/env python3
"""cc-statusline — three-line Claude Code status line.

Line 1  identity   model [effort]  cwd  ⎈ kctx(ns)  git-branch (+changes)
Line 2  session    ⛁ context bar % of SIZE │ ↓in ↑out │ +add/-del │ $cost ⏱ dur
Line 3  limits     5h ●●●◑○ 62% ↻2h11m │ 7d ●◑○○○○○ 23% ↻4d6h │ fable ●●●◑○○○ 51% ↻3d1h

Dots FILL  = share of that window's quota consumed (partial dot = quarter steps).
Dots COLOR = red once REDLINE% of the quota is gone no matter how much of the
             window is left; otherwise burn pace measured as used% minus
             elapsed% in percentage points:

               blue   below PACE_BLUE   capacity going to waste
               green  PACE_BLUE..AMBER  sweet spot
               amber  PACE_AMBER..RED   running warm
               red    above PACE_RED    outspending the clock

             Halfway through the 5h window that reads blue below 30%, green
             30-44%, amber 45-60%, red above 60%. Note the bands sit under the
             clock, so sitting exactly on pace is amber, not green — green means
             productive but comfortably inside budget.

             Read the delta as a projection: hold it and you finish the window
             at (100 + delta)% used, so green lands you at 80-94%.
↻rem       = time until that window resets.

The 5h and 7d windows come from Claude Code's stdin JSON. Per-model weekly
buckets (fable, and any future Mythos-tier model) are NOT in stdin — they only
exist on the OAuth usage endpoint, whose `limits` array is the authoritative
list of every window it knows about:

    {"kind": "weekly_scoped", "group": "weekly", "percent": 11,
     "resets_at": "...", "scope": {"model": {"display_name": "Fable"}}}

`group` picks the pacing window (session -> 5h, weekly -> 7d), `percent` is the
quota consumed, and `scope` names the bucket. The endpoint is fetched and cached
for 2 min; the refresh happens in a detached background process and the cached
copy is served immediately, so a slow network never stalls a redraw.

Env knobs:
  CC_SL_FULL CC_SL_Q1 CC_SL_HALF CC_SL_Q3 CC_SL_EMPTY   dot glyphs
  CC_SL_REDLINE      used% that is red on the clock alone (default 85)
  CC_SL_PACE_BLUE    delta below which you're underusing (default -25)
  CC_SL_PACE_AMBER   delta at which warm starts (default -5)
  CC_SL_PACE_RED     delta above which it's red (default 10)
  CC_SL_USAGE_TTL    seconds to cache the usage API (default 120)
  CC_SL_NO_API=1     skip the usage endpoint entirely (5h/7d only)
  CC_SL_HIDE         comma-separated bucket labels to omit, e.g. "7d,sonnet".
                     Empty by default — `limits` only reports live windows.
  CC_SL_NO_K8S=1     skip the kubectl probe
  CC_SL_NO_GIT=1     skip the git probe
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time

E = os.environ.get
TMP = E("TMPDIR", "/tmp")
NOW = int(time.time())
UID = os.getuid() if hasattr(os, "getuid") else 0
USAGE_CACHE = os.path.join(TMP, f"cc-sl-usage-{UID}.json")
USAGE_LOCK = os.path.join(TMP, f"cc-sl-usage-{UID}.lock")
USAGE_TTL = int(float(E("CC_SL_USAGE_TTL", "120") or 120))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# ══ usage endpoint (per-model weekly buckets) ═════════════════════════════════
def sh(args, timeout=3):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def oauth_token():
    """Read the Claude Code OAuth token. Never written to disk by this script."""
    plat = sys.platform
    blob = ""
    if plat == "darwin":
        blob = sh(["security", "find-generic-password",
                   "-s", "Claude Code-credentials", "-w"])
    if not blob:
        for p in (os.path.expanduser("~/.claude/.credentials.json"),
                  os.path.join(E("APPDATA", ""), "Claude", "credentials.json")
                  if E("APPDATA") else ""):
            if p and os.path.isfile(p):
                try:
                    with open(p) as f:
                        blob = f.read()
                    break
                except Exception:
                    pass
    if not blob and plat.startswith("linux"):
        blob = sh(["secret-tool", "lookup", "service", "Claude Code-credentials"])
    if not blob:
        return ""
    try:
        return json.loads(blob).get("claudeAiOauth", {}).get("accessToken", "") or ""
    except Exception:
        return ""


def fetch_usage():
    tok = oauth_token()
    if not tok:
        return None
    import urllib.request
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def refresh_usage_cache():
    """Entry point for the detached background refresh."""
    data = fetch_usage()
    if isinstance(data, dict) and data:
        try:
            fd = os.open(USAGE_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
    try:
        os.unlink(USAGE_LOCK)
    except Exception:
        pass


if "--refresh-usage" in sys.argv:
    refresh_usage_cache()
    sys.exit(0)


def load_usage():
    """Serve the cache immediately; kick off a background refresh if stale."""
    if E("CC_SL_NO_API"):
        return {}
    cached, age = {}, 1e9
    try:
        age = time.time() - os.path.getmtime(USAGE_CACHE)
        with open(USAGE_CACHE) as f:
            cached = json.load(f)
    except Exception:
        pass
    if age > USAGE_TTL:
        try:                       # skip if a refresh is already in flight
            if time.time() - os.path.getmtime(USAGE_LOCK) < 30:
                return cached
        except Exception:
            pass
        try:
            open(USAGE_LOCK, "w").close()
            devnull = subprocess.DEVNULL
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh-usage"],
                             stdin=devnull, stdout=devnull, stderr=devnull,
                             start_new_session=True)
        except Exception:
            pass
    return cached if isinstance(cached, dict) else {}


# ══ input ═════════════════════════════════════════════════════════════════════
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}


def dig(*path, default=None):
    cur = data
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══ colors / glyphs ═══════════════════════════════════════════════════════════
R = "\033[0m"
B = "\033[1m"
DIM = "\033[2;37m"
BLUE = "\033[94m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
AMBER = "\033[38;5;214m"       # pace warning on line 3
ORANGE = "\033[38;5;173m"      # model
EFFORTC = "\033[38;5;179m"     # [effort]
CYAN = "\033[1;36m"            # cwd
K8SC = "\033[2;34m"
GITC = "\033[31m"              # branch
GITN = "\033[2;33m"            # change count
IN_C = "\033[38;5;69m"         # ↓ input tokens
OUT_C = "\033[38;5;141m"       # ↑ output tokens
MONEY = "\033[38;5;220m"
GRY = "\033[38;5;243m"

FULL = E("CC_SL_FULL", "●")
Q1 = E("CC_SL_Q1", "◔")
HALF = E("CC_SL_HALF", "◑")
Q3 = E("CC_SL_Q3", "◕")
EMPTY = E("CC_SL_EMPTY", "○")
REDLINE = int(num(E("CC_SL_REDLINE"), 85))    # used% that is red regardless of clock
# Pace band edges, in percentage points of (used% - elapsed%). Below BLUE you
# are leaving capacity unused; BLUE..AMBER is the sweet spot; AMBER..RED is
# running warm; past RED you are outspending the clock.
PACE_BLUE = int(num(E("CC_SL_PACE_BLUE"), -20))
PACE_AMBER = int(num(E("CC_SL_PACE_AMBER"), -5))
PACE_RED = int(num(E("CC_SL_PACE_RED"), 10))

SEP = f" {DIM}│{R} "
ANSI = re.compile(r"\033\[[0-9;]*m|\033\].*?\033\\")


def vlen(s):
    return len(ANSI.sub("", s))


COLS = int(num(E("COLUMNS"), 0))


# ══ small disk cache (git/kubectl are the only slow local calls) ══════════════
def cached(key, ttl, fn):
    path = os.path.join(TMP, "cc-sl-" + hashlib.md5(key.encode()).hexdigest()[:12])
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    val = fn()
    try:
        with open(path, "w") as f:
            json.dump(val, f)
    except Exception:
        pass
    return val


# ══ LINE 1 — identity ═════════════════════════════════════════════════════════
model = dig("model", "display_name", default="") or "Claude"

effort = dig("effort", "level")
if effort is None:
    eff = dig("effort")
    effort = eff if isinstance(eff, str) else None

cwd = dig("workspace", "current_dir", default="") or dig("cwd", default="") or ""
home = os.path.expanduser("~")
cwd_disp = ("~" + cwd[len(home):]) if home and cwd.startswith(home) else cwd

parts1 = [f"{ORANGE}{model}{R}"]
if effort:
    parts1.append(f"{EFFORTC}[{effort}]{R}")
if cwd_disp:
    parts1.append(f"{CYAN}{cwd_disp}{R}")

if not E("CC_SL_NO_K8S"):
    def _k8s():
        ctx = sh(["kubectl", "config", "current-context"], 2)
        if not ctx:
            return ["", ""]
        return [ctx, sh(["kubectl", "config", "view", "--minify",
                         "-o", "jsonpath={..namespace}"], 2)]

    kctx, kns = cached("k8s", 15, _k8s)
    if kctx:
        parts1.append(f"{K8SC}⎈ {kctx}({kns}){R}" if kns else f"{K8SC}⎈ {kctx}{R}")

if not E("CC_SL_NO_GIT") and cwd:
    def _git():
        g = ["git", "-C", cwd, "--no-optional-locks",
             "-c", "core.useBuiltinFSMonitor=false"]
        if not sh(g + ["rev-parse", "--git-dir"], 2):
            return ["", 0]
        br = sh(g + ["symbolic-ref", "--short", "HEAD"], 2) or \
             sh(g + ["rev-parse", "--short", "HEAD"], 2)
        if not br:
            return ["", 0]
        st = sh(g + ["status", "--porcelain"], 2)
        return [br, len(st.splitlines()) if st else 0]

    branch, changes = cached("git:" + cwd, 5, _git)
    if branch:
        seg = f"{GITC}{branch}{R}"
        if changes:
            seg += f" {GITN}(+{changes}){R}"
        parts1.append(seg)

line1 = " ".join(parts1)


# ══ LINE 2 — this session ═════════════════════════════════════════════════════
def fmt_tok(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_size(n):
    n = int(n)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.0f}M" if v == int(v) else f"{v:.1f}M"
    return f"{n // 1000}K"


def fmt_dur(ms):
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    return f"{m}m" if m else f"{sec}s"


seg2 = []

ctx_pct_raw = dig("context_window", "used_percentage")
if ctx_pct_raw is not None:
    pct = max(0, min(100, round(num(ctx_pct_raw))))
    size = int(num(dig("context_window", "context_window_size"), 200000))
    filled = min(10, (pct + 5) // 10)
    bar = "█" * filled + "░" * (10 - filled)
    cc = GREEN if pct < 60 else (YELLOW if pct <= 80 else RED)
    warn = f" {RED}{B}⚠{R}" if dig("exceeds_200k_tokens", default=False) else ""
    seg2.append(f"⛁ {cc}{bar} {pct}%{R} {DIM}of{R} {ORANGE}{fmt_size(size)}{R}{warn}")

# Snapshot of the latest API response, NOT session running totals: total_input
# is everything currently in the context window (input + both cache buckets, the
# same figure behind used_percentage above), total_output is just the last
# response. Fall back to the current_usage breakdown on builds that omit them.
tin = int(num(dig("context_window", "total_input_tokens")))
tout = int(num(dig("context_window", "total_output_tokens")))
if not tin:
    cu = dig("context_window", "current_usage", default={})
    if isinstance(cu, dict):
        tin = int(sum(num(cu.get(k)) for k in
                      ("input_tokens", "cache_creation_input_tokens",
                       "cache_read_input_tokens")))
        tout = tout or int(num(cu.get("output_tokens")))
if tin or tout:
    seg2.append(f"{IN_C}↓{R} {fmt_tok(tin)}  {OUT_C}↑{R} {fmt_tok(tout)}")

la = int(num(dig("cost", "total_lines_added")))
lr = int(num(dig("cost", "total_lines_removed")))
if la or lr:
    seg2.append(f"{GREEN}+{la}{R}{DIM}/{R}{RED}-{lr}{R}")

# Cost + wall clock — session-scoped, so they live here rather than on line 3.
tail = []
cost = num(dig("cost", "total_cost_usd"))
if cost > 0:
    tail.append(f"{MONEY}${cost:.2f}{R}")
dur = num(dig("cost", "total_duration_ms"))
if dur >= 1000:
    tail.append(f"{GRY}⏱ {fmt_dur(dur)}{R}")
if tail:
    seg2.append("  ".join(tail))

line2 = SEP.join(seg2)


# ══ LINE 3 — usage limits ═════════════════════════════════════════════════════
def parse_ts(v):
    if v is None or v == "null" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        t = float(v)
    else:
        s = str(v).strip()
        if not s:
            return None
        try:
            t = float(s)
        except ValueError:
            try:
                from datetime import datetime
                return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            except Exception:
                return None
    return int(t / 1000.0 if t > 1e11 else t)


def fmt_remaining(sec, daily):
    sec = max(0, int(sec))
    if daily:
        h = (sec + 1800) // 3600
        d, rh = divmod(h, 24)
        if d > 0:
            return f"{d}d{rh}h" if rh else f"{d}d"
    m = (sec + 30) // 60
    h, m = divmod(m, 60)
    return f"{h}h{m}m" if h else f"{m}m"


def render_window(label, win, units, used, reset_ts, mode=0):
    """mode 0 = label+dots+%+reset, 1 = drop reset, 2 = label+% only."""
    u = max(0, min(100, round(num(used))))

    # Dots track quota consumed, floored to the last fully-eaten quarter so a
    # partial glyph never claims usage you haven't reached.
    maxq = units * 4
    full, rem = divmod(min(maxq, u * maxq // 100), 4)
    partial = {1: Q1, 2: HALF, 3: Q3}.get(rem, "")
    dots = FULL * full + partial + EMPTY * (units - full - (1 if partial else 0))

    # Fuel light: past REDLINE% there is no amount of time left that makes it
    # fine, so the absolute reading outranks whatever the pace says.
    if u >= REDLINE:
        col = RED
    elif reset_ts is None:
        # No reset timestamp (e.g. extra usage) — no clock to pace against, so
        # green just means "under the redline" rather than anything about pace.
        col = GREEN
    else:
        elapsed = max(0, min(win, NOW - (reset_ts - win)))
        # Burning the window evenly puts used% at elapsed%, so the signal is the
        # gap between them in percentage points: negative means you're running
        # under budget. Held as (used% - elapsed%) * win to stay in integer math.
        slack = u * win - elapsed * 100
        if slack > PACE_RED * win:
            col = RED
        elif slack >= PACE_AMBER * win:
            col = AMBER
        elif slack >= PACE_BLUE * win:
            col = GREEN
        else:
            col = BLUE

    left = "" if reset_ts is None else fmt_remaining(reset_ts - NOW, win >= 86400)

    if mode >= 2:
        return f"{DIM}{label}{R} {col}{u}%{R}"
    out = f"{label} {col}{dots}{R} {col}{u}%{R}"
    if mode == 0 and left:
        out += f" {DIM}↻{left}{R}"
    return out


DAY, WEEK = 86400, 7 * 86400
RANK = {"fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}
# Pacing geometry per limit group: (window seconds, dot count). `group` rather
# than `kind` is what we switch on, so a kind added later still renders as long
# as it lands in a group we know how to pace.
GROUPS = {"session": (5 * 3600, 5), "weekly": (WEEK, 7)}
HIDE_DEFAULT = ""   # `limits` only reports live windows, so nothing to suppress
windows = []   # (sortkey, label, win_seconds, units, used, reset_ts)

# 5h / 7d come from stdin when Claude Code provides them (v2.1.6+).
stdin_limits = dig("rate_limits", default={})
if isinstance(stdin_limits, dict):
    for key, label, win, units in (("five_hour", "5h", 5 * 3600, 5),
                                   ("seven_day", "7d", WEEK, 7)):
        v = stdin_limits.get(key)
        if isinstance(v, dict) and v.get("used_percentage") is not None:
            windows.append(((0 if key == "five_hour" else 1, ""), label, win, units,
                            v["used_percentage"], parse_ts(v.get("resets_at"))))

usage = load_usage()
have = {w[1] for w in windows}


def limit_label(entry):
    """5h / 7d for the account-wide windows, else whatever the entry scopes to."""
    kind = entry.get("kind")
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "7d"
    scope = entry.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict) and model.get("display_name"):
            return str(model["display_name"]).lower()
        if scope.get("surface"):
            return str(scope["surface"]).lower()
    return str(kind or "?").lower()


# Every window the endpoint reports lives in `limits`: one entry per bucket
# carrying `group`, `percent`, `resets_at`, and for scoped buckets the model or
# surface it applies to. Labels stdin already supplied win, so a live 5h/7d is
# never overwritten by the (up to 2 min stale) cache.
for entry in usage.get("limits") or []:
    if not isinstance(entry, dict) or entry.get("percent") is None:
        continue
    geom = GROUPS.get(entry.get("group"))
    if not geom:
        continue
    label = limit_label(entry)
    if label in have:
        continue
    have.add(label)
    kind = entry.get("kind")
    if kind == "session":
        order = (0, "")
    elif kind == "weekly_all":
        order = (1, "")
    else:
        order = (2, f"{RANK.get(label, 9)}{label}")
    win, units = geom
    windows.append((order, label, win, units,
                    entry["percent"], parse_ts(entry.get("resets_at"))))

extra = usage.get("extra_usage")
if isinstance(extra, dict) and extra.get("is_enabled") and extra.get("utilization") is not None:
    windows.append(((3, ""), "extra", WEEK, 7,
                    extra["utilization"], parse_ts(extra.get("resets_at"))))

# Drop buckets you don't care to watch. Unset -> HIDE_DEFAULT; explicit empty
# string -> show everything the endpoint returns.
_h = E("CC_SL_HIDE")
hide = {x.strip().lower() for x in (HIDE_DEFAULT if _h is None else _h).split(",") if x.strip()}
if hide:
    windows = [w for w in windows if w[1].lower() not in hide]

windows.sort(key=lambda w: w[0])
line3 = SEP.join(render_window(*w[1:]) for w in windows)

# Too many buckets for the terminal? Shed the ↻ column, then the dots. Labels
# always survive — six anonymous gauges tell you nothing about which is which.
if COLS and vlen(line3) > COLS:
    for mode in (1, 2):
        line3 = SEP.join(render_window(*w[1:], mode=mode) for w in windows)
        if vlen(line3) <= COLS:
            break

# ── emit ──────────────────────────────────────────────────────────────────────
for ln in (line1, line2, line3):
    if ln.strip():
        print(ln)
