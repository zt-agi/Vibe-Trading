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
        "freeze_evidence_packet",
        "inspect_evidence_packet",
        "submit_role_fork",
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

## Frozen packets and role forks (ZT add-on, 2026-09-28)

The swarm preset `actor_mct_team` drives this flow; each step is also a plain tool call.

1. `freeze_evidence_packet(scenario_id, run_asof, series, prices, memos, excluded_or_missing)` re-reads every named series and price through `obs_asof`/`price_asof` at `run_asof` (agents name series; they never type values). Rows without an admissible `pit_class` or known after the as-of are refused. Evidence-worker memos must cite admitted rows. The packet is written once under `VIBE_TRADING_HOME/actor_packets/<sha256>/packet.json`, where the sha256 is that of its canonical JSON, and records the audit-receipt hash, the lake signature and the scenario hash.
2. Role forks read `inspect_evidence_packet(packet_sha256, view="role_fork")`: decision states, actors, actions and the frozen evidence, without outcome labels, rewards or other forks.
3. Each fork calls `submit_role_fork(packet_sha256, temperament, memos, propensities)`. `fork_rules.py` ports `validate_forks` from `run_governed_pilot.py`: full state coverage with the node's actor, memos of 120+ characters that discuss every action, no terminal outcome named (checked on concepts, so paraphrases such as "a severe crude surge" count), propensities strictly between 0 and 1 summing to 1. Citations must resolve to the packet; an uncited state must name its missing observable and stay within 0.10 of uniform. Accepted forks are content-addressed and write-once.
4. `run_market_actor_sim(packet_sha256=..., fork_order=[...])` re-validates the accepted set (three or more, unique labels), refuses a scenario file that changed after the freeze, runs the unchanged simulator, and writes `run_manifest.json` beside `pilot_result.json`. The manifest records the packet, scenario, forks and engine hashes, the lake signature and audit-receipt hash at freeze and at run, the graph as-of, the ontology version (`INVESTMENT_ONTOLOGY_VERSION`, else `UNVERSIONED_NO_ONTOLOGY_RELEASE`), user preset and skill hashes, and the inherited model configuration. Results carry `anchor_status: elicited-only`; report them as bands (`model_form_range`, Wilson intervals), never as point estimates.

Tests: `python -m unittest test_server test_packets` from this folder with `VIBE_TRADING_HOME` set. With `INVESTMENT_AI_PROJECT_ROOT` also set, `PilotReplayTest` replays the 2026-08-28 pilot through freeze, three submissions and a packet run, and requires `ensemble_mc` to match `pilot_result.json` exactly (seed 20260828, 300,000 rollouts).

The existing pilot is an integration example, not calibrated alpha. No live trading, order placement, or autonomous schedule is enabled by this extension.