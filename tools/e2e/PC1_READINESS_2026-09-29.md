# VT PC1 readiness ledger

Work order: WO-20260929-0133-finish-the-wp2-vt-add-ons-ai-hedge-fund-additions-and-qa. Updated 2026-09-29T16:57:42.571812+00:00.

**PC1 desktop and web are ready for user testing.** The broader work order remains open on its original Linux container UI gate; that check must not be represented as passed. The six WP2 packages remain integrated into VT. Canonical source is `G:\My Drive\work\Investment-AI-Drive-Research\third_party\upstream_sources\Vibe-Trading` on branch `feature/pit-actor-sim`; the tested code commit is `6ae78f54c18097a16e9019977b30c845b713a021`. The E: runtime was synchronized from that exact clean code commit at 2026-09-29T12:45:51.6306687-04:00.

## Why the previous attempt stalled, and the repair

Four Windows MemoryExhaustionDetector Event 2004 records between 08:28 and 08:39 Eastern show 99.65%-99.97% of the then-available commit limit consumed. One event names `llama-server.exe` with about 17.1 GB committed; the logs do not identify which host launched it. Replayed work also found a 300-second in-memory PIT audit stuck hydrating a 3.25 GB lake, a Drive-backed test collection that stalled while the E: copy completed in seconds, and an unauthenticated Git push whose remote result was initially unclear. These are evidence-backed stall mechanisms, rather than proof of a single Codex-host failure.

Execution now uses the E: `execution_guard.py`: finite deadlines, 10-second heartbeats, Drive-read and memory preflight, owned-child cleanup, and durable receipts. BLAS worker counts are bounded; the PIT audit uses a closed readonly E: index, checks A1-A11, and records a fresh lake signature. The source was checkpointed via authenticated, bounded Git operations and the remote reference was verified. Production Ollama now uses an E:-stored alias of the same model with `num_ctx 8192`; the prior production environment file was backed up on E:. These changes do not retroactively repair the prior session.

## Verified acceptance

| Gate | Result | Evidence |
| --- | --- | --- |
| Real Electron desktop, first content, `/zt`, reports, scroll at 1280/1024/960, native injected Back/Forward, backend restart, cleanup, console | 14 PASS, 0 FAIL; actual native window | `E:\codex-runtime\investment-ai\vibe-trading\logs\desktop-qa-final\native-confirmed\results.json` |
| Real local Ollama prompt from fresh desktop with tool registry empty | PASS; 0 tool definitions, 0 tool events | Same native receipt |
| Strict live web and emulated desktop routes; overflow, report viewer, wheel/keyboard, controls | 121 PASS, 0 FAIL at 1680/1366/1024/800/500/390 px | `E:\codex-runtime\investment-ai\vibe-trading\logs\e2e\20260929_124708\results.json` |
| Real paper approval browser/API flow with synthetic prices | 3 PASS: one fill, repeat refused, rejection reason, expiry refusal | `E:\codex-runtime\investment-ai\vibe-trading\logs\paper-browser-confirmed-20260929\paper-results.json` |
| Isolated WP2 package suites | 585 PASS, 11 SKIP | `E:\codex-runtime\investment-ai\vibe-trading\logs\wp2_suite_20260929_112214\summary.json` |
| Intraday timing and cross-engine focused suite | 162 PASS | `E:\codex-runtime\investment-ai\vibe-trading\logs\intraday_reviewed_final.xml` |
| Provider regression; optional-goal API | 178 PASS; 8 PASS | `E:\codex-runtime\investment-ai\vibe-trading\logs\provider_current.xml`; `E:\codex-runtime\investment-ai\vibe-trading\logs\optional_goal_api.xml` |
| Ubuntu-on-E: current intraday timing | 25 PASS, 0 FAIL | `E:\codex-runtime\investment-ai\vibe-trading\logs\linux_intraday_current.xml` |
| Canonical PIT audit on readonly E: index | PASS A1-A11 at 2026-09-29T16:52:31.648919+00:00 | `E:\codex-runtime\investment-ai\vibe-trading\home\pit_audit_receipt.json` |

The web server was restarted after the production model change and returned HTTP 200 on `/health`. The real desktop launcher opened a responsive Electron window, and its private backend returned HTTP 200. The signed-in web launcher opened `/zt` in the default browser. The E: home retains `live/HALT`, API authentication, required human order approval, disabled shell tools and scheduler, and English output. No live order was placed. The paper test used only an isolated fixture and synthetic prices.

## Open boundaries and decisions

The original container UI gate is **UNRUN**, because the installed Docker data root is on D: and the project forbids new C:/D: storage. The Linux timing gate above ran in Ubuntu backed by E:, not a container. An earlier Linux as-of run had 21 PASS and 1 dependency failure from missing `fastmcp`; this is not counted as a passing suite. Intraday completion is guarded, but full intraday fill timing is not certified for tradeable claims. The production alias matches the tested model's weights and 8192 context; the user's first production prompt remains unobserved. The PIT audit receipt has a one-hour freshness window and must be refreshed for later backtests or tradeable claims.

Ruling: keep the existing canonical G: component branch and disjoint ownership rather than make an extra content worktree; the mandatory canonical-root rule controls. Ruling: put disposable execution state on E: and keep this durable G: ledger; the E:-only runtime rule controls. Ruling: count known upstream overflow as a strict failure and fix it; the user requested accepted desktop and web surfaces. The PC1 WO133 active-writer marker was preserved; only explicit component paths were staged, with no force push or merge.

Detailed acceptance status and exact receipt paths: `tools/e2e/DESKTOP_ACCEPTANCE_2026-09-29.json`.
