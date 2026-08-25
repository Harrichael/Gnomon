"""Focused suite for the three cost-baseline fixes + multi-session safety."""
import json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time

# Defaults to the deployed script; set SL_SCRIPT to test the repo copy
# before installing it.
SCRIPT = os.environ.get("SL_SCRIPT") or os.path.expanduser(
    "~/.claude/gnomon.py")
BASE = tempfile.mkdtemp(prefix="sl2-")
OK = FAIL = 0

def check(name, got, want):
    global OK, FAIL
    good = got == want
    if good: OK += 1
    else: FAIL += 1
    print("  %s  %-52s got=%r want=%r" % ("PASS" if good else "FAIL", name, got, want))

def run(db, sid, pid, cost, ttl=None, env=None, tr=None, model=None):
    e = dict(os.environ)
    e.update({"CC_SL_STATE_DB": db, "CC_SL_NO_API": "1", "CC_SL_NO_GIT": "1",
              "CC_SL_NO_K8S": "1", "COLUMNS": "400", "LC_ALL": "en_US.UTF-8"})
    if ttl is not None: e["CC_SL_STATE_TTL"] = str(ttl)
    if env: e.update(env)
    body = {"session_id": sid, "prompt_id": pid,
            "model": model or {"display_name": "T"},
            "workspace": {"current_dir": "/tmp"},
            "cost": {"total_cost_usd": cost}}
    if tr:
        body["transcript_path"] = tr
    stdin = json.dumps(body)
    p = subprocess.run([sys.executable, SCRIPT], input=stdin, capture_output=True,
                       text=True, env=e)
    out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)
    m = re.search(r"\(\+\$([0-9.]+)\)", out)
    return p.returncode, (m.group(1) if m else None), out

def rows(db):
    c = sqlite3.connect(db)
    r = c.execute("SELECT session_id,prompt_id,cost_start,cost_last,updated_at "
                  "FROM turn ORDER BY rowid").fetchall()
    c.close(); return r

def db(n): return os.path.join(BASE, n + ".db")

print("\n[1] FIX 1 — no prior row must not bill the whole session")
d = db("f1")
rc, delta, _ = run(d, "S1", "P1", 40.0)
check("orphan session, cost=$40: exit 0", rc, 0)
check("orphan session shows +$0.00 not +$40.00", delta, "0.00")
rc, delta, _ = run(d, "S1", "P2", 41.25)
check("next turn baselines off it correctly", delta, "1.25")

print("\n[2] genuinely new session still measures turn 1")
d = db("f1b")
run(d, "N1", "Q1", 0.02)
_, delta, _ = run(d, "N1", "Q1", 0.90)
check("turn 1 grows within the turn", delta, "0.88")
_, delta, _ = run(d, "N1", "Q2", 1.40)
check("turn 2 delta correct", delta, "0.50")

print("\n[3] FIX 3 — updated_at tie must not pick the wrong predecessor")
d = db("f3")
run(d, "T1", "A", 5.0); run(d, "T1", "B", 8.0)
c = sqlite3.connect(d)
c.execute("UPDATE turn SET updated_at=1000")          # force the tie
c.execute("UPDATE turn SET cost_start=0, cost_last=5.0 WHERE prompt_id='A'")
c.execute("UPDATE turn SET cost_start=5.0, cost_last=8.0 WHERE prompt_id='B'")
c.commit(); c.close()
_, delta, _ = run(d, "T1", "C", 8.50)
check("tied timestamps: rowid picks true predecessor", delta, "0.50")

print("\n[4] cost regression (resume) must not swallow deltas")
d = db("f4")
c = sqlite3.connect(d)
c.execute("""CREATE TABLE turn (session_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
             cost_start REAL NOT NULL, cost_last REAL NOT NULL,
             updated_at INTEGER NOT NULL, PRIMARY KEY (session_id, prompt_id))""")
c.execute("INSERT INTO turn VALUES ('R1','old',30.0,39.50,?)", (int(time.time()),))
c.commit(); c.close()
rc, delta, _ = run(d, "R1", "new", 0.30)
check("resumed session: exit 0", rc, 0)
check("resumed first turn clamps to +$0.00", delta, "0.00")
check("clamp written back (cost_start<=cost_now)",
      [r for r in rows(d) if r[1] == "new"][0][2], 0.30)
_, delta, _ = run(d, "R1", "new2", 0.95)
check("resumed session correct from turn 2 on", delta, "0.65")

print("\n[5] FIX 2 — GC never harvests the running session's own baseline")
d = db("f2a")
run(d, "OWN", "old1", 3.0, ttl=1)
time.sleep(2)
_, delta, _ = run(d, "OWN", "old2", 4.25, ttl=1)      # own stale row must survive
check("own stale row survives own GC -> real delta", delta, "1.25")
check("own rows still present", len({r[0] for r in rows(d)}), 1)

print("\n[6] FIX 2 — a quiet session expires whole, and degrades honestly")
d = db("f2b")
run(d, "Y", "y1", 12.0, ttl=1)
time.sleep(2)
run(d, "X", "x1", 0.10, ttl=1)                        # X's GC reaps quiet Y
check("quiet session Y fully reaped", sorted({r[0] for r in rows(d)}), ["X"])
_, delta, _ = run(d, "Y", "y2", 14.0, ttl=1)
check("reaped session shows +$0.00 not +$14.00", delta, "0.00")

print("\n[7] FIX 2 — a session is never left half-expired")
d = db("f2c")
run(d, "H", "h1", 1.0, ttl=3)
time.sleep(2)
run(d, "H", "h2", 2.0, ttl=3)                          # h1 older than TTL soon
time.sleep(2)
run(d, "Z", "z1", 0.5, ttl=3)                          # Z's GC: H's newest is fresh
check("H kept whole while any turn is fresh", len([r for r in rows(d) if r[0]=="H"]), 2)

print("\n[8] MULTI-SESSION — 6 concurrent sessions on one DB")
d = db("conc")
sids = ["C%d" % i for i in range(6)]
for i, s in enumerate(sids):                           # seed turn 1
    run(d, s, "t1", 1.0 + i)
procs = []
e = dict(os.environ)
e.update({"CC_SL_STATE_DB": d, "CC_SL_NO_API": "1", "CC_SL_NO_GIT": "1",
          "CC_SL_NO_K8S": "1", "COLUMNS": "400", "LC_ALL": "en_US.UTF-8"})
for i, s in enumerate(sids):                           # turn 2, all at once
    stdin = json.dumps({"session_id": s, "prompt_id": "t2",
                        "model": {"display_name": "T"},
                        "workspace": {"current_dir": "/tmp"},
                        "cost": {"total_cost_usd": 1.0 + i + 0.5}})
    procs.append(subprocess.Popen([sys.executable, SCRIPT], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=e))
    procs[-1]._in = stdin
outs = [(p.communicate(input=p._in)) for p in procs]
check("all 6 exited 0", [p.returncode for p in procs], [0] * 6)
check("no stderr noise", any("lock" in o[1].lower() or "error" in o[1].lower()
                             for o in outs), False)
deltas = [re.search(r"\(\+\$([0-9.]+)\)", re.sub(r"\x1b\[[0-9;]*m", "", o[0])) for o in outs]
check("every session got its own +$0.50", [m.group(1) if m else None for m in deltas],
      ["0.50"] * 6)
check("12 rows, no cross-session bleed", len(rows(d)), 12)

print("\n[9] the live DB is never touched by a fixture run")
LIVE = os.path.expanduser("~/.claude/gnomon-state.db")


def live_snapshot():
    """Bytes of the real DB, or None when there isn't one.

    A machine seeing gnomon for the first time has no live DB at all — and that
    is exactly the state install.sh runs the suite in. Demanding the file exist
    turned a first install into a FileNotFoundError before anything was copied.
    "Unchanged" has to include "still absent".
    """
    try:
        with open(LIVE, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


before = live_snapshot()
run(db("iso"), "8d64c573-079a-40dd-8aad-1f11d2ace44a", "4c66c4e5", 11.75)
check("CC_SL_STATE_DB isolates writes from the real DB",
      live_snapshot() == before, True)
check("and a fixture run never conjures one", os.path.exists(LIVE), before is not None)

print("\n[10] degradation still intact")
d = db("deg")
rc, delta, out = run(d, "S", "P", 5.0, env={"CC_SL_NO_STATE": "1"})
check("NO_STATE: exit 0, no parens", (rc, delta), (0, None))
check("NO_STATE: no DB created", os.path.exists(d), False)
d = db("corrupt")
open(d, "wb").write(os.urandom(4096))
rc, delta, out = run(d, "S", "P", 5.0)
check("corrupt DB: exit 0", rc, 0)
check("corrupt DB: parens hidden only", delta, None)
check("corrupt DB: cost total survives", "$5.00" in out, True)

print("\n[11] per-agent cost attribution")
AT = os.path.join(BASE, "att.jsonl")
ASUB = os.path.join(BASE, "att", "subagents")
TS = "2026-08-20T20:00:00.000Z"


def write_parent(*extra, **kw):
    pid = kw.get("pid", "P1")
    with open(AT, "w") as f:
        f.write(json.dumps({"type": "user", "promptId": pid,
                            "message": {"role": "user", "content": "x"}}) + "\n")
        for e in extra:
            f.write(json.dumps(e) + "\n")


def asst(model, cw, out, mid):
    return {"type": "assistant", "isSidechain": True, "timestamp": TS,
            "message": {"role": "assistant", "model": model, "id": mid,
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 1, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": 0, "output_tokens": out}}}


def write_agent(aid, entries, name="A", pid="P1"):
    os.makedirs(ASUB, exist_ok=True)
    with open(os.path.join(ASUB, "agent-%s.jsonl" % aid), "w") as f:
        f.write(json.dumps({"type": "user", "isSidechain": True, "promptId": pid,
                            "agentId": aid, "timestamp": TS,
                            "message": {"role": "user", "content": "x"}}) + "\n")
        for e in entries:
            f.write(json.dumps(dict(e, agentId=aid)) + "\n")
    with open(os.path.join(ASUB, "agent-%s.meta.json" % aid), "w") as f:
        json.dump({"agentType": "general-purpose", "description": name,
                   "spawnDepth": 1}, f)


def agent_rows(db):
    c = sqlite3.connect(db)
    r = dict((x[0], x[1]) for x in
             c.execute("SELECT agent_id, cost FROM agent"))
    c.close()
    return r


def rates(db):
    c = sqlite3.connect(db)
    r = dict((x[0], x[1] / x[2]) for x in
             c.execute("SELECT model, usd, w FROM model_rate") if x[2] > 0)
    c.close()
    return r


shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
write_parent()
d = db("att1")
write_agent("X", [asst("fable", 1000, 100, "x1")])
run(d, "S", "P1", 1.00, tr=AT)                      # first sight: baselines
write_agent("X", [asst("fable", 1000, 100, "x1"), asst("fable", 5000, 900, "x2")])
run(d, "S", "P1", 3.00, tr=AT)                      # +$2.00, only X moved
ar = agent_rows(d)
check("solo agent takes the whole interval", round(ar.get("X", 0), 4), 2.0)
check("solo interval calibrates that model", "fable" in rates(d), True)

# a second agent appears; both grow in the same interval
write_agent("Y", [asst("haiku", 1000, 100, "y1")])
run(d, "S", "P1", 3.00, tr=AT)                      # establish Y's baseline, no spend
write_agent("X", [asst("fable", 1000, 100, "x1"), asst("fable", 5000, 900, "x2"),
                  asst("fable", 4000, 800, "x3")])
write_agent("Y", [asst("haiku", 1000, 100, "y1"), asst("haiku", 4000, 800, "y2")])
run(d, "S", "P1", 4.00, tr=AT)                      # +$1.00 split two ways
ar = agent_rows(d)
check("concurrent agents both accrue", (ar["X"] > 2.0, ar["Y"] > 0.0), (True, True))
c = sqlite3.connect(d)
tot = c.execute("SELECT SUM(cost) FROM agent").fetchone()[0]
turn = c.execute("SELECT cost_last - cost_start FROM turn").fetchone()[0]
c.close()
check("parts reconcile with the turn delta", round(tot, 6), round(turn, 6))

# money arriving before the transcript catches up is banked, never lost
run(d, "S", "P1", 4.75, tr=AT)                      # +$0.75, nobody moved
c = sqlite3.connect(d)
tot = c.execute("SELECT SUM(cost) FROM agent").fetchone()[0]
turn = c.execute("SELECT cost_last - cost_start FROM turn").fetchone()[0]
parent = c.execute("SELECT cost FROM agent WHERE agent_id=''").fetchone()[0]
c.close()
check("unattributable spend goes to the parent", round(parent, 4) >= 0.75, True)
check("still reconciles after a lagging interval", round(tot, 6), round(turn, 6))

# a fresh session's agent starts from zero
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
write_parent()
d2 = db("att2")
write_agent("Z", [asst("fable", 1000, 100, "z1")])
run(d2, "S2", "P1", 0.10, tr=AT)
check("new agent starts at 0", round(agent_rows(d2).get("Z", -1), 4), 0.0)

# calibration must ignore contaminated intervals
d3 = db("att3")
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
write_parent()
write_agent("A", [asst("m1", 1000, 100, "a1")])
write_agent("B", [asst("m2", 1000, 100, "b1")])
run(d3, "S3", "P1", 1.00, tr=AT)
write_agent("A", [asst("m1", 1000, 100, "a1"), asst("m1", 9000, 900, "a2")])
write_agent("B", [asst("m2", 1000, 100, "b1"), asst("m2", 9000, 900, "b2")])
run(d3, "S3", "P1", 2.00, tr=AT)
check("two movers calibrate nothing", rates(d3), {})
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)

print("\n[12] learned rates must track a repricing, not average it away")


def rate_of(db, model):
    c = sqlite3.connect(db)
    r = c.execute("SELECT w, usd FROM model_rate WHERE model=?", (model,)).fetchone()
    c.close()
    return (r[1] / r[0]) if r and r[0] else None


def solo(db, sid, n, up, spend, env=None):
    """One clean solo-mover interval: agent X is the only thing that moved.

    The agent's file has to GROW between the two redraws — an unchanged file is
    a no-mover interval, which banks on the parent and calibrates nothing.
    """
    pid = "T%d" % n
    shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
    write_parent(pid=pid)
    seed = asst("mm", 10, 0, "a%d" % n)
    write_agent("X", [seed], pid=pid)
    # cost_delta is only reached when total_cost_usd > 0, so the baseline
    # redraw needs a nonzero floor; the interval is the difference.
    run(db, sid, pid, 0.01, tr=AT, env=env)               # baselines
    write_agent("X", [seed, asst("mm", up, 0, "b%d" % n)], pid=pid)
    run(db, sid, pid, 0.01 + spend, tr=AT, env=env)       # only X moved


W = lambda up: 1.25 * (up + 1)          # weigh(); asst() adds 1 input token

# halflife 0 keeps only the newest observation, so the rate is exactly it
d = db("rate0")
solo(d, "R0", 1, 100000, 1.00, env={"CC_SL_RATE_HALFLIFE": "0"})
check("first solo interval sets the rate",
      round(rate_of(d, "mm") * 1e6, 2), round(1.00 / W(100000) * 1e6, 2))
solo(d, "R0", 2, 100000, 3.00, env={"CC_SL_RATE_HALFLIFE": "0"})
check("halflife=0 relearns the new price exactly",
      round(rate_of(d, "mm") * 1e6, 2), round(3.00 / W(100000) * 1e6, 2))

# with a long halflife the old evidence still counts, so it lands between
d = db("ratelong")
solo(d, "RL", 1, 100000, 1.00, env={"CC_SL_RATE_HALFLIFE": "31536000"})
solo(d, "RL", 2, 100000, 3.00, env={"CC_SL_RATE_HALFLIFE": "31536000"})
lo, hi = 1.00 / W(100000), 3.00 / W(100000)
check("long halflife blends old and new", lo < rate_of(d, "mm") < hi, True)

# the default must actually decay -- a lifetime mean would not move at all here
d = db("ratedecay")
solo(d, "RD", 1, 100000, 1.00, env={"CC_SL_RATE_HALFLIFE": "1"})
time.sleep(3)                                   # ~3 half-lives
solo(d, "RD", 2, 100000, 3.00, env={"CC_SL_RATE_HALFLIFE": "1"})
got = rate_of(d, "mm") * 1e6
want = 3.00 / W(100000) * 1e6
check("aged evidence is discounted, new price nearly wins",
      abs(got - want) / want < 0.15, True)
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)

print("\n[13] growth seen in a zero-spend interval must stay claimable")

# Redraws are polled, and stdin's cost lags the transcript flush -- so an agent
# routinely grows during an interval that has no money in it. If that growth is
# consumed anyway, the agent reads as a no-mover when the money finally lands
# and its whole spend falls to the parent. Observed live: 1.36M weighted agent
# tokens credited at $0.00 across four agents of one turn.
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
write_parent()
d = db("lag")
seed = asst("fable", 1000, 100, "l1")
write_agent("X", [seed])
run(d, "S13", "P1", 1.00, tr=AT)                    # first sight, spent = 0
write_agent("X", [seed, asst("fable", 90000, 900, "l2")])
run(d, "S13", "P1", 1.00, tr=AT)                    # X grew, still no money
run(d, "S13", "P1", 2.50, tr=AT)                    # money lands, X is static
ar = agent_rows(d)
check("unpaid growth is credited when the money arrives",
      round(ar.get("X", 0.0), 4), 1.50)
check("it does not fall through to the parent", round(ar.get("", -1.0), 4), 0.0)

print("\n[14] the parent must calibrate too -- but only when it is clean")


def passt(model, cw, out, mid):
    e = dict(asst(model, cw, out, mid))
    e.pop("isSidechain", None)
    return e


OPUS = {"id": "claude-opus-5[1m]", "display_name": "Opus 5 (1M context)"}

# turn that spawned nothing: the parent is the only possible spender
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
d = db("pcal")
p0 = passt("x", 10, 0, "q0")
write_parent(p0)
run(d, "SP", "P1", 0.01, tr=AT, model=OPUS)         # baseline
write_parent(p0, passt("x", 100000, 0, "q1"))
run(d, "SP", "P1", 1.01, tr=AT, model=OPUS)         # +$1.00, parent alone
check("an agent-free solo interval calibrates the parent",
      list(rates(d)), ["claude-opus-5"])
check("rate keyed on model.id, not the display name",
      round(rates(d)["claude-opus-5"] * 1e6, 2),
      round(1.00 / (1.25 * 100001) * 1e6, 2))

# same shape, but the turn has an agent in it -- the parent row can carry money
# banked from unattributable intervals, so it is no longer a clean sample
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
d = db("pcal2")
write_parent(p0)
write_agent("W", [asst("fable", 1000, 100, "w1")])
run(d, "SP2", "P1", 0.01, tr=AT, model=OPUS)        # first sight, spent = 0
run(d, "SP2", "P1", 0.02, tr=AT, model=OPUS)        # pays off both baselines
before = agent_rows(d).get("", 0.0)
write_parent(p0, passt("x", 100000, 0, "q1"))       # only the parent moves now
run(d, "SP2", "P1", 1.02, tr=AT, model=OPUS)
check("a turn with agents never calibrates the parent", rates(d), {})
check("the parent still takes the interval it earned",
      round(agent_rows(d).get("", 0.0) - before, 4), 1.00)
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)

print("\n[15] a thin or wild sample must not steer a split")


def n_of(db, model):
    c = sqlite3.connect(db)
    r = c.execute("SELECT n FROM model_rate WHERE model=?", (model,)).fetchone()
    c.close()
    return r[0] if r else None


def calib(db, sid, pid, model, up, spend, env=None):
    """One clean solo-mover interval for `model`. w = 1.25 * (up + 12)."""
    shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
    write_parent(pid=pid)
    seed = asst(model, 10, 0, pid + "s")
    write_agent("C", [seed], pid=pid)
    run(db, sid, pid, 0.01, tr=AT, env=env)             # first sight, spent 0
    write_agent("C", [seed, asst(model, up, 0, pid + "g")], pid=pid)
    run(db, sid, pid, 0.01 + spend, tr=AT, env=env)     # only C moved


# n is an EFFECTIVE sample count -- it decays with the evidence, so a rate built
# from 400 observations is distinguishable from one built from 2. Without it the
# table cannot say which of its rows is trustworthy.
d = db("n1")
calib(d, "N", "P1", "mm", 100000, 1.00)
check("one interval -> n = 1", round(n_of(d, "mm"), 1), 1.0)
calib(d, "N", "P2", "mm", 100000, 1.00)
check("two intervals -> n = 2", round(n_of(d, "mm"), 1), 2.0)

# above the floor, an interval whose implied price is absurd is dropped whole
d = db("out1")
calib(d, "O", "P1", "mm", 800000, 1.00)             # w = 1.0M, clears the floor
r1 = rate_of(d, "mm")
calib(d, "O", "P2", "mm", 800000, 100.00)           # implied 100x
check("a 100x interval is refused outright", rate_of(d, "mm"), r1)
check("and never counts toward n", round(n_of(d, "mm"), 1), 1.0)

# but a genuine repricing is inside the guard and must still land
d = db("out2")
calib(d, "R", "P1", "mm", 800000, 1.00)
calib(d, "R", "P2", "mm", 800000, 3.00)             # 3x < RATE_OUTLIER
check("a 3x repricing is accepted", round(n_of(d, "mm"), 1), 2.0)
check("and moves the rate", rate_of(d, "mm") > rate_of(db("out1"), "mm"), True)

# below the floor there is nothing to judge against, so everything is taken
d = db("out3")
calib(d, "T", "P1", "mm", 100000, 1.00)             # w = 125K, under the floor
calib(d, "T", "P2", "mm", 100000, 100.00)
check("under the floor a wild interval is still taken",
      round(n_of(d, "mm"), 1), 2.0)

# the floor in action: a thin high rate must not price a concurrent split
d = db("floor")
calib(d, "F", "C1", "thin", 100000, 5.00)           # w = 125K, ~$40/1M
calib(d, "F", "C2", "thick", 800000, 1.00)          # w = 1.0M, ~$1/1M


def split(db, sid, env=None):
    """One concurrent interval, both agents growing by exactly the same weight."""
    shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)
    write_parent(pid="X1")
    sa, sb = asst("thin", 10, 0, "as"), asst("thick", 10, 0, "bs")
    write_agent("A", [sa], pid="X1")
    write_agent("B", [sb], pid="X1")
    run(db, sid, "X1", 10.00, tr=AT, env=env)       # first sight, spent 0
    write_agent("A", [sa, asst("thin", 50000, 0, "ag")], pid="X1")
    write_agent("B", [sb, asst("thick", 50000, 0, "bg")], pid="X1")
    run(db, sid, "X1", 11.00, tr=AT, env=env)       # +$1.00 to divide
    return agent_rows(db)


ar = split(d, "F2")
# thin is below the floor so it prices at the pooled rate ($6.00 / 1.125M),
# thick at its own ($1.00 / 1.0M) -- about 5.3x apart, not 40x
check("thin agent priced off the pool, not its own thin rate",
      0.78 < ar["A"] < 0.88, True)
check("the interval still reconciles", round(ar["A"] + ar["B"], 4), 1.0)

# same DB, same evidence -- only the floor differs
ar0 = split(d, "F3", env={"CC_SL_RATE_FLOOR": "0"})
check("with the floor off, the thin rate dominates instead", ar0["A"] > 0.95, True)
shutil.rmtree(os.path.join(BASE, "att"), ignore_errors=True)

print("\n%s  (%d pass, %d fail)" % ("ALL PASS" if not FAIL else "FAILURES", OK, FAIL))
sys.exit(1 if FAIL else 0)
