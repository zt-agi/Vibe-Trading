---
name: PIT Health
description: Daily health read of ZT's point-in-time warehouse — the latest daily run and its integrity audit, ledger state and the index and audit receipts — with honest freshness.
markets: [us, global]
suggested_schedule: "15 9 * * *"
suggested_timezone: America/New_York
data_capabilities:
  - The latest point-in-time warehouse daily run with its integrity checks, overall result and exit code, plus the recent run history
  - Warehouse ledger summaries for sources, file coverage, ingest runs, papers and the inventory
  - The disposable index receipt and the one-hour audit receipt used by the simulation gate
  - Provenance for every figure (as-of instant, source path, content digest, point-in-time label and a fresh, stale or missing label)
---

# PIT health

Produce ZT's daily point-in-time warehouse health read, from the warehouse's
own log, ledgers and receipts, read through the zt-dashboards tools.

Resolve the current date and time from the run environment on every run. This
instruction text is stored once and replayed on every fire, so it contains no
date of its own — never assume the day it was written is the day it is running.

## Data to gather

1. `mcp_zt_dashboards_pit_inventory` — the latest daily run, recent runs,
   ledger summaries and receipts.

The response is an envelope: `status` (FRESH, STALE or MISSING), `as_of`
(UTC), `source_path`, `sha256` and `pit_label`, and each ledger and receipt
carries its own path, digest and status. Carry them into the read exactly as
returned.

## Method

- Lead with the latest daily run: when it started, its OVERALL result, its
  exit code, and how many of its integrity checks passed. Name every check
  that did not pass. A run with no exit code is reported as unfinished or
  interrupted, never as clean.
- Recent runs: show the sequence of OVERALL results so a break in the streak
  is visible.
- Ledgers: report sources by status, every source whose last status is not
  ok, ingest errors in the latest 24 hours, and file coverage by status.
- Receipts: report each receipt's status and age. The simulation gate accepts
  an audit receipt only while it is under one hour old and matches the lake;
  an older receipt is expected between simulations and is not a failure of
  the warehouse.

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

1. `## Latest daily run` — start time, OVERALL, exit code, checks passed out
   of checks run, and any check that did not pass.
2. `## Recent runs` — table of start time, OVERALL and exit code.
3. `## Ledgers` — one short block per ledger with its path, the first 12
   characters of its `sha256` and its key counts.
4. `## Receipts` — audit and index receipts: status, age, and what each gates.
5. `## Data gaps` — always present; write `none` when nothing was missing.
6. `## Verdict` — the machine-readable tail, and the only section
   nothing may follow. Exactly these lines, in this order:
   `- PIT-DAILY: STATE - reason` (the tool's status),
   `- PIT-AUDIT: STATE - reason` (the latest OVERALL result),
   `- PIT-EXIT: STATE - reason` (exit code zero is CLEAN, non-zero is ERRORS,
   absent is UNKNOWN), `- INGEST-24H: STATE - reason` (CLEAN or ERRORS),
   `- AUDIT-RECEIPT: STATE - reason` and `- INDEX-RECEIPT: STATE - reason`
   (each receipt's status). STATE is one of `FRESH`, `STALE`, `MISSING`,
   `PASS`, `FAIL`, `CLEAN`, `ERRORS`, `UNKNOWN`. These lines report data
   state, not advice: the Boundaries below still apply.

Keep the whole read under roughly 500 words.

## Boundaries

- This is a factual health read. No buy, sell, or hold calls, no price
  targets, no position sizing, no leverage suggestions.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. Every order needs a human's approval outside this run.
- Do not state, estimate, elicit or revise the probability of any outcome.
- Read only: do not run the warehouse audit, rebuild the index, start an
  ingest or run a simulation, and do not write, move or delete any file.
