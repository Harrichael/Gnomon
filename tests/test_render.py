import json, os, re, subprocess, sys, time, shutil
# Defaults to the deployed script; set SL_SCRIPT to test the repo copy
# before installing it.
SCRIPT = os.environ.get("SL_SCRIPT") or os.path.expanduser(
    "~/.claude/statusline.py")
T = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(T, "home"); os.makedirs(os.path.join(HOME, ".claude"), exist_ok=True)
ANSI = re.compile(r"\033\[[0-9;]*m")
RAW = []   # last raw stdout, for the tests that care about escapes
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
    RAW.append(r.stdout)
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

print("\n[7b] subagent uploads — delegated work belongs to the turn that asked")
# Claude Code writes each agent beside the parent as
#   <transcript-without-.jsonl>/subagents/agent-<id>.jsonl
# and tags every entry with the PARENT's promptId.
TRS = os.path.join(T, "tr_sub.jsonl")
with open(TRS, "w") as f:
    for e in [U("AAA"), A(10, 90, 1000, 5), U("BBB"), A(1, 99, 2000, 10)]:
        f.write(json.dumps(e) + "\n")
SUB = os.path.join(T, "tr_sub", "subagents"); os.makedirs(SUB, exist_ok=True)

def agent_file(name, pid, entries):
    with open(os.path.join(SUB, name), "w") as f:
        f.write(json.dumps(dict(U(pid), isSidechain=True)) + "\n")
        for e in entries:
            f.write(json.dumps(dict(e, isSidechain=True, promptId=pid)) + "\n")

# BBB's own upload is 1+99=100; its agents add 500 and 40.
agent_file("agent-aaa.jsonl", "BBB", [A(200, 300, 9000, 50)])
agent_file("agent-bbb.jsonl", "BBB", [A(10, 30, 500, 5)])
agent_file("agent-ccc.jsonl", "AAA", [A(7000, 1000, 100, 5)])   # different turn
rc, o = run(payload("BBB", 5000, tr=TRS))
check("subagent tokens fold into up", grab(o, "⇧"), "640")
rc, o = run(payload("AAA", 5000, tr=TRS))
check("other turn's agent excluded from BBB", grab(o, "⇧"), "8.1K")

# One response spanning several content blocks must still count once, and ids are
# shared with the parent scan so nothing is double counted.
agent_file("agent-ddd.jsonl", "DDD", [
    dict(A(5, 45, 100, 5), message=dict(A(5, 45, 100, 5)["message"], id="m1")),
    dict(A(5, 45, 100, 5), message=dict(A(5, 45, 100, 5)["message"], id="m1")),
])
with open(TRS, "a") as f:
    f.write(json.dumps(U("DDD")) + "\n")
rc, o = run(payload("DDD", 5000, tr=TRS))
check("duplicate content blocks counted once", grab(o, "⇧"), "50")

# Degradation: a corrupt agent file must not take the line down.
with open(os.path.join(SUB, "agent-bad.jsonl"), "w") as f:
    f.write('{"promptId":"BBB","isSidechain":true}\n{not json\n')
rc, o = run(payload("BBB", 5000, tr=TRS))
check("corrupt agent file: exit 0", rc, 0)
check("corrupt agent file: up still correct", grab(o, "⇧"), "640")

# An agent file larger than the scan cap is skipped, not read.
rc, o = run(payload("BBB", 5000, tr=TRS), env={"CC_SL_SCAN_CAP": "80"})
check("oversized agent files skipped", grab(o, "⇧"), "100")

# No subagents directory at all — the common case must be untouched.
rc, o = run(payload("BBB", 5000))
check("no subagents dir: unchanged", grab(o, "⇧"), "200")

print("\n[7c] subagent rows — delegated work gets its own line")
AT = os.path.join(T, "ag.jsonl")
with open(AT, "w") as f:
    for e in [U("AAA"), A(10, 90, 1000, 5), U("BBB"), A(1, 99, 2000, 10)]:
        f.write(json.dumps(e) + "\n")
ASUB = os.path.join(T, "ag", "subagents")
TS = "2026-08-20T20:00:00.000Z"


def AA(model, i, cw, cr, out, mid=None, stop="end_turn", ts=TS):
    m = {"role": "assistant", "model": model, "stop_reason": stop,
         "usage": {"input_tokens": i, "cache_creation_input_tokens": cw,
                   "cache_read_input_tokens": cr, "output_tokens": out}}
    if mid:
        m["id"] = mid
    return {"type": "assistant", "isSidechain": True, "message": m, "timestamp": ts}


def agent(aid, pid, entries, name="Agent", depth=1, par=None, meta=None, t0=TS):
    os.makedirs(ASUB, exist_ok=True)
    with open(os.path.join(ASUB, "agent-%s.jsonl" % aid), "w") as f:
        f.write(json.dumps({"type": "user", "isSidechain": True, "promptId": pid,
                            "agentId": aid, "timestamp": t0,
                            "message": {"role": "user", "content": "x"}}) + "\n")
        for e in entries:
            f.write(json.dumps(dict(e, agentId=aid)) + "\n")
    if meta is None:
        meta = {"agentType": "general-purpose", "description": name, "spawnDepth": depth}
        if par:
            meta["parentAgentId"] = par
    if meta is not False:
        with open(os.path.join(ASUB, "agent-%s.meta.json" % aid), "w") as f:
            json.dump(meta, f)


def wipe():
    shutil.rmtree(os.path.join(T, "ag"), ignore_errors=True)


def rows(out):
    return [l for l in out.split("\n") if "⇉" in l or "more" in l]


# --- 1. nothing changes when no agent ran -------------------------------------
wipe()
rc, base3 = run(payload("BBB", 5000, tr=AT))
check("no subagents: 2 lines (no rate limits in this fixture)",
      len([l for l in base3.split("\n") if l]), 2)

agent("aaa", "BBB", [AA("claude-fable-5", 2, 400, 9000, 900, mid="m1")], name="Audit thing")
rc, o = run(payload("BBB", 5000, tr=AT))
check("agent row appears", len(rows(o)), 1)
rc, o2 = run(payload("BBB", 5000, tr=AT), env={"CC_SL_NO_AGENTS": "1"})
check("CC_SL_NO_AGENTS=1 restores the un-delegated line count",
      len([l for l in o2.split("\n") if l]), 2)

# --- 2. line 2's up must absorb the agent, unchanged by the refactor ----------
check("line 2 ⇧ includes the agent (100 parent + 402 agent)", grab(o, "⇧"), "502")

# --- 3. output dedupe: streaming placeholders share one id --------------------
wipe()
agent("bbb", "BBB", [AA("claude-fable-5", 2, 400, 9000, 4, mid="m1"),
                     AA("claude-fable-5", 2, 400, 9000, 883, mid="m1"),
                     AA("claude-fable-5", 2, 400, 9000, 883, mid="m1")], name="Dedupe")
rc, o = run(payload("BBB", 5000, tr=AT))
r0 = rows(o)[0]
check("agent ⇩ takes max-per-id, not first or sum", "⇩ 883" in r0, True)
check("agent ⇧ counts the response once", "⇧ 402" in r0, True)

# non-contiguous duplicate ids must still fold
wipe()
agent("ccc", "BBB", [AA("claude-fable-5", 0, 100, 0, 10, mid="mA"),
                     AA("claude-fable-5", 0, 200, 0, 20, mid="mB"),
                     AA("claude-fable-5", 0, 100, 0, 55, mid="mA")], name="NonContig")
rc, o = run(payload("BBB", 5000, tr=AT))
r0 = rows(o)[0]
check("non-contiguous ids fold correctly (⇧)", "⇧ 300" in r0, True)
check("non-contiguous ids fold correctly (⇩)", "⇩ 75" in r0, True)

# --- 4. the tick mark ---------------------------------------------------------
wipe()
agent("run", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="r1", stop="tool_use")],
      name="Still going")
rc, o = run(payload("BBB", 5000, tr=AT))
check("stop_reason=tool_use ⇒ no tick", "✓" in rows(o)[0], False)
for stop in ("max_tokens", "pause_turn", None):
    wipe()
    agent("run", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="r1", stop=stop)], name="X")
    rc, o = run(payload("BBB", 5000, tr=AT))
    check("stop_reason=%s ⇒ no tick" % stop, "✓" in rows(o)[0], False)
wipe()
agent("fin", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="f1", stop="end_turn")], name="Done")
rc, o = run(payload("BBB", 5000, tr=AT))
check("stop_reason=end_turn ⇒ tick", "✓" in rows(o)[0], True)

# a completion recorded in the PARENT, but only when it isn't a launch record
for status, want in (("async_launched", False), ("completed", True)):
    wipe()
    agent("asy", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="a1", stop="tool_use")], name="Async")
    with open(AT, "w") as f:
        for e in [U("AAA"), A(10, 90, 1000, 5), U("BBB"), A(1, 99, 2000, 10)]:
            f.write(json.dumps(e) + "\n")
        f.write(json.dumps(dict(U("BBB", True),
                toolUseResult={"agentId": "asy", "status": status})) + "\n")
    rc, o = run(payload("BBB", 5000, tr=AT))
    check("parent toolUseResult status=%s ⇒ tick %s" % (status, want),
          "✓" in rows(o)[0], want)
with open(AT, "w") as f:
    for e in [U("AAA"), A(10, 90, 1000, 5), U("BBB"), A(1, 99, 2000, 10)]:
        f.write(json.dumps(e) + "\n")

# a nested agent's completion lives in its SPAWNER's file, not the parent's
wipe()
agent("par", "BBB", [AA("claude-opus-5", 1, 10, 0, 5, mid="p1", stop="tool_use"),
                     dict(U("BBB", True), timestamp=TS,
                          toolUseResult={"agentId": "kid", "status": "completed"})],
      name="Spawner")
agent("kid", "BBB", [AA("claude-haiku-4-5-20251001", 1, 10, 0, 5, mid="k1", stop="tool_use")],
      name="Nested", depth=2, par="par")
rc, o = run(payload("BBB", 5000, tr=AT))
rr = rows(o)
check("nested completion found in spawner's file", sum("✓" in l for l in rr), 1)
check("child renders under its parent, indented deeper",
      ANSI.sub("", rr[1]).index("⇉") > ANSI.sub("", rr[0]).index("⇉"), True)

# staleness reaps an agent nobody ever closed out
wipe()
agent("old", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="o1", stop="tool_use")], name="Stale")
old = time.time() - 4000
os.utime(os.path.join(ASUB, "agent-old.jsonl"), (old, old))
rc, o = run(payload("BBB", 5000, tr=AT))
check("stale agent gets a tick", "✓" in rows(o)[0], True)
rc, o = run(payload("BBB", 5000, tr=AT), env={"CC_SL_AGENT_STALE": "0"})
check("CC_SL_AGENT_STALE=0 disables the reap", "✓" in rows(o)[0], False)

# --- 5. the context bar's inferred denominator --------------------------------
wipe()
agent("big", "BBB", [AA("claude-opus-5", 0, 0, 100000, 1, mid="b1")], name="Same model")
agent("sml", "BBB", [AA("claude-fable-5", 0, 0, 100000, 1, mid="s1")], name="Other model")
def opus_payload():
    p = payload("BBB", 5000, tr=AT)
    p["model"]["display_name"] = "Opus 5 (1M context)"
    return p


rc, o = run(opus_payload())
rr = {ANSI.sub("", l).split("⇉ ")[1].split()[0]: ANSI.sub("", l) for l in rows(o)}
check("agent on the main model uses the main window (100K/1M)", "10%" in rr["opus"], True)
check("other model falls back to 200K (100K/200K)", "50%" in rr["fable"], True)
rc, o = run(opus_payload(), env={"CC_SL_AGENT_CTX": "400000"})
rr = {ANSI.sub("", l).split("⇉ ")[1].split()[0]: ANSI.sub("", l) for l in rows(o)}
check("CC_SL_AGENT_CTX overrides the fallback (100K/400K)", "25%" in rr["fable"], True)

# --- 6. indentation must survive Claude Code's per-line trim() ----------------
def harness(out):
    """What Claude Code actually renders: .trim().split.flatMap(c=>c.trim()||[])"""
    return [l for l in (x.strip("\t\r\n  ﻿") for x in out.strip().split("\n")) if l]


rc, o = run(payload("BBB", 5000, tr=AT))
kept = harness(RAW[-1])
check("no row is dropped by the harness trim", len(kept), len([l for l in o.split("\n") if l]))
check("agent rows survive trim with their indent",
      [l.startswith("\033[0m  ") for l in kept if "⇉" in l], [True, True])

# --- 7. alignment holds across wildly different names and magnitudes ----------
wipe()
agent("w1", "BBB", [AA("claude-opus-5", 1, 5, 3, 7, mid="w1")], name="a")
agent("w2", "BBB", [AA("claude-haiku-4-5-20251001", 900000, 900000, 900000, 900000, mid="w2")],
      name="a much longer agent description here")
rc, o = run(payload("BBB", 5000, tr=AT))
plain = [ANSI.sub("", l) for l in rows(o)]
check("⇧ starts at the same column on every row",
      len({l.index("⇧") for l in plain}), 1)
check("⇩ starts at the same column on every row",
      len({l.index("⇩") for l in plain}), 1)

# --- 8. degradation: every case must still exit 0 -----------------------------
wipe()
agent("nm", "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="n1")], meta=False)
rc, o = run(payload("BBB", 5000, tr=AT))
check("missing .meta.json: exit 0 and still a row", (rc, len(rows(o))), (0, 1))
with open(os.path.join(ASUB, "agent-nm.meta.json"), "w") as f:
    f.write("not json")
rc, o = run(payload("BBB", 5000, tr=AT))
check("corrupt .meta.json: exit 0", rc, 0)
with open(os.path.join(ASUB, "agent-nm.meta.json"), "w") as f:
    f.write("[]")
rc, o = run(payload("BBB", 5000, tr=AT))
check("wrong-type .meta.json: exit 0", rc, 0)
with open(os.path.join(ASUB, "agent-nm.jsonl"), "a") as f:
    f.write('{"type":"assist')
rc, o = run(payload("BBB", 5000, tr=AT))
check("truncated agent line: exit 0", rc, 0)
rc, o = run(payload("BBB", 5000, tr=AT), env={"CC_SL_SCAN_CAP": "40"})
check("agent over SCAN_CAP is skipped", len(rows(o)), 0)
os.makedirs(ASUB, exist_ok=True)
wipe(); os.makedirs(ASUB, exist_ok=True)
rc, o = run(payload("BBB", 5000, tr=AT))
check("empty subagents dir: exit 0, no rows", (rc, len(rows(o))), (0, 0))

# an agent belonging to another turn is skipped without its body being read
wipe()
agent("other", "ZZZ", [AA("claude-fable-5", 5000, 5000, 0, 5000, mid="z1")], name="Wrong turn")
rc, o = run(payload("BBB", 5000, tr=AT))
check("agent from another turn: no row", len(rows(o)), 0)
check("agent from another turn: not counted in ⇧", grab(o, "⇧"), "100")

# --- 9. overflow cap ----------------------------------------------------------
wipe()
for i in range(9):
    agent("m%d" % i, "BBB", [AA("claude-fable-5", 1, 10, 0, 5, mid="m%d" % i)],
          name="Agent %d" % i, t0="2026-08-20T20:0%d:00.000Z" % i)
rc, o = run(payload("BBB", 5000, tr=AT))
check("default cap of 6 rows", len([l for l in rows(o) if "⇉" in l]), 6)
check("overflow says how many were hidden", "+3 more" in ANSI.sub("", o), True)
rc, o = run(payload("BBB", 5000, tr=AT), env={"CC_SL_AGENT_MAX": "0"})
check("CC_SL_AGENT_MAX=0 shows them all", len([l for l in rows(o) if "⇉" in l]), 9)

# --- 10. shedding -------------------------------------------------------------
wipe()
agent("s1", "BBB", [AA("claude-fable-5", 2, 400, 9000, 900, mid="q1")],
      name="A fairly long agent description")
for cols, want in ((200, "full"), (100, "trunc"), (60, "none"), (46, "nocost")):
    rc, o = run(payload("BBB", 5000, tr=AT), cols=str(cols))
    r0 = ANSI.sub("", rows(o)[0])
    check("COLUMNS=%d row fits" % cols, len(r0) <= cols, True)
    if want == "full":
        check("COLUMNS=200 keeps the whole name", r0.endswith("description ✓"), True)
    if want == "nocost":
        check("COLUMNS=46 drops the cost", "(+$" in r0, False)
rc, o = run(payload("BBB", 5000, tr=AT), cols="0")
check("COLUMNS=0 disables shedding entirely",
      ANSI.sub("", rows(o)[0]).endswith("description ✓"), True)
wipe()

print("\n[7d] context-bar bands scale with the window size")

# 60/80 answers "how close to compaction?" -- right on 200K, useless on 1M where
# 60% is 600K. The band edges are absolute token counts interpolated log-log
# between the two anchors, so an in-between or future window lands sensibly.
GRN, AMB, RD = "38;5;114", "38;5;214", "31"


def bar_color(size, pct):
    d = payload("AAA", 1000)
    d["context_window"] = {"used_percentage": pct, "context_window_size": size,
                           "total_input_tokens": int(size * pct / 100)}
    run(d, env={"CC_SL_NO_STATE": "1"})
    m = re.search(r"\033\[([0-9;]+)m[█░]+ +[0-9]+%", RAW[-1])
    return m.group(1) if m else None


for size, pct, want, why in [
        (200000,  23, GRN, "200K at 23% stays green"),
        (200000,  65, AMB, "200K at 65% is amber"),
        (200000,  85, RD,  "200K at 85% is red"),
        (1000000, 15, GRN, "1M under 200K resident is green"),
        (1000000, 23, AMB, "1M at 23% (232K) is amber, not green"),
        (1000000, 55, RD,  "1M past 500K resident is red"),
        (400000,  30, GRN, "400K interpolates: 30% still green"),
        (400000,  50, AMB, "400K interpolates: 50% is amber"),
        (2000000, 10, GRN, "2M at 10% (200K) is green"),
        (2000000, 45, RD,  "2M past 817K is red"),
        (100000,  65, AMB, "small windows hold the flat 60/80 bands")]:
    check(why, bar_color(size, pct), want)

# continuity: the curve must not jump at the anchors
check("no discontinuity below the 200K anchor",
      bar_color(199000, 59), bar_color(200000, 59))

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
