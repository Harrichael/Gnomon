import json, os, re, subprocess, sys, time, shutil
# Defaults to the deployed script; set SL_SCRIPT to test the repo copy
# before installing it.
SCRIPT = os.environ.get("SL_SCRIPT") or os.path.expanduser(
    "~/.claude/statusline.py")
T = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(T, "home"); os.makedirs(os.path.join(HOME, ".claude"), exist_ok=True)
ANSI = re.compile(r"\033\[[0-9;]*m")
fails = []

def U(pid, tool=False):
    e = {"type": "user", "promptId": pid, "message": {"role": "user", "content": "x"}}
    if tool: e["toolUseResult"] = {}
    return e

def A(i, cw, cr, out):
    return {"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": i, "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr, "output_tokens": out}}}

# A: up=100 ctx=1100 | B: up=200 ctx 2100->3100 | C: up=10 ctx=4010
# B's first user entry is preceded by an assistant entry — the break-condition trap.
ENTRIES = [U("AAA"), A(10, 90, 1000, 5),
           U("BBB"), A(1, 99, 2000, 10), U("BBB", True), A(2, 98, 3000, 20),
           U("CCC"), A(5, 5, 4000, 30)]
TR = os.path.join(T, "tr.jsonl")
with open(TR, "w") as f:
    for e in ENTRIES: f.write(json.dumps(e) + "\n")

def run(stdin, env=None, cols="200"):
    e = dict(os.environ, HOME=HOME, COLUMNS=cols, CC_SL_NO_API="1",
             CC_SL_NO_GIT="1", CC_SL_NO_K8S="1", TMPDIR=T)
    e.update(env or {})
    r = subprocess.run([sys.executable, SCRIPT], input=json.dumps(stdin),
                       capture_output=True, text=True, env=e)
    return r.returncode, ANSI.sub("", r.stdout)

def payload(pid, tin, cost=5.0, sid="S1", tr=TR):
    return {"model": {"display_name": "M"}, "prompt_id": pid, "session_id": sid,
            "transcript_path": tr, "context_window": {"used_percentage": 5,
            "context_window_size": 1000000, "total_input_tokens": tin},
            "cost": {"total_cost_usd": cost, "total_duration_ms": 60000}}

def grab(out, glyph):
    m = re.search(re.escape(glyph) + r" ([0-9.]+[KM]?)", out)
    return m.group(1) if m else None

def parens_after(out, glyph):
    if glyph == "$":
        m = re.search(r"\$[0-9.]+ \(\+(\$[0-9.]+)\)", out)
    else:
        m = re.search(re.escape(glyph) + r" [0-9.]+[KM]? \(\+([^)]+)\)", out)
    return m.group(1) if m else None

def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} got={got!r} want={want!r}")
    if not ok: fails.append(name)

print("\n[1] per-turn sums, bounded at both ends")
rc, o = run(payload("CCC", 5000))
check("current turn C: up", grab(o, "⇧"), "10")
check("current turn C: ctx delta vs prev_ctx 3100", parens_after(o, "↓"), "1.9K")
rc, o = run(payload("BBB", 5000))
check("middle turn B: up (upper bound honoured)", grab(o, "⇧"), "200")
check("middle turn B: ctx delta vs prev_ctx 1100", parens_after(o, "↓"), "3.9K")
rc, o = run(payload("AAA", 5000))
check("first turn A: up (break cond. at BOF)", grab(o, "⇧"), "100")
check("first turn A: no ctx delta (no prev)", parens_after(o, "↓"), None)

print("\n[2] cost delta")
db = os.path.join(HOME, ".claude", "statusline-state.db")
for f in (db, db + "-wal", db + "-shm"):
    if os.path.exists(f): os.remove(f)
rc, o = run(payload("CCC", 5000, cost=5.0, sid="FRESH"))
# An unknown baseline is no longer treated as zero -- billing one turn for a
# whole session was the bug. First sight anchors on the live reading.
check("turn 1 of fresh session anchors on live cost", parens_after(o, "$"), "$0.00")
rc, o = run(payload("CCC", 5000, cost=7.5, sid="FRESH"))
check("same turn, cost grew 5.00->7.50", parens_after(o, "$"), "$2.50")
rc, o = run(payload("DDD", 5000, cost=8.25, sid="FRESH"))
check("new turn baselines off prior cost_last", parens_after(o, "$"), "$0.75")
rc, o = run(payload("DDD", 5000, cost=9.00, sid="FRESH"))
check("new turn accrues", parens_after(o, "$"), "$1.50")
rc, o = run(payload("DDD", 5000, cost=9.00, sid="OTHER"))
check("different session is independent", parens_after(o, "$"), "$0.00")

print("\n[3] degradation — every case must exit 0")
cases = [
    ("empty stdin",            {},                                          {}),
    ("no prompt_id",           payload("", 5000),                           {}),
    ("missing transcript",     payload("CCC", 5000, tr="/nope/x.jsonl"),    {}),
    ("CC_SL_NO_STATE=1",       payload("CCC", 5000),          {"CC_SL_NO_STATE": "1"}),
]
for name, pl, env in cases:
    rc, o = run(pl, env)
    check(f"exit 0: {name}", rc, 0)
rc, o = run(payload("CCC", 5000), {"CC_SL_NO_STATE": "1"})
check("NO_STATE hides $ parens, keeps ⇧", (parens_after(o, "$"), grab(o, "⇧")), (None, "10"))
rc, o = run(payload("CCC", 5000, tr="/nope/x.jsonl"))
check("missing transcript: no ⇧, no ctx parens", (grab(o, "⇧"), parens_after(o, "↓")), (None, None))

# truncated final line
TR2 = os.path.join(T, "trunc.jsonl")
shutil.copy(TR, TR2)
with open(TR2, "a") as f: f.write('{"type":"assistant","mess')
rc, o = run(payload("CCC", 5000, tr=TR2))
check("truncated trailing line tolerated", (rc, grab(o, "⇧")), (0, "10"))

# interrupted turn: user entry, zero assistant entries
TR3 = os.path.join(T, "empty_turn.jsonl")
with open(TR3, "w") as f:
    for e in ENTRIES + [U("ZZZ")]: f.write(json.dumps(e) + "\n")
rc, o = run(payload("ZZZ", 5000, tr=TR3))
check("interrupted turn (0 assistant entries)", (rc, grab(o, "⇧")), (0, None))

print("\n[4] width shedding")
for cols, expect in ((200, "all"), (70, "shed"), (60, "shed more"), (45, "minimal")):
    rc, o = run(payload("CCC", 5000), cols=str(cols))
    l2 = [l for l in o.strip().split("\n") if "⛁" in l][0]
    print(f"  COLUMNS={cols:<4} len={len(l2):<4} {l2}")
for cols in (200, 100, 80, 60):
    rc, o = run(payload("CCC", 5000), cols=str(cols))
    l2 = [l for l in o.strip().split("\n") if "⛁" in l][0]
    check(f"line 2 fits COLUMNS={cols}", len(l2) <= cols, True)

print("\n[5] read-only DB path")
ro = os.path.join(T, "ro"); os.makedirs(ro, exist_ok=True)
roclaude = os.path.join(ro, ".claude"); os.makedirs(roclaude, exist_ok=True)
os.chmod(roclaude, 0o500)
e = dict(os.environ, HOME=ro, COLUMNS="200", CC_SL_NO_API="1", CC_SL_NO_GIT="1",
         CC_SL_NO_K8S="1", TMPDIR=T)
r = subprocess.run([sys.executable, SCRIPT], input=json.dumps(payload("CCC", 5000)),
                   capture_output=True, text=True, env=e)
os.chmod(roclaude, 0o700)
check("unwritable DB dir: still exits 0", r.returncode, 0)
check("unwritable DB dir: $ parens hidden", parens_after(ANSI.sub("", r.stdout), "$"), None)
check("unwritable DB dir: ⇧ survives", grab(ANSI.sub("", r.stdout), "⇧"), "10")

print("\n[6] expiry")
import sqlite3
c = sqlite3.connect(db); c.execute("INSERT OR REPLACE INTO turn VALUES (?,?,?,?,?)",
                                   ("STALE", "OLD", 0.0, 1.0, int(time.time()) - 999999))
c.commit(); before = c.execute("SELECT COUNT(*) FROM turn WHERE session_id='STALE'").fetchone()[0]
c.close()
run(payload("NEWTURN", 5000, cost=1.0, sid="GCTEST"))   # new-turn insert triggers GC
c = sqlite3.connect(db); after = c.execute("SELECT COUNT(*) FROM turn WHERE session_id='STALE'").fetchone()[0]; c.close()
check("stale row GC'd on new-turn insert", (before, after), (1, 0))

print("\n[7] concurrency — 8 simultaneous redraws, distinct sessions")
_env = dict(os.environ, HOME=HOME, COLUMNS="200", CC_SL_NO_API="1",
            CC_SL_NO_GIT="1", CC_SL_NO_K8S="1", TMPDIR=T)
_pl = [json.dumps(payload("P", 5000, cost=float(i), sid=f"C{i}")) for i in range(1, 9)]
procs = [subprocess.Popen([sys.executable, SCRIPT], stdin=subprocess.PIPE,
         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env)
         for _ in range(8)]
outs = [p.communicate(input=_pl[i]) for i, p in enumerate(procs)]
check("all 8 exited 0", [p.returncode for p in procs], [0] * 8)
check("no 'locked'/'error' on stderr", any("lock" in e.lower() or "error" in e.lower() for _, e in outs), False)

print("\n[8] timing")
pl = json.dumps(payload("CCC", 5000))
e = dict(os.environ, HOME=HOME, COLUMNS="200", CC_SL_NO_API="1", CC_SL_NO_GIT="1", CC_SL_NO_K8S="1", TMPDIR=T)
ts = []
for _ in range(15):
    s = time.perf_counter()
    subprocess.run([sys.executable, SCRIPT], input=pl, capture_output=True, text=True, env=e)
    ts.append((time.perf_counter() - s) * 1000)
ts.sort(); print(f"  median {ts[7]:.1f} ms   min {ts[0]:.1f} ms   max {ts[-1]:.1f} ms")
check("median under 90ms budget", ts[7] < 90, True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
