# Peak-transition OS snapshot profile schedule

This experimental schedule is accepted only with `oracle_mode=true`,
`oracle_profile=peak_transition_os_snapshot`, and an explicit
`oracle_backend=proc_file` or `oracle_backend=force_shrink_all` choice. The
profile is disabled by default. Set `oracle_profile_action_interval_ms` to
control the delay between target actions (minimum 1000 ms). The legacy profile
keeps its original version-1 two-donor schedule.

The machine-readable one-donor example is
[`oracle_peak_transition_v2.schedule.example`](oracle_peak_transition_v2.schedule.example).
Its PID, start ticks, and timestamps are illustrative; a runner must replace
them with current observations. The header names eight donor slots. A row has
`schema_version=2`, then event ID, case ID, the absolute launch deadline in
`CLOCK_BOOTTIME` seconds, incremental planned bytes, donor count, and exactly
that many 21-field donor records. `donor_count` is 1–8. Each donor record
contains the exact `io.memscene.scene4.workload.a1` through `.a8` package,
configured UID, one PID and start-tick identity, `daily_ready`, current
background/window evidence, proc identity, lifecycle evidence, and
`os_snapshot` as its source. It has no cold-start-ready or heartbeat field.

Refresh AA, window, proc, and lifecycle observation timestamps as the event
runs. Each action re-reads the schedule and requires those observations to be
within 2.5 seconds. Keep the event ID, case ID, deadline, planned bytes, donor
count, package, UID, PID, start ticks, daily-ready time, and background-since
time fixed while refreshing observation fields.

`planned_needed_bytes` is incremental demand. For the original eight-app
workload it is `8 × (5184 − 2400) MiB = 22272 MiB = 23353884672` bytes. It is
not the 40.5 GiB total peak resident size. The service also checks the live
system deficit before each action.

After a `proc_file` write returns, the service samples identity and process
anon/file RSS plus system MemFree/MemAvailable immediately. The ordinary
sampling loop then tracks file-RSS recovery and process identity between
actions. A positive file-RSS decrease is file feedback only.

With `oracle_backend=proc_file`, the service writes the string `1` to each target's
`/proc/<pid>/reclaim`. It requests the process's whole file-backed reclaim
category; `request_bytes` is therefore null. The service estimates that
category with process `RssFile`, and reserves the entire estimate from the
event, batch, global, case, and per-app limits before writing. `RssFile`
reduction is file-category feedback and is never used as anonymous feedback.
It also requires `VmRSS − RssFile` to remain at least the larger of the
configured app keep value and 512 MiB.
The write does not promise that all estimated pages become `MemAvailable`.
Detailed proc-file action samples, write return length/errno, and monotonic
syscall bounds are appended to `<oracle_ledger_file>.proc_file_v1.csv`.

With `oracle_backend=force_shrink_all`, the existing anonymous resident,
swap-room, ratio, and keep-floor checks apply. Each action uses only its
selected backend; a failed or unknown write never triggers the other backend.

The separate schema-3 writeback backend is described in
[`oracle_peak_transition_v3_writeback.md`](oracle_peak_transition_v3_writeback.md).
