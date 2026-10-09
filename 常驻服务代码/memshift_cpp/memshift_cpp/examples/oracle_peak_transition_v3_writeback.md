# Peak-transition schema 3: registered-file writeback then reclaim

This experiment is default-off. Select it explicitly with
`oracle_mode=true`, `oracle_profile=peak_transition_os_snapshot`, and
`oracle_backend=proc_file_writeback`. It accepts schema version 3 only; the
existing version-1 legacy schedule and version-2 profile schedules retain
their existing backend behavior.

For active peak-transition events, `oracle_profile_poll_interval_ms` sets the
lightweight scheduler poll interval (default 200 ms, accepted range 100–1000
ms). The independent action-start interval remains at least 1000 ms. Fast
polls re-read the current system sample, control, schedule, target identity,
RSS, and budget; they do not repeat the all-app `smaps_rollup` observation,
which remains on the configured `interval_ms` cadence. Outside an active
peak-transition event, the existing main-loop cadence is unchanged.

The header has the schema-2 event fields and eight fixed donor slots. Each
registered donor keeps the same 21 OS-snapshot fields, followed by
`writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size`.
Rows contain exactly `donor_count` records, from one through eight. Each target
registers one file named exactly `memscene_<case_id>.pages`; device, inode, and
size are decimal values. The file UID must match that donor's registered UID.
The sample schedule is
[`oracle_peak_transition_v3_writeback.schedule.example`](oracle_peak_transition_v3_writeback.schedule.example).

The service resolves that basename only beneath the selected PID's
`/proc/<pid>/root/data/storage/el2/base/haps/entry/files` directory. It follows
the `/proc/<pid>/root` namespace entry intentionally, then opens every directory
and the final regular file without following links. Before and after writeback,
the process PID, start ticks, UID, cgroup membership, namespace root identity,
and registered file device, inode, size, owner, and single-link status must
match. The file is opened read/write so `fdatasync` is supported; the service
does not alter its contents.

Each target uses one action ID and one shared reservation. Before `fdatasync`,
the service durably records the prepared stage, then refreshes the deadline,
control, schedule, identity, and budget gates. The conservative work estimate
is `max(process RssFile, registered file size)`. The service reserves and
charges that estimate plus the 64 MiB safety margin, and the full reservation
must fit the system deficit, batch, global, case, and app room. `nominal_bytes`
therefore records the charged reservation, not predicted or observed reclaimed
bytes. The observed RSS delta remains a separate measurement. Before both
`fdatasync` and reclaim, the live process `RssFile` and registered file size
must each fit within the reservation, while the process retains the configured
minimum resident reserve. After a successful `fdatasync`, the service repeats
those gates and proceeds to a single `/proc/<pid>/reclaim` write of `1` only
while the original event remains before its deadline. The second stage never
extends the deadline.

A failed or late `fdatasync`, stale or changed identity, insufficient current
budget, partial/failed reclaim write, or unknown result stops the event without
reclaim, retry, quota refund, or backend fallback. Once the `fdatasync` syscall
starts, that target is consumed for the event. The service keeps one worker.

The sidecar `<oracle_ledger_file>.proc_file_writeback_v1.csv` records the
writeback preflight and return, the reclaim write return, and immediate process
and system samples. It includes syscall return values and errno, monotonic
start/return times, RssAnon/RssFile, Dirty/Cached, MemFree/MemAvailable, and the
registered file identity. RssFile change is process file-category evidence;
MemFree and MemAvailable are separate measured system outcomes. Neither is a
promise that every registered file page will be reclaimed.
