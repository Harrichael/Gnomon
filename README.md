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
    tests/test_render.py     82 checks: rendering, subagent rows, widths, degradation
    tests/test_cost_state.py 33 checks: cost baselines, per-agent attribution, GC
    tests/fixtures/          a captured real stdin payload
    archive/                 pre-feature snapshot, kept from before git existed

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
