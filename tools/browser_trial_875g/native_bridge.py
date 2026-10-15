"""Stage and collect the fixed-device-clock pressure controller and peak gate."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sys
import time
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FROZEN = ROOT / "results" / "daily_lead90_20261012_1640" / "candidate_runner"
NATIVE = HERE / "native"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def gate_interpretation(valid: bool, raw_gate_events: str) -> str:
    if valid:
        return "no new disk swapout was verified for the complete T-1 through T+930 gate interval; zram remains permitted"
    reasons = []
    for line in raw_gate_events.splitlines():
        if line.startswith("GATE_INVALID|"):
            fields = line.split("|", 2)
            reasons.append(fields[2] if len(fields) == 3 else line)
    reason = ",".join(dict.fromkeys(reasons)) if reasons else "gate audit incomplete or failed"
    return f"disk swapout exclusion was not verified for the complete T-1 through T+930 interval; reason={reason}"


def q(value: str | Path) -> str:
    text = str(value)
    return "'" + text.replace("'", "'\\''") + "'"


def device_boottime(device) -> float:
    fields = device.shell("cat /proc/uptime", timeout=5).split()
    now = float(fields[0])
    if not math.isfinite(now) or now < 0:
        raise RuntimeError("device boottime is invalid")
    return now


def start_ticks(device, pid: int) -> int:
    """Return proc stat field 22, or 0 when a process has already disappeared.

    Device shells commonly return `cat: ... No such file or directory` as text
    even with check=False. Treat missing/malformed proc text as a gone identity,
    not as an IndexError that aborts emergency finalization.
    """
    raw = device.shell(f"cat /proc/{pid}/stat", timeout=5, check=False).strip()
    if not raw or ")" not in raw:
        return 0
    suffix = raw.rsplit(")", 1)[1].split()
    if len(suffix) <= 19 or not suffix[19].isdecimal():
        return 0
    return int(suffix[19])


def send_checked(device, local: Path, remote: str, expected: str | None = None) -> str:
    local = local.resolve(strict=True)
    if not local.is_file():
        raise ValueError(f"transfer source is not a file: {local}")
    digest = expected or sha(local)
    device.call("file", "send", str(local), remote, timeout=60)
    actual = device.shell("sha256sum " + q(remote), timeout=10).split()[0].lower()
    if actual != digest.lower():
        raise RuntimeError(f"device transfer hash mismatch for {remote}: {actual} != {digest}")
    device.shell("chmod 755 " + q(remote), timeout=5)
    return actual


def stop_reader(reader, device, remote_root: str, output_dir: Path) -> dict:
    """Stop/collect the owned kmsg collector, including timeout recovery."""
    if reader is not None:
        reader.terminate()
        try:
            code = reader.wait(timeout=5)
        except Exception:
            reader.kill()
            code = reader.wait(timeout=5)
        reader.collect(output_dir / "kernel_dmesg.raw.log", output_dir / "kernel_reader.stderr.log")
        return {"terminated": True, "pid": reader.pid, "start_ticks": reader.start_ticks,
                "returncode": code, "raw_sha256": sha(output_dir / "kernel_dmesg.raw.log")}

    # DeviceDmesgReader can fail after its remote process starts but before its
    # constructor returns (identity/cmdline validation). The randomized remote
    # executable path is unique to this invocation, so recover only an exact
    # /proc/<pid>/exe match and bind its live start ticks before signalling.
    binary = remote_root + "/kmsg_reader"
    scan = ("for d in /proc/[0-9]*; do p=${d##*/}; x=$(readlink $d/exe 2>/dev/null); "
            f"[ \"$x\" = {q(binary)} ] || continue; "
            "s=$(cat $d/stat 2>/dev/null); set -- $s; "
            "echo RECOVERED_READER=$p:${22}:$x; done")
    found = device.shell(scan, check=False, timeout=10)
    rows = []
    for line in found.splitlines():
        if not line.startswith("RECOVERED_READER="):
            continue
        fields = line.removeprefix("RECOVERED_READER=").split(":", 2)
        if len(fields) == 3 and fields[0].isdecimal() and fields[1].isdecimal() and fields[2] == binary:
            rows.append((int(fields[0]), int(fields[1])))
    if len(rows) != 1:
        return {"terminated": False, "recovered_matches": len(rows),
                "reason": "no unique exact collector process was recoverable"}
    pid, ticks = rows[0]
    signal = (f"x=$(readlink /proc/{pid}/exe 2>/dev/null); s=$(cat /proc/{pid}/stat 2>/dev/null); "
              "set -- $s; "
              f"if [ \"$x\" = {q(binary)} ] && [ \"${{22}}\" = '{ticks}' ]; "
              f"then kill -TERM {pid}; echo COLLECTOR_TERM_SENT; fi")
    outcome = device.shell(signal, check=False, timeout=8)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        now = device.shell(f"cat /proc/{pid}/stat 2>/dev/null", check=False, timeout=5).strip()
        if not now:
            break
        try:
            current_ticks = int(now.rsplit(")", 1)[1].split()[19])
        except (ValueError, IndexError):
            break
        if current_ticks != ticks:
            break
        time.sleep(0.1)
    raw_path = output_dir / "kernel_dmesg.raw.log"
    stderr_path = output_dir / "kernel_reader.stderr.log"
    errors = []
    for remote, local in ((remote_root + "/kernel_0.raw.log", raw_path),
                          (remote_root + "/kernel_0.stderr.log", stderr_path)):
        try:
            device.receive(remote, local)
        except Exception as exc:
            errors.append({"file": remote, "error": type(exc).__name__})
    return {"terminated": "COLLECTOR_TERM_SENT" in outcome, "pid": pid,
            "start_ticks": ticks, "signal_result": outcome, "collection_errors": errors,
            "raw_sha256": sha(raw_path) if raw_path.is_file() else None}


def start_kernel_collector(device, output_dir: Path) -> dict:
    """Start kernel capture before candidate service startup/action is possible."""
    collector = FROZEN / "native_source" / "kmsg_reader"
    if not collector.is_file():
        raise RuntimeError("frozen kernel OOM collector is missing")
    os.environ["MEMSCENE_KERNEL_COLLECTOR_PATH"] = str(collector.resolve())
    sys.path.insert(0, str(FROZEN))
    from memscenes.kernel_capture import DeviceDmesgReader
    remote_root = f"/data/local/tmp/memscenes_s4_{secrets.token_hex(8)}"
    created = device.shell("test ! -e " + q(remote_root) +
        "; rc=$?; test $rc -eq 0 && mkdir -p " + q(remote_root) +
        "; echo NEW_ROOT_RC=$rc", timeout=10)
    if "NEW_ROOT_RC=0" not in created:
        raise RuntimeError("kernel collector remote directory already exists")
    state = {"remote_root": remote_root, "reader": None,
        "capture_start_boottime": device_boottime(device),
        "capture_start_host_monotonic": time.monotonic(),
        "collector_sha256": sha(collector), "done": False}
    try:
        state["reader"] = DeviceDmesgReader(device, remote_root, 0)
        state["collector_ready_boottime"] = device_boottime(device)
        return state
    except BaseException:
        # Constructor failures can occur after its detached /proc reader starts.
        try:
            state["partial_collector_cleanup"] = stop_reader(None, device, remote_root, output_dir)
        except Exception as cleanup_exc:
            state["partial_collector_cleanup"] = {"terminated": False,
                "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"}
        state["done"] = True
        (output_dir / "kernel_collector_arm_failure.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8")
        raise


def execute(device, plan_path: Path, output_dir: Path, collector_state: dict | None = None) -> dict:
    """Run one native phase, gate the hyperhold disk path, and retain raw output.

    This method intentionally does not derive timing from host sleeps: both native
    programs use the immutable CLOCK_BOOTTIME T embedded in the plan.
    """
    manifest_path = NATIVE / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    controller = NATIVE / "device_retry_controller"
    gate = FROZEN / "native_source" / "gate_window"
    gate_manifest_path = FROZEN / "native_source" / "gate_window_manifest.json"
    gate_manifest = json.loads(gate_manifest_path.read_text(encoding="utf-8"))
    if sha(controller) != manifest.get("binary_sha256"):
        raise RuntimeError("local native controller differs from its build manifest")
    expected_role_schema = "device_retry_plan_browser_donor_3target_875g_v1"
    if manifest.get("role_schema") != expected_role_schema:
        raise RuntimeError("native controller was not built for browser 8.75GiB 3-target schema")
    if sha(gate).lower() != "dd6e5e04cb0c92faa3726f191393754c5a1d3f17b29283720dfdef7cfb111f9d":
        raise RuntimeError("frozen gate helper hash differs from reviewed v7 binary")
    if sha(gate).lower() != gate_manifest.get("binary_sha256", "").lower():
        raise RuntimeError("gate helper binary/manifest mismatch")
    lines = plan_path.read_text(encoding="ascii").splitlines()
    if len(lines) != 5 or lines[0] != expected_role_schema:
        raise ValueError("native retry plan must be the closed 3-target plan")
    from importlib.util import module_from_spec, spec_from_file_location
    native_plan_spec = spec_from_file_location("browser_trial_875g_native_plan", HERE / "native_plan.py")
    if native_plan_spec is None or native_plan_spec.loader is None:
        raise RuntimeError("cannot load local 8.75GiB native plan validator")
    native_plan_module = module_from_spec(native_plan_spec)
    native_plan_spec.loader.exec_module(native_plan_module)
    native_plan_module.validate_serialized(plan_path.read_text(encoding="ascii"))
    meta = lines[1].split("|")
    if len(meta) != 16:
        raise ValueError("native plan metadata has wrong field count")
    case_id, boot_id, T = meta[0], meta[1], float(meta[2])
    current_boot = device.shell("cat /proc/sys/kernel/random/boot_id", timeout=5).strip()
    if current_boot != boot_id:
        raise RuntimeError("native plan boot id no longer matches device")
    if device_boottime(device) >= T:
        raise RuntimeError("native plan was not staged before fixed device T")
    if device.shell("getenforce", timeout=5).strip() != "Enforcing":
        raise RuntimeError("SELinux is not Enforcing")

    remote_root = f"/data/local/tmp/memscenes_s4_{secrets.token_hex(8)}"
    created = device.shell("test ! -e " + q(remote_root) +
                           "; rc=$?; test $rc -eq 0 && mkdir -p " + q(remote_root) +
                           "; echo NEW_ROOT_RC=$rc", timeout=10)
    if "NEW_ROOT_RC=0" not in created:
        raise RuntimeError("remote controller directory already exists")
    collector = FROZEN / "native_source" / "kmsg_reader"
    if collector_state is None:
        collector_state = start_kernel_collector(device, output_dir)
    reader = collector_state["reader"]
    collector_remote_root = collector_state["remote_root"]
    capture_start_boottime = float(collector_state["capture_start_boottime"])
    capture_start_host_monotonic = float(collector_state["capture_start_host_monotonic"])
    reader_done = False
    launch_attempted = False
    remote_plan = remote_root + "/plan.tsv"
    remote_controller = remote_root + "/controller"
    remote_gate = remote_root + "/gate_window"
    remote_prefix = remote_root + "/gate"

    try:
        local_plan = output_dir / "native_retry_plan.tsv"
        local_plan.write_bytes(plan_path.read_bytes())
        controller_hash = send_checked(device, controller, remote_controller)
        gate_hash = send_checked(device, gate, remote_gate)
        plan_hash = send_checked(device, local_plan, remote_plan)

        # The helper is armed first; its own CLOCK_BOOTTIME logic closes the peak
        # gate at T-1, allows one reopen at T+930, and has a T+935 watchdog.
        gate_args = " ".join((q(remote_gate), "--T", q(f"{T:.9f}"), "--boot", q(boot_id),
                              "--out", q(remote_prefix), "--open-offset", "930",
                              "--watchdog-offset", "935"))
        gate_launch = ("nohup sh -c " + q(gate_args) + " > " + q(remote_root + "/gate_launcher.log") +
                       " 2>&1 < /dev/null & echo GATE_WRAPPER_PID=$!")
        controller_args = q(remote_controller) + " --plan " + q(remote_plan) + " --output " + q(remote_root)
        controller_script = controller_args + "; rc=$?; printf '%s\\n' \"$rc\" > " + q(remote_root + "/native.rc")
        controller_launch = ("nohup sh -c " + q(controller_script) + " > " + q(remote_root + "/stdout.log") +
                             " 2> " + q(remote_root + "/stderr.log") + " < /dev/null & echo CONTROLLER_WRAPPER_PID=$!")
        launch_attempted = True
        launched = device.shell(gate_launch + "; " + controller_launch, timeout=10)
        registration = {"remote_root": remote_root, "boot_id": boot_id, "case_id": case_id,
            "T": T, "clock_domain": "CLOCK_BOOTTIME", "controller_sha256": controller_hash,
            "gate_sha256": gate_hash, "plan_sha256": plan_hash,
            "kernel_capture_start_boottime": capture_start_boottime,
            "kernel_collector_ready_boottime": collector_state.get("collector_ready_boottime"),
            "kernel_collector_remote_root": collector_remote_root,
            "controller_wrapper_pid": None, "gate_wrapper_pid": None,
            "launch_response": launched, "gate_window": {"close": T - 1,
                "reopen": T + 930, "watchdog": T + 935,
                "disk_swapout": "forbidden", "zram": "allowed"}}
        import re
        for key, label in (("controller_wrapper_pid", "CONTROLLER_WRAPPER_PID"),
                           ("gate_wrapper_pid", "GATE_WRAPPER_PID")):
            match = re.search(label + r"=(\d+)", launched)
            if not match:
                raise RuntimeError(f"remote launch did not return {label}")
            registration[key] = int(match.group(1))
            registration[key + "_start_ticks"] = start_ticks(device, registration[key])
            if registration[key + "_start_ticks"] <= 0:
                raise RuntimeError(f"cannot bind remote helper identity: {label}")
        (output_dir / "native_registration.json").write_text(
            json.dumps(registration, indent=2) + "\n", encoding="utf-8")

        deadline = time.monotonic() + 420
        rc = None
        while time.monotonic() < deadline:
            value = device.shell("if test -f " + q(remote_root + "/native.rc") +
                                 "; then cat " + q(remote_root + "/native.rc") +
                                 "; else echo NATIVE_PENDING; fi", timeout=10).strip()
            if value != "NATIVE_PENDING":
                rc = int(value)
                break
            time.sleep(3)
        # Wait for watchdog evidence after T+935, not just controller T+330.
        gate_deadline = time.monotonic() + max(0, T + 965 - device_boottime(device))
        gate_complete = False
        gate_probe = ""
        while time.monotonic() < gate_deadline:
            gate_probe = device.shell("cat /proc/sys/kernel/random/boot_id; tail -n 12 " +
                q(remote_prefix + ".events.tsv") + " 2>/dev/null; echo --WATCHDOG--; tail -n 5 " +
                q(remote_prefix + ".watchdog.tsv") + " 2>/dev/null", timeout=10)
            if (boot_id in gate_probe and "GATE_RESULT|" in gate_probe and "FINAL|" in gate_probe
                    and "WATCHDOG_EXIT|" in gate_probe and "WATCHDOG|" in gate_probe):
                gate_complete = True
                break
            time.sleep(3)

        reader_status = stop_reader(reader, device, collector_remote_root, output_dir)
        collector_state["done"] = True
        reader_exit = reader_status.get("returncode", -1)
        reader_done = True

        remote_files = ("stage3_context.tsv", "main_timing.tsv", "events.tsv", "requests.tsv",
            "reads.tsv", "decisions.tsv", "focus.tsv", "stdout.log", "stderr.log", "native.rc",
            "plan.tsv", "plan.tsv.claimed", "plan.tsv.uid_verified", "identity.tsv",
            "gate.events.tsv", "gate.watchdog.tsv", "gate_launcher.log")
        collection_errors = []
        for name in remote_files:
            try:
                device.receive(remote_root + "/" + name, output_dir / name)
            except Exception as exc:
                collection_errors.append({"file": name, "error": type(exc).__name__})
        raw_gate_events = (output_dir / "gate.events.tsv").read_text(encoding="utf-8", errors="replace") \
            if (output_dir / "gate.events.tsv").is_file() else ""
        raw_watchdog = (output_dir / "gate.watchdog.tsv").read_text(encoding="utf-8", errors="replace") \
            if (output_dir / "gate.watchdog.tsv").is_file() else ""
        gate_restore = device.shell("cat /proc/sys/kernel/random/boot_id; cat /proc/sys/kernel/hyperhold/enable; "
                                    "cat /sys/block/zram0/hyperhold_enable 2>/dev/null", timeout=10)
        preflight = next((line for line in raw_gate_events.splitlines() if line.startswith("PREFLIGHT|")), "")
        gate_result = next((line for line in raw_gate_events.splitlines() if line.startswith("GATE_RESULT|")), "")
        coverage = next((line for line in raw_gate_events.splitlines() if line.startswith("GATE_IO_COVERAGE|")), "")
        def key_values(line: str) -> dict[str, str]:
            fields = line.split("|")
            result = {}
            for item in fields[2:]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    result[key] = value
            return result
    except BaseException as exc:
        emergency = {"error": f"{type(exc).__name__}: {exc}", "remote_root": remote_root,
                     "launch_attempted": launch_attempted, "gate_auto_restore_wait": None,
                     "native_completion_wait": None, "collector_cleanup": None,
                     "final_gate_readback": None, "collection_errors": []}
        if launch_attempted:
            # Never disable/reopen the gate from this host fallback. The device helper
            # owns the fixed deadlines and its independent T+935 watchdog.
            native_done = False
            try:
                current_device = device_boottime(device)
            except Exception:
                current_device = capture_start_boottime + (time.monotonic() - capture_start_host_monotonic)
                emergency["native_completion_clock"] = "host_monotonic_estimate_after_hdc_error"
            native_deadline = time.monotonic() + max(0.0, T + 360.0 - current_device)
            while time.monotonic() < native_deadline:
                try:
                    rc_raw = device.shell("if test -f " + q(remote_root + "/native.rc") +
                        "; then cat " + q(remote_root + "/native.rc") + "; else echo PENDING; fi",
                        timeout=8).strip()
                    if rc_raw != "PENDING":
                        emergency["native_completion_wait"] = {"returncode": int(rc_raw), "completed": True}
                        native_done = True
                        break
                except Exception as poll_exc:
                    emergency["collection_errors"].append("native poll: " + type(poll_exc).__name__)
                time.sleep(2)
            if not native_done:
                emergency["native_completion_wait"] = {"completed": False, "deadline_boottime": T + 360.0}
                try:
                    identity_raw = device.shell("cat " + q(remote_root + "/identity.tsv"),
                                                timeout=8, check=False).strip().split("\t")
                    if (len(identity_raw) == 5 and identity_raw[0] == boot_id and
                            identity_raw[3] == remote_controller and identity_raw[4] == plan_hash):
                        child_pid, child_start = int(identity_raw[1]), int(identity_raw[2])
                        stat_start = start_ticks(device, child_pid)
                        child_exe = device.shell(f"readlink /proc/{child_pid}/exe", check=False).strip()
                        if stat_start == child_start and child_exe == remote_controller:
                            device.shell(f"kill -TERM {child_pid}", check=False, timeout=8)
                            emergency["owned_controller_termination"] = {"pid": child_pid,
                                "start_ticks": child_start, "exe": child_exe,
                                "signal": "TERM", "identity_verified": True}
                        else:
                            emergency["owned_controller_termination"] = {
                                "refused": True, "reason": "identity changed", "pid": child_pid,
                                "observed_start_ticks": stat_start, "observed_exe": child_exe}
                except Exception as kill_exc:
                    emergency["collection_errors"].append("owned controller cleanup: " + type(kill_exc).__name__)
            try:
                current_device = device_boottime(device)
            except Exception:
                current_device = capture_start_boottime + (time.monotonic() - capture_start_host_monotonic)
                emergency["gate_wait_clock"] = "host_monotonic_estimate_after_hdc_error"
            gate_deadline = time.monotonic() + max(0.0, T + 965.0 - current_device)
            gate_done = False
            while time.monotonic() < gate_deadline:
                try:
                    boot_now = device.shell("cat /proc/sys/kernel/random/boot_id", timeout=8).strip()
                    if boot_now != boot_id:
                        emergency["gate_auto_restore_wait"] = {"completed": False, "error": "device_boot_changed"}
                        break
                    probe = device.shell("tail -n 8 " + q(remote_prefix + ".events.tsv") +
                        " 2>/dev/null; echo --WATCHDOG--; tail -n 4 " +
                        q(remote_prefix + ".watchdog.tsv") + " 2>/dev/null", timeout=8)
                    if "FINAL|" in probe and "WATCHDOG_EXIT|" in probe and "WATCHDOG|" in probe:
                        gate_done = True
                        emergency["gate_last_probe"] = probe
                        break
                except Exception as poll_exc:
                    emergency["collection_errors"].append("gate poll: " + type(poll_exc).__name__)
                time.sleep(2)
            emergency["gate_auto_restore_wait"] = emergency.get("gate_auto_restore_wait") or {
                "completed": gate_done, "deadline_boottime": T + 965.0}
            try:
                emergency["final_gate_readback"] = device.shell(
                    "cat /proc/sys/kernel/random/boot_id; cat /proc/sys/kernel/hyperhold/enable; "
                    "cat /sys/block/zram0/hyperhold_enable 2>/dev/null", timeout=8)
            except Exception as poll_exc:
                emergency["collection_errors"].append("gate readback: " + type(poll_exc).__name__)
            for name in ("native.rc", "stdout.log", "stderr.log", "identity.tsv",
                         "events.tsv", "requests.tsv", "reads.tsv", "decisions.tsv", "focus.tsv",
                         "gate.events.tsv", "gate.watchdog.tsv", "gate_launcher.log"):
                try:
                    device.receive(remote_root + "/" + name, output_dir / ("emergency_" + name))
                except Exception as file_exc:
                    emergency["collection_errors"].append(name + ": " + type(file_exc).__name__)
        if not reader_done:
            try:
                emergency["collector_cleanup"] = stop_reader(reader, device, collector_remote_root, output_dir)
                collector_state["done"] = True
            except Exception as reader_exc:
                emergency["collector_cleanup"] = {"terminated": False,
                    "error": f"{type(reader_exc).__name__}: {reader_exc}"}
        (output_dir / "native_emergency_finalization.json").write_text(
            json.dumps(emergency, indent=2) + "\n", encoding="utf-8")
        raise

    preflight_fields = key_values(preflight)
    result_fields = key_values(gate_result)
    coverage_fields = key_values(coverage)
    gate_audit_valid = bool(
        preflight_fields.get("boot_match") == "1" and preflight_fields.get("proc") == "enable" and
        preflight_fields.get("reclaim_in") == "enable" and
        preflight_fields.get("write_total_size", "unknown").isdecimal() and
        result_fields.get("valid") == "1" and
        result_fields.get("close_readback_verified") == "1" and
        result_fields.get("open_readback_verified") == "1" and
        result_fields.get("io_write_counter_stable") == "1" and
        coverage_fields.get("write_counter_stable") == "1" and
        coverage_fields.get("poll_count") == coverage_fields.get("expected_poll_count") == "1862" and
        "GATE_INVALID|" not in raw_gate_events and "PREFLIGHT|" in raw_gate_events and
        "GATE_HELPER|" in raw_gate_events and "GATE_RESULT|" in raw_gate_events and
        "WATCHDOG_EXIT|" in raw_gate_events and "FINAL|" in raw_gate_events and
        "WATCHDOG|" in raw_watchdog)
    result = {"registration": registration, "native_returncode": rc,
        "native_completed": rc is not None, "gate_terminal_evidence": gate_complete,
        "gate_last_probe": gate_probe, "gate_restore_readback": gate_restore,
        "gate_events_sha256": sha(output_dir / "gate.events.tsv") if (output_dir / "gate.events.tsv").is_file() else None,
        "gate_watchdog_sha256": sha(output_dir / "gate.watchdog.tsv") if (output_dir / "gate.watchdog.tsv").is_file() else None,
        "gate_has_preflight": "PREFLIGHT|" in raw_gate_events,
        "gate_has_final": "FINAL|" in raw_gate_events,
        "watchdog_has_terminal": "WATCHDOG_EXIT|" in raw_gate_events and "WATCHDOG|" in raw_watchdog,
        "gate_audit": {"valid": gate_audit_valid, "preflight": preflight_fields,
            "result": result_fields, "io_coverage": coverage_fields,
            "interpretation": gate_interpretation(gate_audit_valid, raw_gate_events)},
        "collection_errors": collection_errors,
        "kernel_collector": {"path": str(collector.resolve()), "sha256": sha(collector),
            "remote_pid": reader.pid, "remote_start_ticks": reader.start_ticks,
            "reader_returncode": reader_exit,
            "raw_path": str((output_dir / "kernel_dmesg.raw.log").resolve()),
            "raw_sha256": sha(output_dir / "kernel_dmesg.raw.log")},
        "coverage_through_t_plus_930": rc is not None and gate_complete and gate_audit_valid,
        "device_execution": "completed" if rc == 0 and gate_complete and gate_audit_valid and not collection_errors else "invalid"}
    raw_kernel = (output_dir / "kernel_dmesg.raw.log").read_text(encoding="utf-8", errors="replace")
    import re
    start_capture = capture_start_boottime
    end_capture = T + 930.0
    oom_records, renderer_records, victim_rows = [], [], {}
    seen_sequences = set()
    for line in raw_kernel.splitlines():
        stamp = re.match(r"^\[(\d+)\.(\d{6})\]\s+\[kmsg_seq=(\d+)\]\s+(.*)$", line)
        if not stamp:
            continue
        event_at = int(stamp.group(1)) + int(stamp.group(2)) / 1_000_000
        sequence, message = int(stamp.group(3)), stamp.group(4)
        if event_at < start_capture or event_at > end_capture:
            continue
        if re.search(r"oom-kill:|memory cgroup out of memory|out of memory: killed process|killed process |\[mem_oom:.*out of memory", message, re.I):
            if sequence not in seen_sequences:
                oom_records.append({"boottime": event_at, "kmsg_seq": sequence, "line": line})
                seen_sequences.add(sequence)
            victim = re.search(r"(?:killed process|kill process)\s+(\d+)\s*\(([^)]*)\)", message, re.I)
            if victim:
                victim_rows.setdefault((int(victim.group(1)), victim.group(2)), {
                    "pid": int(victim.group(1)), "comm": victim.group(2), "first_boottime": event_at,
                    "first_kmsg_seq": sequence})
        elif re.search(r"(browser|renderer|webview).*(process has exited|exited|died|crash)", message, re.I):
            renderer_records.append({"boottime": event_at, "kmsg_seq": sequence, "line": line})
    result["kernel_oom_evidence"] = {"records": oom_records,
        "record_count_unique_kmsg_sequences": len(oom_records),
        "unique_victims": list(victim_rows.values()), "unique_victim_count": len(victim_rows),
        "capture_window_boottime": [start_capture, end_capture],
        "count_is_kernel_evidence_not_renderer_exit": True}
    result["renderer_exit_diagnostics"] = renderer_records
    result["primary_outcome"] = "kernel_oom_kill"
    result["renderer_exit_is_oom"] = False
    (output_dir / "native_collection.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
