param(
    [Parameter(Mandatory = $true)][string]$Hdc,
    [Parameter(Mandatory = $true)][string]$RunId,
    [Parameter(Mandatory = $true)][string]$LocalBinary,
    [Parameter(Mandatory = $true)][string]$RemoteBinary,
    [Parameter(Mandatory = $true)][string]$RemoteOutputDirectory,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
if ($RunId -notmatch '^[A-Za-z0-9_-]{1,32}$') { throw 'RunId must be a unique ASCII identifier.' }
if ($RemoteBinary -notmatch '^/data/local/tmp/[A-Za-z0-9_-]+$') { throw 'RemoteBinary must be a direct safe /data/local/tmp child.' }
if ($RemoteOutputDirectory -notmatch '^/data/local/tmp/[A-Za-z0-9_-]+$') { throw 'RemoteOutputDirectory must be a direct safe /data/local/tmp child.' }
if (-not (Test-Path -LiteralPath $Hdc -PathType Leaf)) { throw "HDC executable not found: $Hdc" }
if (-not (Test-Path -LiteralPath $LocalBinary -PathType Leaf)) { throw "Probe binary not found: $LocalBinary" }

$cell = 'external_two_step'
$resultFile = "$RemoteOutputDirectory/${RunId}_${cell}.jsonl"
$ledgerFile = "$RemoteOutputDirectory/${RunId}_${cell}_actions.jsonl"
Write-Output "Single fresh 16 MiB external-writer object; at most two one-shot 20% writes."
Write-Output 'Action 2 is allowed only after complete same-identity 5-second RSSAnon and mapping-residency drops following action 1.'
Write-Output 'No third action, retry, alternate node, service change, pressure workload, application operation, score change, or swap change.'
Write-Output "Remote binary: $RemoteBinary"
Write-Output "Remote output: $RemoteOutputDirectory"

if (-not $Execute) {
    Write-Output "DRY-RUN: '$RemoteBinary' --two-step-external '$RunId' '$RemoteOutputDirectory'"
    Write-Output 'No device command was run. Pass -Execute only after source review and authorization.'
    exit 0
}

$uidMarker = "__ORACLE_SMALL_UID_${RunId}__"
$uidReply = @(& $Hdc shell "id -u; echo $uidMarker" 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($uidReply | Where-Object { $_.ToString().Trim() -eq $uidMarker }) -or
    -not ($uidReply | Where-Object { $_.ToString().Trim() -eq '0' })) {
    throw 'Root shell uid read lacks the exact nonce marker or uid=0; no action launched.'
}
$outputAbsentMarker = "__ORACLE_SMALL_OUTPUT_ABSENT_${RunId}__"
$outputReply = @(& $Hdc shell "if test ! -e '$RemoteOutputDirectory'; then echo $outputAbsentMarker; else echo __ORACLE_SMALL_OUTPUT_EXISTS__; fi" 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($outputReply | Where-Object { $_.ToString().Trim() -eq $outputAbsentMarker })) {
    throw 'Remote output directory absence was not confirmed; refusing to reuse it.'
}
$binaryAbsentMarker = "__ORACLE_SMALL_BINARY_ABSENT_${RunId}__"
$binaryReply = @(& $Hdc shell "if test ! -e '$RemoteBinary'; then echo $binaryAbsentMarker; else echo __ORACLE_SMALL_BINARY_EXISTS__; fi" 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($binaryReply | Where-Object { $_.ToString().Trim() -eq $binaryAbsentMarker })) {
    throw 'Remote binary absence was not confirmed; refusing to overwrite it.'
}
& $Hdc file send $LocalBinary $RemoteBinary
if ($LASTEXITCODE -ne 0) { throw 'Probe binary transfer failed; no action launched.' }
$localHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $LocalBinary).Hash.ToLowerInvariant()
$hashMarker = "__ORACLE_SMALL_HASH_${RunId}__"
$hashReply = @(& $Hdc shell "sha256sum '$RemoteBinary'; echo $hashMarker" 2>&1)
$hashLine = $hashReply | Where-Object { $_.ToString() -match '^[0-9a-fA-F]{64}\s+' } | Select-Object -First 1
$remoteHash = if ($null -eq $hashLine) { '' } else { ($hashLine.ToString().Trim() -split '\s+')[0].ToLowerInvariant() }
if ($LASTEXITCODE -ne 0 -or $remoteHash -ne $localHash -or
    -not ($hashReply | Where-Object { $_.ToString().Trim() -eq $hashMarker })) {
    throw 'Remote probe hash mismatch or missing nonce; no action launched.'
}
Write-Output "Probe binary SHA256 verified: $localHash"
$setupMarker = "__ORACLE_SMALL_SETUP_${RunId}__="
$setup = @(& $Hdc shell "chmod 755 '$RemoteBinary' && mkdir '$RemoteOutputDirectory' && test -x '$RemoteBinary'; rc=`$?; echo ${setupMarker}`$rc; exit 0" 2>&1)
if (-not ($setup | Where-Object { $_.ToString().Trim() -eq ($setupMarker + '0') })) {
    throw 'Remote setup success marker missing; no action launched.'
}

$exitMarker = "__ORACLE_SMALL_EXIT_${RunId}_${cell}__="
$completeMarker = "__ORACLE_SMALL_COMPLETE_${RunId}_${cell}__=1"
$remote = "'$RemoteBinary' --two-step-external '$RunId' '$RemoteOutputDirectory'; rc=`$?; " +
    "if test -f '$resultFile'; then tail -n 1 '$resultFile'; fi; " +
    "if test -f '$ledgerFile'; then cat '$ledgerFile'; fi; " +
    "echo __ORACLE_SMALL_CELL_DONE_${RunId}__; echo ${exitMarker}`$rc; exit 0"
$reply = @(& $Hdc shell $remote 2>&1)
if ($LASTEXITCODE -ne 0) { throw 'HDC transport failed; preserve remote partial output and do not retry.' }
$reply | ForEach-Object { Write-Output $_ }
$exitLine = $reply | Where-Object { $_.ToString().Contains($exitMarker) } | Select-Object -Last 1
$exitText = if ($null -eq $exitLine) { '' } else { ($exitLine.ToString().Split('=') | Select-Object -Last 1).Trim() }
$completeLine = $reply | Where-Object { $_.ToString().Contains('"kind":"complete"') -and
    $_.ToString().Contains('"run_id":"' + $RunId + '"') -and $_.ToString().Contains('"cell":"' + $cell + '"') } | Select-Object -Last 1
if (-not $completeLine) { throw 'Remote invocation lacks its durable complete row; preserve partial output, do not retry.' }
try { $complete = $completeLine.ToString().Trim() | ConvertFrom-Json -ErrorAction Stop }
catch { throw 'Remote complete row is not valid JSON; preserve partial output, do not retry.' }
$doneMarker = "__ORACLE_SMALL_CELL_DONE_${RunId}__"
if ($exitText -ne '0' -or -not ($reply | Where-Object { $_.ToString().Trim() -eq $completeMarker }) -or
    -not ($reply | Where-Object { $_.ToString().Trim() -eq $doneMarker }) -or
    $complete.kind -ne 'complete' -or $complete.crc_verified -ne $true -or $complete.group_removed -ne $true -or
    $complete.two_step -ne $true -or $complete.action_count -notin @(1,2) -or
    $complete.sample_count -ne (1 + 4 * $complete.action_count)) {
    throw 'Remote cell failed exit/completion/CRC/group/action/sample validation; do not retry.'
}
if ($complete.action_count -eq 2 -and $complete.first_feedback_positive -ne $true) {
    throw 'Second action exists without positive first feedback; preserve data and stop.'
}
$ledgerLines = $reply | Where-Object { $_.ToString().Trim().StartsWith('{"action_id":') }
$ledger = @()
try { $ledger = @($ledgerLines | ForEach-Object { $_.ToString().Trim() | ConvertFrom-Json -ErrorAction Stop }) }
catch { throw 'Action ledger rows are not valid JSON; preserve raw output and stop.' }
if ($ledger.Count -ne (2 * $complete.action_count)) {
    throw "Action ledger row count $($ledger.Count) does not match $($complete.action_count) action IDs."
}
$ledgerActionIds = @($ledger | Select-Object -ExpandProperty action_id -Unique)
$completeActionIds = @($complete.action_ids)
if ($ledgerActionIds.Count -ne $complete.action_count -or
    $completeActionIds.Count -ne $complete.action_count -or
    @($ledger | Where-Object { $_.row -eq 'reserved' }).Count -ne $complete.action_count -or
    @($ledger | Where-Object { $_.row -eq 'result' }).Count -ne $complete.action_count) {
    throw 'Action ledger has duplicate/missing action IDs or unmatched reserve/result rows.'
}
for ($i = 0; $i -lt $completeActionIds.Count; $i++) {
    $id = [string]$completeActionIds[$i]
    if ($ledgerActionIds -notcontains $id -or $id -ne ($RunId + '_' + $cell + '_a' + ($i + 1))) {
        throw 'Complete row action IDs do not match the exact sequential action ledger IDs.'
    }
    $rows = @($ledger | Where-Object { $_.action_id -eq $id })
    $reservation = $rows | Where-Object { $_.row -eq 'reserved' } | Select-Object -First 1
    $result = $rows | Where-Object { $_.row -eq 'result' } | Select-Object -First 1
    if ($rows.Count -ne 2 -or @($rows | Where-Object { $_.row -eq 'reserved' }).Count -ne 1 -or
        $null -eq $result -or $result.write_length -ne 2 -or $result.errno -ne 0 -or
        $result.post_identity_verified -ne $true) {
        throw "Action $id does not have one accepted, same-identity two-byte result."
    }
    if ($reservation.writer -ne 'external_controller' -or $reservation.payload -ne '20' -or
        $reservation.write_calls -ne 1 -or $reservation.pid -le 0 -or
        $reservation.start_ticks -le 0 -or $reservation.writer_pid -le 0 -or
        $reservation.writer_start_ticks -le 0) {
        throw "Action $id reservation lacks the expected writer/target identity or single literal payload."
    }
    if ($i -gt 0) {
        $previousId = [string]$completeActionIds[$i - 1]
        $previousResult = $ledger | Where-Object { $_.action_id -eq $previousId -and $_.row -eq 'result' } | Select-Object -First 1
        if ($reservation.trigger_boottime_s -lt ($previousResult.syscall_return_boottime_s + 5.0)) {
            throw 'Second action trigger preceded the complete first 5-second observation window.'
        }
    }
}
Write-Output "Two-step cell complete: action_count=$($complete.action_count), samples=$($complete.sample_count), second_decision=$($complete.second_action_decision)"
