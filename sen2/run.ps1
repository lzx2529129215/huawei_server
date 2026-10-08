param([ValidateSet('smoke','development','acceptance')][string]$Profile='development', [ValidateSet('baseline','optimized')][string]$Phase='baseline', [ValidateSet('combined','startup','oom','popup')][string]$Subcase='combined', [int]$Rounds=1, [string]$Frozen='')
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskArgs = @('-X','utf8',(Join-Path $taskRoot 'run.py'),'run','--scene','2','--profile',$Profile,'--phase',$Phase,'--subcase',$Subcase,'--rounds',$Rounds)
if($Frozen) { $taskArgs += @('--frozen',$Frozen) }
& python @taskArgs
exit $LASTEXITCODE
