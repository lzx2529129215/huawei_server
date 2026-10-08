param(
    [Parameter(Mandatory = $true)][string]$Hdc,
    [Parameter(Mandatory = $true)][string]$RunId,
    [Parameter(Mandatory = $true)][string]$LocalBinary,
    [Parameter(Mandatory = $true)][string]$RemoteBinary,
    [Parameter(Mandatory = $true)][string]$RemoteOutputDirectory,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'

if ($RunId -notmatch '^[A-Za-z0-9_-]{1,32}$') { throw 'RunId must be a new unique ASCII identifier (1..32 characters).' }
if ($RemoteBinary -notmatch '^/data/local/tmp/[A-Za-z0-9_-]+$') { throw 'RemoteBinary must be a direct safe /data/local/tmp child.' }
if ($RemoteOutputDirectory -notmatch '^/data/local/tmp/[A-Za-z0-9_-]+$') { throw 'RemoteOutputDirectory must be a direct safe /data/local/tmp child.' }
if (-not (Test-Path -LiteralPath $Hdc -PathType Leaf)) { throw "HDC executable not found: $Hdc" }
if (-not (Test-Path -LiteralPath $LocalBinary -PathType Leaf)) { throw "Local probe binary not found: $LocalBinary" }

$cells = @('self_fill', 'external_fill', 'self_extra_read', 'external_extra_read')
Write-Output "Fixed matrix: $($cells -join ', ')"
Write-Output "Each invocation creates one new /dev/memcg/oracle_small_${RunId}_<cell> group, fills 16 MiB, waits 5 s, issues exactly one 20% write, samples immediate/250ms/1s/5s, then checks CRC and removes its group."
Write-Output 'Unknown/partial/error stops the remaining cells. No retry, alternate endpoint, service change, pressure workload, or app operation is performed.'
Write-Output "Remote binary: $RemoteBinary"
Write-Output "Remote output directory: $RemoteOutputDirectory"
Write-Output "Local binary: $LocalBinary"

if (-not $Execute) {
    foreach ($cell in $cells) { Write-Output "DRY-RUN: hdc shell '$RemoteBinary' '$RunId' '$cell' '$RemoteOutputDirectory'; rc=`$?; echo __ORACLE_SMALL_EXIT_${RunId}_${cell}__=`$rc; exit 0" }
    Write-Output 'No device command was run. Pass -Execute only after source review and explicit authorization.'
    exit 0
}

$uid = (& $Hdc shell 'id -u' 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $uid -ne '0') { throw "Expected the existing root shell, got '$uid'; no matrix cell was launched." }
$outputAbsentMarker = "__ORACLE_SMALL_OUTPUT_ABSENT_${RunId}__"
$checkOutput = @(& $Hdc shell "if test ! -e '$RemoteOutputDirectory'; then echo $outputAbsentMarker; else echo __ORACLE_SMALL_PATH_EXISTS__; fi" 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($checkOutput | Where-Object { $_.ToString().Trim() -eq $outputAbsentMarker })) {
    throw 'Remote output path exists or absence was not confirmed; refusing to reuse it.'
}
$binaryAbsentMarker = "__ORACLE_SMALL_BINARY_ABSENT_${RunId}__"
$checkBinary = @(& $Hdc shell "if test ! -e '$RemoteBinary'; then echo $binaryAbsentMarker; else echo __ORACLE_SMALL_BINARY_EXISTS__; fi" 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($checkBinary | Where-Object { $_.ToString().Trim() -eq $binaryAbsentMarker })) {
    throw 'Remote binary path exists or absence was not confirmed; refusing to overwrite it.'
}
& $Hdc file send $LocalBinary $RemoteBinary
if ($LASTEXITCODE -ne 0) { throw "Probe binary transfer failed ($LASTEXITCODE)." }
$localHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $LocalBinary).Hash.ToLowerInvariant()
$hashMarker = "__ORACLE_SMALL_HASH_${RunId}__"
$hashReply = @(& $Hdc shell "sha256sum '$RemoteBinary'; echo $hashMarker" 2>&1)
$hashLine = $hashReply | Where-Object { $_.ToString() -match '^[0-9a-fA-F]{64}\s+' } | Select-Object -First 1
$remoteHash = if ($null -eq $hashLine) { '' } else { ($hashLine.ToString().Trim() -split '\s+')[0].ToLowerInvariant() }
if ($LASTEXITCODE -ne 0 -or $remoteHash -ne $localHash -or -not ($hashReply | Where-Object { $_.ToString().Trim() -eq $hashMarker })) {
    throw 'Remote binary hash does not match the built local artifact; no matrix cell was launched.'
}
Write-Output "Probe binary SHA256 verified: $localHash"
$setupMarker = "__ORACLE_SMALL_SETUP_${RunId}__="
$setup = @(& $Hdc shell "chmod 755 '$RemoteBinary' && mkdir '$RemoteOutputDirectory' && test -x '$RemoteBinary'; rc=`$?; echo ${setupMarker}`$rc; exit 0" 2>&1)
if (-not ($setup | Where-Object { $_.ToString().Trim() -eq ($setupMarker + '0') })) {
    throw 'Remote binary/output setup lacks a successful marker; no matrix cell was launched.'
}

foreach ($cell in $cells) {
    $exitMarker = "__ORACLE_SMALL_EXIT_${RunId}_${cell}__="
    $completeMarker = "__ORACLE_SMALL_COMPLETE_${RunId}_${cell}__=1"
    $resultFile = "$RemoteOutputDirectory/${RunId}_${cell}.jsonl"
    $remote = "'$RemoteBinary' '$RunId' '$cell' '$RemoteOutputDirectory'; rc=`$?; if test -f '$resultFile'; then tail -n 1 '$resultFile'; fi; echo ${exitMarker}`$rc; exit 0"
    Write-Output "Starting cell $cell"
    $reply = @(& $Hdc shell $remote 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "HDC transport failed in cell $cell ($LASTEXITCODE); remaining cells stopped." }
    $reply | ForEach-Object { Write-Output $_ }
    $exitLine = $reply | Where-Object { $_.ToString().Contains($exitMarker) } | Select-Object -Last 1
    $exitText = if ($null -eq $exitLine) { '' } else { ($exitLine.ToString().Split('=') | Select-Object -Last 1).Trim() }
    $completeRecord = $reply | Where-Object {
        $s = $_.ToString()
        $s.Contains('"kind":"complete"') -and $s.Contains('"run_id":"' + $RunId + '"') -and
            $s.Contains('"cell":"' + $cell + '"') -and $s.Contains('"action_count":1') -and
            $s.Contains('"crc_verified":true') -and $s.Contains('"group_removed":true')
    }
    if ($exitText -ne '0' -or -not ($reply | Where-Object { $_.ToString().Contains($completeMarker) }) -or -not $completeRecord) {
        throw "Cell $cell lacks a successful remote exit marker or durable complete record; remaining cells stopped."
    }
}

Write-Output 'All four cells returned success. Copy the run-specific JSONL and action ledgers off-device before any cleanup of the output directory.'
