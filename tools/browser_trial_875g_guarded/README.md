# Guarded 8.75 GiB gate diagnostic helper

This directory is an isolated, local-only first slice for a bounded one-reclose diagnostic. It does not change or wrap `tools/browser_trial_875g`, and it does not contain an end-to-end A/B runner. No HDC, device connection, pressure launch, service switch, or deployment is implemented here. Do not treat this helper as authorization to run a trial.

The gate candidate is pinned to `gate_window_v8_manifest_v4.json` and SHA-256 `1f21af9b06aec90543e24bffb3f234bb540b74c713a7f1b02f1dc88bed3e732e`. The helper emits only an argv specification; it never launches the candidate. Producing that specification requires explicit `--accept-bounded-diagnostic-only`:

```powershell
python tools/browser_trial_875g_guarded/guarded_gate.py emit-launch-argv `
  --remote-binary /data/local/tmp/memscenes_s4_example/gate_window `
  --remote-prefix /data/local/tmp/memscenes_s4_example/gate `
  --T 9000.0 --boot-id c3821db7-989a-4977-a721-b1bcd955c17c `
  --accept-bounded-diagnostic-only
```

The fixed offsets remain T−1 close, T+930 open, and T+935 watchdog. The emitted argv includes the candidate's single-use `--reclose-once` option. The helper's audit result keeps `strict_continuous_readonly`, `original_gate_valid`, and `valid_for_oom_comparison` separate from `bounded_diagnostic_valid`. A bounded result requires exactly one successful reclose, all 1862 polls, `no_new_disk_write_observed=1`, bounded-control completion, same-boot restoration, native return code zero, complete raw kernel/OOM capture, exact original-service hashes, and all three target HAPs absent. Missing or unknown evidence fails closed. Even a bounded pass explicitly does not guarantee zero physical writes and cannot qualify as the formal strict-readonly A/B comparison.

The independent unit fixtures can be run without a device:

```powershell
python -m unittest tools.browser_trial_875g_guarded.test_guarded_gate -v
```

Before a guarded diagnostic can run, a separate runner still needs to be built and reviewed. It must thread the emitted argv into the unchanged 875g load/controller path, make baseline and optimized phases both opt into the same bounded mode, persist raw gate and collector evidence, and write a distinct diagnostic result schema without upgrading strict gate validity. That integration is intentionally not claimed by this helper.
