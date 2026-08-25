# gnomon

A three-line status line for Claude Code that keeps books. Python 3, standard
library only.

Named for the sundial's pointer — the part of an instrument that shows you the
reading. Most status lines print numbers; this one accounts for them: a per-turn
ledger that reconciles, per-model rates learned from your own usage rather than a
hardcoded price table, and one row per subagent showing where the work went.

```
Opus 5 (1M context) [high] ~/install
⛁ ██░░░░░░░░ 23% of 1M ⚠ │ ↓ 232.5K (+64.9K)  ⇧ 105.1K │ +458/-173 │ $11.76 (+$1.18)  ⏱ 1h52m
5h ●●◔○○ 46% ↻51m │ 7d ●○○○○○○ 15% ↻4d5h │ fable ◕○○○○○○ 12% ↻4d5h
```

`gnomon.py`'s module docstring is the reference for what every glyph means,
how line 3's colours are derived, and all `CC_SL_*` env knobs. This file covers
layout, install, and the invariants that are easy to break by accident.

## Layout

    gnomon.py                the whole thing; deployed to ~/.claude/gnomon.py
    install.sh               installs + registers it (testing is ./test.sh)
    test.sh                  every suite against the repo copy
    tests/test_render.py     112 checks: rendering, subagent rows, widths, degradation
    tests/test_cost_state.py  67 checks: cost baselines, per-agent attribution, GC
    tests/test_install.sh     34 checks: what the installer does to a machine
    tests/fixtures/          a captured real stdin payload
    archive/                 pre-feature snapshot, kept from before git existed

One glyph, one meaning, one order. A **single** arrow `↓` is a level — what is
resident in a context window right now. **Double** arrows are flows: `⇧` is what
was uploaded this turn. Every line leads with the level and follows with the
upload, line 2 stating its level as a number and agent rows as a bar:

    ⛁ ██░░░░░░░░ 23% of 1M │ ↓ 232.5K (+64.9K)  ⇧ 105.1K │ +458/-173 │ $11.76 …
      ⇉ fable   ███░░░ 38%  ⇧ 75.6K  ⏱ 7m42s  (+$0.62)  Audit state schema ✓
      ⇉ opus    ██░░░░ 23%  ⇧ 45.4K  ⏱ 12m03s (+$0.31)  Long running thing ↩

Status sits at the end of the name, the only unaligned field: `✓` finished, `↩`
still working but started in an **earlier** turn. That second case is the one the
rows exist for — a foreground agent is visible because you are waiting on it,
whereas a background agent is invisible by definition, so this line is the only
signal there is. Carried rows are dropped once the agent finishes, and
`CC_SL_AGENT_CARRY` (1800s of silence, `0` to disable) bounds how long a quiet
one is presumed alive. It is deliberately *not* `CC_SL_AGENT_STALE`: that decides
a tick mark, this decides whether a row exists at all, and healthy agents go
quiet for minutes while blocked in a tool.

A carried agent's uploads are **not** added to line 2's `⇧` — they belong to the
turn that spawned it — and its cost ledger is seeded on arrival so it is credited
only for what it does from that point on.

Agent rows carry no output column. A down arrow there read as the opposite of
line 2's `↓` while meaning something unrelated, which is exactly the sort of
false symmetry that makes a dense line unreadable. Generated tokens are the 5×
term in `weigh()`, so `(+$…)` already tracks them — `out` is still collected and
still prices the cost split, it simply is not drawn.

Line 1 marks the working directory with `⌂` and the git branch with `⎇`, both
overridable via `CC_SL_DIR_ICON` / `CC_SL_GIT_ICON` (set either to `""` to drop
it, or to a Nerd Font glyph if your terminal has one). U+2387 is the immediate
neighbour of the `⎈` already used for the kubernetes segment, so any terminal
rendering one renders the other.

## Install

    git clone git@github.com:Harrichael/Gnomon.git
    cd Gnomon && ./install.sh

**Requirements:** Python 3.9+ and Claude Code. Nothing else — no pip, no
third-party packages, standard library only. Verified end to end on the macOS
system `python3` (3.9.6), which is what a machine has before anyone installs
anything. `git` and `kubectl` are used if present and silently skipped if not.

`install.sh` installs and nothing else — testing is `./test.sh`, worth running
first. What the installer *does* do is a **smoke check**: render the sample
payload once against a scratch database with the network off, and refuse to
deploy anything that will not run. It costs ~0.1s and catches the failure that
matters at that moment — a script too broken to execute, on its way to something
that runs on every keystroke.

It then copies the script to `~/.claude/gnomon.py` and merges a `statusLine`
block into `~/.claude/settings.json` — merges, so unrelated settings (theme,
voice, permissions) survive. It creates `~/.claude` if it doesn't exist, and
works against a machine with no `settings.json` at all.

**What the installer owns, and what it does not.** Inside `statusLine`, `type`
and `command` are *wiring* — they say where the status line comes from, the
installer owns them permanently, and re-running always corrects a stale path.
`padding` and `refreshInterval` are *tuning*: seeded on first install, then
yours. This became a real bug the moment a bootstrap started delegating here on
every pass — a tuned `refreshInterval` was reverted by a run invoked for entirely
unrelated reasons. Everything else in the file, inside the block or outside it,
is untouched.

**The deployment source is deliberately not written into `settings.json`.** The
command is always `python3 ~/.claude/gnomon.py`, never the path of the checkout
it came from. Putting a source path there couples the status line to that
location's layout — a package registry's internal format, say — so the registry
could never change shape without breaking every install, and recovery from a
moved or pruned source becomes hand-editing JSON instead of re-running the
installer. If a by-reference mode is ever wanted, the shape is a symlink at
`~/.claude/gnomon.py` plus a refusal to link a dirty checkout, *not* a rewritten
command. This was argued out and rejected once already; the trade it loses is the
smoke gate on updates, since a `git pull` would then go live with nothing between
the pull and the next keystroke.

**Which clone deployed?** Any checkout anywhere can run `install.sh` and become
the deployed version; the smoke check proves it *runs*, not that it is current,
so a stale clone installs a silent downgrade. The installer prints
`deployed from <path> @ <commit>` and leaves `~/.claude/gnomon.provenance`
(source, commit, dirty flag) so that question is answerable months later. Without
git it records `commit=unknown` and installs anyway — this has to work from a
tarball.

Several checkouts of this repo on one machine is a legitimate arrangement, not a
smell: one place installs are made from, another where you develop, and a package
registry may hold its own clone as a development *source*. None of them announce
which one is deployed, so the provenance file is the thing that disambiguates
them — `cat ~/.claude/gnomon.provenance` beats inferring it from directory names,
which is how two sessions once spent an afternoon reading the wrong copy.

**No database setup.** `~/.claude/gnomon-state.db` is created on the first
redraw, tables and all, and every schema change since has been an
`IF NOT EXISTS` or a guarded `ALTER`. There is nothing to migrate and nothing to
seed.

**What a first session looks like.** The very first turn reads `(+$0.00)`. That
is correct, not a bug: `total_cost_usd` accumulates per process, so with no prior
turn on record the baseline is *unknown*, and anchoring it at zero would bill one
turn for the entire session. It anchors on the live reading instead and is exact
from the second turn onward. Per-model rates start empty and learn from use — the
first concurrent subagent split is priced by unweighted tokens until a model has
run alone once. Everything else is right immediately.

`refreshInterval` is read at startup, so **restart open sessions** after install.

**Re-running is safe.** The installer converges rather than accumulating: it
merges its four keys into `statusLine` and leaves everything else in
`settings.json` alone, including keys inside `statusLine` it knows nothing about.
Three consecutive runs produce a byte-identical file.

**It is all-or-nothing.** The merged settings are staged before anything is
copied, so a `settings.json` that cannot be parsed aborts with a plain-language
message and leaves you exactly as you were. It used to fail halfway — script
deployed, nothing registered, a raw `JSONDecodeError` as the only clue.

**Uninstall:** delete `~/.claude/gnomon.py`, `~/.claude/gnomon-state.db*`, and
the `statusLine` block from `~/.claude/settings.json`.

## Tests

    ./test.sh              all three suites against the repo copy
    ./test.sh render       one by name: render | cost | install

`render` and `cost` cover what gnomon draws and what it records. `install` covers
what `install.sh` does to a machine — the part that can only be got wrong once,
on someone else's laptop: a fresh `HOME` with no `~/.claude`, three runs
converging rather than accumulating, a malformed `settings.json` refused without
leaving a half-install behind, and a deliberately corrupted `gnomon.py` never
reaching the deploy target.

The suites also run directly, and on their own they exercise the *deployed*
script — handy for checking what is actually live on your machine. `./test.sh`
points them at the repo copy instead, which is what you want before installing:

    python3 tests/test_cost_state.py                     # the deployed script
    SL_SCRIPT=./gnomon.py python3 tests/test_render.py   # a candidate

Fixtures (transcripts, sandbox `HOME`s, scratch databases) are generated at run
time inside `tests/` and are gitignored. Nothing touches the real
`~/.claude/gnomon-state.db`; `CC_SL_STATE_DB` redirects every write.

## Invariants — each of these was a bug once

**Never crash, never hang.** A failure anywhere must drop one segment and leave
the rest of the line. Every risky path returns a sentinel instead of raising;
SQLite gets `busy_timeout` and a 0.25 s connect timeout.

**One API response spans several transcript entries.** Claude Code writes one
entry per content block (thinking, text, each `tool_use`), *all repeating the
same `usage` object*. Summing per entry inflates a turn 2–4x. Fold on
`message.id`.

**A turn's span is delimited at both ends by `promptId`, which only appears on
`user` entries.** Two traps: an `assistant` entry before the turn's first `user`
entry usually belongs to the *same* turn, so "something precedes us" does not
prove a boundary — only a `user` entry with a *different* `promptId` does. And
without an upper bound the scan sums every later turn too.

**Parse only newly-read chunks when walking backward.** Re-parsing the whole
accumulated buffer is O(n²): 24.6 ms at 4 KB chunks vs 2.2 ms at 64 KB.

**Subagent work lives in its own transcript.** Delegated tokens are not in the
parent file — Claude Code writes one file per agent (nested agents included) at
`<transcript-without-.jsonl>/subagents/agent-*.jsonl`, and every entry there
carries the *parent's* `promptId`, so a file belongs wholly to one turn. Miss
them and a turn that delegates reads at a fraction of its real upload: measured
8,907 parent tokens against 121,026 from its two agents. Check the first line's
`promptId` before reading a body, or every redraw re-reads every agent a session
ever spawned.

**Do not use `toolUseResult.totalTokens` from the parent transcript.** It is the
agent's *final request context size*, not cumulative spend; it omits nested
agents; and it only appears once the agent finishes. It is the obvious number to
reach for and it is wrong.

**Leading spaces do not survive.** Claude Code post-processes the command's
output as `.trim().split("\n").flatMap(c => c.trim() || [])`, so indentation is
stripped and a line blank after trimming is dropped. Start an indented line with
an ESC (`\033[0m`) — `trim()` stops there, the real spaces survive, and the reset
also cancels the SGR carry-over the renderer applies from the line above.

**A `toolUseResult` is not proof an agent finished.** An *async* agent gets one at
**launch**, with `status: "async_launched"`. Only a status that isn't that means
done. And a nested agent's completion is recorded in its **spawner's** file, not
the parent transcript, so both have to be scanned.

**Output tokens need max-per-message-id, uploads do not.** The early streaming
records for a response carry a placeholder `output_tokens` of 1–5 while
`input`/`cache_creation` are already final. First-wins gives 218 where the truth
is 28,425.

**Per-agent cost is measured, never modelled.** Fitting a per-model price table to
turn-level history does not work — parent and agent spend are entangled there and
the residuals come out the size of the values. Instead each redraw's cost movement
is attributed to whoever's tokens moved with it: exact when one agent runs alone,
weighted only when several run in the same interval, and the parts always sum to
the turn. Solo intervals are also the only clean per-model price observation, so
they are the sole input to the learned rates.

**Learned rates decay, or they can never be relearned.** Accumulating price
evidence forever makes a lifetime average that gets stiffer with every sample: if
Anthropic reprices a model, the old evidence outvotes the new indefinitely.
Measured on a simulated 3x repricing after 60 days of history — a lifetime mean
reaches only $20.08 of the true $30.00 after two further months, while a 14-day
half-life reaches $29.07. `CC_SL_RATE_HALFLIFE` sets it; 0 keeps only the newest
observation.

**Never bank token growth an interval could not pay for.** Redraws are polled on
a timer and stdin's `total_cost_usd` lags the transcript flush, so intervals with
`spent == 0` are routine. Advancing `w_last` on one of them consumes the growth
for free: the money lands a tick later with `dw == 0`, the agent reads as a
no-mover, and its whole spend falls to the parent. Measured on one live turn —
1.36M weighted agent tokens across four agents, all credited $0.00, with $5.71
banked on the parent. `w_last` now only moves on an interval that had money in
it; `updated_at` still moves so GC sees a live row.

**The parent has to arrive in the same model namespace as its agents.**
`model_rate` is keyed on subagents' `message.model` (`claude-opus-5`), so passing
stdin's `display_name` (`Opus 5 (1M context)`) meant the parent could never match
a rate a same-family agent had already learned, and was priced at the mean of all
known rates in every concurrent split. Key on `model.id` with the `[<n>m]` window
suffix stripped — that suffix is a context size, not a price.

**The parent calibrates, but only on a turn that spawned nothing.** With agents
in the turn its row also carries money banked from unattributable intervals, so
the sample would price the main model with an agent's spend. Agent-free turns are
the majority, so the main model still calibrates quickly.

**A context-bar threshold is a token count, not a percentage.** 60/80 answers
"how close am I to compaction?", which is the right question on a 200K window and
a useless one on 1M — 60% there is 600K, so the bar sits green through an entire
session. What matters on a big window is what every turn costs, and those lines
are absolute: 200K resident and 500K. Two anchors (200K → 60/80, 1M → 20/50) with
the *edges* interpolated log-log, so an in-between or future window size lands on
the curve rather than on whichever anchor got hardcoded:

```
   200K   green <60%  amber <80%      1M   green <20%  amber <50%
   400K   green <37%  amber <65%      2M   green <12%  amber <41%
   500K   green <32%  amber <61%     10M   green < 4%  amber <26%
```

Percentages hold flat below the small anchor — extrapolating downward would make
a 100K window 96% green.

**A thin learned rate is worse than no learned rate.** `model_rate` is a
decay-weighted ratio of sums — `Σusd / Σw` over every solo-mover interval, which
volume-weights the observations so a tiny interval with a chunk of money attached
cannot vote as loudly as a large clean one. But a fixed piece of misattributed
money barely moves a model with millions of weighted tokens behind it and badly
distorts one with a hundred thousand: two models an order of magnitude apart in
list price ended up 1.4× apart in their learned rates, compressed toward each
other, which is the signature of a constant additive error. Three guards:

- `n` — an effective sample count, decayed with the evidence, so the table can
  say which of its rows is trustworthy. Nothing reads it yet; it exists so the
  question is answerable.
- `CC_SL_RATE_FLOOR` (500K weighted tokens) — below it a model prices at the
  pooled rate across all models rather than its own.
- `CC_SL_RATE_OUTLIER` (5×) — once above the floor, an interval whose implied
  price is off the established rate by more than this is dropped whole. A
  genuine repricing is well inside 5×, so it still lands.

Below the floor everything is accepted: there is nothing yet to judge against.

**An unknown cost baseline is not zero.** `total_cost_usd` accumulates per
*process*, so treating a missing baseline as 0 bills one turn for the whole
session. Anchor on the live reading instead.

**`--resume` keeps `session_id` but restarts `total_cost_usd` from zero**
(verified on 2.1.226). A stored baseline can therefore sit *above* the live
reading; clamp it down or every delta is swallowed until spending passes the old
total.

**`updated_at` cannot order turns within a session.** It is second-granular and
bumped on every redraw. Tie-break on `rowid`.

**Expire whole quiet sessions, never the running one.** Expiring individual rows
by age lets one session harvest another live session's baseline — including the
row for the turn in progress.

Two of the invariants above are guarded by mutation-checked tests rather than by
assertion alone. Deleting the response-fold gate in `turn_tokens`, or moving the
`pcr`/`pout` writes after it, each leaves every other check in both suites green
— only `[1b]` fails. If you touch that loop, re-run the mutants:

```sh
python3 - <<'EOF'
import io; s = io.open("gnomon.py").read()
io.open("/tmp/mut.py","w").write(s.replace(
    "                if rid in seen:\n                    continue\n                seen.add(rid)\n", ""))
EOF
SL_SCRIPT=/tmp/mut.py python3 tests/test_render.py   # must report exactly 2 failures
```

## Known limitations

- **A subagent's context-window size is inferred**, since nothing on disk states
  it. An agent on the main model borrows the main window, `resolvedModel`'s `[1m]`
  marker is honoured when present, and everything else falls back to
  `CC_SL_AGENT_CTX`. A wrong guess makes the bar read too full, never too empty.
- **A carried agent's row mixes scopes.** An agent still working from an earlier
  turn shows *lifetime* `⇧` and `⏱` beside a *this-turn* `(+$…)`. Each is the
  most useful reading of its own field, so this is deliberate — but do not expect
  `⇧ × rate ≈ $` to reconcile on a row marked `↩`.
- **`SCAN_CAP` can still hide the agent you most want.** A file over 4 MB is
  skipped without trace, and a long-running background agent is exactly the file
  most likely to reach it — so the case carried rows exist for can still vanish,
  now inexplicably. A degraded name-only row would fix it and is not built yet.
- **A lingering background agent stops the main model calibrating.** Agent-free
  turns are the parent's only source of rate samples, and a carried row makes the
  turn non-empty for as long as it lasts. This is correct — the parent's banked
  money may contain that agent's lagged spend — but it means main-model
  convergence quietly slows while you delegate in the background.
- **A resumed session's first turn under-reports** by whatever was spent before
  its first redraw — bounded by `refreshInterval`, so ≤10 s of spend.
- **Retention is per-session, not per-row.** A session left open forever keeps
  every turn (~250 B each). Age no longer bounds the table.
- **A forward clock jump** pins a row as the permanent "most recent" and keeps
  its session from expiring.
- **The learned rates are compressed toward each other — root cause unresolved.**
  `weigh()` is an input-token-equivalent, so `usd/w` should land near a model's
  input price. Measured after ~2h of real use, Opus read $4.48/1M and Haiku
  $3.14/1M — a 1.4× spread where the real one is several times that, both pulled
  toward the middle. That is what a constant additive error looks like when it
  lands on samples of very different sizes. `CC_SL_RATE_FLOOR` and
  `CC_SL_RATE_OUTLIER` contain the damage; neither explains it. The full note,
  with the three places to look, is the block comment above `attribute()`. Its
  suggested next step now exists: `CC_SL_RATE_LOG=1` records every calibration
  sample — model, `dw`, `spent`, that participant's share, the mover count and
  whether it was accepted — to a `rate_sample` table, so the scatter can be
  looked at before anyone theorises further. Rejected and contaminated intervals
  are logged too; seeing what was *excluded* is most of the point. Off by
  default, and the table is not created unless it is on. Bounded meanwhile: rates only divide an
  interval between concurrent participants — every solo interval is measured
  money, and line 2's total never depends on them.
