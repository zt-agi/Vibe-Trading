# Investor-style factor pack (zt_style)

This opt-in, read-only MCP server gives Vibe-Trading one tool, `investor_style_scores(ticker, asof)`. It recomputes the numeric rules behind ai-hedge-fund's investor personas (MIT, commit 5d2c7ca2) from the Investment project's point-in-time warehouse, with no LLM: defensive-value checks (P/E against 15 and 20, current ratio at least 1.5, earnings positive every fiscal year, long-term debt against net current assets), the EPS growth tier (fast grower at 20 percent or more, stalwart at 10 to 12 percent) and PEG, return-on-equity consistency, book value per share CAGR, operating and free cash flow, debt/equity, and growth and margin inflection. Every rule reports its value, a status (PASS, PASS_LENIENT, FAIL, NOT_MEANINGFUL, UNKNOWN), its input ids and the concepts it missed; every input carries its series id, period end, form and knowledge time. A style label describes which families pass. The output has no probabilities and no trade instructions.

## Boundaries

- Facts are read only through the sibling `pit_actor_sim` extension's guarded query path: `pit_security`, `pit_price_history` (price_asof) and `issuer_annual_facts` (obs_asof over `SEC:<TICKER>:*:FY`). That path runs the approved SQL in a worker process against the E: index and refuses a stale index receipt. Nothing is written.
- Growth rates use the real spacing of fiscal-year ends. ai-hedge-fund's snapshot CAGR assumed evenly spaced periods, so a missing year inflated the rate; here it widens the span.
- `obs_asof` returns no period start, so a three-month value filed at a fiscal-year end is indistinguishable from the annual value. Annual periods are the fiscal-year ends of the latest annual filing (within 10 days, for 52/53-week years), and an annual revenue far below its neighbours is flagged in `warnings`.
- The warehouse's XBRL connector does not yet ingest diluted EPS, equity, current assets and liabilities, operating cash flow or share counts. Until it does, the rules that need them read UNKNOWN with the missing concept named, and EPS growth falls back to net income (`basis` says so).
- `pit_actor_sim.freeze_evidence_packet` adds the same outcomes to evidence packets as `derived_style_scores` rows with neutral family names; blind packets keep only their scale-free values.

## Operator configuration

Add an entry beside `pit-actor-sim` in the runtime's `agent.json`:

~~~json
{
  "mcpServers": {
    "zt-style": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\zt_style\\server.py"],
      "env": {
        "VIBE_TRADING_HOME": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home",
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PITDB_INDEX": "E:\\pitdb\\pit.duckdb",
        "PYTHONDONTWRITEBYTECODE": "1"
      },
      "enabledTools": ["investor_style_scores"],
      "toolTimeout": 300
    }
  }
}
~~~

The index receipt from `pit_actor_sim/server.py --refresh-index` must be current. The VT user skill `investor-style-checklists` explains the output to agents.

Tests: `python -B -m unittest discover -s extensions/zt_style -p "test_*.py"` with `VIBE_TRADING_HOME` set; `INVESTMENT_AI_PROJECT_ROOT` adds the SQL test against the project's pitdb schema, and `ZT_STYLE_SKILL_DIR` the skill-loader test.
