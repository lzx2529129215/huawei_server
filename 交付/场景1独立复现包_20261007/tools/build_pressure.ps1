param([string]$Studio = $env:DEVECO_STUDIO_HOME)
$ErrorActionPreference = 'Stop'
if (-not $Studio) { throw '请通过 -Studio 或 DEVECO_STUDIO_HOME 指定本机 DevEco Studio 路径。' }
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskNative = Join-Path $Studio 'sdk\default\openharmony\native'
$taskBin = Join-Path $taskRoot 'bin'
New-Item -ItemType Directory -Path $taskBin -Force | Out-Null
$taskArgs = @('--target=aarch64-linux-ohos', "--sysroot=$(Join-Path $taskNative 'sysroot')", '-O2', '-Wall', '-Wextra', (Join-Path $taskRoot 'native\pressure.c'), '-o', (Join-Path $taskBin 'pressure'))
& (Join-Path $taskNative 'llvm\bin\clang.exe') @taskArgs
if ($LASTEXITCODE -ne 0) { throw '内存加压程序编译失败' }
