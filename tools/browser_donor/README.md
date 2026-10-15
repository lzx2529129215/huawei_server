# Browser donor variant

`workload_6x3_20261013.json` maps the planned six browser windows to their three
device paths each. All 18 device files reuse the verified local source file, and
the sampler checks each device copy with `sha256sum` before it takes
three read-only device samples 30 seconds apart. A sample is eligible for review
only when all six browser windows are observed hidden, every window PID belongs
to the browser memcg, the complete member list has stable PID/start-time/UID/cgroup
identity, the group `app_score` is positive, and every member has positive
`oom_score_adj`. Window labels map the operator's page assignment; Harmony's
window dump does not expose browser tab URLs, so loaded-page readiness remains an
explicit operator assertion.

After the 18 named HTML copies have been placed at the device paths in the
workload manifest and opened through File Manager, run from the repository root:

```powershell
python tools/browser_donor/prepare.py `
  --hdc "C:\path\to\hdc.exe" `
  --expected-windows 6 `
  --pages-per-window 3 `
  --samples 3 `
  --interval 30 `
  --workload-manifest tools/browser_donor/workload_6x3_20261013.json `
  --pages-loaded `
  --all-minimized `
  --output results/browser_donor_integration_20261013/donor_prepare_manifest.json
```

The result retains raw device snapshots plus parsed group capacity, swap, zram,
window and process identity data. It never launches apps or writes device state.

## One-shot service-chain diagnostic

`run_reclaim_diagnostic.py` is a bounded diagnostic, not an A/B trial and not
benefit evidence. It requires a fresh hidden-window/member identity check,
pulls and hash-verifies the currently unique daemon binary/config/control file,
stages a separate browser-profile binary/config/event, and runs the new binary's
read-only `--check` before stopping the resident service. The generated profile
has one browser app group, 512 MiB keep and batch, 1,024 MiB global/app quotas,
one lifetime submission, a 95-second event lead, and `daily_disk_mode=off`.
Demand is rounded up from fresh `MemFree + margin` and rejected if demand plus
the configured reserve exceeds `MemTotal`.

After the fixed event plus the short feedback window, the tool SIGTERMs only its
own verified PID, captures fresh `/proc/meminfo`, browser group state, service
CSVs and proof ledgers, verifies the original remote hashes are unchanged, and
restarts the exact original daemon command. The original control file is only
backed up/read; the diagnostic uses a separate control file. SELinux must remain
Enforcing. It does not alter swap settings or the pressure applications.

Root review is required before device execution because the diagnostic replaces
the one resident daemon temporarily. From PowerShell, after reviewing the
generated profile and restore behavior, the bounded invocation is:

```powershell
python tools/browser_donor/run_reclaim_diagnostic.py `
  --hdc "C:\path\to\hdc.exe" `
  --target "3QC0124C03000514" `
  --binary "$env:TEMP\memshift_browser_donor_build\memshiftd" `
  --browser-manifest results/browser_donor_integration_20261013/donor_prepare_manifest_v2.json `
  --output results/browser_donor_integration_20261013/reclaim_diag_20261013_01 `
  --execute `
  --confirm-service-replacement
```

The output directory must not already exist. The final JSON records whether one
completion row was observed and whether the original service was restored; the
service-chain result must not be described as a repeatable memory-pressure gain.

## Repeat the prepared browser page loads

`reload_prepared.py` reuses the current six windows and their existing three
tabs. It does not open or close windows/tabs. Dry-run is the default. In execute
mode it hashes all 18 device files first, then uses the calibrated 3120×2080 UI
coordinates. Before and after each window selection it verifies WMS owner PID,
WinId, visibility, and `WindowRect`; a mismatch stops the sequence. It waits
two seconds after each reload and saves timestamped screenshots, HDC command
results, and errors in a new output directory. Screenshots use the Harmony
`uitest screenCap -p <absolute-device-path>` command and are pulled to a fresh
local filename, then checked against the device SHA-256 and PNG dimensions. The
code does not inspect page
URLs automatically; use `--window 1 --guided` to verify a one-window pass by
eye before approving each next action.

Preview the full sequence without UI clicks:

```powershell
python tools/browser_donor/reload_prepared.py `
  --hdc "C:\path\to\hdc.exe" `
  --target "3QC0124C03000514" `
  --preparation-manifest results/browser_donor_integration_20261013/donor_prepare_manifest_v2.json `
  --output results/browser_donor_integration_20261013/reload_dryrun_01
```

For a guided one-window execution, use the same invocation with a new output
directory and append `--window 1 --guided --execute --confirm-ui-reload`. The
operator must type `CONTINUE` after checking each saved screenshot. Full replay
uses the same reviewed manifest and flags without `--window 1 --guided`.

## Restore named pages into existing tabs after browser recovery

`restore_prepared.py` is a separate path for a restored browser whose existing
window-to-page assignment is unknown. It navigates each of the 18 existing tabs
to the URL assigned by the workload manifest; it does not create or close tabs.
The preparation manifest is used only as a fresh identity binding, so it may
have `eligible_for_review: false` while confirming that the six windows and
their process group are stable and hidden. After URL screenshots are visually
checked, run `prepare.py` again with `--pages-loaded --all-minimized` to create
the actual experimental readiness manifest.

Before execution, prepare a screenshot-derived UI layout manifest with schema
`browser-donor-ui-layout-v1`. It must contain `device_target`,
`screen_size: [3120, 2080]`, `screenshot_path` relative to the layout JSON,
`screenshot_sha256`, `browser_taskbar: [x,y]`, and six `windows` objects. Each
window object is bound to the exact current preparation sample using
`name`, `pid`, `start_ticks`, `win_id`, and `rect`, and supplies screenshot-
verified `thumbnail: [x,y]`, `tabs: [[x,y], [x,y], [x,y]]`, `address: [x,y]`,
and `minimize: [x,y]` coordinates. The layout loader verifies the reference
screenshot hash and rejects stale process or WMS bindings. Never carry these
coordinates or IDs across a changed screenshot, PID, WinId, or rectangle.

Preview first. UI input is disabled unless both `--execute` and
`--navigate-expected-pages` are explicitly provided. `--guided` pauses after
each screenshot for visual URL/table review.

```powershell
python tools/browser_donor/restore_prepared.py `
  --hdc "C:\path\to\hdc.exe" `
  --target "3QC0124C03000514" `
  --preparation-manifest results/browser_restore_20261014/binding_manifest.json `
  --ui-layout results/browser_restore_20261014/ui_layout.json `
  --output results/browser_restore_20261014/restore_preview `
  --navigate-expected-pages
```

After reviewing the generated plan and screenshot-derived coordinates, add
`--execute --guided` to run the existing-tab navigation. The helper rechecks
the owner PID/start ticks/UID/cgroup, all six WMS names/PIDs/WinIds/rectangles,
and 18 device-page hashes before interacting. It then verifies exactly one
manifest-bound window is visible at a time and that all six are hidden at the
end. URL contents remain a visual check because WMS does not expose tab URLs.
