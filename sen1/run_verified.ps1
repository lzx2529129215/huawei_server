param([ValidateSet('smoke','development','acceptance')][string]$Profile='development', [ValidateSet('baseline','optimized')][string]$Phase='baseline', [int]$Rounds=1)
$taskRoot = Split-Path $PSScriptRoot -Parent
& python -X utf8 (Join-Path $taskRoot 'run.py') run --scene 1 --profile $Profile --phase $Phase --rounds $Rounds
exit $LASTEXITCODE
