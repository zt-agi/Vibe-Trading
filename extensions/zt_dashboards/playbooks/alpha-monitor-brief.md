---
name: Alpha Monitor Brief
description: Weekday pre-open read of ZT's Alpha Monitor export — source health, alerts, event guardrails and signal readiness — with provenance and honest freshness on every figure.
markets: [us, global]
suggested_schedule: "45 8 * * 1-5"
suggested_timezone: America/New_York
data_capabilities:
  - The dated Alpha Monitor export for the current day, or the latest one when today's is not yet written, with its alerts, source health, market and macro context, event guardrails and signal readiness
  - Portfolio import coverage and reconciliation status, without account numbers, quantities or values
  - The monitored source families with their catalogue status
  - Provenance for every figure (as-of instant, source path, content digest, point-in-time label and a fresh, stale or missing label)
---

# Alpha Monitor brief

Produce ZT's pre-open monitoring brief from her own Alpha Monitor project files,
read through the zt-dashboards tools. This is monitoring context for a human
reader, not a signal.

Resolve the current date and time from the run environment on every run. This
instruction text is stored once and replayed on every fire, so it contains no
date of its own — never assume the day it was written is the day it is running.

## Data to gather

1. Call `mcp_zt_dashboards_daily_snapshot` with `date` set to `today`.
2. If that response has status `MISSING`, say so in the first line of the brief
   (the export usually finishes shortly after 09:00 New York time), then call
   `mcp_zt_dashboards_daily_snapshot` with `date` set to `latest` and use that
   export, printing its own date beside every figure taken from it.
3. Call `mcp_zt_dashboards_source_families` for the source-family coverage and
   the catalogue grouped by family.

Every tool response is an envelope: `status` (FRESH, STALE or MISSING),
`as_of` (UTC), `source_path`, `sha256` and `pit_label`. Carry them into the
brief exactly as returned.

## Method

- Lead with freshness: which export was read, its `as_of`, its status, and
  whether the folder was complete (`missing_files`).
- Alerts: group by severity, highest first. Quote each alert title and its
  evidence id; do not reword an ERROR into something milder.
- Source health: list every source whose status is not FRESH, with its age
  label and cadence. A FRESH source needs no commentary.
- Event guardrails: an UNAVAILABLE calendar means no future event is known.
  Never infer or supply an earnings, dividend or macro release date.
- Signal readiness: report the counts by readiness and the `alpha_ready`
  count exactly as returned. If no row is alpha-ready, say that no signal is
  computed; do not describe any row as actionable.
- Portfolio: report reconciliation status, P&L status and value coverage
  percentage only. The tools withhold account numbers, quantities and values;
  never estimate or reconstruct them.
- Market and macro context rows are delayed reference prints. Report them
  with their own as-of dates and status; do not narrate them as moves to act
  on.

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

1. `## Freshness` — a table of tool, status, `as_of`, `source_path` and the
   first 12 characters of `sha256`, one row per tool response used.
2. `## Alerts` — by severity, with titles and evidence ids.
3. `## Source health` — the non-FRESH sources, with age and cadence.
4. `## Guardrails and readiness` — event calendar availability, signal
   readiness counts, portfolio coverage and reconciliation status.
5. `## Context` — market and macro rows with their as-of dates and status.
6. `## Data gaps` — always present; write `none` when nothing was missing.
7. `## Verdict` — the machine-readable tail, and the only section
   nothing may follow. The first line is the export itself:
   `- EXPORT: STATE - date read and why`. Then one line per source-health row,
   with the source id in upper case as the symbol:
   `- SOURCE: STATE - one short reason`, with STATE one of `FRESH`, `STALE`,
   `PARTIAL`, `UNDATED`, `BLOCKED`, `MISSING` (write `UNDATED` for an
   unknown-date source and `BLOCKED` for a policy-blocked one). When the
   export is missing entirely, write only the EXPORT line. These lines report
   data state, not advice: the Boundaries below still apply.

Keep the whole brief under roughly 700 words.

## Boundaries

- This is a factual monitoring brief. No buy, sell, or hold calls, no price
  targets, no position sizing, no leverage suggestions.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. Every order needs a human's approval outside this run.
- Do not state, estimate, elicit or revise the probability of any outcome.
- Read only: do not write, move or delete any file, and do not start any
  collector, refresh or backfill.
- Do not forecast the day's direction. Report the state; the reader decides.
