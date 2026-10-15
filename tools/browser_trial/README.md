# Real browser donor A/B runner

This is an independent 3-target variant. It retains the frozen native retry
controller semantics for `a14`–`a16`, but changes the registered donor to one
real browser cgroup: six hidden windows, three settled offline pages per window,
and 21 identity-bound processes. It never starts the 13 synthetic donor HAPs.
The old `candidate_runner` tree is only read for its HAP launch/device helpers,
kernel collector, and fixed-T gate helper; no files there are modified.

The workload is fixed at 1536 MiB daily, 8192 MiB peak, 256 MiB file-backed,
four fill workers, ten 30-second retry slots, a 330-second request window, and a
420-second hold. The registered future increment is exactly 19968 MiB. Each
phase gets a fresh device `CLOCK_BOOTTIME` T 90 seconds after the newest
verified browser sample. The baseline service is paused; the optimized service
is active. Both use `owned_apply` and the same load cadence. The v7 gate helper
closes hyperhold at T−1 and reopens at T+930, with a T+935 watchdog. It verifies
the hyperhold I/O counter stayed unchanged while closed; zram remains allowed.
Only kernel OOM evidence is a primary outcome. Renderer exits are retained as
diagnostics and never counted as OOM.

Each phase has three steps so target HAPs can be made ready before collecting a
fresh browser sample and choosing T:

1. Prepare the three targets at daily baseline, leaving them resident:

   ```powershell
   python tools/browser_trial/run_trial.py --phase baseline --prepare-targets `
     --case-id s4_0123456789ab_c0001 `
     --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
     --output results/browser_trial/A/target_ready.json
   ```

   `target_ready.json` is an outer receipt; the command also writes
   `target_daily_ready.json` beside it. Execution accepts the wrapper and checks
   the embedded three-target PID/start/UID proof.

   Use a new lowercase 12-hex case token for each A/B phase.

2. Restore the six browser windows with the 18 offline pages, wait until each
   page is settled, minimize every browser window, then sample the current
   browser group. UI preparation is an explicit operator step; the sampler
   validates the resulting evidence:

   ```powershell
   python tools/browser_donor/prepare.py --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
     --target 3QC0124C03000514 --expected-windows 6 --pages-per-window 3 `
     --samples 3 --interval 30 --pages-loaded --all-minimized `
     --workload-manifest tools/browser_donor/workload_6x3_20261013.json `
     --output results/browser_trial/A/browser_manifest.json
   python tools/browser_trial/create_browser_plan.py `
     --manifest results/browser_trial/A/browser_manifest.json `
     --case-id s4_0123456789ab_c0001 `
     --output results/browser_trial/A/browser_event_plan.tsv
   ```

3. Run the phase using the exact target-ready proof, newly sampled browser
   manifest/plan, candidate service binary, and browser-profile service INI:

   ```powershell
   python tools/browser_trial/run_trial.py --phase baseline --execute `
     --target-ready results/browser_trial/A/target_ready.json `
     --browser-manifest results/browser_trial/A/browser_manifest.json `
     --browser-plan results/browser_trial/A/browser_event_plan.tsv `
     --service-ini tools/browser_trial/browser_profile_template.ini `
     --candidate-daemon <browser-profile-memshiftd> `
     --hdc "交付/内存场景复现包_20261006/tools/hdc.exe" `
     --output results/browser_trial/A/result.json
   ```

Run the same steps for B with a fresh case token, fresh target-ready proof, and
fresh browser sample. Change only `--phase optimized` and the result directory.
Do not reuse an A browser manifest or event plan for B. The runner checks that
the event T is exactly 90 seconds after that phase's final verified donor sample.

`--preflight-only` validates files and hashes locally without connecting to the
device. `--prepare-targets` is the only mode that launches the three HAPs. The
execution mode verifies the live target PID/start/UID and service backup hashes,
checks the candidate daemon with `--check` before stopping the original,
stages every host file from an absolute resolved path and verifies remote
SHA-256, runs one service instance, starts the kernel collector, native
controller, and fixed-T gate, then restores and verifies the exact original
service/control state in `finally`. It records raw controller, gate, kernel,
known-event, and service logs in the result directory.

This runner has only been locally compiled and statically reviewed here. No
device trial is claimed by this variant until both phases complete with valid
native, gate, collector, and exact service-restore evidence. Accept one valid
A/B pair before proceeding to the planned three pairs.
