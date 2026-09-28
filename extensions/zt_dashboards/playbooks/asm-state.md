---
name: ASM State
description: Weekday read of ZT's Alpha Signal Monitor boards — monitored signal states, refresh lanes, the multiple-testing ledger and the promotion board — with honest freshness.
markets: [us, global]
suggested_schedule: "0 9 * * 1-5"
suggested_timezone: America/New_York
data_capabilities:
  - The monitored-signal state table with value, standardized score, state, freshness and point-in-time class per signal
  - The refresh-lane board with collector status, schedule and age against each lane's staleness limit
  - The cumulative multiple-testing count and the admission hurdle it implies
  - The promotion board with eligibility, the gate battery and recorded verdicts
  - Provenance for every figure (as-of instant, source path, content digest, point-in-time label and a fresh, stale or missing label)
---

# ASM state

Produce ZT's Alpha Signal Monitor state read from her own ASM board files,
read through the zt-dashboards tools. Signals on these boards are monitored,
not admitted; this read changes nothing about that.

Resolve the current date and time from the run environment on every run. This
instruction text is stored once and replayed on every fire, so it contains no
date of its own — never assume the day it was written is the day it is running.

## Data to gather

1. `mcp_zt_dashboards_signal_state` — one row per monitored signal.
2. `mcp_zt_dashboards_lane_status` — one row per refresh lane.
3. `mcp_zt_dashboards_test_ledger` — cumulative tests and the hurdle.
4. `mcp_zt_dashboards_promotion_board` — eligibility and gates per candidate.

Every tool response is an envelope: `status` (FRESH, STALE or MISSING),
`as_of` (UTC), `as_of_basis`, `source_path`, `sha256` and `pit_label`. Carry
them into the read exactly as returned.

## Method

- Lead with freshness per board, including `as_of_basis`: a board dated only
  by its file time is weaker evidence than one dated by its content.
- Lanes: list every lane whose own freshness is STALE, with its age against
  its limit and its collector status. Count lanes FRESH versus total.
- Monitored signals: report state and freshness per signal as the file states
  them. Only a FRESH signal may be described as current. Never describe a
  monitored signal as a trade, an edge or a recommendation.
- Ledger: report the cumulative test count and `hurdle_t` exactly as returned
  by the tool. Do not recompute it with another rule.
- Promotion board: report eligible candidates, gates run out of gates total,
  and any recorded verdicts with their dates. A gate that is NOT_RUN is
  reported as not run, never as passed.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook. A number that
  did not come back from a tool on this run does not appear in this report.
- Never present a stale figure as current. A response whose status is STALE
  keeps that word beside every figure taken from it.
- If a whole section has no evidence, keep its heading and write
  `no data retrieved` under it.
- Every figure carries its as-of date and the source path it came from.

## Output

Markdown, in this order:

1. `## Freshness` — a table of tool, status, `as_of`, `as_of_basis`,
   `source_path` and the first 12 characters of `sha256`.
2. `## Lanes` — table of lane, freshness, age versus limit, collector status,
   scheduled flag.
3. `## Monitored signals` — table of signal, state, freshness, as-of.
4. `## Multiple-testing ledger` — cumulative tests and the hurdle t.
5. `## Promotion board` — eligible candidates, gates run, verdicts.
6. `## Data gaps` — always present; write `none` when nothing was missing.
7. `## Verdict` — the machine-readable tail, and the only section
   nothing may follow. First one line per board, using these symbols:
   `- SIGNAL-STATE: STATE - reason`, `- LANE-BOARD: STATE - reason`,
   `- TEST-LEDGER: STATE - reason`, `- PROMOTION-BOARD: STATE - reason`,
   where STATE is the tool's status. Then one line per lane, with the lane id
   as the symbol: `- LANE_ID: STATE - one short reason`, where STATE is the
   lane's own freshness. STATE is one of `FRESH`, `STALE`, `MISSING`. These
   lines report data state, not advice: the Boundaries below still apply.

Keep the whole read under roughly 600 words.

## Boundaries

- This is a factual state read. No buy, sell, or hold calls, no price
  targets, no position sizing, no leverage suggestions.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. Every order needs a human's approval outside this run.
- Do not state, estimate, elicit or revise the probability of any outcome, and
  do not run or re-score any statistical test.
- Read only: do not write, move or delete any file, and do not change any
  lane, gate or verdict.
