# cc-statusline

A three-line status line for Claude Code. Python 3, standard library only.

```
Opus 5 (1M context) [high] ~/install
⛁ ██░░░░░░░░ 23% of 1M ⚠ │ ↓ 232.5K (+64.9K)  ⇧ 105.1K │ +458/-173 │ $11.76 (+$1.18)  ⏱ 1h52m
5h ●●◔○○ 46% ↻51m │ 7d ●○○○○○○ 15% ↻4d5h │ fable ◕○○○○○○ 12% ↻4d5h
```

`statusline.py`'s module docstring is the reference for what every glyph means,
how line 3's colours are derived, and all `CC_SL_*` env knobs. This file covers
layout, install, and the invariants that are easy to break by accident.

## Layout

    statusline.py            the whole thing; deployed to ~/.claude/statusline.py
    install.sh               runs the tests, then installs + registers it
    tests/test_render.py     105 checks: rendering, subagent rows, widths, degradation
    tests/test_cost_state.py  53 checks: cost baselines, per-agent attribution, GC
    tests/fixtures/          a captured real stdin payload
    archive/                 pre-feature snapshot, kept from before git existed

One glyph, one meaning, one order. A **single** arrow `↓` is a level — what is
resident in a context window right now. **Double** arrows are flows: `⇧` is what
was uploaded this turn. Every line leads with the level and follows with the
upload, line 2 stating its level as a number and agent rows as a bar:

    ⛁ ██░░░░░░░░ 23% of 1M │ ↓ 232.5K (+64.9K)  ⇧ 105.1K │ +458/-173 │ $11.76 …
      ⇉ fable   ███░░░ 38%  ⇧ 75.6K  ⏱ 7m42s  (+$0.62)  Audit state schema ✓

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

    ./statusline/install.sh

Copies the script to `~/.claude/statusline.py` and merges a `statusLine` block
into `~/.claude/settings.json` without disturbing other settings. It runs both
suites against the repo copy first and aborts if either fails.

`refreshInterval` is read at startup, so **restart open sessions** after install.

## Tests

    python3 statusline/tests/test_render.py
    python3 statusline/tests/test_cost_state.py

By default both exercise the *deployed* script. Set `SL_SCRIPT` to point at a
candidate instead:

    SL_SCRIPT=statusline/statusline.py python3 statusline/tests/test_render.py

Fixtures (transcripts, sandbox `HOME`s, scratch databases) are generated at run
time inside `tests/` and are gitignored. Nothing touches the real
`~/.claude/statusline-state.db`; `CC_SL_STATE_DB` redirects every write.

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
import io; s = io.open("statusline.py").read()
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
- **An async agent that outlives its turn disappears** from the rows at the next
  prompt while still burning tokens. Rows are `promptId`-scoped, which is what
  keeps the scan bounded and the no-agent case free.
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
  with the three places to look and the suggested next step (log raw calibration
  samples under a flag and look at the scatter before theorising further), is the
  block comment above `attribute()`. Bounded meanwhile: rates only divide an
  interval between concurrent participants — every solo interval is measured
  money, and line 2's total never depends on them.
