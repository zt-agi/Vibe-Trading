# Daily research report extension

This opt-in MCP server lets Vibe-Trading validate and publish the owner's daily research report, record its forecast track, and report PIT audit freshness honestly. It imports the report package from the canonical project (`implementation/daily_research_report`) and reads prices only through the `pit_actor_sim` extension's point-in-time layer. It adds no broker, order or trading tool.

## Tools

| Tool | Writes | Purpose |
|---|---|---|
| `validate_daily_report(input_json)` | nothing | Validator receipt (v1 or v2 input) plus server checks: a PASS audit claim must match a receipt this server observed as PASS; forecast counts must equal the staged rows; watchlist symbols must be on the research watchlist config. |
| `publish_daily_report(input_json)` | project `reports/daily/<date>/<run_id>/`, `DAILY_BRIEF_LATEST.html` | Validate, render, run a full-text QA of the rendered page, write `report.html`, `input.json`, `validation.json`, `manifest.json` and `evidence/*.txt` in an E: scratch folder, then publish the folder once. Refuses an existing run folder or a run id published on another date. The pointer only moves to a later cutoff. |
| `pit_audit_status()` | E: runtime archive | PASS, STALE, MISSING, FAIL or UNVERIFIED for `VIBE_TRADING_HOME/pit_audit_receipt.json` (PASS needs age <= 60 minutes and the current lake signature). Observed receipts and observations are archived for the publish check. |
| `price_figures(tickers, freeze_time)` | nothing | Code-computed report figures (reference close, session and five-session change, twenty-session mean absolute move, volume ratio) from closed PIT bars, each with a complete reference. |
| `stage_forecast_rows(tickers, freeze_time, run_id)` | E: `forecast_staging/` | Mechanical baseline and CBOE market-implied distributions as forecast-ledger rows (kind `distribution`). Returns counts only; distributions are never returned or displayed until the P2 gate passes. Prospective only (freeze within six hours), research-watchlist tickers only, one staging per run. |

## Boundaries

- Research only. The validator rejects operational wording, LLM-set probabilities, raw numbers without references, holdings fields and any trading tool in `tools_used`.
- Point in time: every warehouse read uses `asof = freeze_time`; nothing known or captured after the cutoff is accepted; only bars closed by 16:00 New York count.
- Storage: runtime state (scratch builds, staging, audit archive, CBOE raw chains) lives under `VIBE_TRADING_HOME`, which must be on E: on Windows and outside the project. Published reports go to the project. Nothing is written to C: or D:; the server sets `sys.dont_write_bytecode` so no `__pycache__` lands in the shared project.
- Free data only: the CBOE delayed chain is a free public endpoint, at most ten requests per run, one second apart.

## Operator configuration

Add an entry beside `pit-actor-sim` in the runtime's `agent.json`:

~~~json
{
  "mcpServers": {
    "zt-daily-report": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\zt_daily_report\\server.py"],
      "env": {
        "VIBE_TRADING_HOME": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home",
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PITDB_INDEX": "E:\\pitdb\\pit.duckdb",
        "PYTHONDONTWRITEBYTECODE": "1"
      },
      "enabledTools": [
        "validate_daily_report",
        "publish_daily_report",
        "pit_audit_status",
        "price_figures",
        "stage_forecast_rows"
      ],
      "toolTimeout": 600
    }
  }
}
~~~

Vibe-Trading names the tools `mcp_zt_daily_report_<tool>`. The swarm preset `premarket_brief_team` and the `premarket-brief` playbook call them by those names. Optional environment: `ZT_DAILY_REPORT_PACKAGE` (package folder override), `PIT_ACTOR_SIM_SERVER` (path to the sibling server), `ZT_SKILLS_DIR` (skills to hash into the manifest).

## Tests

~~~powershell
$env:VIBE_TRADING_HOME = "E:\codex-runtime\investment-ai\vibe-trading\home"
$env:INVESTMENT_AI_PROJECT_ROOT = "G:\My Drive\work\Investment-AI-Drive-Research"
python -m unittest -v test_server
~~~

The tests build a throw-away project under `VIBE_TRADING_HOME`, copy the report package into it, and fake the warehouse and the CBOE endpoint; they never write to the real project, the system temp folder, C: or D:.
