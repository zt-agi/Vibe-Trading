# Earnings-events extension (zt-events)

This opt-in MCP server gives Vibe-Trading read-only, point-in-time access to 8-K
Item 2.02 earnings releases and the standardized unexpected earnings (SUE)
behind them, an event study over them, and a post-earnings-announcement-drift
(PEAD) scan. Everything is read from ZT's pitdb warehouse (free SEC EDGAR data)
through the sanctioned as-of macros on the read-only E: index. It adds no
broker, order or write tool.

## Tools

| Tool | Returns |
|---|---|
| `earnings_8k_events(tickers, start, asof)` | 8-K Item 2.02 releases knowable at `asof`: accession, form, items, SEC acceptance (`knowledge_time`), estimated fiscal period, and `duplicate_of` for a second release in the same firm-quarter (the earliest wins). |
| `sue(ticker, asof)` | SUE of the newest quarter known at `asof` (seasonal random walk on diluted EPS, scaled by the standard deviation of the last eight seasonal changes, at least four; split-adjusted), with every input's knowledge time. |
| `event_study(tickers, windows, start, end, asof, ...)` | CARs by SUE bucket (Q1..Q5, fixed edges -1, -0.25, 0.25, 1) and SIGNED: market model over sessions [-250, -11], day 0 the first session whose close is after the acceptance; t, clustered t, Patell, BMP, Kolari-Pynnonen, sign and GRANK tests, a cluster bootstrap interval, Benjamini-Hochberg across windows x groups, hit counts, and every skipped event with its reason. |
| `pead_candidates(asof, tickers, lookback_days, short_bottom, hold_sessions, slot_weight)` | Fresh releases with their SUE, bucket, decision session, entry and exit opens and status; `candidates` (entries due), `watchlist`, `exits` (holds ending at the next open), and the bucket history as counts. Each candidate and exit carries `proposal`: order-proposal arguments built mechanically for the approvals tool. |

Every response carries `asof`, knowledge times and PIT classes. `asof` is
required, must carry an offset (`...Z`) and must not be in the future.

## Boundaries

- Read only: nothing is written, fetched or traded. `pead_candidates` builds
  proposal arguments; recording a proposal is the zt-approvals tool's job, and
  a human approves every order.
- No probabilities: the tools return hit counts (successes out of trials).
  The only probability in the stack is the forecast-ledger hook below, and it
  is a mechanical Beta-binomial rate, never an LLM's.
- Point in time: a release is visible from its SEC acceptance instant; a SUE
  from the acceptance of the 10-Q or 10-K that first reported the quarter's
  EPS. The PEAD decision instant is the later of the two. The scan, the bucket
  history, the ledger hook and the `pead_8k` backtest all drop a release whose
  SUE came after the close of day +1.
- Storage: the server sets `sys.dont_write_bytecode`; nothing lands on C: or D:.

## Data (pitdb)

Two connectors in `implementation/pit_warehouse/pitdb/connectors/sec_8k_earnings.py`
(`sec_8k_earnings`, `sec_xbrl_eps`, both TRUE_PIT, free SEC JSON at most
about seven requests a second) feed `fact_event` and `fact_observation`;
`pitdb/events.py` adds the `event_asof` and `obs_revisions_asof` macros from
`events_schema.sql` (schema.sql is untouched) and the pull CLI. Run order on
PC1, after the pitdb daily job, with `PITDB_INDEX` unset (the in-memory engine
hydrates from the lake and flushes back to it):

~~~powershell
python -m pitdb.events pull        # 8-K events, then XBRL EPS; persists to the lake
python E:\codex-runtime\investment-ai\vibe-trading\src\extensions\pit_actor_sim\server.py --refresh-index
~~~

## Operator configuration

Add an entry beside `pit-actor-sim` in the runtime's `agent.json`:

~~~json
{
  "mcpServers": {
    "zt-events": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\zt_events\\server.py"],
      "env": {
        "VIBE_TRADING_HOME": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home",
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PITDB_INDEX": "E:\\pitdb\\pit.duckdb",
        "PYTHONDONTWRITEBYTECODE": "1"
      },
      "enabledTools": ["earnings_8k_events", "sue", "event_study", "pead_candidates"],
      "toolTimeout": 300
    }
  }
}
~~~

Vibe-Trading names the tools `mcp_zt_events_<tool>`. The `zt-pead-scan`
playbook (`playbooks/`, deployed to `vt_addons/playbooks`) calls them, and
forwards proposals to `mcp_zt_approvals_propose_orders` (the zt-approvals
server) for a human to approve.

## PEAD pieces

- `strategies/pead_8k/` -- backtest template for VT's runner (`source="pitdb"`
  with a `pit` block); see its README.
- `pead_ledger.py` -- forecast-ledger hook: one row per fresh eligible release
  with P(CAR[+2,+20] > 0 | SUE bucket) = (k + 1) / (n + 2) from the bucket's
  historical counts, baseline = the pooled buckets, resolved from pitdb prices:

~~~powershell
python pead_ledger.py stage --asof <now, ...Z>                      # dry run: counts only
python pead_ledger.py stage --asof <now> --write --publish --project "G:\My Drive\work\Investment-AI-Drive-Research"
python pead_ledger.py resolve --project "G:\My Drive\work\Investment-AI-Drive-Research"
~~~

Stage and publish before the close of day +1 (the ledger refuses late rows);
the row resolves at the close of session +20.

## Tests

~~~powershell
$env:PYTHONPATH = "E:\codex-runtime\investment-ai\vibe-trading\src\agent"
$env:INVESTMENT_AI_PROJECT_ROOT = "G:\My Drive\work\Investment-AI-Drive-Research"
python -m pytest extensions\zt_events\tests -q -p no:cacheprovider
~~~

The store-backed tests build an in-memory warehouse from the project's
`schema.sql` and saved SEC samples (no network) and skip without
`INVESTMENT_AI_PROJECT_ROOT`. Set `ZT_PLAYBOOK_DEPLOY_DIR` to the deployed
`vt_addons\playbooks` folder to check the deployed playbook copy.
