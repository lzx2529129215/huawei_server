param([string]$Device='3QC0124C03000514')
$ErrorActionPreference='Stop'
$packageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$hdc=Join-Path $PSScriptRoot 'hdc.exe'
if(-not (Test-Path -LiteralPath $hdc -PathType Leaf)){throw "包内HDC缺失：$hdc"}
& $hdc -t $Device list targets | Out-Null
if($LASTEXITCODE -ne 0){throw "HDC无法连接设备：$Device"}
foreach($hap in Get-ChildItem -LiteralPath (Join-Path $packageRoot 'bin\apps') -Filter 'a*.hap' -File | Sort-Object Name){
  Write-Host "Installing $($hap.Name)"
  & $hdc -t $Device install -r $hap.FullName
  if($LASTEXITCODE -ne 0){throw "HAP install failed: $($hap.Name)"}
}
