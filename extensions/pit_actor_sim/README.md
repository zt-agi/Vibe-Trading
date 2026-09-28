# PIT and market-actor extension

This opt-in MCP server connects Vibe-Trading to the existing Investment-AI-Drive-Research project. It adds tools to resolve a ticker to a permanent security ID, read price and observation vintages through the project's PIT macros, and run the project's existing evidence-frozen actor simulator. It does not add a broker connector or a second data store.

## Boundaries

- The Parquet lake at implementation/pit_warehouse/lake remains the canonical time-series store. The tools query price_asof and obs_asof with a required knowledge-time cutoff. A disposable E: DuckDB index (E:\pitdb\pit.duckdb) is rebuilt from the G: lake for responsive MCP reads, and the extension refuses stale index receipts. Each returned row includes source, knowledge time, revision, and PIT class.
- A row with NON_PIT or missing PIT class is labeled in the response. A data response is research context; it is not a certified backtest or trade signal.
- The simulator is market_actor_sim/run_governed_pilot.py. Agents provide state-local action propensities with cited evidence; the extension does not assign terminal probabilities. The original simulator validates forks, computes exact and sampled outcome distributions, and keeps the RESEARCH_PILOT_ONLY_UNCALIBRATED authority label.
- Simulation inputs remain in the canonical project. Temporary output, runtime history, and package dependencies stay on E: on Windows (ZT 2026-09-28: everything on E:); the extension refuses a runtime or index on C:, D: or the system drive. A passing pitdb audit receipt no older than one hour and bound to the current lake must exist before the tool runs the simulator. Accepted research results can be promoted separately through the project's governed ledger.
- This extension does not register Vibe-Trading's standard backtest data loaders. Their free-provider fallback does not meet the project's PIT contract by itself. Backtests use VT's dedicated `source="pitdb"` loader (`agent/backtest/loaders/pitdb_loader.py`), which requires a `pit` block in the run config and enforces the same index and audit receipts through `pit_guard.py`, the guard module this server also uses. Audit receipts now list `checks_passed`; the loader's formation mode requires check A11.

## Operator configuration

Set VIBE_TRADING_HOME to an isolated E: directory and INVESTMENT_AI_PROJECT_ROOT to the canonical project folder. Add a mcpServers entry to the runtime's agent.json:

~~~json
{
  "mcpServers": {
    "pit-actor-sim": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\pit_actor_sim\\server.py"],
      "env": {
        "VIBE_TRADING_HOME": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home",
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PITDB_INDEX": "E:\\pitdb\\pit.duckdb"
      },
      "enabledTools": [
        "pit_security",
        "pit_price_history",
        "pit_series_history",
        "run_market_actor_sim",
        "inspect_market_actor_run"
      ],
      "toolTimeout": 300
    }
  }
}
~~~

Before the first lookup, run `python extensions/pit_actor_sim/server.py --refresh-index` with `VIBE_TRADING_HOME`, `INVESTMENT_AI_PROJECT_ROOT`, and `PITDB_INDEX=E:\pitdb\pit.duckdb` set. Repeat after the compact G: lake tables change. The receipt at `VIBE_TRADING_HOME/pit_index_receipt.json` records the lake signature. Before a simulation, run `python extensions/pit_actor_sim/server.py --refresh-audit` with the same environment. That preflight audits the canonical lake in memory and writes a passing receipt valid for one hour, unless the lake changes sooner. Restart Vibe-Trading after changing the operator configuration. No API key or broker authorization is needed for these tools.

## Example research flow

1. Call pit_security with a ticker and an explicit UTC as-of instant. Use the returned sec_id.
2. Call pit_price_history or pit_series_history with the same as-of and a bounded date range. Inspect row-level PIT class and knowledge time.
3. For a scenario whose role forks and evidence snapshot already exist under market_actor_sim, call run_market_actor_sim with paths relative to that folder, such as config/scenario_iran_oil.yaml, runs/iran_oil_pit_pilot_20260828/role_forks.json, and runs/iran_oil_pit_pilot_20260828/evidence_snapshot.json.
4. Read the returned authority, audit and cross-check results, uncertainty intervals, and limitations. Use inspect_market_actor_run for the full result.

The existing pilot is an integration example, not calibrated alpha. No live trading, order placement, or autonomous schedule is enabled by this extension.