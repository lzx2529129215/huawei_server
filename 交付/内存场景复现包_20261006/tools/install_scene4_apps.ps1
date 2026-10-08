param([string]$Device='3QC0124C03000514')
$ErrorActionPreference='Stop'
$packageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$hdc=Join-Path $PSScriptRoot 'hdc.exe'
$hapRoot=Join-Path $packageRoot 'bin\scene4_apps'
$manifestPath=Join-Path $hapRoot 'manifest.json'
if(-not (Test-Path -LiteralPath $hdc -PathType Leaf)){throw "包内HDC缺失：$hdc"}
if(-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)){throw "Scene4 HAP manifest缺失：$manifestPath"}
$manifest=@(Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json)
$expected=@(1..8 | ForEach-Object { "io.memscene.scene4.workload.a$_" })
if($manifest.Count -ne 8 -or @($manifest.bundle | Sort-Object -Unique).Count -ne 8){
  throw 'Scene4 manifest必须包含8个唯一bundle'
}
if(@($manifest.bundle | Where-Object { $_ -notin $expected }).Count -gt 0){
  throw 'Scene4 manifest含有非Scene4 bundle，拒绝安装'
}
$targets=& $hdc list targets
if($LASTEXITCODE -ne 0 -or @($targets | Where-Object { $_ -match [regex]::Escape($Device) }).Count -eq 0){
  throw "HDC无法连接指定设备：$Device"
}
$records=@()
foreach($row in ($manifest | Sort-Object bundle)){
  $relative=([string]$row.path).Replace('/','\')
  $hap=Join-Path $packageRoot $relative
  if(-not (Test-Path -LiteralPath $hap -PathType Leaf)){throw "HAP缺失：$hap"}
  $sha=(Get-FileHash -LiteralPath $hap -Algorithm SHA256).Hash.ToLowerInvariant()
  if($sha -ne ([string]$row.sha256).ToLowerInvariant()){throw "HAP哈希不匹配：$($row.bundle)"}
  Write-Host "Installing Scene4-owned $($row.bundle)"
  $installOutput=& $hdc -t $Device install -r $hap 2>&1
  if($LASTEXITCODE -ne 0){throw "HAP install failed: $($row.bundle): $installOutput"}
  $readbackOutput=& $hdc -t $Device shell bm dump -n $row.bundle 2>&1
  $readback=($readbackOutput -join "`n")
  if($LASTEXITCODE -ne 0 -or $readback -notmatch '"uid"\s*:\s*\d+'){
    throw "安装后bundle readback失败：$($row.bundle): $readback"
  }
  $uid=[regex]::Match($readback,'"uid"\s*:\s*(\d+)').Groups[1].Value
  $records+=@([pscustomobject]@{bundle=$row.bundle;hap=$relative;sha256=$sha;uid=$uid;installed=$true})
}
$outDir=Join-Path $packageRoot 'results\scene4_install'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$record=[pscustomobject]@{device=$Device;installed_at=(Get-Date).ToString('o');manifest=$manifestPath;bundles=$records}
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $outDir 'install_record.json') -Encoding utf8
Write-Host "Scene4 install/readback verified for $($records.Count) owned bundles."
