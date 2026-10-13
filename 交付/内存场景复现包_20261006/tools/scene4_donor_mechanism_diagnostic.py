from __future__ import annotations

"""Diagnostic-only two-donor oracle observation; never launches target apps."""
import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
from memscenes.device import Device, quote
from memscenes import runner as r
from memscenes.model import proc_identity_mismatch

ROOT = PACKAGE_ROOT
WORK_ROOT = ROOT.parents[1]
RESULT_ROOT = WORK_ROOT / "results" / "oracle_followup_20261008_0340"
PROJECT_ROOT = WORK_ROOT / "常驻服务代码" / "memshift_cpp" / "memshift_cpp"
DEFAULT_HDC = Path(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe")
DEFAULT_DAEMON = PROJECT_ROOT / "build-ohos-aarch64" / "memshiftd"
ORIGINAL = {
    "exe": "/data/local/tmp/memshift_validation_20261007/full_chain/memshiftd",
    "config": "/data/local/tmp/memshift_validation_20261007/full_chain/resident.ini",
    "control": "/data/local/tmp/memshift_validation_20261007/full_chain/resident_control.conf",
    "exe_sha256": "cae17800a71ae5e7953050d0fffc8e2588c364c2d3f3257f49eb2abce67d25ab",
    "config_sha256": "7cbc072be80325cacb6149092507a87aaf893d92f920b4f93ab4a340e1655cb5",
    "control_sha256": "5062cae48b51ed17573558e265b80bed7905001e6cb2024aee3b03290becb80d",
    "boot_id": "e09e5885-8d15-40db-8965-d9a0135030dd",
}
BUNDLES = ("io.memscene.scene4.workload.a1", "io.memscene.scene4.workload.a2")
DONOR_MB = 3072
SYNTHETIC_NEEDED = 6 * 6400 * 1024 * 1024


def control_content_for_mode(mode: str, phase: str = "baseline") -> str:
    if mode not in ("active", "paused-a", "runner"):
        raise ValueError(f"unsupported service diagnostic mode: {mode}")
    if phase not in ("baseline", "optimized"):
        raise ValueError(f"unsupported runner phase: {phase}")
    paused = "true" if mode == "paused-a" or (mode == "runner" and phase == "baseline") else "false"
    return f"paused={paused}\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n"


def validate_bound_scene4_runner_config(path: Path, *, schedule: str, ledger: str,
        control: str, clock_helper: str, clock_sha256: str, hdc: str,
        device_id: str) -> dict:
    bound = json.loads(path.read_text(encoding="utf-8"))
    scene4 = bound.get("scene4", {})
    expected_apps = {f"io.memscene.scene4.workload.a{index}" for index in range(1, 9)}
    if (scene4.get("oracle_schedule_remote_path") != schedule or
            scene4.get("oracle_ledger_remote_path") != ledger or
            scene4.get("oracle_clock_probe_remote_path") != clock_helper or
            scene4.get("oracle_clock_probe_sha256") != clock_sha256 or
            schedule.rsplit("/", 1)[0] + "/control.conf" != control):
        raise ValueError("runner config mutable/helper paths differ from isolated daemon configuration")
    if set(bound.get("test_apps", [])) != expected_apps:
        raise ValueError("runner config does not target exactly the reviewed eight Scene4 packages")
    if str(Path(bound.get("hdc", "")).resolve()).lower() != str(Path(hdc).resolve()).lower():
        raise ValueError("runner config HDC path differs from service wrapper")
    if bound.get("device") != device_id:
        raise ValueError("runner config device differs from service wrapper target")
    return {"path": str(path.resolve()), "schedule_path_match": True,
        "ledger_path_match": True, "control_path_match": True,
        "clock_helper_path_match": True, "clock_helper_sha256_match": True,
        "test_apps_exact": True, "device_match": True, "hdc_match": True}


def runner_launch_evidence(result_dir: Path) -> dict:
    """Count only actual Scene4 Ability request calls in preserved dispatch logs."""
    files = sorted(result_dir.rglob("launch_dispatch_timing.jsonl"))
    evidence = []
    total = 0
    for path in files:
        raw = path.read_bytes()
        parsed = 0
        malformed = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed += 1
                continue
            targets = row.get("targets")
            if isinstance(targets, list):
                parsed += sum(1 for target in targets if isinstance(target, dict) and
                              target.get("request_call_started_host_monotonic_ns") is not None)
        total += parsed
        evidence.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                         "attempted_request_calls": parsed, "malformed_lines": malformed})
    return {"count": total if files and not any(item["malformed_lines"] for item in evidence) else None,
            "source": "launch_dispatch_timing.jsonl request_call_started_host_monotonic_ns",
            "files": evidence, "complete": bool(files) and not any(item["malformed_lines"] for item in evidence)}


def runner_result_metadata(*, runner_mode: bool, runner_config: dict | None = None) -> dict:
    scene4 = (runner_config or {}).get("scene4", {})
    if not runner_mode:
        return {"scope": "diagnostic_only_no_target_launch_no_performance_counts",
                "diagnostic_only": True, "synthetic_future_demand": True,
                "target_launch_count": 0, "file_mib": 0,
                "planned_needed_bytes": SYNTHETIC_NEEDED,
                "planned_needed_semantics": "synthetic future demand; no target app is started"}
    if scene4.get("execution_profile") == "legacy_peak_transition":
        apps = (runner_config or {}).get("test_apps", [])
        peak = scene4.get("peak_mib", scene4.get("single_app_peak_mib"))
        return {"scope": "scene4_legacy_peak_transition_real_runner_execution",
                "diagnostic_only": True, "synthetic_future_demand": False,
                "workload_profile": "legacy_peak_transition",
                "configured_test_app_count": len(apps) if isinstance(apps, list) else None,
                "target_launch_count": None,
                "file_mib": scene4.get("file_mib"),
                "daily_mib": scene4.get("daily_mib"),
                "peak_mib": peak,
                "daily_cold_seconds": scene4.get("daily_cold_seconds"),
                "case_repetitions": scene4.get("case_repetitions"),
                "planned_needed_bytes": None,
                "planned_needed_semantics": "not_applicable; actual legacy peak-transition demand is recorded by the bound runner cases and journals"}
    return {"scope": "scene4_bound_real_runner_execution",
            "diagnostic_only": False, "synthetic_future_demand": False,
            "target_launch_count": None,
            "file_mib": scene4.get("target_file_mib", scene4.get("file_mib")),
            "planned_needed_bytes": scene4.get("planned_needed_bytes"),
            "planned_needed_semantics": "frozen real Scene4 demand from bound runner config"}


def attach_runner_config_preflight(initial_result: dict, preflight: dict | None) -> dict:
    if preflight is not None:
        initial_result["runner_config_preflight"] = preflight
    return initial_result


def invoke_bound_scene4_runner(command: list[str], *, cwd: Path, log_path: Path,
                               result_root: Path, popen_factory=None, on_started=None) -> dict:
    """Run one bound child and preserve execution evidence even for invalid results."""
    popen_factory = popen_factory or subprocess.Popen
    before = {str(path.resolve()) for path in result_root.glob("scene4_*_*") if path.is_dir()}
    started = time.monotonic()
    with log_path.open("wb") as stream:
        child = popen_factory(command, cwd=str(cwd), stdout=stream, stderr=subprocess.STDOUT)
        child_pid = getattr(child, "pid", None)
        child_started = time.monotonic()
        if on_started is not None:
            on_started({"host_pid": child_pid, "started_host_monotonic": child_started,
                        "argv": command, "cwd": str(cwd), "log_path": str(log_path)})
        returncode = child.wait()
    elapsed = time.monotonic() - started
    after = {str(path.resolve()) for path in result_root.glob("scene4_*_*") if path.is_dir()}
    created = sorted(after - before)
    result_obj = None
    result_path = None
    if len(created) == 1:
        candidate = Path(created[0]) / "result.json"
        if candidate.is_file():
            try:
                result_obj = json.loads(candidate.read_text(encoding="utf-8"))
                result_path = str(candidate)
            except (OSError, ValueError):
                result_obj = None
    dispatch = runner_launch_evidence(Path(created[0])) if len(created) == 1 else {
        "count": None, "complete": False, "source": "launch_dispatch_timing.jsonl", "files": []}
    return {"state": "exited", "host_pid": child_pid,
            "started_host_monotonic": child_started, "returncode": returncode,
            "elapsed_seconds": elapsed, "created_result_dirs": created,
            "result_path": result_path,
            "result_status": result_obj.get("status") if isinstance(result_obj, dict) else None,
            "measurement_complete": result_obj.get("measurement_complete") if isinstance(result_obj, dict) else None,
            "execution_complete": isinstance(result_obj, dict) and result_path is not None,
            "target_launch_count": dispatch["count"], "launch_evidence": dispatch}


def runner_wrapper_success(driver_returncode: int, final: dict) -> bool:
    run = final.get("scene4_run", {})
    restore = final.get("service_restore", {})
    return bool(driver_returncode == 0 and run.get("execution_complete") is True
                and run.get("result_path") and restore.get("verified") is True)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def split_ps(raw: str) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        if re.match(r"^\s*PID\b", line, re.I):
            continue
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if m:
            rows.append({"pid": int(m.group(1)), "uid": int(m.group(2)),
                         "name": m.group(3), "args": m.group(4)})
    return rows


class Diag:
    def __init__(self) -> None:
        # The runner's journal helper persists this evidence on the session.
        self.scene4_journal_readback: dict[tuple[str, str], dict] = {}


def device_clock(d: Device, remote_clock: str) -> dict:
    raw = d.shell(quote(remote_clock), timeout=8, check=False).strip()
    values = dict(re.findall(r"([a-z_]+)=([^\s]+)", raw))
    required = ("boottime_s", "monotonic_s", "realtime_s", "clk_tck", "page_size_bytes")
    if not raw or any(k not in values for k in required):
        raise RuntimeError(f"clock helper incomplete: {raw!r}")
    out = {k: float(values[k]) for k in required}
    out["raw"] = raw
    return out


def system_sample(d: Device, clock: dict, out: Path, label: str) -> dict:
    raw = d.shell("cat /proc/meminfo; echo __VMSTAT__; cat /proc/vmstat; "
                  "echo __ZRAM__; for f in /sys/block/zram*/mm_stat; do "
                  "test -r \"$f\" && echo \"$f: $(cat $f)\"; done", timeout=12, check=False)
    path = out / f"system_{label}_{clock['boottime_s']:.6f}.txt"
    path.write_text(raw, encoding="utf-8")
    return {"boottime_s": clock["boottime_s"], "raw_path": path.name,
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(), "raw": raw}


def donor_memcg_raw(d: Device, bundle: str) -> dict:
    snap = r._scene4_memcg_snapshot(d, bundle, include_smaps=False)
    leaf = snap["leaf"]
    extra = d.shell("echo __STAT__; cat " + quote(leaf + "/memory.stat") +
                    "; echo __CURRENT__; cat " + quote(leaf + "/memory.current"), timeout=8, check=False)
    return {"snapshot": snap, "memory_stat_raw": extra}


def current_processes(d: Device) -> list[dict]:
    raw, rows = process_inventory(d)
    return rows


def process_inventory(d: Device) -> tuple[str, list[dict]]:
    raw = d.shell("ps -A -o PID,UID,NAME,ARGS; echo __S4_PS_EXIT__$?",
                  timeout=8, check=False)
    marker = re.search(r"(?m)^__S4_PS_EXIT__(\d+)\s*$", raw)
    if not marker or int(marker.group(1)) != 0:
        raise RuntimeError(f"remote ps exit status missing/nonzero; service state unknown: {raw[:500]!r}")
    raw = raw[:marker.start()].rstrip()
    if not re.search(r"(?im)^\s*PID\s+UID\s+NAME\s+ARGS\s*$", raw):
        raise RuntimeError(f"process inventory header missing; service state unknown: {raw[:500]!r}")
    return raw, split_ps(raw)


def start_ticks(d: Device, pid: int) -> str | None:
    raw = d.shell(f"cat /proc/{pid}/stat", timeout=5, check=False)
    try:
        return str(r.parse_stat(raw)["start_ticks"])
    except Exception:
        return None


def inspect_daemon_identity(d: Device, pid: int, expected_exe: str, expected_config: str) -> dict:
    """Verify executable link and exact argv, bracketed by stable PID/UID/ticks."""
    before = d.inspect_process_identity(pid, timeout=10)
    exe_raw = d.shell(f"readlink /proc/{pid}/exe", timeout=5, check=False).strip()
    # HDC's text channel truncates NUL-delimited cmdline at argv[0]; convert
    # separators remotely before reading so every argument remains verifiable.
    cmdline_raw = d.shell(f"tr '\\000' '\\n' < /proc/{pid}/cmdline", timeout=5, check=False)
    after = d.inspect_process_identity(pid, timeout=10)
    first, last = before.get("identity"), after.get("identity")
    ticks_before = str((first or {}).get("start_ticks", ""))
    ticks_after = str((last or {}).get("start_ticks", ""))
    uid_before = (first or {}).get("uid")
    uid_after = (last or {}).get("uid")
    tokens = [token for token in cmdline_raw.splitlines() if token]
    argv0_ok = bool(tokens and tokens[0] in (expected_exe, Path(expected_exe).name))
    expected_tail = ["--config", expected_config, "--backend", "harmony-v1-ratio", "--apply"]
    args_ok = bool(tokens and tokens[1:] == expected_tail)
    verified = bool(first and last and int((first or {}).get("pid", -1)) == pid
        and int((last or {}).get("pid", -1)) == pid and ticks_before and ticks_before == ticks_after
        and uid_before is not None and uid_before == uid_after and exe_raw == expected_exe
        and argv0_ok and args_ok)
    return {"verified": verified, "pid": pid, "uid_before": uid_before, "uid_after": uid_after,
        "start_ticks_before": ticks_before, "start_ticks_after": ticks_after,
        "exe_readlink": exe_raw, "cmdline_tokens": tokens, "argv0_ok": argv0_ok,
        "args_ok": args_ok, "identity_before_raw": before.get("raw"),
        "identity_after_raw": after.get("raw"), "cmdline_raw": cmdline_raw}


def install_independent_config(template: Path, local_config: Path, remote_root: str,
                               uids: dict[str, int]) -> dict:
    text = template.read_text(encoding="utf-8")
    substitutions = {
        "/data/local/tmp/memshift_validation_20261007/oracle_cold_20261007_luna01_A1024_run2/daemon_logs": remote_root + "/daemon_logs",
        "/data/local/tmp/memshift_validation_20261007/oracle_cold_20261007_luna01_A1024_run2/daemon.lock": remote_root + "/daemon.lock",
        "/data/local/tmp/memshift_validation_20261007/oracle_cold_20261007_luna01_A1024_run2/control.conf": remote_root + "/control.conf",
        "/data/local/tmp/memshift_validation_20261007/oracle_cold_20261007_luna01_A1024_run2/oracle_schedule.tsv": remote_root + "/oracle_schedule.tsv",
        "/data/local/tmp/memshift_validation_20261007/oracle_cold_20261007_luna01_A1024_run2/oracle_action_ledger.csv": remote_root + "/oracle_action_ledger.csv",
        "io.memscene.scene4.workload.a1_20020188": f"io.memscene.scene4.workload.a1_{uids[BUNDLES[0]]}",
        "io.memscene.scene4.workload.a2_20020189": f"io.memscene.scene4.workload.a2_{uids[BUNDLES[1]]}",
    }
    for old, new in substitutions.items():
        if old not in text:
            raise RuntimeError(f"expected template value missing: {old}")
        text = text.replace(old, new)
    local_config.write_text(text, encoding="utf-8", newline="")
    return {"template": str(template), "template_sha256": sha(template),
            "local_config": str(local_config), "local_config_sha256": sha(local_config),
            "substitutions": substitutions, "settings_preserved": True}


def make_wms_snapshot(d: Device, identities: dict[str, dict]) -> tuple[str, dict[str, dict]]:
    table = d.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
    selected = {}
    selected_rows = {}
    for bundle, ident in identities.items():
        pre = r._scene4_wms_window_snapshot(table, "", int(ident["pid"]))
        row = pre.get("list_row")
        if not row:
            selected[bundle] = {"known": False, "reason": "exact_pid_window_missing", "pre": pre}
            continue
        selected_rows[bundle] = row
    bundles = list(selected_rows)
    commands = [f"echo __DONOR_WMS_{index}__; hidumper -s WindowManagerService -a '-w {int(selected_rows[bundle]['win_id'])}'"
                for index, bundle in enumerate(bundles)]
    batch = d.shell("; ".join(commands), timeout=12, check=False) if commands else ""
    markers = [int(x) for x in re.findall(r"(?m)^__DONOR_WMS_(\d+)__\s*$", batch)]
    sections = re.split(r"(?m)^__DONOR_WMS_\d+__\s*$", batch)
    if len(markers) != len(bundles) or len(sections) < len(bundles) + 1:
        for bundle in bundles:
            selected[bundle] = {"known": False, "reason": "batched_wms_detail_missing_marker",
                                "list_row": selected_rows[bundle]}
        return table, selected
    for index, bundle in enumerate(bundles):
        detail = sections[index + 1].strip()
        win = int(selected_rows[bundle]["win_id"])
        selected[bundle] = r._scene4_wms_window_snapshot(table, detail, int(identities[bundle]["pid"]), win)
        selected[bundle]["detail_raw"] = detail
    return table, selected


def proof_round(d: Device, session: Diag, identities: dict[str, dict],
                case_id: str, clock_path: str) -> tuple[float, dict[str, dict]]:
    anchor = device_clock(d, clock_path)
    sample_at = anchor["boottime_s"]
    evidence: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        aa_future = pool.submit(d.shell, "aa dump -r", timeout=8, check=False)
        wms_future = pool.submit(make_wms_snapshot, d, identities)
        futures = {pool.submit(r._scene4_donor_evidence, session, bundle, case_id,
                    identities[bundle], int(d.app(bundle)["uid"]), sample_at,
                    include_smaps=False): bundle for bundle in BUNDLES}
        for future in concurrent.futures.as_completed(futures):
            evidence[futures[future]] = future.result()
        aa_raw = aa_future.result()
        table, windows = wms_future.result()
    now = device_clock(d, clock_path)["boottime_s"]
    rows_out = {}
    for bundle in BUNDLES:
        identity = identities[bundle]
        fields, proof = r._scene4_dynamic_donor_fields(bundle, identity,
            int(d.app(bundle)["uid"]), now, aa_raw, sample_at,
            {"wms_window_snapshot": windows[bundle]}, sample_at,
            evidence[bundle]["cgroup_snapshot"], sample_at,
            evidence[bundle], sample_at)
        ages = {"aa": now-sample_at, "window": now-sample_at,
                "proc": now-sample_at, "lifecycle": now-sample_at}
        fields_map = dict(zip(r.SCENE4_DONOR_DYNAMIC_SUFFIXES, fields))
        valid = bool(proof.get("valid") and all(0 <= age <= 2.5 for age in ages.values()))
        rows_out[bundle] = {"fields": fields_map, "proof": proof, "donor_evidence": evidence[bundle],
            "wms": windows[bundle], "aa_raw": aa_raw, "wms_table": table,
            "sample_anchor_boottime_s": sample_at, "post_batch_boottime_s": now,
            "ages_at_readback_s": ages, "valid": valid}
    return sample_at, rows_out


def schedule_row(event_id: str, case_id: str, launch: float, donors: tuple[str, str],
                 identities: dict[str, dict], ready_rows: dict[str, dict],
                 proof_rows: dict[str, dict]) -> list[str]:
    row = [event_id, case_id, f"{launch:.9f}", str(SYNTHETIC_NEEDED)]
    for bundle in donors:
        uid = int(identities[bundle]["uid"])
        ready = ready_rows[bundle]
        cold_at = r._scene4_event_time(ready)
        jrows = proof_rows[bundle]["donor_evidence"]["journal_rows"]
        enters = [e for e in r._scene4_identity_events(jrows, int(identities[bundle]["pid"]), uid,
                     identities[bundle]["start_ticks"]) if e.get("event") == "donor_background_enter"]
        heartbeats = [e for e in r._scene4_identity_events(jrows, int(identities[bundle]["pid"]), uid,
                          identities[bundle]["start_ticks"]) if e.get("event") == "donor_background_heartbeat"]
        if not enters or not heartbeats or cold_at is None:
            raise RuntimeError(f"missing lifecycle rows for {bundle}")
        bg_at, hb_at = r._scene4_event_time(enters[-1]), r._scene4_event_time(heartbeats[-1])
        # The daemon protocol accepts canonical booleans only; numeric 1/0 is rejected.
        base = [bundle, str(uid), str(identities[bundle]["pid"]), str(identities[bundle]["start_ticks"]),
                "true", "true", f"{cold_at:.9f}", f"{bg_at:.9f}", f"{hb_at:.9f}"]
        dynamic = [proof_rows[bundle]["fields"][key] for key in r.SCENE4_DONOR_DYNAMIC_SUFFIXES]
        row.extend(base + dynamic)
    if len(row) != 48:
        raise RuntimeError(f"schedule row size mismatch {len(row)}")
    return row


def write_schedule(d: Device, local: Path, remote: str, header: list[str], values: list[str]) -> str:
    tmp = local.with_suffix(local.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="|", lineterminator="\n")
        w.writerow(header); w.writerow(values)
    payload = tmp.read_text(encoding="utf-8")
    d.call("file", "send", str(tmp), remote + ".tmp", timeout=45)
    readback = d.shell("mv -f " + quote(remote + ".tmp") + " " + quote(remote) +
                       " && cat " + quote(remote), timeout=10, check=False)
    if readback.strip() != payload.strip():
        raise RuntimeError("schedule atomic write/readback mismatch")
    return hashlib.sha256(payload.encode()).hexdigest()


def copy_remote(d: Device, remote: str, local: Path) -> dict:
    presence = d.shell("test -r " + quote(remote) + " && echo yes || echo no", check=False).strip()
    if presence != "yes":
        return {"remote": remote, "exists": False}
    d.receive(remote, local)
    return {"remote": remote, "exists": True, "local": str(local), "sha256": sha(local),
            "bytes": local.stat().st_size}


def verify_original_state(d: Device, expected_boot: str) -> dict:
    boot = d.shell("cat /proc/sys/kernel/random/boot_id", check=False).strip()
    if not boot or boot != expected_boot:
        raise RuntimeError(f"boot_id missing/changed during restore: {boot!r}")
    raw_ps, rows_all = process_inventory(d)
    rows = [p for p in rows_all if p["name"] == "memshiftd"]
    control = d.shell("cat " + quote(ORIGINAL["control"]), check=False)
    hashes = {
        "exe": d.shell("sha256sum " + quote(ORIGINAL["exe"]), check=False).lower(),
        "config": d.shell("sha256sum " + quote(ORIGINAL["config"]), check=False).lower(),
        "control": d.shell("sha256sum " + quote(ORIGINAL["control"]), check=False).lower(),
    }
    expected_control = "paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0"
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one daemon after restore, got {rows!r}")
    row = rows[0]
    process_identity = inspect_daemon_identity(d, int(row["pid"]), ORIGINAL["exe"], ORIGINAL["config"])
    if not process_identity["verified"]:
        raise RuntimeError("restored daemon executable/cmdline/stat identity did not verify")
    for key, expected in (("exe", ORIGINAL["exe_sha256"]),
                          ("config", ORIGINAL["config_sha256"]),
                          ("control", ORIGINAL["control_sha256"])):
        if expected not in hashes[key]:
            raise RuntimeError(f"restored {key} hash mismatch: {hashes[key]!r}")
    if control.strip() != expected_control:
        raise RuntimeError(f"restored control mismatch: {control!r}")
    ticks = start_ticks(d, int(row["pid"]))
    if not ticks:
        raise RuntimeError("restored process start_ticks unavailable")
    return {"verified": True, "boot_id": boot, "process": row, "process_identity": process_identity,
            "start_ticks": ticks,
            "control": control, "hashes": hashes}


ACCEPTED_WRITE_TERMINAL_STATUSES = {
    "write_accepted_feedback_measured",
    "write_accepted_feedback_measured_positive_rss_observation",
    "write_accepted_feedback_unknown",
    "write_accepted_feedback_unknown_after_exception",
    "write_accepted_identity_or_background_changed_postwrite",
    "write_accepted_feedback_nonpositive_halt",
}


def accepted_write_records(ledger_path: Path) -> dict[str, dict]:
    """Return fully accepted unique actions, excluding reservations and partial/failed writes.

    A force_shrink_all payload is the decimal requested ratio with no newline, so
    expected write_length is len(str(requested_ratio)), not a fixed two bytes.
    Duplicate ledger rows collapse by action_id; conflicting terminal rows fail closed.
    """
    if not ledger_path.exists() or ledger_path.stat().st_size == 0:
        return {}
    with ledger_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    terminals: dict[str, list[dict]] = {}
    invalid: set[str] = set()
    for row in rows:
        status = (row.get("status") or row.get("outcome") or "").strip()
        action_id = (row.get("action_id") or "").strip()
        if not action_id or status not in ACCEPTED_WRITE_TERMINAL_STATUSES:
            continue
        ratio_text = (row.get("requested_ratio") or row.get("ratio") or "").strip()
        write_text = (row.get("write_length") or row.get("write_len") or "").strip()
        errno_text = (row.get("errno") or row.get("error_no") or row.get("errorNo") or "").strip()
        if not re.fullmatch(r"(?:[1-9]|[1-9][0-9]|100)", ratio_text):
            invalid.add(action_id)
            continue
        try:
            ratio = int(ratio_text)
            write_len = int(write_text)
            error_no = int(errno_text)
        except ValueError:
            invalid.add(action_id)
            continue
        if error_no != 0 or write_len != len(str(ratio)):
            invalid.add(action_id)
            continue
        terminals.setdefault(action_id, []).append({
            "action_id": action_id,
            "bundle": (row.get("donor_bundle") or "").strip(),
            "requested_ratio": ratio,
            "write_length": write_len,
            "errno": error_no,
            "status": status,
        })
    accepted: dict[str, dict] = {}
    for action_id, candidates in terminals.items():
        if action_id in invalid:
            continue
        signatures = {(row["bundle"], row["requested_ratio"], row["write_length"], row["errno"])
                      for row in candidates}
        if len(signatures) != 1:
            continue
        accepted[action_id] = candidates[-1]
    return accepted


def accepted_write_ids(ledger_path: Path) -> list[str]:
    return sorted(accepted_write_records(ledger_path))


def accepted_write_bundles(ledger_path: Path) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for action_id, record in accepted_write_records(ledger_path).items():
        bundle = record["bundle"]
        if bundle in BUNDLES:
            found.setdefault(bundle, set()).add(action_id)
    return {bundle: sorted(ids) for bundle, ids in found.items()}


def validate_written_donors(d: Device, out: Path, donor_case_id: str,
                            identities: dict[str, dict], ledger: Path,
                            clock_path: str) -> dict:
    accepted = accepted_write_bundles(ledger)
    report = {"performed": bool(accepted), "accepted_write_ids_by_bundle": accepted,
              "outside_main_window": True, "donors": {}}
    for bundle, action_ids in accepted.items():
        expected = identities[bundle]
        before = r._scene4_memcg_snapshot(d, bundle, include_smaps=False)
        members = before.get("processes", {})
        key = f"{expected['pid']}:{expected['start_ticks']}"
        proc = members.get(key)
        if not proc or not proc.get("verified") or int(proc.get("uid", -1)) != int(expected["uid"]):
            report["donors"][bundle] = {"status": "unknown_identity_not_same_surviving_process",
                "before_snapshot": before, "action_ids": action_ids}
            continue
        before_clock = device_clock(d, clock_path)
        before_raw, before_rows = d.journal(bundle, donor_case_id)
        append_jsonl(out / "post_window_validation.jsonl", {"phase": "before_request",
            "bundle": bundle, "clock": before_clock, "identity": expected,
            "memcg": before, "journal_rows": len(before_rows)})
        result = {"action_ids": action_ids, "identity": expected,
                  "before_clock": before_clock, "before_memcg": before,
                  "request_output": None, "status": "requested_waiting"}
        report["donors"][bundle] = result
        result["request_output"] = d.launch(bundle, params={"op": "donor_validate", "case_id": donor_case_id}, timeout=30)
        deadline = time.monotonic() + 180.0
        matched_end = None
        latest_raw, latest_rows = before_raw, before_rows
        while time.monotonic() < deadline:
            latest_raw, latest_rows = d.journal(bundle, donor_case_id)
            for row in reversed(latest_rows):
                if row.get("event") == "donor_validation_end" and int(row.get("pid", -1)) == int(expected["pid"]) \
                        and str(row.get("start_ticks", "")) == str(expected["start_ticks"]):
                    matched_end = row
                    break
            if matched_end is not None:
                break
            time.sleep(1.0)
        (out / "journals" / f"{bundle}.jsonl").write_text(latest_raw, encoding="utf-8")
        after = r._scene4_memcg_snapshot(d, bundle, include_smaps=False)
        after_clock = device_clock(d, clock_path)
        result.update({"validation_end": matched_end,
            "status": "completed" if matched_end and matched_end.get("donor_validation_complete") is True else
                     "completed_checksum_mismatch_or_failed" if matched_end else "unknown_timeout_no_end",
            "after_clock": after_clock, "after_memcg": after,
            "same_process_after": bool(f"{expected['pid']}:{expected['start_ticks']}" in after.get("processes", {})),
            "validation_faults": ({"minor_before": matched_end.get("donor_validation_minor_before"),
                                   "minor_after": matched_end.get("donor_validation_minor_after"),
                                   "major_before": matched_end.get("donor_validation_major_before"),
                                   "major_after": matched_end.get("donor_validation_major_after")} if matched_end else None)})
        append_jsonl(out / "post_window_validation.jsonl", {"phase": "after_request", **result})
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hdc", default=str(DEFAULT_HDC))
    ap.add_argument("--target", default="3QC0124C03000514")
    ap.add_argument("--daemon", default=str(DEFAULT_DAEMON))
    ap.add_argument("--config-template", default=str(WORK_ROOT / "results" / "scene4_oracle_cold_20261007_luna01" / "A1024_service_run2" / "resident_oracle_run2.ini"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--runner-config", default=None,
                    help="exact bound config used by run.py when --mode runner")
    ap.add_argument("--phase", choices=("baseline", "optimized"), default="baseline")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--mode", choices=("active", "paused-a", "runner"), default="active",
                    help="runner invokes the real bound Scene4 config while this isolated daemon is the sole service")
    args = ap.parse_args()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    run_id = args.run_id or ("s4_" + secrets.token_hex(6) + "_c0001")
    if not re.fullmatch(r"s4_[0-9a-f]{12}_c0001", run_id):
        raise ValueError("run-id must be s4_<12 lowercase hex>_c0001")
    event_id = run_id + "_diag"
    out = Path(args.output_dir) if args.output_dir else RESULT_ROOT / ("donor_mechanism_" + stamp + "_" + run_id[3:15])
    out.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.runner_config) if args.runner_config else (
        ROOT / "config" / "scenarios_scene4_oracle_cold_A1024_20261008.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bundles = BUNDLES
    runner_bundles = tuple(f"io.memscene.scene4.workload.a{index}" for index in range(1, 9))
    owned_bundles = runner_bundles if args.mode == "runner" else bundles
    remote_root = "/data/local/tmp/memshift_validation_20261008/oracle_followup_" + run_id[3:15]
    session_remote = "/data/local/tmp/memscenes_" + run_id
    remote_daemon = remote_root + "/memshiftd"
    remote_config = remote_root + "/resident.ini"
    control_remote = remote_root + "/control.conf"
    schedule_remote = remote_root + "/oracle_schedule.tsv"
    ledger_remote = remote_root + "/oracle_action_ledger.csv"
    local_config = out / "resident_donor_diag.ini"
    metadata_path = out / "diagnostic_metadata.json"
    scene_cfg = config["scene4"]
    scene_cfg["oracle_clock_probe_remote_path"] = remote_root + "/clock_probe_clkinfo"
    clock_local = WORK_ROOT / "results" / "scene4_oracle_cold_20261007_luna01" / "scene4_clock_domain_probe_clkinfo"
    scene_cfg["oracle_clock_probe_sha256"] = sha(clock_local)
    installed_uids = {b: 20020188 if b == BUNDLES[0] else 20020189 for b in bundles}
    config_proof = install_independent_config(Path(args.config_template), local_config, remote_root, installed_uids)
    local_ini_text = local_config.read_text(encoding="utf-8")
    def ini_uint(key: str, default: int) -> int:
        match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(\d+)\s*$", local_ini_text)
        return int(match.group(1)) if match else default
    oracle_batch_mib = ini_uint("oracle_batch_mb", 512)
    oracle_max_ratio = ini_uint("oracle_max_ratio", 30)
    oracle_feedback_delay_ms = ini_uint("oracle_feedback_delay_ms", 0)
    oracle_ratio_semantics_match = re.search(r"(?m)^oracle_ratio_semantics\s*=\s*([A-Za-z0-9_]+)\s*$", local_ini_text)
    oracle_ratio_semantics = oracle_ratio_semantics_match.group(1) if oracle_ratio_semantics_match else "resident_incremental"
    oracle_global_cap_mib = ini_uint("oracle_global_cap_mb", 4096)
    oracle_case_cap_mib = ini_uint("oracle_case_cap_mb", 4096)
    oracle_app_cap_mib = ini_uint("oracle_app_cap_mb", 2048)
    oracle_cumulative_keep_mib = ini_uint("oracle_cumulative_keep_mb", 512)
    runner_config_preflight = None
    if args.mode == "runner":
        if not args.runner_config:
            raise ValueError("runner mode requires --runner-config before any device switch")
        bound_path = Path(args.runner_config).resolve()
        if not bound_path.is_file():
            raise ValueError("bound Scene4 runner config does not exist")
        bound_proof = validate_bound_scene4_runner_config(bound_path,
            schedule=schedule_remote, ledger=ledger_remote, control=control_remote,
            clock_helper=str(scene_cfg["oracle_clock_probe_remote_path"]),
            clock_sha256=str(scene_cfg["oracle_clock_probe_sha256"]), hdc=args.hdc,
            device_id=args.target)
        runner_config_preflight = {**bound_proof, "sha256": sha(bound_path),
            "phase": args.phase, "control_expected": "paused=true" if args.phase == "baseline" else "paused=false",
        }
    if args.prepare_only:
        atomic_json(metadata_path, {"scope": "diagnostic_only_no_target_launch_no_performance_counts",
            "diagnostic_only": True, "synthetic_future_demand": True, "target_launch_count": 0,
            "run_id": run_id, "event_id": event_id, "planned_needed_bytes": SYNTHETIC_NEEDED,
            "planned_needed_semantics": "synthetic six target peak demand to exercise normal oracle action policy only; no target application launch or performance count",
            "launch_lead_seconds": 30, "config_proof": config_proof,
            "local_config": str(local_config), "remote_config": remote_config,
            "remote_root": remote_root, "remote_daemon": remote_daemon,
            "control_file": control_remote, "schedule_file": schedule_remote,
            "v2_ledger": ledger_remote, "v3_ledger": ledger_remote + ".timing_v3.csv",
            "clock_helper": str(clock_local), "clock_helper_sha256": sha(clock_local),
            "mode": args.mode,
            "settings": {"donor_peak_mib": 3072, "file_mib": 0, "initial_read_once": True,
                "background_minimize": "verified focus->EnhanceMinimizeBtn->AA BACKGROUND+WMS invisible",
                "max_ratio_per_action": oracle_max_ratio, "oracle_batch_mb": oracle_batch_mib,
                "ratio_semantics": oracle_ratio_semantics,
                "ratio_semantics": oracle_ratio_semantics,
                "oracle_feedback_delay_ms": oracle_feedback_delay_ms,
                "oracle_global_cap_mb": oracle_global_cap_mib, "oracle_case_cap_mb": oracle_case_cap_mib,
                "oracle_app_cap_mb": oracle_app_cap_mib, "rolling_global_mb_per_minute": 512,
                "oracle_cumulative_keep_mb": oracle_cumulative_keep_mib,
                "rolling_app_mb_per_minute": 512, "donor_min_keep_mb": 512,
                "swap_reserve_mb": 1024},
            "restore": {"exe": ORIGINAL["exe"], "config": ORIGINAL["config"],
                "control": ORIGINAL["control"], "original_args": "--config resident.ini --backend harmony-v1-ratio --apply",
                "original_control_expected": "paused=false"}})
        return 0
    device = Device(args.hdc, args.target)
    session = Diag()
    session.device = device; session.config = config; session.id = run_id
    session.output = out; session.remote = session_remote
    session.result = {"scene4_clock_domain": {"trace_clock_raw": ""}}
    # The runner's journal helper records byte-integrity/readback provenance
    # on the session itself.  This lightweight diagnostic intentionally avoids
    # constructing a full Session (which would create a separate run directory
    # and mutate device paths), but must provide the same collector slot.
    session.scene4_journal_readback = {}
    session.scene4_clock_probe_verified = True
    session.scene4_trace_clock_verified = True

    r._scene4_device_boottime_s = lambda _session: device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"]))["boottime_s"]
    initial_paused = args.mode == "paused-a" or (args.mode == "runner" and args.phase == "baseline")
    control_contents = control_content_for_mode(args.mode, args.phase)
    runner_metadata = runner_result_metadata(runner_mode=args.mode == "runner", runner_config=config)
    legacy_peak_transition = config.get("scene4", {}).get("execution_profile") == "legacy_peak_transition"
    result: dict = {**runner_metadata, "mode": args.mode,
        "event_id": event_id if args.mode != "runner" else None,
        "driver_run_id": run_id, "case_id": run_id,
        "launch_lead_seconds": None if legacy_peak_transition else 30,
        "donors": {}, "actions": [], "raw_remote": {},
        "settings": {"donor_peak_mib": None if legacy_peak_transition else 3072,
            "file_mib": runner_metadata["file_mib"], "initial_read_once": None if legacy_peak_transition else True,
            "target_peak_mib": scene_cfg.get("target_peak_mib", scene_cfg.get("peak_mib")) if args.mode == "runner" else None,
            "target_daily_mib": scene_cfg.get("target_daily_mib", scene_cfg.get("daily_mib")) if args.mode == "runner" else None,
            "background_minimize": "not_applicable_to_legacy_eight_app_peak_transition" if legacy_peak_transition else "verified focus->EnhanceMinimizeBtn->AA BACKGROUND+WMS invisible",
            "max_ratio_per_action": oracle_max_ratio, "oracle_batch_mb": oracle_batch_mib,
            "oracle_feedback_delay_ms": oracle_feedback_delay_ms, "batch_mb": 512, "keep_mb": 512,
            "oracle_cumulative_keep_mb": oracle_cumulative_keep_mib,
            "oracle_global_cap_mb": oracle_global_cap_mib, "oracle_case_cap_mb": oracle_case_cap_mib,
            "oracle_app_cap_mb": oracle_app_cap_mib, "rolling_global_mb_per_minute": 512,
            "rolling_app_mb_per_minute": 512, "swap_reserve_mb": 1024},
        "oracle_reclaim_applied": bool(args.mode == "runner" and args.phase == "optimized"),
        "actual_target_timeline": None, "formal_event_counts": None}
    attach_runner_config_preflight(result, runner_config_preflight)
    atomic_json(out / "diagnostic_initial_state.json", result)
    original_started = False
    original_stop_attempted = False
    diagnostic_started = False
    owned_launches: set[str] = set()
    device_boot = None
    original_pid = None
    schedule_created = False
    first_launch = None
    schedule_rows = 0
    diagnostic_pid = None
    identities: dict[str, dict] = {}
    ready_rows: dict[str, dict] = {}
    try:
        daemon_local = Path(args.daemon)
        if not daemon_local.is_file(): raise RuntimeError("V3 daemon binary missing")
        if sha(daemon_local) not in {
            "92ed54d49a05151112aedd76db4c301a65e5bdec37eba578b587b71b1eaea310",
            "1e0c83274ca1a9b1bb2fdd038b9bf3302d33791f3847936ce0021f550e98b619",
            "5d73000551a50b101a442b91a669457a96b6f8106ba141762b6e89d2c8736b5a",
            "a65f113e4d85159fb3c3dc18ec5f07e1ee3369f2d69615d61f8bb9ca62669060",
            "ea56ca0f620726ad17efdb3759e529932e52b73d15774cec9ef5c7de288b9a76",
            "93d7c2071d769efcb57def72c70d77fc4fd5facafae16f3e526577dac3119499",
            "ea9a72a69cb84b80b66dfbf284357d56b86d1894d91f91e20d80c61431b67741",
            "ac66b53e8ec4be889f7005e9f52fd89e7884fda8057802cf9d76377c3b023ee7",
            "fc0c0389591f3a0a95db16b18c200b82d1b3403e1dddb42337ea4706daf0220f",
            "ea9778521142e651434e27812d7fd55bbe8f391d3dd31fc5caf2271d75f6a9f3",
            "eee6217e985c10f22087b7561eff882106fb3244c4b6d5fd617cc6d7a33537fe",
            "ce1f1898c473e161f99356898c4f9ecff33ccabd9580e396d25bd70c1f954e23",
            "e04b8a2ae0bcce2a22c9decf75198b1c8c679a876810a6be204473a9ebbd60c6",
            "93a8b99365551273f550131b2c03c69ca7c360a40d56c05c963e154dd0b08740",
            "4ed88c12e30baf552526230c1eada7cdcddd6d77fcc67b54e88b65c0ac4787f7",
            "4ffca18401d4927da48465110977df3ef942a883480723d0ac7ece9f0a5b0a48",
            "15fd753a9a1a3ada647be495e8c0d32e066abe19b52659c45c5da6109387d277",
            "9fef955ea9165114cfaffd02b0fcb7b0f75e65cee21b18a11bc47973d48b8d8b",
            "a73d00e8a206718de822a7231d3e1c0cf1f0ba70f01951b5f0c0137f3bbece1a",
            "003d212c05601912f1d194633578716ad8fc9fc59f6ff9d09a147791b80641c5",
            "1915eeacdecd107239158bb4d9f84f1d3b7bfb88ac2ba2a54b3c03548cd8975c",
            "4f1ea68dedf34787d6a41cce4b7447ae611899ae251458d294d71e4c7fafdcd9",
            "bb429fed27af673c536e192fa06248de35e0c3184e75393c3bec82ce71f9b487",
            "e010f93ffd830dd450448e9969b6196ecc85049b5bdcc05920bd224d95e5ab91",
            "5032ddb7f9becfc679b54179dd4f00f1d6a30396a5e10487db9c12b13a79a8d7",
            "080a55e4e3c79b5ec2ca81214f2586c00953fe37da9bf76e91eed072d44dfdf1",
            "13d581600a48c2ce288ef26794d0943611639361494552150679ec16bf48567d",
        }:
            raise RuntimeError("daemon binary hash is not a reviewed timing-V3 artifact")
        template = Path(args.config_template)
        installed_uids = {b: int(device.app(b)["uid"]) for b in owned_bundles}
        expected_uids = {BUNDLES[0]: 20020188, BUNDLES[1]: 20020189}
        if {bundle: installed_uids.get(bundle) for bundle in bundles} != expected_uids:
            raise RuntimeError(f"installed donor UID differs from reviewed config mapping: {installed_uids!r}")
        for bundle, uid in expected_uids.items():
            expected_leaf = bundle + "_" + str(uid)
            if expected_leaf not in local_config.read_text(encoding="utf-8"):
                raise RuntimeError(f"independent config does not contain exact installed leaf {expected_leaf}")
        result["config_proof"] = config_proof
        preboot = device.shell("cat /proc/sys/kernel/random/boot_id", check=False).strip()
        if preboot != ORIGINAL["boot_id"]: raise RuntimeError(f"boot_id changed; fresh review needed: {preboot}")
        old_ps, old_rows_all = process_inventory(device)
        old_raw = {"ps": old_ps,
            "control": device.shell("cat " + quote(ORIGINAL["control"]), check=False),
            "exe_hash": device.shell("sha256sum " + quote(ORIGINAL["exe"]), check=False),
            "config_hash": device.shell("sha256sum " + quote(ORIGINAL["config"]), check=False),
            "control_hash": device.shell("sha256sum " + quote(ORIGINAL["control"]), check=False)}
        atomic_json(out / "original_service_before.json", old_raw)
        old_rows = [p for p in old_rows_all if p["name"] == "memshiftd"]
        if len(old_rows) != 1:
            raise RuntimeError(f"original service instance count is not one: {len(old_rows)}")
        original_pid = int(old_rows[0]["pid"])
        original_identity = inspect_daemon_identity(device, original_pid, ORIGINAL["exe"], ORIGINAL["config"])
        old_raw["daemon_identity"] = original_identity
        atomic_json(out / "original_service_before.json", old_raw)
        if not original_identity["verified"]:
            raise RuntimeError("original executable link/exact argv/stat identity did not verify")
        original_ticks = start_ticks(device, original_pid)
        if not original_ticks: raise RuntimeError("original service start_ticks unavailable")
        if old_raw["control"].strip() != "paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0":
            raise RuntimeError("original actual control no longer equals captured paused=false state")
        for path_key, raw_key, expected_key in (("exe", "exe_hash", "exe_sha256"),
                ("config", "config_hash", "config_sha256"),
                ("control", "control_hash", "control_sha256")):
            if ORIGINAL[expected_key] not in old_raw[raw_key].lower():
                raise RuntimeError(f"original {path_key} hash changed since approved baseline")
        if any(device.app_processes(b) for b in owned_bundles):
            raise RuntimeError("owned Scene4 residual process before diagnostic")
        for b in owned_bundles:
            snap = r._scene4_memcg_snapshot(device, b, include_smaps=False)
            if snap["processes"]: raise RuntimeError(f"owned Scene4 leaf not empty before run: {b}")
        device_boot = preboot
        result["preflight"] = {"boot_id": preboot,
            "original_pid": old_rows[0]["pid"], "original_start_ticks": original_ticks,
            "original_control": old_raw["control"], "donor_uids": installed_uids,
            "daemon_sha256": sha(daemon_local)}

        remote_preexist = device.shell("test -e " + quote(remote_root) + " && echo exists || echo absent", check=False).strip()
        if remote_preexist != "absent": raise RuntimeError("unique remote run directory already exists")
        session_preexist = device.shell("test -e " + quote(session_remote) + " && echo exists || echo absent", check=False).strip()
        if session_preexist != "absent": raise RuntimeError("unique UI export directory already exists")
        setup_raw = device.shell("mkdir -p " + quote(remote_root + "/daemon_logs") + " " + quote(session_remote) +
            " && test -d " + quote(remote_root + "/daemon_logs") + " && test -d " + quote(session_remote) +
            " && echo DIAG_SETUP_OK_" + run_id, check=False).strip()
        if setup_raw != "DIAG_SETUP_OK_" + run_id:
            raise RuntimeError(f"unique remote directories not verified: {setup_raw!r}")
        device.call("file", "send", str(daemon_local), remote_daemon, timeout=60)
        device.call("file", "send", str(local_config), remote_config, timeout=45)
        device.call("file", "send", str(clock_local), str(scene_cfg["oracle_clock_probe_remote_path"]), timeout=45)
        device.shell("chmod 700 " + quote(remote_daemon) + " " + quote(str(scene_cfg["oracle_clock_probe_remote_path"])), check=False)
        csha = hashlib.sha256(local_config.read_bytes()).hexdigest()
        if csha not in device.shell("sha256sum " + quote(remote_config), check=False).lower():
            raise RuntimeError("device config hash mismatch")
        if sha(clock_local) not in device.shell("sha256sum " + quote(str(scene_cfg["oracle_clock_probe_remote_path"])), check=False).lower():
            raise RuntimeError("device clock helper hash mismatch")
        if sha(daemon_local) not in device.shell("sha256sum " + quote(remote_daemon), check=False).lower():
            raise RuntimeError("device daemon hash mismatch")
        clock_before = device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"]))
        result["preflight"]["clock"] = clock_before
        device.shell("printf '" + control_contents.replace("\n", "\\n") + "' > " + quote(control_remote) +
                     "; : > " + quote(schedule_remote), check=False)
        if device.shell("cat " + quote(control_remote), check=False).strip() != control_contents.strip():
            raise RuntimeError("diagnostic control readback mismatch")
        # Stop the exact old process only after all independent files are staged and hashed.
        preterm = inspect_daemon_identity(device, original_pid, ORIGINAL["exe"], ORIGINAL["config"])
        if not preterm.get("verified") or preterm["start_ticks_before"] != original_ticks:
            raise RuntimeError("original process identity changed before serialized switch")
        original_stop_attempted = True
        device.shell(f"kill -TERM {original_pid}", check=False)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and start_ticks(device, original_pid) == original_ticks:
            time.sleep(0.25)
        if start_ticks(device, original_pid) == original_ticks: raise RuntimeError("original daemon did not exit after TERM")
        switch_boot = device.shell("cat /proc/sys/kernel/random/boot_id", check=False).strip()
        if not switch_boot or switch_boot != device_boot:
            raise RuntimeError(f"boot_id changed before isolated start: {switch_boot!r}")
        post_term_ps, post_term_rows = process_inventory(device)
        post_term_daemons = [p for p in post_term_rows if p["name"] == "memshiftd"]
        if post_term_daemons:
            raise RuntimeError(f"daemon still present after original TERM; refusing second instance: {post_term_daemons!r}")
        original_started = True
        started = device.shell("nohup " + quote(remote_daemon) + " --config " + quote(remote_config) +
            " --backend harmony-v1-ratio --apply > " + quote(remote_root + "/daemon.stdout.log") +
            " 2>&1 < /dev/null &", timeout=10, check=False)
        time.sleep(1.0)
        prows = current_processes(device)
        active = [p for p in prows if p["name"] == "memshiftd"]
        if len(active) != 1:
            raise RuntimeError(f"isolated daemon instance count is not one: {len(active)}")
        diagnostic_pid = int(active[0]["pid"]); diagnostic_started = True
        diag_ticks = start_ticks(device, diagnostic_pid)
        if not diag_ticks: raise RuntimeError("diagnostic daemon start_ticks missing")
        diag_identity = inspect_daemon_identity(device, diagnostic_pid, remote_daemon, remote_config)
        if not diag_identity["verified"]:
            raise RuntimeError("isolated daemon executable link/exact argv/stat identity did not verify")
        result["switch"] = {"stop_original_output": "TERM exact pid/ticks; process disappearance verified",
            "post_term_boot_id": switch_boot, "post_term_ps_raw": post_term_ps,
            "post_term_daemon_rows": post_term_daemons,
            "diagnostic_start_output": started, "diagnostic_pid": diagnostic_pid,
            "diagnostic_start_ticks": diag_ticks, "diagnostic_args": active[0]["args"],
            "diagnostic_identity": diag_identity,
            "daemon_sha256_remote": device.shell("sha256sum " + quote(remote_daemon), check=False).strip(),
            "config_sha256_remote": device.shell("sha256sum " + quote(remote_config), check=False).strip()}

        if args.mode == "runner":
            if not args.runner_config:
                raise RuntimeError("runner mode requires the exact bound --runner-config")
            runner_config_path = Path(args.runner_config).resolve()
            if not runner_config_path.is_file():
                raise RuntimeError("bound Scene4 runner config is missing")
            runner_command = [sys.executable, str(ROOT / "run.py"), "--config",
                str(runner_config_path), "run", "--scene", "4", "--phase", args.phase,
                "--profile", "development", "--rounds", "1"]
            runner_log = out / "scene4_runner_stdout_stderr.log"
            owned_launches.update(owned_bundles)
            result["scene4_run"] = {"state": "running", "argv": runner_command,
                "cwd": str(ROOT), "config_path": str(runner_config_path),
                "config_sha256": sha(runner_config_path), "phase": args.phase,
                "log_path": str(runner_log), "daemon_pid": diagnostic_pid,
                "daemon_start_ticks": diag_ticks, "daemon_ini_path": str(local_config),
                "daemon_ini_sha256": sha(local_config), "schedule_file": schedule_remote,
                "control_file": control_remote, "ledger_file": ledger_remote,
                "execution_complete": False}
            atomic_json(out / "diagnostic_progress.json", result)
            def persist_runner_started(started: dict) -> None:
                result["scene4_run"].update(started)
                atomic_json(out / "diagnostic_progress.json", result)
            run_evidence = invoke_bound_scene4_runner(runner_command, cwd=ROOT,
                log_path=runner_log, result_root=ROOT / "results",
                on_started=persist_runner_started)
            result["scene4_run"].update(run_evidence)
            launch_count = run_evidence.get("target_launch_count")
            result["target_launch_count"] = launch_count
            result["scene4_run"]["measurement_pending_review"] = bool(
                run_evidence.get("execution_complete") and run_evidence.get("measurement_complete") is not True)
            atomic_json(out / "diagnostic_progress.json", result)
            if not run_evidence.get("execution_complete"):
                raise RuntimeError("bound Scene4 runner did not produce one result artifact; see runner log and lifecycle evidence")
            return 0

        # No other test app is launched; only a1/a2 form the donor pair.
        for b in owned_bundles:
            if r._scene4_memcg_snapshot(device, b, False)["processes"]:
                raise RuntimeError(f"owned Scene4 leaf did not remain empty before runner launch: {b}")
        donor_case_id = run_id
        params_base = {"case_id": donor_case_id, "daily_case_id": donor_case_id + "_daily",
            "daily_mib": DONOR_MB, "peak_mib": DONOR_MB, "file_mib": 0,
            "hold_seconds": 0, "lifetime_seconds": 1200, "seed": 20261005,
            "await_peak": 0, "donor_evidence": 1, "cold_start_ready": 1,
            "window_ready_gate": 1, "donor_mode": 1}
        launch_out = {}
        owned_launches.update(bundles)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(device.launch, b, params=dict(params_base, bundle=b), timeout=60): b for b in bundles}
            for f in concurrent.futures.as_completed(futures): launch_out[futures[f]] = f.result()
        append_jsonl(out / "donor_launches.jsonl", {"host_time": time.time(), "outputs": launch_out,
            "case_id": donor_case_id, "params": params_base, "bundle_uids": installed_uids})
        ready_deadline = time.monotonic() + 180
        while time.monotonic() < ready_deadline and len(ready_rows) < 2:
            for b in bundles:
                if b in ready_rows: continue
                raw, rows = device.journal(b, donor_case_id)
                (out / "journals").mkdir(exist_ok=True)
                (out / "journals" / f"{b}.jsonl").write_text(raw, encoding="utf-8")
                found = next((row for row in reversed(rows) if r._scene4_cold_ready(
                    row, DONOR_MB * 1024 * 1024, donor=True, expected_file_bytes=0)), None)
                if found:
                    ident = r._scene4_donor_ready_identity(device, b, found)
                    if not ident["verified"]: raise RuntimeError(f"donor ready identity failed {b}: {ident}")
                    identities[b] = ident["identity"]
                    identities[b]["uid"] = int(ident["installed_uid"])
                    ready_rows[b] = found
                    result["donors"][b] = {"ready": found, "identity_proof": ident,
                        "initial_memcg": donor_memcg_raw(device, b), "initial_system": system_sample(
                            device, device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"])),
                            out, b.rsplit(".",1)[-1] + "_coldready")}
            time.sleep(0.5)
        if len(ready_rows) != 2: raise RuntimeError("two donor cold_ready events not both observed within 180s")
        # Preserve startup initial-read evidence before any background transition.
        for b in bundles:
            raw, rows = device.journal(b, donor_case_id)
            (out / "journals" / f"{b}.jsonl").write_text(raw, encoding="utf-8")
            result["donors"][b]["initial_journal_events"] = rows
        result["donor_ready_boottime_s"] = {b: r._scene4_event_time(ready_rows[b]) for b in bundles}
        atomic_json(out / "diagnostic_progress.json", result)
        for b in bundles:
            result["donors"][b]["background_transition"] = r._scene4_minimize_donor(
                session, b, donor_case_id, identities[b])
            result["donors"][b]["background_snapshot"] = donor_memcg_raw(device, b)
            result["donors"][b]["background_system"] = system_sample(device,
                device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"])), out,
                b.rsplit(".",1)[-1] + "_background")
            atomic_json(out / "diagnostic_progress.json", result)

        # Create the event only after verified initial donor proofs; deadline remains exact T=registration+30s.
        header = r._scene4_schedule_header()
        proof_log = out / "schedule_proofs.jsonl"
        last_schedule_hash = None
        while True:
            sample_anchor, proofs = proof_round(device, session, identities, donor_case_id,
                str(scene_cfg["oracle_clock_probe_remote_path"]))
            now = device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"]))["boottime_s"]
            if not all(proofs[b]["valid"] for b in bundles):
                append_jsonl(proof_log, {"phase": "proof_rejected", "now_boottime_s": now,
                    "sample_anchor_boottime_s": sample_anchor, "proofs": proofs})
                raise RuntimeError("fresh donor proof not fully qualified; no schedule/action submitted")
            proof_now = device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"]))["boottime_s"]
            if first_launch is None:
                first_launch = proof_now + 30.0
            values = schedule_row(event_id, donor_case_id, first_launch, bundles, identities, ready_rows, proofs)
            if proof_now >= first_launch:
                append_jsonl(proof_log, {"phase": "lead_expired_before_write", "proof_now": proof_now,
                    "launch": first_launch, "proofs": proofs})
                break
            ages = [proof_now - proofs[b]["sample_anchor_boottime_s"] for b in bundles]
            if any(age < 0 or age > 2.5 for age in ages):
                append_jsonl(proof_log, {"phase": "proof_stale_before_schedule_commit", "proof_now": proof_now,
                    "ages": ages, "launch": first_launch, "proofs": proofs})
                raise RuntimeError("proof became stale before schedule commit")
            values[2] = f"{first_launch:.9f}"
            schedule_hash = write_schedule(device, out / "oracle_schedule.tsv", schedule_remote, header, values)
            last_schedule_hash = schedule_hash; schedule_rows += 1; schedule_created = True
            append_jsonl(proof_log, {"phase": "schedule_committed", "event_id": event_id,
                "case_id": donor_case_id, "synthetic_future_demand": True, "target_launch_count": 0,
                "sample_anchor_boottime_s": sample_anchor, "proof_now_boottime_s": proof_now,
                "schedule_commit_host_monotonic": time.monotonic(), "schedule_hash": schedule_hash,
                "donor_proofs": proofs, "T_boottime_s": first_launch})
            if proof_now >= first_launch: break
            time.sleep(max(0.0, min(0.5, first_launch - proof_now)))
            if device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"]))["boottime_s"] >= first_launch:
                break
        result["schedule"] = {"event_id": event_id, "case_id": donor_case_id,
            "planned_needed_bytes": SYNTHETIC_NEEDED, "target_launch_count": 0,
            "first_T_boottime_s": first_launch, "schedule_rows": schedule_rows,
            "last_schedule_sha256": last_schedule_hash, "T_is_registration_plus_seconds": 30,
            "first_donor_background_boottime_s": {b: result["donors"][b]["background_transition"]["background_enter"]["monotonic_ns"] / 1e9 for b in bundles}}
        # At T invalidate this diagnostic schedule so it cannot authorize a later write.
        invalidated = r._scene4_invalidate_schedule(session, schedule_remote)
        result["schedule_invalidation"] = invalidated
        if not invalidated.get("verified"): raise RuntimeError("diagnostic schedule invalidation unverified")
        result["ledger"] = {"v2": copy_remote(device, ledger_remote, out / "oracle_action_ledger.csv"),
            "timing_v3": copy_remote(device, ledger_remote + ".timing_v3.csv", out / "oracle_action_timing_v3.csv"),
            "cumulative_watermark_v1": copy_remote(device, ledger_remote + ".watermark_v1.csv", out / "oracle_action_watermark_v1.csv") if oracle_ratio_semantics=="cumulative_anon_watermark" else None,
            "swap_telemetry_v1": copy_remote(device, ledger_remote + ".swap_telemetry_v1.csv", out / "oracle_action_swap_telemetry_v1.csv")}
        for name in ("system.csv", "apps.csv", "events.csv"):
            result["raw_remote"][name] = copy_remote(device, remote_root + "/daemon_logs/harmony-v1-oracle-scene4/" + name,
                out / f"daemon_{name}")
        result["daemon_events_tail"] = device.shell("tail -n 500 " + quote(remote_root + "/daemon_logs/harmony-v1-oracle-scene4/events.csv"), check=False)
        result["daemon_logs"] = device.shell("ls -l " + quote(remote_root + "/daemon_logs/harmony-v1-oracle-scene4"), check=False)
        result["accepted_write_ids"] = accepted_write_ids(out / "oracle_action_ledger.csv")
        if initial_paused:
            ledger_path = out / "oracle_action_ledger.csv"
            ledger_rows = []
            if ledger_path.exists() and ledger_path.stat().st_size:
                with ledger_path.open("r", encoding="utf-8-sig", newline="") as ledger_file:
                    ledger_rows = list(csv.DictReader(ledger_file))
            statuses = [(row.get("status") or row.get("outcome") or "").strip() for row in ledger_rows]
            registered = any(status == "event_registered_once" for status in statuses)
            paused_refusal = any(status == "rejected:control_paused_or_unknown" for status in statuses)
            unexpected_action_rows = [status for status in statuses
                if status.startswith("reserved_") or status.startswith("write_")]
            v3_path = out / "oracle_action_timing_v3.csv"
            result["paused_a_verification"] = {
                "control_readback": device.shell("cat " + quote(control_remote), check=False),
                "ledger_rows": len(ledger_rows), "ledger_statuses": statuses,
                "event_registered_once": registered, "paused_refusal_seen": paused_refusal,
                "unexpected_action_rows": unexpected_action_rows,
                "accepted_write_ids": result["accepted_write_ids"],
                "v3_exists": bool(v3_path.exists() and v3_path.stat().st_size),
                "valid": bool(registered and paused_refusal and not result["accepted_write_ids"]
                    and not unexpected_action_rows
                    and device.shell("cat " + quote(control_remote), check=False).strip() == control_contents.strip()
                    and v3_path.exists() and v3_path.stat().st_size),
            }
            if not result["paused_a_verification"]["valid"]:
                raise RuntimeError("paused-A service-path diagnostic did not produce registered/refused/no-write/V3 evidence")
        for b in bundles:
            raw, rows = device.journal(b, donor_case_id)
            (out / "journals" / f"{b}.jsonl").write_text(raw, encoding="utf-8")
            result["donors"][b]["final_journal_events"] = rows
            result["donors"][b]["final_memcg"] = donor_memcg_raw(device, b)
            result["donors"][b]["final_system"] = system_sample(device,
                device_clock(device, str(scene_cfg["oracle_clock_probe_remote_path"])), out,
                b.rsplit(".",1)[-1] + "_final")
        result["post_window_validation"] = validate_written_donors(device, out, donor_case_id,
            identities, out / "oracle_action_ledger.csv", str(scene_cfg["oracle_clock_probe_remote_path"]))
        atomic_json(out / "diagnostic_progress.json", result)
        return 0
    except Exception as exc:
        result["error"] = repr(exc)
        result["failed_at_host_monotonic"] = time.monotonic()
        atomic_json(out / "diagnostic_progress.json", result)
        raise
    finally:
        try:
            if diagnostic_started and (schedule_created or args.mode == "runner"):
                result["finally_schedule_invalidation"] = r._scene4_invalidate_schedule(session, schedule_remote)
        except Exception as exc:
            result["finally_schedule_invalidation_error"] = repr(exc)
        if owned_launches:
            for b in sorted(owned_launches):
                try:
                    device.force_stop(b)
                    deadline = time.monotonic() + 20
                    last = None
                    while time.monotonic() < deadline:
                        snap = r._scene4_memcg_snapshot(device, b, include_smaps=False)
                        last = snap.get("processes", {})
                        if not last:
                            break
                        for proc in last.values():
                            pid, ticks = int(proc["pid"]), str(proc["start_ticks"])
                            known = identities.get(b, {})
                            expected_uid = int(installed_uids.get(b, -1))
                            exact_owned = bool(proc.get("verified") and expected_uid >= 0 and
                                int(proc.get("uid", -1)) == expected_uid and
                                (not known or (pid == int(known["pid"]) and
                                    ticks == str(known["start_ticks"]) and
                                    int(proc.get("uid", -1)) == int(known["uid"]))))
                            if exact_owned:
                                device.shell(f"kill -TERM {pid}", check=False)
                        time.sleep(0.5)
                    result.setdefault("cleanup", {})[b] = {"processes": last,
                        "verified_absent": not bool(last)}
                except Exception as exc:
                    result.setdefault("cleanup", {})[b] = {"error": repr(exc), "verified_absent": False}
        try:
            rows_now = current_processes(device)
            active = [p for p in rows_now if p["name"] == "memshiftd"]
            original_row = None
            diagnostic_row = None
            if len(active) == 1:
                candidate = active[0]
                candidate_id = inspect_daemon_identity(device, int(candidate["pid"]), ORIGINAL["exe"], ORIGINAL["config"])
                if candidate_id["verified"]:
                    original_row = candidate
                elif original_stop_attempted:
                    diag_id = inspect_daemon_identity(device, int(candidate["pid"]), remote_daemon, remote_config)
                    if diag_id["verified"]:
                        diagnostic_row = candidate
                        result["finally_diagnostic_identity"] = diag_id
            if original_row and len(active) == 1:
                status = "original_unchanged" if (not original_stop_attempted and original_pid == original_row["pid"]) else "original_already_active"
                result["service_restore"] = {"status": status,
                    **verify_original_state(device, device_boot or ORIGINAL["boot_id"])}
            elif not original_stop_attempted:
                result["service_restore"] = {"status": "original_state_changed_before_our_switch",
                    "verified": False, "instances": active,
                    "detail": "preflight did not stop the original service; refusing to start or stop any daemon"}
            else:
                if active and (len(active) != 1 or diagnostic_row is None):
                    raise RuntimeError("unexpected daemon identity present; refusing to signal or start another instance")
                for proc in ([diagnostic_row] if diagnostic_row else []):
                    pid = int(proc["pid"])
                    ticks = str(start_ticks(device, pid) or "")
                    if not ticks or str(start_ticks(device, pid)) != ticks:
                        raise RuntimeError("isolated daemon identity changed; refusing signal")
                    device.shell(f"kill -TERM {pid}", check=False)
                    end = time.monotonic() + 12
                    while time.monotonic() < end and start_ticks(device, pid) == ticks:
                        time.sleep(0.25)
                    if start_ticks(device, pid) == ticks:
                        raise RuntimeError("isolated daemon did not exit after TERM")
                remaining = [p for p in current_processes(device) if p["name"] == "memshiftd"]
                if remaining:
                    raise RuntimeError(f"daemon remains after isolated shutdown; refusing another start: {remaining!r}")
                restore_log = remote_root + "/original_restore.stdout.log"
                restore = device.shell("nohup " + quote(ORIGINAL["exe"]) + " --config " + quote(ORIGINAL["config"]) +
                    " --backend harmony-v1-ratio --apply > " + quote(restore_log) + " 2>&1 < /dev/null &", check=False)
                time.sleep(1)
                result["service_restore"] = {"status": "started_original", "start_output": restore,
                    "stdout_log": restore_log,
                    **verify_original_state(device, device_boot or ORIGINAL["boot_id"])}
        except Exception as exc:
            result["service_restore"] = {"status": "unverified", "error": repr(exc)}
        atomic_json(out / "diagnostic_final.json", result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise





