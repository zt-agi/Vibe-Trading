# ZT add-on: research tools (ranker, ASM board, hypothesis import)

An opt-in stdio MCP server (`zt-research`) that gives Vibe-Trading's agent the
project's PIT alpha ranker and the Alpha Signal Monitor (ASM) boards. It extends
VT through `agent.json` and VT's own hypotheses registry; no VT file is changed.
Read-only by default, no order path, no probabilities.

| Tool | Writes | Purpose |
|---|---|---|
| `rank_pit_signals(config_name, asof, limit=20)` | E: scratch only | Runs `python -m pit_alpha_ranker run` (implementation/alpha_signal_pit_ranker) on a named input cut at `asof`. Returns the ranking summary (top rows per like-for-like group, counts by status / PIT status / predictive status) and `manifest_path`, `ranker_manifest_path`. |
| `asm_promotion_board()` | nothing | ASM phase-3 board: eligibility, status, verdict and the eight gates per candidate (zt-dashboards envelope: as_of, sha256, FRESH/STALE/MISSING). |
| `hypothesis_test_ledger(limit=200)` | nothing | Multiple-testing ledger: cumulative tests, the rising t hurdle, duplicate test ids, the latest `limit` rows. |
| `import_promotion_board_to_vt(apply=False)` | VT hypotheses store, only with `apply=True` | Maps each board row to a VT research hypothesis (marker data source `asm:<signal_id>`). Default returns the create/update/unchanged/orphan/conflict diff and writes nothing. |

### rank_pit_signals

- Named inputs live in `rank_configs.json`:
  - `rank01_market_prices` - pitdb EOD bars as known at `asof` (the `price_asof`
    macro, in an in-memory DuckDB with spilling off; lake parquet read only),
    last 550 days, equities and ETFs, turned into the eight Rank-01 features by
    the ranker's own `eod_market_prices_to_signal_panel` (labelled
    `REVISED_VALUE`, so results stay `PIT_RESEARCH_ONLY`).
  - `rank01_market_prices_retained` - the retained Rank-01 observation panel.
- Every input is cut at `asof`: a row is ranked only if its available, decision
  and target times are all at or before `asof` (`pit_cut` in the result).
- `asof` must carry a timezone offset and be in the past.
- Scratch: `VIBE_TRADING_HOME\zt_research\rank_runs\<utc>_<config>_<asof>_<id>\`
  (`input\`, `output\` = the ranker's own files, `zt_research_manifest.json` with
  config, lake and package hashes, the exact commands and exit codes). On Windows
  it must be on E:, and it may not be inside the project. Both steps run in child
  interpreters with `-B` and TEMP/TMP inside the run folder, so nothing is written
  to the project or C:. `ZT_RESEARCH_RANK_TIMEOUT` (default 900 s) bounds both child
  steps together; keep `toolTimeout` above it.

### import_promotion_board_to_vt

Status mapping (conservative): `exploring` (no gate run), `testing` (a gate run,
or a PASS verdict without G0..A6 all PASS), `validated` (PASS/ADMITTED verdict and
G0..A6 PASS), `monitoring` (validated and A7 decay watch running), `rejected` (any
FAIL gate or verdict). The importer owns title, status, signal definition and
invalidation notes and adds its markers to data sources; thesis, universe, skills
and other sources set in VT survive. Signals that left the board are reported as
`orphan` and never deleted; two VT hypotheses with one marker are a `conflict` and
are left alone. The store is VT's own (`VIBE_TRADING_HYPOTHESES_PATH`, else
`~/.vibe-trading/hypotheses.json` of the VT process); on Windows `apply` requires
it on E:.

## Operator configuration (PC1)

Add beside the other ZT servers in `E:\codex-runtime\investment-ai\vibe-trading\home\agent.json`:

~~~json
{
  "mcpServers": {
    "zt-research": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["-B", "E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\zt_research\\server.py"],
      "env": {
        "VIBE_TRADING_HOME": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home",
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PYTHONDONTWRITEBYTECODE": "1"
      },
      "enabledTools": [
        "rank_pit_signals",
        "asm_promotion_board",
        "hypothesis_test_ledger",
        "import_promotion_board_to_vt"
      ],
      "toolTimeout": 960
    }
  }
}
~~~

VT names the tools `mcp_zt_research_<tool>`. The server inherits VT's environment,
so the hypotheses store is the one VT's Web UI shows. Restart VT after editing
`agent.json`.

## Tests

~~~powershell
$env:TEMP = "E:\codex-runtime\investment-ai\vibe-trading\home\tmp"; $env:TMP = $env:TEMP
python -B -m pytest extensions\zt_research\tests -q -p no:cacheprovider
# optional, reads the canonical project (about a minute, writes only to TEMP):
$env:ZT_RESEARCH_REAL_PROJECT = "1"
$env:INVESTMENT_AI_PROJECT_ROOT = "G:\My Drive\work\Investment-AI-Drive-Research"
python -B -m pytest extensions\zt_research\tests\test_real_project.py -q -p no:cacheprovider
~~~

The synthetic suite builds a project with a stub `pit_alpha_ranker` (same CLI),
a three-security lake with a late revision and bars first known after the as-of,
and an ASM board covering every status mapping.
