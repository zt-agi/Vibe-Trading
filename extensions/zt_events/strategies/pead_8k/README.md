# pead_8k -- post-earnings drift on 8-K releases (VT backtest template)

A Vibe-Trading strategy template: long the top SUE bucket (optionally short the
bottom one) after 8-K Item 2.02 earnings releases, held a fixed number of
sessions. It runs on VT's own runner with `source="pitdb"`, so prices, the
benchmark and the events all come from ZT's point-in-time warehouse. A research
backtest only: nothing here places an order.

## Timing

- Day 0 of a release is the first session whose close (16:00 New York, 13:00 on
  early-close days) is strictly after its SEC acceptance.
- The SUE is known when the 10-Q or 10-K that first reported the quarter's EPS
  was accepted. The decision bar is the first session whose close is after the
  later of the two instants, so a release accepted after the close is never
  traded on its own session's bar.
- VT shifts every signal by one bar: the position fills at the next session's
  open and is held `hold_sessions` sessions (default 20).
- Releases whose SUE came after the close of day +1 are left out by default
  (`--max-sue-lag-sessions 1`), matching the zt-events bucket history and scan;
  `--no-sue-lag-limit` keeps them, entered whenever the SUE became known.
- A holding window must be at least as long as a `--rebalance-mask` cadence, or
  an event could open and close between two rebalance dates unseen; the
  builder refuses otherwise.

## Files

| File | Role |
|---|---|
| `signal_engine.py` | The template VT loads (literal constants only, as VT's sandbox requires). `EVENTS` is empty here. |
| `config.template.json` | Run config: `source="pitdb"`, a `pit` block (snapshot mode, research claim), the `pead` parameters. |
| `build_run.py` | Reads the events and SUE as knowable at `--run-asof` through the zt-events store and writes a run directory: `config.json`, `code/signal_engine.py` with `EVENTS` filled in, and `pead_events.json` (every release used or excluded, with the reason, knowledge times and PIT classes). |

## Run

Needs VT importable, `INVESTMENT_AI_PROJECT_ROOT`, `VIBE_TRADING_HOME` and
`PITDB_INDEX` (as for the zt-events server), a fresh index
(`server.py --refresh-index`) and, for VT's pitdb loader, a PASS audit receipt
under an hour old (`server.py --refresh-audit`), both from `pit_actor_sim`.
The run directory must sit under one of VT's allowed run roots.

~~~powershell
$env:PYTHONPATH = "E:\codex-runtime\investment-ai\vibe-trading\src\agent"
python build_run.py --run-dir E:\codex-runtime\investment-ai\vibe-trading\home\runs\pead_8k `
    --start 2018-01-01 --end <last completed session> --run-asof <now, ...Z>
cd E:\codex-runtime\investment-ai\vibe-trading\src\agent
python -m backtest.runner E:\codex-runtime\investment-ai\vibe-trading\home\runs\pead_8k
~~~

Options: `--tickers`, `--hold`, `--short-bottom`, `--slot-weight` (default
0.1 of equity per position), `--edges`, `--rebalance-mask`, `--benchmark`,
`--initial-cash`, `--pit-mode`, `--claim`, `--availability-lag`.

The lake's price history is a backfill, so historical runs use
`--pit-mode snapshot --claim research` (the default); `formation` with a
`tradeable` claim is only for windows whose bars were captured daily.
