param(
    [string]$Device,
    [string]$Hdc = (Join-Path (Split-Path $PSScriptRoot -Parent) 'tools\hdc.exe')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path -LiteralPath $Hdc -PathType Leaf)) { throw "HDC不存在: $Hdc" }
$targetsText = & $Hdc list targets 2>&1
if ($LASTEXITCODE -ne 0) { throw "HDC无法列举设备: $targetsText" }
$targets = @($targetsText | ForEach-Object { "$($_)".Trim() } | Where-Object { $_ -and $_ -notmatch 'Empty|\[' } | ForEach-Object { ($_ -split '\s+')[0] })
if (-not $Device) {
    if ($targets.Count -ne 1) { throw "检测到$($targets.Count)台在线设备；请用 -Device 指定唯一serial。在线: $($targets -join ', ')" }
    $Device = $targets[0]
}
if ($targets -notcontains $Device) { throw "指定设备不在线: $Device；在线: $($targets -join ', ')" }
$haps = @(Get-ChildItem -LiteralPath (Join-Path $root 'bin\apps') -Filter 'a*.hap' -File | Sort-Object Name)
if ($haps.Count -ne 8) { throw "期望8个Scene 1 HAP，实际$($haps.Count)个" }
foreach ($hap in $haps) {
    $output = & $Hdc -t $Device install $hap.FullName 2>&1
    if ($LASTEXITCODE -ne 0 -or (($output -join "`n") -notmatch '(?i)success')) {
        throw "安装失败 $($hap.Name)（可能签名profile不匹配此设备）: $($output -join ' ')"
    }
    Write-Host "已安装 $($hap.Name) 到 $Device"
}
