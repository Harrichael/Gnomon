#!/usr/bin/env python3
"""cc-statusline — three-line Claude Code status line.

Line 1  identity   model [effort]  cwd  ⎈ kctx(ns)  git-branch (+changes)
Line 2  session    ⛁ bar % of SIZE │ ↓ctx (+added)  ⇧up │ +add/-del │ $cost (+spent) ⏱ dur
Line 2b agents     ⇉ model  ███░░░ 38%  ⇧up  ⇩out  ⏱ran  (+$spent)  name ✓
Line 3  limits     5h ●●●◑○ 62% ↻2h11m │ 7d ●◑○○○○○ 23% ↻4d6h │ fable ●●●◑○○○ 51% ↻3d1h

Line 2b appears once per subagent this turn and disappears when none ran. A turn
that delegates would otherwise read as a flat context bar and a still transcript
while the cost climbed: the agents' traffic is real but lands in their own files.
Rows persist until the next prompt so a finished agent can still be read; ✓ marks
one that is done. Their cost is measured a redraw at a time rather than modelled
from a price table — see attribute().

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
for CC_SL_USAGE_TTL seconds (30 by default); the refresh happens in a detached background process and the cached
copy is served immediately, so a slow network never stalls a redraw.

Line 2's parenthesised figures are scoped to ONE HUMAN TURN — your prompt plus
every tool call the agent made under it — keyed on stdin's prompt_id. That is the
boundary you control; the bare stdin token counts describe only the last API call
of 3-17, so they barely budge however much work happens. ⇧ counts fresh input
plus cache writes and skips cache reads, which are ~99% of the bytes at a tenth
of the price. Tokens come free from a bounded backward walk of the transcript;
only the cost baseline needs disk, in one shared SQLite file keyed by
(session, turn) that expires its own rows. Subagents get their own transcripts
under <session-id>/subagents/, tagged with the parent's promptId, so delegated
work lands in ⇧ for the turn that asked for it.

Env knobs:
  CC_SL_FULL CC_SL_Q1 CC_SL_HALF CC_SL_Q3 CC_SL_EMPTY   dot glyphs
  CC_SL_REDLINE      used% that is red on the clock alone (default 85)
  CC_SL_PACE_BLUE    delta below which you're underusing (default -20)
  CC_SL_PACE_AMBER   delta at which warm starts (default -5)
  CC_SL_PACE_RED     delta above which it's red (default 10)
  CC_SL_USAGE_TTL    seconds to cache the usage API (default 30). Only the
                     model-scoped buckets depend on it -- 5h/7d come from
                     stdin and are fresh on every redraw. The cache is
                     shared per-UID, so extra sessions cost no extra calls.
  CC_SL_NO_API=1     skip the usage endpoint entirely (5h/7d only)
  CC_SL_HIDE         comma-separated bucket labels to omit, e.g. "7d,sonnet".
                     Empty by default — `limits` only reports live windows.
  CC_SL_STATE_TTL    seconds of session inactivity before its rows are GC'd
                     (default 604800). Only whole quiet sessions expire, and
                     never the running one.
  CC_SL_STATE_DB     override the state file (default ~/.claude/statusline-state.db)
  CC_SL_SCAN_CAP     bytes of transcript the turn scan may walk (default 4M)
  CC_SL_NO_STATE=1   skip SQLite entirely; drops the $ delta, keeps ↓/⇧
  CC_SL_NO_AGENTS=1  drop the per-subagent rows; output returns to three lines
  CC_SL_AGENT_MAX    subagent rows before "… +N more" (default 6, 0 = unlimited)
  CC_SL_AGENT_STALE  seconds of agent silence before it counts as finished
                     (default 300; 0 disables the reap)
  CC_SL_AGENT_CTX    context-window denominator for an agent whose window can't
                     be inferred (default 200000)
  CC_SL_RATE_HALFLIFE  seconds after which learned per-model price evidence is
                     worth half as much (default 14 days; 0 = keep only the
                     newest sample)
  CC_SL_NO_K8S=1     skip the kubectl probe
  CC_SL_NO_GIT=1     skip the git probe
"""

import glob
import hashlib
import json
import math
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
USAGE_TTL = int(float(E("CC_SL_USAGE_TTL", "30") or 30))
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
RED = "\033[31m"
GREEN = "\033[38;5;114m"    # bar under 60%, +lines, agent done
AMBER = "\033[38;5;214m"       # context bar mid band, pace warning
ORANGE = "\033[38;5;173m"      # model
EFFORTC = "\033[38;5;179m"     # [effort]
CYAN = "\033[1;36m"            # cwd
K8SC = "\033[2;34m"
GITC = "\033[31m"              # branch
GITN = "\033[2;33m"            # change count
IN_C = "\033[38;5;69m"         # ↓ input tokens
OUT_C = "\033[38;5;141m"       # ↑ output tokens
GEN_C = "\033[38;5;108m"       # ⇩ tokens an agent generated
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

# Context-bar bands. The colour has to answer a question that changes with the
# window size. On 200K it is "how close am I to compaction?" -- hence 60/80. On
# 1M that reading is useless: 60% is 600K, which a session realistically never
# reaches, so the bar sits green forever. What matters on a big window is "how
# much does every turn cost now?", and the honest lines there are absolute:
# 200K (one standard window resident) and 500K.
#
# Anchor both readings and interpolate between them, so a model shipping 400K or
# 2M lands somewhere sensible instead of on whichever anchor was hardcoded.
CTX_A = (200000, 0.60, 0.80)      # 200K: green <120K, amber <160K
CTX_B = (1000000, 0.20, 0.50)     # 1M:   green <200K, amber <500K


def ctx_bands(size):
    """(green_below, amber_below), as percentages of a `size`-token window.

    Interpolated log-log on the ABSOLUTE edges, not on the percentages: the
    band edges are token counts that grow sub-linearly with the window, which is
    exactly what lets 200K and 1M both read correctly off one curve. Below the
    small anchor the percentages hold flat — extrapolating downward would turn a
    100K window 96% green, which is the opposite of the point.

        200K -> 60% / 80%        500K -> 32% / 61%
        400K -> 37% / 67%          1M -> 20% / 50%
                                   2M -> 12% / 41%
    """
    s = int(num(size, CTX_A[0])) or CTX_A[0]
    if s <= CTX_A[0]:
        return CTX_A[1] * 100.0, CTX_A[2] * 100.0
    x = math.log(float(s) / CTX_A[0]) / math.log(float(CTX_B[0]) / CTX_A[0])
    edge = []
    for i in (1, 2):
        a, b = CTX_A[0] * CTX_A[i], CTX_B[0] * CTX_B[i]
        edge.append(a * (b / a) ** x * 100.0 / s)
    return edge[0], edge[1]


STATE_DB = os.path.expanduser(E("CC_SL_STATE_DB")
                              or "~/.claude/statusline-state.db")
STATE_TTL = int(num(E("CC_SL_STATE_TTL"), 604800))
# Ceiling on the backward transcript walk, in bytes.
SCAN_CAP = int(num(E("CC_SL_SCAN_CAP"), 4 << 20))
# Subagent rows. STALE is generous on purpose: a live agent blocked inside a
# long tool call writes nothing for minutes, and measured gaps inside genuinely
# running agents already reach ~110s on short read-only work.
AGENT_STALE = int(num(E("CC_SL_AGENT_STALE"), 300))
AGENT_MAX = int(num(E("CC_SL_AGENT_MAX"), 6))
AGENT_CTX = int(num(E("CC_SL_AGENT_CTX"), 200000))
# Half-life of learned per-model price evidence. A lifetime average gets
# stiffer with every sample and would sit on a stale price forever; decaying
# the old evidence lets a repricing wash through in a few weeks.
RATE_HALFLIFE = int(num(E("CC_SL_RATE_HALFLIFE"), 14 * 86400))
CK = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

SEP = f" {DIM}│{R} "
# Leading ESC, not decoration: Claude Code trim()s every line, so plain spaces
# would vanish. trim() stops at the escape, and the reset also cancels the SGR
# carry-over the renderer applies from the line above.
PAD = f"{R}  "
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


def weigh(up, cr, out):
    """A price-shaped proxy for tokens, used only to split one redraw interval.

    Cache writes cost ~1.25x fresh input, cache reads a tenth, output ~5x. `up`
    folds input and cache writes together -- writes dominate it by four orders of
    magnitude, so pricing the pair at the write rate is close enough for a split.
    Absolute scale is irrelevant: every use divides one weight by a sum of them.
    """
    return 1.25 * up + 0.1 * cr + 5.0 * out


def agent_meta(jsonl_path):
    """`description` / `spawnDepth` / `parentAgentId` from the sibling meta file."""
    try:
        with open(jsonl_path[:-6] + ".meta.json", "rb") as fh:
            m = json.loads(fh.read())
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def subagent_scan(path, pid, seen):
    """Per-agent traffic for this turn, plus the shared upload total.

    Claude Code writes each agent to its own transcript beside the parent's, one
    file per agent and nested agents too:

        <dir>/<session-id>.jsonl                    <- parent
        <dir>/<session-id>/subagents/agent-*.jsonl  <- one per agent

    Every entry in those files carries the PARENT's promptId, so a file belongs
    wholly to one turn and its first line settles which. Skipping on that keeps
    a foreign agent to one readline instead of a 400KB read.

    Don't be tempted by `toolUseResult.totalTokens` in the parent transcript: it
    is the agent's final request context size, not its cumulative spend, it
    omits nested agents, and it only appears once the agent finishes.

    Two independent dedupes, on purpose. `seen` is shared with the parent scan
    so the line-2 total counts each response once across every file. The
    per-file dicts are separate because folding them into `seen` would zero an
    agent's own numbers whenever the parent happened to touch that id first.
    Uploaded takes first-wins (input/cache_creation repeat identically), but
    OUTPUT must take the max: the early streaming records for a response carry a
    placeholder of 1-5 tokens, so first-wins reports 218 where the truth is
    28,425.

    Returns (up_total, recs, child_done, child_res). child_done is agent ids
    whose completion was recorded inside another AGENT's file, which is where a
    nested agent's result lands rather than in the parent transcript; child_res
    maps agent id to resolvedModel, the only source of the [1m] window marker.
    """
    recs, up_total, child_done, child_res = [], 0, set(), {}
    try:
        base = path[:-6] if path.endswith(".jsonl") else path
        files = glob.glob(os.path.join(base, "subagents", "agent-*.jsonl"))
    except Exception:
        return 0, [], child_done, child_res
    for f in files:
        try:
            st = os.stat(f)
            if st.st_size > SCAN_CAP:
                continue
            with open(f, "rb") as fh:
                owner = None
                for _ in range(4):      # first parseable entry names the turn
                    ln = fh.readline()
                    if not ln:
                        break
                    if not ln.strip():
                        continue
                    try:
                        owner = json.loads(ln).get("promptId")
                    except Exception:
                        continue
                    break
                if owner != pid:
                    continue            # someone else's agent: never read the body
                fh.seek(0)
                lines = fh.read().split(b"\n")
            up, out, cr = {}, {}, {}
            up_free = 0                 # records with no id to fold on
            aid = model = stop = t0 = tend = None
            ctx = 0
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if t0 is None:
                    t0 = o.get("timestamp")
                tend = o.get("timestamp") or tend
                aid = aid or o.get("agentId")
                t = o.get("toolUseResult")
                # An async agent gets a toolUseResult at LAUNCH, so the record
                # existing is not proof of death -- only its status is.
                if isinstance(t, dict) and t.get("agentId"):
                    if t.get("status") != "async_launched":
                        child_done.add(t["agentId"])
                    if t.get("resolvedModel"):
                        child_res[t["agentId"]] = t["resolvedModel"]
                m = o.get("message") or {}
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                rid = m.get("id") or o.get("requestId")
                inp = num(u.get("input_tokens")) + num(u.get("cache_creation_input_tokens"))
                if rid is None:
                    up_total += int(inp)
                    up_free += inp
                else:
                    if rid not in seen:
                        seen.add(rid)
                        up_total += int(inp)
                    if rid not in up:
                        up[rid] = inp
                    cr[rid] = num(u.get("cache_read_input_tokens"))
                    out[rid] = max(out.get(rid, 0), num(u.get("output_tokens")))
                if o.get("type") == "assistant":
                    ctx = int(sum(num(u.get(k)) for k in CK))
                    model = m.get("model") or model
                    stop = m.get("stop_reason")
            meta = agent_meta(f)
            recs.append({
                "aid": aid or os.path.basename(f)[6:-6],
                "depth": max(1, int(num(meta.get("spawnDepth"), 1))),
                "par": meta.get("parentAgentId"),
                "name": (meta.get("description") or meta.get("agentType")
                         or "agent"),
                "model": model or "",
                "ctx": ctx,
                "up": int(sum(up.values()) + up_free),
                "cr": int(sum(cr.values())),
                "out": int(sum(out.values())),
                "stop": stop,
                "t0": t0,
                "tend": tend,
                "mtime": int(st.st_mtime),
            })
        except Exception:
            continue
    return up_total, recs, child_done, child_res


def agent_done(rec, done_ids):
    """Finished? Used only for the tick mark, so a wrong answer costs a glyph.

    Only `end_turn` ends a turn -- `max_tokens`, `pause_turn` and `tool_use` all
    continue, and a null stop_reason is normal mid-stream. Staleness is the only
    signal that reaps an agent someone cancelled, whose file just stops.
    """
    return bool(rec["aid"] in done_ids
                or rec["stop"] == "end_turn"
                or (AGENT_STALE > 0 and NOW - rec["mtime"] > AGENT_STALE))


def turn_tokens(path, pid, chunk=65536):
    """Tokens uploaded during the current human turn + the context it started at.

    Walks the transcript backward so the work is bounded by one turn rather than
    the whole session. The span is delimited at BOTH ends by promptId, which only
    ever appears on `user` entries — tool results included, assistant entries
    never. Two traps live in that asymmetry:

      * Don't cut the span short. An `assistant` entry sitting before the turn's
        first `user` entry usually belongs to the SAME turn, so "something exists
        before us" is not proof of a boundary. Only a `user` entry carrying a
        DIFFERENT promptId proves it.
      * Don't let the span run past the turn's end, or every later turn is summed
        in too.

    Uploaded counts fresh input + cache writes and skips cache reads: reads are
    ~99% of the bytes but a tenth of the price, so including them would track how
    hard the agent worked instead of what it cost.

    Returns (uploaded, prev_ctx, agents, parent_weight). prev_ctx is None when
    the preceding turn's context size can't be established — the caller must then show no delta
    rather than invent a baseline.
    """
    if not path or not pid:
        return 0, None, [], 0.0
    try:
        size = os.path.getsize(path)
        pos, buf, objs, start = size, b"", [], None
        with open(path, "rb") as f:
            while pos > 0 and size - pos < SCAN_CAP:
                rd = min(chunk, pos)
                pos -= rd
                f.seek(pos)
                parts = (f.read(rd) + buf).split(b"\n")
                if pos == 0:            # first line of the file, nothing precedes it
                    buf, raw = b"", parts
                else:
                    buf, raw = parts[0], parts[1:]
                fresh = []
                for ln in raw:
                    if ln.strip():
                        try:
                            fresh.append(json.loads(ln))
                        except Exception:
                            pass
                objs = fresh + objs     # prepend, never reparse: O(n), not O(n^2)
                start = next((i for i, o in enumerate(objs)
                              if o.get("type") == "user"
                              and o.get("promptId") == pid), None)
                if start is not None and any(
                        o.get("type") == "user" and o.get("promptId")
                        and o["promptId"] != pid for o in objs[:start]) and any(
                        isinstance((o.get("message") or {}).get("usage"), dict)
                        for o in objs[:start]):
                    break
        if start is None:
            return 0, None, [], 0.0
        end = next((i for i in range(start + 1, len(objs))
                    if objs[i].get("type") == "user" and objs[i].get("promptId")
                    and objs[i]["promptId"] != pid), len(objs))
        prev = None
        for o in reversed(objs[:start]):
            u = (o.get("message") or {}).get("usage")
            if isinstance(u, dict):
                prev = int(sum(num(u.get(k)) for k in CK))
                break
        up, seen, parent_done, parent_res = 0, set(), set(), {}
        pcr, pout = {}, {}
        for o in objs[start:end]:
            t = o.get("toolUseResult")
            if isinstance(t, dict) and t.get("agentId"):
                if t.get("status") != "async_launched":
                    parent_done.add(t["agentId"])
                if t.get("resolvedModel"):
                    parent_res[t["agentId"]] = t["resolvedModel"]
            m = o.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            # One API response is written as several entries -- one per content
            # block (thinking / text / each tool_use) -- all repeating the SAME
            # usage. Summing per entry inflates the turn 2-4x, so fold on the
            # response id and count each response once.
            rid = m.get("id") or o.get("requestId")
            if rid is not None:
                # Recorded before the dedupe gate: output needs max-per-id, and
                # these feed only the cost split, never the displayed totals.
                pcr[rid] = num(u.get("cache_read_input_tokens"))
                pout[rid] = max(pout.get(rid, 0), num(u.get("output_tokens")))
                if rid in seen:
                    continue
                seen.add(rid)
            up += int(num(u.get("input_tokens"))
                      + num(u.get("cache_creation_input_tokens")))
        pw = weigh(up, sum(pcr.values()), sum(pout.values()))
        sub_up, agents, child_done, child_res = subagent_scan(path, pid, seen)
        up += sub_up
        done_ids = parent_done | child_done
        res = dict(child_res); res.update(parent_res)
        for a in agents:
            a["done"] = agent_done(a, done_ids)
            a["res"] = res.get(a["aid"], "")
        agents.sort(key=lambda a: (a["t0"] or "", a["aid"]))
        return up, prev, agents, pw
    except Exception:
        return 0, None, [], 0.0


def clamp_base(base, cost_now):
    """A stored baseline above the live reading means cost restarted under us.

    `--resume` keeps the session_id but starts a fresh process, and
    total_cost_usd accumulates per process — so a session resumed after $40 of
    work comes back reading cents. Verified on 2.1.226: resuming preserved the
    session_id and reported the new turn's cost alone. Left alone, the stale
    baseline would swallow every delta until spending passed the old total.
    """
    return cost_now if base > cost_now else base


def attribute(con, sid, pid, spent, agents, parent_w, main_model):
    """Split the money spent since the last redraw across whoever did work.

    Per-agent cost is recorded nowhere, and a price table fitted to turn-level
    history does not survive contact with the data — parent and agent spend are
    entangled there, and the residuals come out the same size as the values. So
    measure it one redraw interval at a time instead: whatever the cumulative
    cost moved by belongs to whoever's token count moved with it.

    When exactly ONE participant grew, that interval is exact and needs no
    weighting at all — the common case, since sequential delegation only ever
    has one thing running. It is also the only clean observation of a model's
    price, so those intervals are the sole input to `model_rate`; anything with
    two movers is contaminated and is never used for calibration.

    Concurrent movers split the interval by weighted growth, priced with the
    rates learned so far. An uncalibrated model falls back to the mean of the
    known rates rather than a bare 1.0, which would otherwise dwarf real rates
    (dollars per token are ~1e-6) and hand it the whole interval.

    The parent rides along as agent_id "" so the parts always sum to the turn.
    """
    prior = {r[0]: (float(r[1]), float(r[2])) for r in con.execute(
        "SELECT agent_id, cost, w_last FROM agent "
        "WHERE session_id=? AND prompt_id=?", (sid, pid))}
    parts = [("", main_model, parent_w)]
    for a in agents:
        parts.append((a["aid"], a["model"],
                      weigh(a["up"], a.get("cr", 0), a["out"])))
    grew = [(aid, mdl, w, w - prior.get(aid, (0.0, 0.0))[1]) for aid, mdl, w in parts]
    grew = [g for g in grew if g[3] > 0]

    share = {}
    if spent > 0:
        if len(grew) == 1:
            aid, mdl, _w, dw = grew[0]
            share[aid] = spent
            # A lone agent is always a clean observation. The parent is clean
            # only on a turn that spawned nothing at all -- otherwise its row
            # also carries money banked from unattributable intervals, and the
            # sample would price the parent's model with an agent's spend.
            if mdl and dw > 0 and (aid or not agents):
                r = con.execute("SELECT w, usd, updated_at FROM model_rate "
                                "WHERE model=?", (mdl,)).fetchone()
                w0 = u0 = 0.0
                if r:
                    # Age the stored evidence before adding to it. Without this
                    # the rate is a lifetime mean: a price change two months in
                    # would be outvoted by every sample that came before it and
                    # would never be learned.
                    age = max(0, NOW - int(num(r[2])))
                    k = (0.5 ** (float(age) / RATE_HALFLIFE)) if RATE_HALFLIFE > 0 else 0.0
                    w0, u0 = float(r[0]) * k, float(r[1]) * k
                con.execute("INSERT OR REPLACE INTO model_rate VALUES (?,?,?,?)",
                            (mdl, w0 + dw, u0 + spent, NOW))
        elif len(grew) > 1:
            known = {}
            for m_, w_, u_ in con.execute("SELECT model, w, usd FROM model_rate"):
                if float(w_) > 0:
                    known[m_] = float(u_) / float(w_)
            dflt = (sum(known.values()) / len(known)) if known else 1.0
            wts = [(aid, dw * known.get(mdl, dflt)) for aid, mdl, _w, dw in grew]
            tot = sum(x[1] for x in wts)
            for aid, x in wts:
                share[aid] = spent * x / tot if tot > 0 else 0.0
            if tot <= 0:
                share[""] = spent
        else:
            # Nobody's tokens moved. The transcript lags stdin by a flush, so
            # money can land before the records that explain it — bank it on the
            # parent rather than lose it, or the parts stop summing to the turn.
            share[""] = spent

    out = {}
    for aid, _mdl, w in parts:
        prev_cost, prev_w = prior.get(aid, (0.0, 0.0))
        cost = prev_cost + share.get(aid, 0.0)
        # Advance w_last ONLY on an interval that had money in it. Redraws are
        # polled on a timer and stdin's cost lags the transcript flush, so many
        # intervals see tokens grow with spent == 0. Banking that growth for
        # free stranded it: the money landed a tick later with dw == 0, the
        # agent read as a no-mover, and its whole spend fell to the parent.
        # Measured on one real turn -- 1.36M weighted agent tokens credited at
        # $0.00. Holding w_last back keeps the growth claimable until an
        # interval can pay for it. updated_at still moves so GC sees a live row.
        con.execute("INSERT OR REPLACE INTO agent VALUES (?,?,?,?,?,?)",
                    (sid, pid, aid, cost, w if spent > 0 else prev_w, NOW))
        if aid:
            out[aid] = cost
    return out


def cost_delta(sid, pid, cost_now, agents=(), parent_w=0.0, main_model=""):
    """Dollars burned since the current human turn began, or None.

    Cost is the one figure absent from the transcript, so it needs a baseline on
    disk. One shared SQLite file keyed by (session, turn) gives every session a
    single place to expire from instead of a litter of per-session files. Every
    failure path returns None so the caller drops the delta and leaves the rest
    of the line untouched — a status line must never crash or hang.

    Returns (turn_delta, {agent_id: dollars}); (None, {}) on any failure.
    """
    if E("CC_SL_NO_STATE") or not sid or not pid:
        return None, {}
    con = None
    try:
        import sqlite3
        con = sqlite3.connect(STATE_DB, timeout=0.25, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=250")
        con.execute("""CREATE TABLE IF NOT EXISTS turn (
                         session_id TEXT NOT NULL,
                         prompt_id  TEXT NOT NULL,
                         cost_start REAL NOT NULL,
                         cost_last  REAL NOT NULL,
                         updated_at INTEGER NOT NULL,
                         PRIMARY KEY (session_id, prompt_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS turn_age ON turn(updated_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS agent (
                         session_id TEXT NOT NULL,
                         prompt_id  TEXT NOT NULL,
                         agent_id   TEXT NOT NULL,
                         cost       REAL NOT NULL,
                         w_last     REAL NOT NULL,
                         updated_at INTEGER NOT NULL,
                         PRIMARY KEY (session_id, prompt_id, agent_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS agent_age ON agent(updated_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS model_rate (
                         model      TEXT PRIMARY KEY,
                         w          REAL NOT NULL,
                         usd        REAL NOT NULL,
                         updated_at INTEGER NOT NULL)""")
        row = con.execute("SELECT cost_start, cost_last FROM turn "
                          "WHERE session_id=? AND prompt_id=?", (sid, pid)).fetchone()
        if row is None:
            # New turn. Baseline off the previous turn's last reading rather than
            # this instant: redraws are event-driven, so by the time a new
            # prompt_id first shows up some of its cost has already landed. With
            # no prior turn the session baseline is a true zero.
            # updated_at is second-granular and bumped on every redraw, so it
            # cannot order turns by itself: the last redraw of turn N and the
            # first of turn N+1 land in the same second often enough to matter.
            # rowid breaks the tie in true insert order.
            prev = con.execute("SELECT cost_last FROM turn WHERE session_id=? "
                               "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                               (sid,)).fetchone()
            # No prior turn means the baseline is UNKNOWN, not zero. Zero bills
            # one turn for the whole session, because total_cost_usd accumulates
            # per process — so a session whose rows expired, or that predates
            # this file, reads high through no fault of the turn. Anchoring on
            # the live reading costs only the sliver spent before first redraw.
            base = float(prev[0]) if prev else cost_now
            base = clamp_base(base, cost_now)
            prev_read = base            # first sight: the turn starts here
            con.execute("INSERT OR REPLACE INTO turn VALUES (?,?,?,?,?)",
                        (sid, pid, base, cost_now, NOW))
            # GC on turn boundaries only — once per human prompt, not per redraw.
            # Never the running session, and only sessions that have gone quiet
            # as a WHOLE: expiring individual rows by age lets one session
            # harvest another live session's history, its baseline included.
            con.execute("DELETE FROM turn WHERE session_id <> ? AND session_id IN ("
                        " SELECT session_id FROM turn GROUP BY session_id"
                        "  HAVING MAX(updated_at) < ?)", (sid, NOW - STATE_TTL))
            con.execute("DELETE FROM agent WHERE session_id <> ? AND session_id IN ("
                        " SELECT session_id FROM agent GROUP BY session_id"
                        "  HAVING MAX(updated_at) < ?)", (sid, NOW - STATE_TTL))
        else:
            # Write the clamp back so a re-anchored turn stays re-anchored.
            base = clamp_base(float(row[0]), cost_now)
            prev_read = max(base, float(row[1]))
            con.execute("UPDATE turn SET cost_start=?, cost_last=?, updated_at=? "
                        "WHERE session_id=? AND prompt_id=?",
                        (base, cost_now, NOW, sid, pid))
        costs = {}
        try:
            # Isolated: a failure here must cost the per-agent figures only, not
            # the turn delta that line 2 depends on.
            costs = attribute(con, sid, pid, max(0.0, cost_now - prev_read),
                              agents, parent_w, main_model)
        except Exception:
            costs = {}
        return max(0.0, cost_now - base), costs
    except Exception:
        return None, {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# Per-turn figures are scoped to one human prompt and everything the agent did
# under it. That is the boundary you actually control — the stdin token counts
# describe only the last API call, so they barely move however much work happens.
prompt_id = dig("prompt_id", default="") or ""
session_id = dig("session_id", default="") or ""
transcript = dig("transcript_path", default="") or ""

bar_txt = ""
ctx_pct_raw = dig("context_window", "used_percentage")
if ctx_pct_raw is not None:
    pct = max(0, min(100, round(num(ctx_pct_raw))))
    size = int(num(dig("context_window", "context_window_size"), 200000))
    filled = min(10, (pct + 5) // 10)
    bar = "█" * filled + "░" * (10 - filled)
    lo, hi = ctx_bands(size)
    cc = GREEN if pct < lo else (AMBER if pct <= hi else RED)
    warn = f" {RED}{B}⚠{R}" if dig("exceeds_200k_tokens", default=False) else ""
    bar_txt = f"⛁ {cc}{bar} {pct}%{R} {DIM}of{R} {ORANGE}{fmt_size(size)}{R}{warn}"

# ↓ is a LEVEL: everything sitting in the context window right now (input + both
# cache buckets, the same figure behind used_percentage above). Its parens are a
# FLOW: how much this human turn added to that level. Anchor the delta on the
# stdin level rather than the transcript's last entry so the number and its delta
# can never disagree — the transcript can lag stdin by a flush.
tin = int(num(dig("context_window", "total_input_tokens")))
if not tin:
    cu = dig("context_window", "current_usage", default={})
    if isinstance(cu, dict):
        tin = int(sum(num(cu.get(k)) for k in CK))

up, prev_ctx, agents, parent_w = turn_tokens(transcript, prompt_id)

ctx_txt = ctx_extra = ""
if tin:
    ctx_txt = f"{IN_C}↓{R} {fmt_tok(tin)}"
    # Only ever a surplus. /compact shrinks the window, and "(+-12K)" is noise.
    if prev_ctx is not None and tin > prev_ctx:
        ctx_extra = f" {DIM}(+{fmt_tok(tin - prev_ctx)}){R}"

up_txt = f"{OUT_C}⇧{R} {fmt_tok(up)}" if up else ""

la = int(num(dig("cost", "total_lines_added")))
lr = int(num(dig("cost", "total_lines_removed")))
lines_txt = f"{GREEN}+{la}{R}{DIM}/{R}{RED}-{lr}{R}" if (la or lr) else ""

# Cost + wall clock — session-scoped, so they live here rather than on line 3.
money_txt = money_extra = dur_txt = ""
agent_cost = {}
cost = num(dig("cost", "total_cost_usd"))
if cost > 0:
    money_txt = f"{MONEY}${cost:.2f}{R}"
    # model_rate is keyed on the subagents' message.model ("claude-opus-5"), so
    # the parent has to arrive in the same namespace or it can never match a
    # rate a same-family agent already learned. stdin carries the context
    # variant ("claude-opus-5[1m]"); the suffix is a window size, not a price.
    mid = str(dig("model", "id", default="") or "").split("[")[0]
    d, agent_cost = cost_delta(session_id, prompt_id, cost, agents, parent_w,
                               mid or (dig("model", "display_name",
                                           default="") or ""))
    if d is not None:
        money_extra = f" {DIM}(+${d:.2f}){R}"
dur = num(dig("cost", "total_duration_ms"))
if dur >= 1000:
    dur_txt = f"{GRY}⏱ {fmt_dur(dur)}{R}"


def compose2(level=0):
    """level 0 = everything, then shed per-turn detail as the terminal narrows:
    1 drops the cost delta, 2 also drops ⇧, 3 also drops the context delta.
    Totals and labels always survive — a narrow line degrades to the old one."""
    segs = [s for s in (bar_txt,) if s]
    # Level, its delta and the upload are all one thought about context, so they
    # ride in a single segment -- no divider between the arrows.
    ctx_grp = []
    if ctx_txt:
        ctx_grp.append(ctx_txt + (ctx_extra if level < 3 else ""))
    if up_txt and level < 2:
        ctx_grp.append(up_txt)
    if ctx_grp:
        segs.append("  ".join(ctx_grp))
    if lines_txt:
        segs.append(lines_txt)
    tail = []
    if money_txt:
        tail.append(money_txt + (money_extra if level < 1 else ""))
    if dur_txt:
        tail.append(dur_txt)
    if tail:
        segs.append("  ".join(tail))
    return SEP.join(segs)


line2 = compose2()
if COLS:
    for _lvl in (1, 2, 3):
        if vlen(line2) <= COLS:
            break
        line2 = compose2(_lvl)


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

# ══ LINE 2b — one row per subagent of this turn ═══════════════════════════════
def short_model(m):
    """`claude-haiku-4-5-20251001` -> `haiku`. The family is what tells two
    agents apart at a glance, and line 1 already carries the model in full."""
    m = (m or "").lower()
    for pre in ("claude-", "anthropic."):
        if m.startswith(pre):
            m = m[len(pre):]
    return m.split("-")[0] or "?"


def agent_window(rec, main_family, main_win):
    """How much context the agent is allowed. Nothing on disk states it.

    `resolvedModel` carries the [1m] marker when we have it, but it only appears
    once an agent finishes (or, for async ones, at launch) — so most of the time
    the fallback is what runs: an agent on the main model gets the main model's
    window, anything else the default. A wrong guess reads too full, never too
    empty, and CC_SL_AGENT_CTX overrides it.
    """
    if "[1m]" in (rec.get("res") or ""):
        return 1000000
    if main_win and short_model(rec.get("model")) == main_family:
        return main_win
    return AGENT_CTX


def order_agents(recs):
    """Depth-first, each agent followed by whatever it spawned.

    Depth comes from the tree rather than meta's spawnDepth: an agent whose
    parent isn't in this turn's set is a root here whatever its absolute depth.
    Siblings sort by start time — stable across redraws, unlike mtime, which
    would make rows jump around as agents write.
    """
    by = {r["aid"]: r for r in recs}
    kids, roots = {}, []
    for r in recs:
        par = r.get("par")
        if par and par in by and par != r["aid"]:
            kids.setdefault(par, []).append(r)
        else:
            roots.append(r)
    out, seen = [], set()

    def walk(r, depth):
        if r["aid"] in seen:        # malformed parent chain must not loop
            return
        seen.add(r["aid"])
        out.append(dict(r, depth=depth))
        for k in sorted(kids.get(r["aid"], []), key=lambda x: (x["t0"] or "", x["aid"])):
            walk(k, depth + 1)

    for r in sorted(roots, key=lambda x: (x["t0"] or "", x["aid"])):
        walk(r, 1)
    for r in recs:                  # anything a cycle stranded still gets a row
        walk(r, 1)
    return out


def agent_bar(ctx, win):
    """Six cells against an inferred window, coloured like line 2's bar."""
    pct = 0 if win <= 0 else max(0, min(100, int(round(ctx * 100.0 / win))))
    filled = min(6, (pct * 6 + 50) // 100)
    lo, hi = ctx_bands(win)
    col = GREEN if pct < lo else (AMBER if pct <= hi else RED)
    return f"{col}{'█' * filled}{'░' * (6 - filled)} {pct:>3d}%{R}"


def compose_agents(rows, costs, main_family, main_win, level=0):
    """One aligned row per agent. level 1 truncates the name, 2 drops it, 3 also
    drops the cost, 4 also drops the elapsed time."""
    if not rows:
        return []
    fixed, names = [], []
    for r in rows:
        head = "  " * (r["depth"] - 1) + f"{GRY}⇉{R} {ORANGE}{short_model(r['model'])}{R}"
        seg = [head, agent_bar(r["ctx"], agent_window(r, main_family, main_win)),
               f"{OUT_C}⇧{R} {fmt_tok(r['up'])}", f"{GEN_C}⇩{R} {fmt_tok(r['out'])}"]
        if level < 4:
            t0, t1 = parse_ts(r.get("t0")), parse_ts(r.get("tend"))
            end = t1 if (r.get("done") and t1) else NOW
            if t0:
                seg.append(f"{GRY}⏱ {fmt_dur(max(0, end - t0) * 1000)}{R}")
        if level < 3 and r["aid"] in costs:
            seg.append(f"{DIM}(+${costs[r['aid']]:.2f}){R}")
        fixed.append(seg)
        names.append(r["name"] + (" ✓" if r.get("done") else ""))

    # Pad each column to the block's widest, so the numbers line up in a column
    # rather than wherever the model name happens to end.
    ncol = max(len(f) for f in fixed)
    widths = [max(vlen(f[i]) for f in fixed if len(f) > i) for i in range(ncol)]
    body = []
    for seg in fixed:
        body.append("  ".join(c + " " * (widths[i] - vlen(c))
                              for i, c in enumerate(seg)).rstrip())

    if level >= 2:
        return [PAD + b for b in body]
    budget = 0
    if COLS:
        budget = COLS - vlen(PAD) - max(vlen(b) for b in body) - 2
        if budget < 6:
            return [PAD + b for b in body]
    out = []
    for b, nm in zip(body, names):
        if level >= 1 and COLS and len(nm) > budget:
            nm = nm[:max(0, budget - 1)] + "…"
        out.append(f"{PAD}{b}  {DIM}{nm}{R}" if nm else PAD + b)
    return out


agent_lines = []
if agents and not E("CC_SL_NO_AGENTS"):
    _rows = order_agents(agents)
    _more = 0
    if AGENT_MAX > 0 and len(_rows) > AGENT_MAX:
        _more, _rows = len(_rows) - AGENT_MAX, _rows[:AGENT_MAX]
    _fam = (dig("model", "display_name", default="") or "").strip().split(" ")[0].lower()
    _win = int(num(dig("context_window", "context_window_size"), 0))
    agent_lines = compose_agents(_rows, agent_cost, _fam, _win)
    if COLS:
        for _lvl in (1, 2, 3, 4):
            if max(vlen(l) for l in agent_lines) <= COLS:
                break
            agent_lines = compose_agents(_rows, agent_cost, _fam, _win, _lvl)
    if _more:
        agent_lines.append(f"{PAD}{DIM}… +{_more} more{R}")


# ── emit ──────────────────────────────────────────────────────────────────────
for ln in [line1, line2] + agent_lines + [line3]:
    if ln.strip():
        print(ln)
