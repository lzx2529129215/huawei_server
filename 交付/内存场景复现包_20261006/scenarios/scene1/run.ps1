param(
  [ValidateSet('smoke','development','acceptance')][string]$Profile='development',
  [ValidateSet('baseline','optimized')][string]$Phase='baseline',
  [int]$Rounds=1,
  [string]$Config=''
)
$ErrorActionPreference='Stop'
$packageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if(-not $Config){$Config=Join-Path $packageRoot 'config\scenarios_scene1_full48_pressure12.json'}
$python=Get-Command python -ErrorAction SilentlyContinue
if($python){& $python.Source -X utf8 (Join-Path $packageRoot 'run.py') --config $Config run --scene 1 --profile $Profile --phase $Phase --rounds $Rounds}
else{$py=Get-Command py -ErrorAction SilentlyContinue;if(-not $py){throw '找不到Python 3；请安装Python 3并加入PATH，或安装py启动器'};& $py.Source -3 -X utf8 (Join-Path $packageRoot 'run.py') --config $Config run --scene 1 --profile $Profile --phase $Phase --rounds $Rounds}
exit $LASTEXITCODE
