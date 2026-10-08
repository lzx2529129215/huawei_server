$ErrorActionPreference = 'Stop'
$scenarioRoot = $PSScriptRoot
$pythonExe = (Get-Command python -ErrorAction Stop).Source
& $pythonExe (Join-Path $scenarioRoot 'scripts/collect_harmony_short_kpis.py') `
    --replay-root $scenarioRoot `
    --output-root (Join-Path $scenarioRoot 'results') `
    --pressure-helper (Join-Path $scenarioRoot 'bin/harmony_mem_pressure') `
    --page-helper (Join-Path $scenarioRoot 'bin/harmony_workload_pages') `
    --pressure-mib 16384 `
    --pressure-wait-seconds 10
exit $LASTEXITCODE
