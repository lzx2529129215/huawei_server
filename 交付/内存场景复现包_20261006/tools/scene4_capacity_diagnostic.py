"""Same-load capacity diagnostic. Sequential peaks are not an A/B benefit test.

Uses only the eight owned HAPs. No reclaim/control/swap writes or service changes.
Earlier apps hold their full peak for 900 seconds, preventing an artificial pass
from their release before later apps reach peak. Every stage checks identities.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memscenes.device import Device, quote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hdc", default=str(ROOT / "tools/hdc.exe"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    device = Device(args.hdc)
    case = f"s4_{secrets.token_hex(6)}_c0000"
    apps = [f"io.memscene.scene4.workload.a{i}" for i in range(1, 9)]
    result = {"scope": "capacity_diagnostic_not_optimization_comparison",
              "case_id": case, "daily_mib": 2400, "peak_mib": 5184,
              "file_mib": 1200, "seed": 20261005, "per_app_hold_seconds": 900,
              "settle_after_each_peak_seconds": 20, "all_peak_hold_seconds": 60,
              "changed_vs_burst": ["sequential_peak_requests", "longer_hold"],
              "reclaim_nodes_written": False, "swap_configuration_changed": False,
              "capacity_proven": False, "identities": {}, "stages": [], "cleanup": {}}
    result["driver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    baseline_service = device.shell("ps -A -o PID,NAME,ARGS | grep '[m]emshiftd'", check=False)
    result["service_before"] = baseline_service
    def save():
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    def journal(bundle, daily=False):
        ident = case + ("_daily" if daily else "")
        path = device.app(bundle)["files"] + f"/memscene_{ident}.jsonl"
        raw = device.shell("tail -n 1000 " + quote(path), check=False)
        (args.output / f"{bundle.rsplit('.', 1)[-1]}_{'daily' if daily else 'peak'}.jsonl").write_text(raw, encoding="utf-8")
        rows = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if row.get("case_id") == ident:
                    rows.append(row)
            except ValueError:
                pass
        return rows
    def snapshot(label):
        raw = device.shell("cat /proc/uptime /proc/meminfo /sys/block/zram0/hyperhold_io_info_file", timeout=15)
        (args.output / f"{label}_system.txt").write_text(raw, encoding="utf-8")
        identities = {bundle: device.app_processes(bundle) for bundle in apps}
        (args.output / f"{label}_identities.json").write_text(json.dumps(identities, indent=2), encoding="utf-8")
        for bundle, expected in result["identities"].items():
            if set(identities[bundle]) != set(expected):
                raise RuntimeError(f"identity_lost_or_replaced:{bundle}:{label}")
        for bundle in apps:
            group = f"/dev/memcg/100/{bundle}_{device.app(bundle)['uid']}"
            stat = device.shell("cat " + quote(group + "/memory.stat"), check=False)
            (args.output / f"{label}_{bundle.rsplit('.', 1)[-1]}_memory.stat").write_text(stat, encoding="utf-8")
        return identities
    def wait_event(bundle, event, timeout, daily=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = journal(bundle, daily)
            if any(row.get("event") == "allocation_failed" for row in rows):
                raise RuntimeError(f"allocation_failed:{bundle}:{event}")
            if not device.app_processes(bundle):
                raise RuntimeError(f"process_absent:{bundle}:{event}")
            found = next((row for row in rows if row.get("event") == event), None)
            if found:
                return found
            time.sleep(1)
        raise RuntimeError(f"event_timeout:{bundle}:{event}")
    prepared = False
    try:
        if any(device.app_processes(bundle) for bundle in apps):
            raise RuntimeError("owned_workload_already_present_no_mutation")
        service_rows = [line.split() for line in baseline_service.splitlines() if line.strip()]
        if len(service_rows) != 1 or not service_rows[0][0].isdigit():
            raise RuntimeError("single_original_service_not_verified_no_mutation")
        service_pid = int(service_rows[0][0])
        service_exe = device.shell(f"readlink /proc/{service_pid}/exe").strip()
        service_hash = device.shell(f"sha256sum /proc/{service_pid}/exe").split()[0].lower()
        result["original_service_identity"] = {"pid": service_pid, "exe": service_exe, "sha256": service_hash}
        if (service_exe != "/data/local/tmp/memshift_validation_20261007/full_chain/memshiftd" or
                service_hash != "cae17800a71ae5e7953050d0fffc8e2588c364c2d3f3257f49eb2abce67d25ab"):
            raise RuntimeError("original_service_not_verified_no_mutation")
        result["kernel_before_tail"] = "kernel_before.txt"
        (args.output / "kernel_before.txt").write_text(device.shell("dmesg | tail -n 1000", check=False), encoding="utf-8")
        prepared = True
        def launch(bundle):
            return device.launch(bundle, params={"case_id": case, "bundle": bundle,
                "daily_case_id": case + "_daily", "daily_mib": 2400, "peak_mib": 5184,
                "file_mib": 1200, "hold_seconds": 900, "lifetime_seconds": 1200,
                "seed": 20261005, "await_peak": 1, "no_periodic_msync": 1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            result["launch_outputs"] = dict(zip(apps, pool.map(launch, apps)))
        for bundle in apps:
            wait_event(bundle, "daily_ready", 120, daily=True)
        result["identities"] = {bundle: device.app_processes(bundle) for bundle in apps}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda bundle: device.launch(bundle, params={"op": "redirty_minimize", "case_id": case}), apps))
        for bundle in apps:
            wait_event(bundle, "file_minimize_succeeded", 90, daily=True)
        snapshot("daily_all_ready")
        save()
        for index, bundle in enumerate(apps):
            stage = {"bundle": bundle, "request_host_monotonic": time.monotonic()}
            result["stages"].append(stage)
            stage["request_output"] = device.promote(bundle, case)
            stage["peak_reached"] = wait_event(bundle, "peak_reached", 90)
            if int(stage["peak_reached"].get("anon_bytes", 0)) + int(stage["peak_reached"].get("file_bytes", 0)) < 5184 * 1024 * 1024:
                raise RuntimeError("journal_peak_bytes_below_target")
            snapshot(f"peak_{index+1:02d}")
            time.sleep(20)
            snapshot(f"settled_{index+1:02d}")
            for previous in apps[:index+1]:
                if any(row.get("event") in ("startup_ready", "cold_start_hold_end") for row in journal(previous)):
                    raise RuntimeError(f"early_peak_release:{previous}")
            save()
        for hold_sample in range(6):
            time.sleep(10)
            snapshot(f"all_peak_hold_{hold_sample+1:02d}")
        for bundle in apps:
            if any(row.get("event") in ("startup_ready", "cold_start_hold_end") for row in journal(bundle)):
                raise RuntimeError(f"peak_released_before_final_check:{bundle}")
        result["capacity_proven"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if prepared:
            for bundle in apps:
                try:
                    journal(bundle)
                    journal(bundle, True)
                except Exception as exc:
                    result.setdefault("collection_errors", []).append(str(exc))
            try:
                (args.output / "kernel_after.txt").write_text(device.shell("dmesg | tail -n 1000", check=False), encoding="utf-8")
            except Exception as exc:
                result.setdefault("collection_errors", []).append(str(exc))
            for bundle in apps:
                try:
                    device.force_stop(bundle)
                    absent = not device.app_processes(bundle)
                    result["cleanup"][bundle] = {"verified_absent": absent}
                    if absent:
                        result["cleanup"][bundle]["pages"] = device.remove_owned_case_pages(bundle, case)
                except Exception as exc:
                    result["cleanup"][bundle] = {"error": str(exc)}
        result["service_after"] = device.shell("ps -A -o PID,NAME,ARGS | grep '[m]emshiftd'", check=False)
        result["service_unchanged"] = result["service_after"] == baseline_service
        save()
    print(json.dumps({"capacity_proven": result["capacity_proven"], "error": result.get("error"), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
