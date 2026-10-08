param(
  [ValidateSet('smoke','development','qualification','acceptance')][string]$Profile='development',
  [ValidateSet('baseline','optimized')][string]$Phase='baseline',
  [ValidateSet('combined','startup','oom','popup')][string]$Subcase='combined',
  [int]$Rounds=1,
  [string]$Frozen='',
  [string]$Config=''
)
$ErrorActionPreference='Stop'
$packageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if(-not $Config){$Config=Join-Path $packageRoot 'config\scenarios_qualification32.json'}
$taskArgs=@('-X','utf8',(Join-Path $packageRoot 'run.py'),'--config',$Config,'run','--scene','2','--profile',$Profile,'--phase',$Phase,'--subcase',$Subcase,'--rounds',$Rounds)
if($Frozen){$taskArgs+=@('--frozen',$Frozen)}
$python=Get-Command python -ErrorAction SilentlyContinue
if($python){& $python.Source @taskArgs}
else{$py=Get-Command py -ErrorAction SilentlyContinue;if(-not $py){throw '找不到Python 3；请安装Python 3并加入PATH，或安装py启动器'};& $py.Source -3 @taskArgs}
exit $LASTEXITCODE
