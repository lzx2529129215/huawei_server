# Real browser donor trial — 8.5 GiB target calibration

This is a separate calibration runner; it does not edit or replace `tools/browser_trial` and must not reuse an A03/8 GiB native plan, binary, or result directory. It keeps the same real donor (six minimized browser windows and 18 settled offline pages). The initial 21-member count is an observed reference only: each run binds the complete currently verified process set, requiring 1–64 unique members and stable PID/UID/start identities across all three samples. A later sample with 19 members is acceptable only when its member lists, per-process identities, cgroup placement, window ownership, and service-plan row all agree exactly. The profile records the per-run observed count. Donor anon has no absolute 5.07 GiB threshold: each run must have three positive samples with no more than 2% max-to-min spread, and optimized must match the completed baseline's measured three-sample mean within 2%. Other load settings remain 1536 MiB daily baseline, 256 MiB file-backed allocation, four fill workers, 90-second fixed-device-clock lead, ten 30-second retry slots, 330-second request window, 420-second hold, and 930-second OOM evidence horizon.

Each of the three targets is raised from 1536 to 8704 MiB. The exact registered future increment is therefore `(8704 − 1536) × 3 = 21504 MiB`. This intermediate calibration sits between the zero-OOM 8 GiB run and the invalid 9 GiB high-pressure attempt; it does not reuse either result as its baseline. The variant uses `device_retry_plan_browser_donor_3target_85g_v1`; its controller rejects the neighboring 8 GiB and 9 GiB schemas and fingerprints (and does not accept the 10 GiB fingerprint). The owned-apply whole-work cap and per-app/minute quota remain 8192 MiB as independent policy limits. The frozen v7 gate still forbids new disk swapout from T−1 through T+930, permits zram, and has its T+935 watchdog. SELinux must remain Enforcing. Only kernel OOM evidence is the primary outcome; renderer exits, browser anon reduction, and zram/Eswap movement remain diagnostics. No browser reclaim/compression behavior is disabled or changed.

Use a new `s4_<12 lowercase hex>_c0001` case ID and a new output folder for every phase. Prepare targets first; this mode does not run the pressure controller:

```powershell
python tools/browser_trial_85g/run_trial.py --phase baseline --prepare-targets `
  --case-id s4_0123456789ab_c0001 `
  --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --output results/browser_trial_85g/A01/target_ready.json
```

The command writes an outer `target_ready.json` and sibling `target_daily_ready.json`. Restore the six browser windows and their 18 offline pages, ensure each page is settled, minimize all windows, then sample the hidden browser group freshly. Do not turn off browser reclaim, compression, zram, or Eswap behavior to stabilize the donor:

```powershell
python tools/browser_donor/prepare.py --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --target 3QC0124C03000514 --expected-windows 6 --pages-per-window 3 `
  --samples 3 --interval 30 --pages-loaded --all-minimized `
  --workload-manifest tools/browser_donor/workload_6x3_20261013.json `
  --output results/browser_trial_85g/A01/browser_manifest.json
python tools/browser_trial_85g/create_browser_plan.py `
  --manifest results/browser_trial_85g/A01/browser_manifest.json `
  --case-id s4_0123456789ab_c0001 `
  --output results/browser_trial_85g/A01/browser_event_plan.tsv
```

The event writer fixes T at 90 seconds after the latest verified browser sample and registers exactly 21504 MiB. Run local checks before any device execution. Candidate daemon must be the separately built browser-profile service, not an old pre-profile binary; the native controller is built independently by `tools/browser_trial_85g/build_controller.py` with the configured Harmony compiler:

```powershell
python tools/browser_trial_85g/build_controller.py
python tools/browser_trial_85g/run_trial.py --phase baseline --preflight-only `
  --service-ini tools/browser_trial_85g/browser_profile_template.ini `
  --browser-manifest results/browser_trial_85g/A01/browser_manifest.json `
  --browser-plan results/browser_trial_85g/A01/browser_event_plan.tsv `
  --candidate-daemon <browser-profile-memshiftd> `
  --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --output results/browser_trial_85g/A01/preflight.json
```

Only after the local preflight and the required device/service preflight are reviewed, execute the baseline with the exact target-ready proof, fresh donor sample, matching 8.5 GiB plan, candidate binary, and trusted original backup set:

```powershell
python tools/browser_trial_85g/run_trial.py --phase baseline --execute `
  --target-ready results/browser_trial_85g/A01/target_ready.json `
  --browser-manifest results/browser_trial_85g/A01/browser_manifest.json `
  --browser-plan results/browser_trial_85g/A01/browser_event_plan.tsv `
  --service-ini tools/browser_trial_85g/browser_profile_template.ini `
  --candidate-daemon <browser-profile-memshiftd> `
  --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --output results/browser_trial_85g/A01/result.json
```

The optimized phase uses a new case ID, new target-ready proof, new browser preparation manifest, and new 90-second plan; change only the phase and output directory. Pass the completed 8.5 GiB baseline `result.json` with `--baseline-reference` to both its preflight and execution commands. It must have completed with a valid full gate/OOM horizon, exact same profile/pressure targets, verified service restore and target cleanup. The runner compares a stable SHA-256 fingerprint of all 18 page hashes, device paths, and window/page slots, then compares the fresh optimized three-sample anon mean to that baseline and rejects a difference above 2% before creating the device lifecycle or changing the service. Do not reuse A01 timing or process identities. The runner verifies 8.5 GiB browser-event demand and native-plan fingerprints before staging, checks the candidate daemon before switching the single service instance, arms kernel capture before pressure can begin, uses the fixed-T gate/controller, and restores the exact original service state in `finally`.

Example optimized preflight and execution (use the real baseline result and fresh B paths):

```powershell
python tools/browser_trial_85g/run_trial.py --phase optimized --preflight-only `
  --baseline-reference results/browser_trial_85g/A01/result.json `
  --service-ini tools/browser_trial_85g/browser_profile_template.ini `
  --browser-manifest results/browser_trial_85g/B01/browser_manifest.json `
  --browser-plan results/browser_trial_85g/B01/browser_event_plan.tsv `
  --candidate-daemon <browser-profile-memshiftd> `
  --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --output results/browser_trial_85g/B01/preflight.json

python tools/browser_trial_85g/run_trial.py --phase optimized --execute `
  --baseline-reference results/browser_trial_85g/A01/result.json `
  --target-ready results/browser_trial_85g/B01/target_ready.json `
  --browser-manifest results/browser_trial_85g/B01/browser_manifest.json `
  --browser-plan results/browser_trial_85g/B01/browser_event_plan.tsv `
  --service-ini tools/browser_trial_85g/browser_profile_template.ini `
  --candidate-daemon <browser-profile-memshiftd> `
  --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
  --output results/browser_trial_85g/B01/result.json
```

During the A03 8 GiB baseline, at about T+539 and after the hold, the observed browser anon was 278684 KiB, zram was 1795904 KiB, and Eswap was 689484 KiB. Review this as real browser/system behavior; do not disable browser reclaim/compression or interpret anon/zram/Eswap movement as OOM. A renderer exit is also not an OOM. This runner has not run on the device. Its local plan-fingerprint and lifecycle regressions must pass before any trial is considered.
