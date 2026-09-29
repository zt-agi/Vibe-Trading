# VT PC1 readiness ledger
Spec: ../../../../../../00_协作/02_工单_Workorders/WORKORDER_WO-20260929-0133-finish-the-wp2-vt-add-ons-ai-hedge-fund-additions-and-qa.md
Controller: Codex PC1; resumed 2026-09-29T15:15:42Z.
Baseline: feature/pit-actor-sim, 3e6306a99874d614a896a5999d606349179bed70.
Remote recovery baseline verified: b0e4cb4bd6518b1d5b2aecbed93cd067f52f34ce.
Source: canonical G: project; installed runtime and disposable QA state: E:.

## Scope and recovery map
All six WP2 packages are implemented at the baseline. Resume acceptance and fix confirmed defects rather than reconstructing completed packages. No real orders, mandate, scheduler enablement or pay-as-you-go models.

- Task 1: in progress - real Electron acceptance harness and isolated local-model prompt.
- Task 2: pending - narrow Options/Runtime layout; early-close timing; PEAD event/SUE PIT eligibility.
- Task 3: pending - deploy coherent fixes and repeat affected unit/UI acceptance.
- Task 4: pending - final independent review, durable receipts and GitHub checkpoint.

## Observed baseline checks
- Guard preflight: PASS at 15:26:45Z; Drive read 0.074 s, Windows commit 49.24%.
- Web preflight warm: 0.237 s; 13 OK, 2 WARN, 0 FAIL. Warnings: expired PIT audit and deliberate live HALT.
- Web strict gate: 43/45 PASS. Blockers: Options and Runtime horizontal overflow at 390 px.
- Isolated regression suites: 13/13 PASS; receipts in E runtime logs/wp2_suite_20260929_112214.
- Combined pytest collection does not support same-name extension tests; importlib mode also breaks their sibling imports. Use isolated suites.
- PIT audit refresh: timeout after 300 s. Prior receipt remains expired; no simulation/backtest acceptance inferred.
- Independent review: confirmed early-close bar rejection and tradeable PEAD admission of disallowed event/SUE PIT classes.

## Decisions
Ruling: reuse the existing canonical component branch with disjoint task ownership, rather than create another content worktree - the mandatory canonical-root rule controls - incorrect ownership would require rework.
Ruling: retain E-only disposable workflow state and this durable G ledger, rather than the skill's default G scratch workspace - project runtime storage rules control - recovery depends on this ledger and Git checkpoints.
Ruling: treat known upstream overflow as a failing acceptance row using Strict mode - the user requested both apps through our QA - remaining layout defects block handoff.

## Open boundaries
The source writer marker belongs to claude-cowork PC1 WO133 and is preserved. This controller does not blanket-stage or reset that worker's files. Remote writes use the verified component wrapper; no force push or merge.
