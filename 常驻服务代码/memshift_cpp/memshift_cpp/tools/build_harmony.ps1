param(
    [string]$Studio = 'D:\Lzx\deveco studio\dev eco\DevEco Studio',
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$nativeRoot = Join-Path $Studio 'sdk\default\openharmony\native'
$compiler = Join-Path $nativeRoot 'llvm\bin\clang++.exe'
$sysroot = Join-Path $nativeRoot 'sysroot'
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $output = Join-Path $projectRoot 'build-ohos-aarch64'
} elseif ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $output = [System.IO.Path]::GetFullPath($OutputDir)
} else {
    $output = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDir))
}

if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw "C++ compiler not found: $compiler" }
if (-not (Test-Path -LiteralPath $sysroot -PathType Container)) { throw "OpenHarmony sysroot not found: $sysroot" }
New-Item -ItemType Directory -Path $output -Force | Out-Null

$targets = @(
    @{ Sources = @('src\memshiftd.cpp', 'src\harmony_v1.cpp', 'src\device_oracle_evidence.cpp'); Output = 'memshiftd'; Thread = $true },
    @{ Sources = @('src\memshiftctl.cpp'); Output = 'memshiftctl'; Thread = $false },
    @{ Sources = @('tools\compare_runs.cpp'); Output = 'compare_runs'; Thread = $false },
    @{ Sources = @('tests\test_policy.cpp'); Output = 'test_policy'; Thread = $false },
    @{ Sources = @('tests\test_harmony_v1.cpp', 'src\harmony_v1.cpp', 'src\device_oracle_evidence.cpp'); Output = 'test_harmony_v1'; Thread = $true },
    @{ Sources = @('tests\test_daily_action_executor.cpp'); Output = 'test_daily_action_executor'; Thread = $true },
    @{ Sources = @('tests\test_daily_disk_backend.cpp', 'src\device_oracle_evidence.cpp'); Output = 'test_daily_disk_backend'; Thread = $true },
    @{ Sources = @('tools\device_daily_evidence_probe.cpp', 'src\device_oracle_evidence.cpp'); Output = 'device_daily_evidence_probe'; Thread = $true },
    @{ Sources = @('tools\harmony_v1_probe.cpp'); Output = 'harmony_v1_probe'; Thread = $true },
    @{ Sources = @('examples\harmony_v1_isolated_workload.cpp'); Output = 'harmony_v1_isolated_workload'; Thread = $false },
    @{ Sources = @('tools\harmony_v1_test_reclaim.cpp'); Output = 'harmony_v1_test_reclaim'; Thread = $false },
    @{ Sources = @('tools\oracle_small_force_all_probe.cpp'); Output = 'oracle_small_force_all_probe'; Thread = $true }
)

foreach ($target in $targets) {
    $sources = @($target.Sources | ForEach-Object { Join-Path $projectRoot $_ })
    $args = @(
        '--target=aarch64-linux-ohos',
        "--sysroot=$sysroot",
        '-D__MUSL__',
        '-std=c++17',
        '-O2',
        '-Wall',
        '-Wextra',
        '-Wpedantic',
        '-Wconversion',
        '-Wshadow'
    )
    if ($target.Thread) { $args += '-pthread' }
    $args += $sources
    $args += @('-o', (Join-Path $output $target.Output))
    & $compiler @args
    if ($LASTEXITCODE -ne 0) { throw "Build failed for $($target.Output), exit code $LASTEXITCODE" }
    Write-Output "Built $($target.Output)"
}

Write-Output "Artifacts: $output"
