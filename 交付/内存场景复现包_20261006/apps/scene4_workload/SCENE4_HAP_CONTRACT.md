# Scene 4 HAP and runner contract

## Cold start

The Ability starts the native workload only after `loadContent('pages/Index')`
returns successfully. Native records `window_content_loaded` and the Ability
acknowledges the loaded window. Native touches the full configured peak first and enters phase
`peak_ready`. It waits for the window acknowledgement, emits
`cold_start_ready` with `memory_ready=true` and `window_ready=true`, then emits
`cold_start_hold_begin` and starts the configured hold. A missing
acknowledgement emits `cold_start_ack_timeout` and skips the hold.
`cold_start_hold_end` marks the end of that interval. The later
`startup_ready` event preserves the old post-hold meaning and must not be used
as the Scene 4 cold-start terminal.

Pass `cold_start_ready=1` on each Scene 4 target start. The `case_id` identifies
the peak/terminal journal and `daily_case_id` identifies the initial daily
allocation journal; keep both unique for each fresh target attempt. Start with
the same six-target launch barrier and parameters in A and B.

The runner should use `cold_start_ready` as the cold-start terminal and verify
both boolean fields, `peak_bytes == touched_bytes` (accounting for the requested
anonymous/file mix), phase `cold_start_ready`, and identity continuity. It
should not use `startup_ready` as evidence that the window was ready before the
hold. All event rows include `pid`, `uid`, `/proc/self/stat` `start_ticks`,
`boottime_ns`, cold/donor mode flags, phase, byte counters, and fault counters. `monotonic_ns` is kept
as a compatibility alias for the same `CLOCK_BOOTTIME` timestamp.

## Donor process and validation

Set `donor_mode=1` only for a1/a2, with `hold_seconds=0`. Native validates this
mode's workload as 3072 MiB daily and peak, with zero file-backed bytes, so all measured donor
pages are private anonymous pages. After first-touch allocation completes, it
does one sequential read/hash pass, records `donor_post_alloc_read_begin/end`
and pre/post minor/major fault counts, then emits
`donor_first_touch_complete`. It retains the region idle after that pass and
ignores the legacy retouch request. Background liveness events do not read the
donor region. With zero hold, native immediately enters the idle loop after
cold readiness while retaining all 3072 MiB. The runner must leave the donor untouched during the main
counter window, then send an `op=donor_validate` Want after that window closes.

The runner should stop and recreate a1/a2 independently before every case in A
and B, verify their prior process/cgroup absence, then launch them as new
processes. Each fresh `startup_begin` / `donor_first_touch_complete` journal row
provides the new PID and `start_ticks`; pair these with the event ID and device
process snapshot. `op=stop` joins and stops an active native session, while
`onDestroy` records its delivered lifecycle event. The runner must still
force-stop and verify process absence between cases to guarantee a fresh
process identity rather than reusing the same Ability process.

The worker validates every 64-bit word against a byte-wise FNV-1a checksum
recorded during first-touch. `donor_validation_begin` and
`donor_validation_end` include expected/observed checksums, completion status,
and minor/major fault counts before and after the post-window scan. The runner
should require the first-read checksum and final checksum to match the expected
checksum, `donor_initial_read_complete=true`, `donor_validation_complete=true`,
and the expected
3,221,225,472 touched bytes; keep validation faults outside the main counter
window.

The Ability sends foreground/background transitions to native. Native emits
`donor_background_enter` and `donor_foreground_resume`; while the background
flag is set, the native worker emits a one-second `donor_background_heartbeat`
with a strictly increasing `donor_heartbeat_sequence`, sampled against
`CLOCK_BOOTTIME` with up to 100 ms worker polling jitter. The first heartbeat
can appear about one second after cold-ready/background, and the runner should
wait for one before considering the donor ready. Each heartbeat is
evidence that the workload thread ran in the background at that time. A gap
means the HAP did not provide continuous heartbeat evidence; it does not alone
identify why the process stopped. `donor_ability_destroy` is emitted from the
Ability destroy callback when delivered. A kill can leave no destroy event, so
the runner must pair the journal with independent device lifecycle/log evidence
and validate every row against PID/UID/start_ticks.

## Rebuild

From the package root, rebuild and sign the eight Scene 4 HAPs with:

```powershell
py -3 .\tools\build_apps.py --studio 'D:\Lzx\deveco studio\dev eco\DevEco Studio' --target 3QC0124C03000514 --count 8 --source .\apps\scene4_workload --bundle-prefix io.memscene.scene4.workload.a --output-dir .\bin\scene4_apps --signing-dir .\build\signing_scene4 --device-udid F05AA8DB46A300AB9E8C95216100DC80BCE0BB41B5D2165B6EF78D276D9A28A2
```

The helper writes HAPs and a manifest in `bin\scene4_apps`; installation is a
separate step through `tools\install_scene4_apps.ps1`. Do not rebuild the main
`apps/workload` app for this change.
