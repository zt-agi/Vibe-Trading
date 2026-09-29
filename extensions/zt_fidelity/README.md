# ZT add-on: Fidelity positions connector (read-only)

A Vibe-Trading local connector plugin (`transport: local_plugin`) that feeds VT's
**Portfolio** page from ZT's Fidelity positions exports. It extends VT through
VT's own plugin contract (`agent/src/trading/local_plugins.py`); no VT file is
changed and no VT feature is disabled.

- Reads the newest `Portfolio_Positions_<Mon-DD-YYYY>.csv` under
  `<broker_raw_dir>/*_refresh/` (matches both `<date>_portfolio_refresh` and
  `<date>_refresh`), plus `portfolio_export_context_<stamp>.json` and
  `csv_download_receipts.jsonl` beside it.
- No login, no credentials (`auth.type: none`), no network, no writes: it never
  opens anything under `broker/raw` for writing. Standard library only.
- Declares only `account.read` and `positions.read`; there is no order path.

## Behaviour

| Topic | Rule |
|---|---|
| Accounts | Every account number is shown as `****` + last 4. A full number found in any text field is masked the same way; errors never quote CSV content. |
| As-of | Export time from the context JSON, else the download receipt, else the CSV's `Date downloaded` footer, else the file mtime. `export.as_of_source` says which. Times without an offset are New York time. |
| STALE | Older than `stale_after_days` (default 3): status `STALE` with the reason. VT's portfolio then lists the source as failed with that reason, rather than showing old holdings as current. `serve_stale: true` serves them anyway, marked `export.stale`. |
| Newest file | Grouped by the date in the file name (else folder date, else mtime); the latest export time within the newest date wins. An unreadable newest file is skipped with a warning. |
| Parsing | `$1,234.56`, `+$1.20`, `-$3`, `(4.00)`, `+1.2%`, `--`, BOM, CRLF, preamble lines, trailing commas, `Today's`/`Today’s`, any header case, the disclaimer and `Date downloaded` footer lines, cp1252 fallback. |
| Cash | `SPAXX**`-style core money-market and FCASH rows are account cash; `Pending activity` is added to cash (net). |
| Holdings | Merged per symbol across accounts (VT keys a source's rows by symbol); the split stays in `accounts` / `held_in`. `quantity × market_price` equals Fidelity's current value, so an option is priced per contract and a bond per $1 of face (`price_multiplier` records the ratio to the quoted last price). |
| Asset type | `option` (Fidelity `-` symbols), `bond` (CUSIP), `mutual_fund` (5 letters ending in X); others use VT's default. |

## Install on PC1

~~~powershell
$env:VIBE_TRADING_HOME = "E:\codex-runtime\investment-ai\vibe-trading\home"
vibe-trading connector validate E:\codex-runtime\investment-ai\vibe-trading\src\extensions\zt_fidelity\connector
vibe-trading connector install  E:\codex-runtime\investment-ai\vibe-trading\src\extensions\zt_fidelity\connector
vibe-trading connector setup zt-fidelity-live-readonly     # creates connection "zt-fidelity-live" and tests it
~~~

Then in the Web UI: Portfolio -> add the `zt-fidelity-live` connection as a
source -> Refresh. To upgrade, delete `%VIBE_TRADING_HOME%\connectors\zt-fidelity`
and install again (VT refuses to overwrite an installed connector).

## Settings

Optional `settings.json` in the installed folder (see `settings.example.json`):

| Key | Default |
|---|---|
| `broker_raw_dir` | `$ZT_FIDELITY_BROKER_RAW`, else `$INVESTMENT_AI_PROJECT_ROOT\broker\raw`, else `G:\My Drive\work\Investment-AI-Drive-Research\broker\raw` (the env override wins over the file) |
| `folder_glob` | `*_refresh` |
| `file_glob` | `Portfolio_Positions_*.csv` |
| `stale_after_days` | `3` |
| `serve_stale` | `false` |

## Tests

~~~powershell
$env:TEMP = "E:\codex-runtime\investment-ai\vibe-trading\home\tmp"; $env:TMP = $env:TEMP
python -B -m pytest extensions\zt_fidelity\tests -q -p no:cacheprovider
~~~

`fixtures/Portfolio_Positions_Sep-26-2026.csv` is synthetic (invented accounts
and values). `test_vt_integration.py` installs the connector with VT's own
`plugin_scaffold`, reads through `src.trading.service`, and runs
`PortfolioService.refresh` end to end.
