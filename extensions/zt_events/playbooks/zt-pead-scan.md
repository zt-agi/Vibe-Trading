---
name: ZT PEAD Scan
description: Weekday after-close scan of 8-K earnings releases with their point-in-time earnings surprise; lists post-earnings-drift entries and exits and records each one as an order proposal for a human to approve. Nothing is submitted.
markets: [us]
suggested_schedule: "45 17 * * 1-5"
suggested_timezone: America/New_York
data_capabilities:
  - 8-K Item 2.02 earnings releases knowable at the run instant, with SEC acceptance times, accession numbers and point-in-time labels
  - Standardized unexpected earnings from the EPS in the first 10-Q or 10-K that reported the quarter, with that filing's acceptance time
  - Entry and exit timing under the PEAD backtest rule, with a status for every fresh release
  - Historical counts of post-release drift by surprise bucket, as successes out of trials and never as probabilities
  - Order proposals recorded for human approval in Vibe-Trading, with nothing submitted
---

# PEAD scan

Scan ZT's point-in-time earnings events for post-earnings-announcement drift
(PEAD) after the close, and record order proposals for a human to approve. The
scan is mechanical: the zt-events tool computes every status, weight and
proposal; this playbook reports them and forwards the proposals unchanged.

Resolve the current date and time from the run environment on every run. This
instruction text is stored once and replayed on every fire, so it contains no
date of its own — never assume the day it was written is the day it is running.

## Data to gather

1. Take the run instant in UTC as an ISO timestamp ending in `Z`: the run
   as-of. Use the same value for every call below.
2. Call `mcp_zt_events_pead_candidates` with `asof` set to the run as-of and
   no other argument (long the top surprise bucket, a 20-session hold).
3. For each row in `candidates` and in `exits`, call `mcp_zt_events_sue` with
   that ticker and the same `asof`, to cite the EPS filings behind the surprise.
4. Call `mcp_zt_approvals_list_order_proposals` with `limit` set to 100. A
   proposal whose rationale begins with a row's `proposal_tag` already exists:
   that row is a duplicate. An exit row is proposable only when a proposal with
   status `FILLED` has a rationale beginning with the row's `entry_tag`.

## Method

- Lead with the run as-of and the `asof` the scan returned; they must match.
  Every release carries `knowledge_time` (the SEC acceptance) with its
  `pit_class`, and `sue_knowledge_time` (the filing that first reported the
  quarter's EPS) with its `sue_pit_class`. Print them beside the release.
- Candidates: for each row in `candidates`, give the ticker, direction,
  surprise and bucket, `decision_session`, `entry_open_utc` and
  `exit_open_utc`, and the bucket's record from `bucket_history` as "k of n
  past releases in this bucket drifted up over sessions +2 to +20" — a count,
  never a percentage and never a chance.
- Proposals: for each candidate that is not a duplicate, call
  `mcp_zt_approvals_propose_orders` once with the row's `proposal` fields
  exactly as returned — `rationale`, `evidence_ids`, `targets`, `signals` and
  `scope_symbols` — and no broker, so the simulated paper account is used.
  Record the returned proposal id and status, or quote the refusal.
- Exits: the same, for each row in `exits` that is proposable and not a
  duplicate. An exit without a filled entry is reported, not proposed.
- Watchlist: one line per row with its status — `SUE_PENDING`, `SUE_LATE`,
  `AWAITING_DECISION_CLOSE`, `STALE`, another `SUE_` status, or a release
  outside the traded buckets (direction `NONE`).

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved. If the scan itself
  failed, record no proposal on this run.
- If the approvals tools are unavailable, record no proposal and report every
  candidate and exit as `BLOCKED`.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook. A number that
  did not come back from a tool on this run does not appear in this report.
- If a whole section has no evidence, keep its heading and write
  `no data retrieved` under it.
- Every release carries its acceptance time and point-in-time label.

## Output

Markdown, in this order:

1. `## Run` — the run as-of, the scan's `asof`, its `history_status` and the
   parameters it reports.
2. `## Candidates` — one table row per candidate, as described above.
3. `## Exits` — one table row per exit, with `exit_open_utc` and whether a
   filled entry was found.
4. `## Proposals` — one line per proposal call: ticker, entry or exit, and the
   proposal id and status, or the refusal quoted.
5. `## Watchlist` — one line per watchlist row, with its status.
6. `## Data gaps` — always present; write `none` when nothing was missing.
7. `## Verdict` — the machine-readable tail, and the only section nothing may
   follow. One line per ticker in candidates, exits and watchlist:
   `- TICKER: STATE - one short reason`, with STATE one of:
   - `PROPOSED` — an entry or exit proposal was recorded on this run (name
     which, with the proposal id);
   - `DUPLICATE` — the proposal already existed, so none was recorded;
   - `BLOCKED` — the approvals tool refused or was unavailable (quote why);
   - `WATCH` — a release outside the traded buckets, one awaiting its decision
     close, a surprise that cannot be computed, or an exit without a filled
     entry;
   - `PENDING` — the surprise is not known yet (no 10-Q or 10-K EPS);
   - `SUE-LATE` — the surprise was first known after the close of day +1, so
     the release is not traded;
   - `STALE` — the entry window has passed.
   When a ticker has two rows, write one line with the state of its entry row
   and name both in the reason. With no rows at all, write the heading alone.

Keep the whole report under roughly 700 words.

## Boundaries

- Orders are proposals only. The one tool that records a proposal is
  `mcp_zt_approvals_propose_orders`; it submits nothing, and a human approves
  or rejects every proposal in Vibe-Trading.
- Do not place, modify, or cancel any order, do not approve or reject a
  proposal, and do not touch a live trading connector.
- Propose only rows the scan returned in `candidates` or `exits`, with their
  `proposal` fields unchanged. Never add a symbol, change a direction, a
  weight or the text, choose a broker, or propose anything from judgment.
- No price targets, no leverage suggestions, and no buy, sell, or hold calls
  beyond the forwarded proposals.
- Do not state, estimate, elicit or revise the probability of any outcome.
  Historical drift is reported as counts of past releases.
- Read only, apart from recording proposals: do not write, move or delete any
  file, and do not start any collector, refresh or backfill.
- Do not forecast prices or returns. Report the state; the human decides.
