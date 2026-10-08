# HarmonyOS memory backend

The default `linux-v2` backend is unchanged. Harmony support is explicit and device-qualified; the observer and ratio writer use different backend names and log directories. The original v2 binary/source snapshot is retained separately as `memshiftd_v01_original` in the test results.

## What the device reports

The tested device is HarmonyOS / HongMeng Kernel 1.13.0, OpenHarmony 7.0.0.112, AArch64. Its `/proc/self/cgroup` and `/proc/self/mounts` expose a memory-controller cgroup-v1-style hierarchy; statfs reports raw magic `0x63677270`, so the backend validates proc topology and mount/device identity instead of assuming Linux magic values. `/dev/cgroup/memory/default` is accepted only when its device/inode matches the `/dev/memcg` mount.

The vendor `memory.stat` lines `Anon:`, `Zram:`, and `Eswap:` use kB as confirmed by the device's MemoryManagerService parser. `memory.current`, vendor `inactive_*`, pressure-stall values, and the app-group file counters are not used as reclaimable-byte measurements. Process `RssAnon`, `VmSwap`, PSS, and fault deltas from `/proc` are diagnostics and fail closed when incomplete. Missing PSI stays unknown; the ratio profile requires `require_psi=false` because this firmware's PSI semantics are unverified and `/proc/pressure/io` is absent.

On isolated 64 MiB and 128 MiB anonymous workloads, writes of 5, 10, 20, repeated 20, 10, and 30 were accepted. RSS and VmSwap/Zram deltas fit approximately a percentage of the remaining eligible anonymous pages, with batching; full revisit checks passed without mismatches or major-fault deltas. Vendor library strings identify the argument as `swapRatio`/`anonRatio` and report reclaimed kB. This is empirical evidence for this exact firmware, not a portable ABI and not a hard byte limit. The daemon converts a nominal byte budget to a scalar with floor rounding, caps each write at 20%, and subtracts a 128 KiB empirical margin. Kernel batching and concurrent system activity can still make actual effects differ from the nominal budget. It never reports RSS loss as net system memory saved.

The `memory.force_shrink_anon_bycount` scalar attempts and `/proc/PID/reclaim anon` returned `EINVAL`; they are not used. Public OpenHarmony source behavior differs from this device's implementation. Device evidence is in `results/memshift_hdc_20261007/ratio_qualification.txt`, `ratio_matrix.txt`, `vendor_strings.txt`, and `vendor_config_dump.txt`; the tested `libmemmgrservice.z.so` SHA256 is `94476D42F7233821465E86E6C367E5AB0321F11A5835C6E8E56B8ECDC6743A70`.

## Observer

```sh
./memshiftd --config device.ini --backend harmony-v1 --check
./memshiftd --config device.ini --backend harmony-v1 --observe --ticks 3
```

Observer logs are written to `<log_dir>/harmony-v1-observe-v3/` using schema `harmony-v1-observe-3`. Their columns intentionally differ from Linux v2 logs. Empty fields mean unknown or unavailable, not zero. The observer does not derive file-cache, cold-page, or reclaim budgets.

## Experimental ratio apply

Two explicit profiles are available, both requiring `--apply` to write. `harmony-v1-ratio-probe` is restricted to exactly one direct `/dev/memcg/memshift_probe_*` group, a sole `harmony_v1_isolated_workload` process, and a 128 MiB cgroup cap. This is the profile to use for isolated end-to-end verification:

```sh
./memshiftd --config probe.ini --backend harmony-v1-ratio-probe --apply --ticks 20
```

`harmony-v1-ratio` accepts only explicitly configured `reclaim=true` app cgroups one user/package level below `/dev/memcg`. Every member must remain in the configured memory cgroup, the group `memory.app_score` and every process `oom_score_adj` must be positive, and the control file must be readable, valid, unpaused, and not protect the app. Missing/zero/negative scores, empty groups, changed PID/start-time, incomplete telemetry, or unknown control state prevent a write. The actual device's browser, file manager, app gallery, and WPS groups had score zero or a nonpositive process score, so those observed apps were protected. The generic profile is experimental and should first be run with ordinary reserves to verify that gates stay closed for those groups:

```sh
./memshiftd --config resident.ini --backend harmony-v1-ratio --apply
```

Both ratio profiles require `cgroup_root=/dev/memcg`, `require_psi=false`, a configured control file, and a `HarmonyOS` / `HongMeng Kernel 1.13.0` uname fingerprint. They use MemAvailable trend EWMA as a nominal system gap, require warmup, obey configured min-keep, batch, and per-minute limits, apply a 32 MiB nominal controller budget cap per write, allow at most one write per interval, and apply cooldown/backoff. That cap limits the request calculation; it is not a hard bound on kernel reclaim. After a write they recheck member identity and collect process RSS feedback. A post-write telemetry failure causes backoff; a >1 MiB RSSAnon recovery or a major-fault/minor-fault spike activates the configured backoff. Fault deltas are activity guards, not refault measurements. PSI and `memory.anon_refault` are logged as unverified and do not enable writes.

Ratio logs are isolated under `<log_dir>/harmony-v1-ratio/` or `<log_dir>/harmony-v1-ratio-probe/`; schema `harmony-v1-observe-3` adds planned nominal bytes and action/target fields. `candidate_budget_bytes` is a controller estimate. `reclaim_action` reports the submission state; it is not a count of bytes freed.

## Probe helpers and build

`harmony_v1_isolated_workload` joins the supplied `cgroup.procs` before allocating and filling a deterministic anonymous mapping. `SIGUSR1` rereads all bytes and reports checksum, elapsed time, and minor/major fault deltas; `SIGTERM` exits cleanly. `harmony_v1_test_reclaim` remains a separately guarded manual writer for the dedicated probe group only.

Build all OpenHarmony AArch64 artifacts in PowerShell with `./tools/build_harmony.ps1`; outputs go to `build-ohos-aarch64`. Do not point the probe helper or probe profile at an app group or the hierarchy root.
