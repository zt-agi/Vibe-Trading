# Canonical source for PC1 bin/run_logged.ps1. Guard version 1.0.0.
param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$CommandLine,
    [double]$TimeoutSeconds = 900,
    [double]$HeartbeatSeconds = 10
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$' -or $Name.Contains('..')) {
    throw 'Unsafe log name'
}
. "$PSScriptRoot\vt_env.ps1"
$guardPath = "$VT_ROOT\tools\execution_guard.py"
if (-not (Test-Path -LiteralPath $guardPath)) { throw 'Execution guard is not installed' }
$legacyOut = "$VT_ROOT\logs\$Name.out.log"
$legacyErr = "$VT_ROOT\logs\$Name.err.log"
$legacyExit = "$VT_ROOT\logs\$Name.exit"
"started=$((Get-Date).ToString('o')) status=STARTING" | Set-Content -Encoding UTF8 $legacyExit
$guardRecords = [System.Collections.Generic.List[string]]::new()
$guardLog = "$VT_ROOT\logs\$Name.guard.log"
'' | Set-Content -Encoding UTF8 $guardLog
& $VT_PY -B $guardPath run --name $Name --cwd $WorkDir --timeout $TimeoutSeconds `
    --heartbeat $HeartbeatSeconds -- cmd.exe /d /c $CommandLine |
    ForEach-Object {
        $guardRecords.Add([string]$_)
        $_ | Add-Content -Encoding UTF8 $guardLog
        Write-Output $_
    }
$code = $LASTEXITCODE
$receipt = ($guardRecords[-1] | ConvertFrom-Json).receipt
$state = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($pair in @(@($state.stdout, $legacyOut), @($state.stderr, $legacyErr))) {
    if (Test-Path -LiteralPath $pair[0]) { Copy-Item -LiteralPath $pair[0] -Destination $pair[1] }
    else { '' | Set-Content -Encoding UTF8 $pair[1] }
}
"exit=$code finished=$((Get-Date).ToString('o')) status=$($state.status) receipt=$receipt" |
    Set-Content -Encoding UTF8 $legacyExit
exit $code
