# VT maintenance execution guard

Version 1.0.0. This guard addresses the September 29 stalled maintenance attempt.
It does not establish web or desktop application acceptance.

The existing PC1 `bin/run_logged.ps1` uses this guard. Every command first checks
canonical Drive reads with a ten-second child deadline, then checks Windows commit
headroom. Work is refused at 90% commitment or below 4 GiB headroom. Children inherit
two-thread BLAS/OMP/MKL/NumExpr limits and E: temporary paths.

The runner prints progress and atomically checkpoints state every ten seconds. Its
heartbeat also rechecks commit headroom; new memory pressure stops only the owned
command tree, records `RESOURCE_LIMIT`, and exits 75.
Its
default command deadline is 900 seconds; select a measured deadline for each build,
test or read. A timeout kills only that command's process tree, records `TIMEOUT`
and exits 124. Interrupted, failed, unlaunchable and preflight-failed commands retain
distinct states. Repeated steps retain separate logs rather than overwrite receipts.
All mutable logs and state live under the E: runtime.

Run preflight before resuming the implementation plan:

```powershell
& 'E:\codex-runtime\investment-ai\vibe-trading\venv\Scripts\python.exe' -B `
  'E:\codex-runtime\investment-ai\vibe-trading\tools\execution_guard.py' preflight
```

Then use the existing runner, setting `-TimeoutSeconds` for that step. Successful
commands are not UI acceptance. Honor the project writer's ownership; preserve
passed steps and repeat only failed or stale work after resolving its recorded cause.
Never repeat an unresolved request indefinitely or manufacture a completion receipt.

The guard bounds its own child commands. It cannot interrupt a Codex host/tool
request that hangs before this runner starts. Therefore the live session's permission
mode and prompt-free ordinary-command behavior must also be verified. Persistent
configuration alone does not repair a session that retained its old policy.

Regression coverage includes actual hangs and descendant cleanup, interruption,
memory refusal, missing Drive, nonzero exits, launch failure, repeated checkpoints,
finite deadlines, E: storage and inherited thread limits.
