param(
    [Parameter(Mandatory=$true)][string]$Config,
    [ValidateSet('baseline','optimized')][string]$Phase='baseline',
    [ValidateRange(1,100)][int]$Rounds=1,
    [string]$Device,
    [string]$Hdc
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) { $pythonExe = $python.Source; $prefix = @('-3') }
else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw '未找到Python 3；请安装Python 3并加入PATH，或安装py启动器。' }
    $pythonExe = $python.Source; $prefix = @()
}
$args = @($prefix + @('-X','utf8',(Join-Path $root 'run.py')))
if ($Device) { $args += @('--device',$Device) }
if ($Hdc) { $args += @('--hdc',$Hdc) }
$args += @('--config',(Resolve-Path $Config).Path,'run','--phase',$Phase,'--rounds',"$Rounds")
& $pythonExe @args
exit $LASTEXITCODE
