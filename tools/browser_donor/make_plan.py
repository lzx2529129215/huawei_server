"""Create the immutable one-browser event-plan row from a reviewed preparation manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def create_plan(manifest_path: Path, case_id: str, launch_boottime: float,
                needed_mib: int, output: Path) -> dict:
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    ready = data.get("readiness", {})
    required = (
        data.get("schema") == "browser-donor-preparation-v1",
        data.get("bundle") == "com.huawei.hmos.browser",
        data.get("eligible_for_review") is True,
        ready.get("identity_verified_each_sample") is True,
        ready.get("all_browser_windows_hidden_each_sample") is True,
        ready.get("all_group_app_scores_positive_each_sample") is True,
        ready.get("all_group_oom_score_adj_positive_each_sample") is True,
        ready.get("every_browser_window_pid_is_group_member") is True,
        ready.get("expected_window_count_stable") is True,
        ready.get("member_pid_sets_equal_across_samples") is True,
        ready.get("member_pid_start_sets_equal_across_samples") is True,
        ready.get("member_pid_uid_sets_equal_across_samples") is True,
        ready.get("browser_primary_pid_uses_package_uid_each_sample") is True,
        ready.get("anon_capacity_within_two_percent_across_samples") is True,
        data.get("operator_assertions", {}).get("pages_loaded_and_settled") is True,
        data.get("operator_assertions", {}).get("all_windows_minimized") is True,
        len(data.get("local_inputs", [])) == data.get("expected_windows", 0) *
            data.get("pages_per_window_operator_assertion", 0),
        len(data.get("device_inputs", [])) == len(data.get("local_inputs", [])),
        len({item.get("device_path") for item in data.get("device_inputs", [])}) ==
            len(data.get("device_inputs", [])),
        all(item.get("verified") is True for item in data.get("device_inputs", [])),
    )
    if not all(required):
        raise ValueError("preparation manifest lacks stable, verified browser group evidence")
    if not (case_id.startswith("s4_") and len(case_id) >= 12 and
            case_id.replace("_", "").isalnum() and case_id[-6:-4] == "_c" and
            case_id[-4:].isdigit()):
        raise ValueError("case id must have the form s4_<hex>_cNNNN")
    if not math.isfinite(launch_boottime) or launch_boottime <= 0 or needed_mib <= 0:
        raise ValueError("launch boottime and planned demand must be positive")
    samples = data.get("samples", [])
    if not samples:
        raise ValueError("preparation manifest has no device samples")
    latest = samples[-1]
    if launch_boottime <= latest.get("device_boottime_seconds", 0):
        raise ValueError("planned T must be later than the last verified browser sample")
    browser_windows = [w for w in latest.get("browser_windows", []) if w.get("is_browser_window")]
    main_pids = {w.get("pid") for w in browser_windows}
    if len(browser_windows) != data.get("expected_windows") or len(main_pids) != 1:
        raise ValueError("browser windows do not resolve to exactly one shared main PID")
    main_pid = next(iter(main_pids))
    process = next((p for p in latest.get("processes", []) if p.get("pid") == main_pid), None)
    if not process or not process.get("verified") or process.get("uid") != data.get("uid"):
        raise ValueError("browser main PID has no verified UID/start-ticks identity")
    group_processes = latest.get("processes", [])
    if not group_processes or len(group_processes) != latest.get("member_count"):
        raise ValueError("browser cgroup member set is incomplete")
    members = []
    for member in group_processes:
        if not member.get("verified") or not member.get("start_ticks") or not member.get("uid"):
            raise ValueError("browser cgroup member lacks a verified PID/UID/start identity")
        members.append(f"{member['pid']}:{member['uid']}:{member['start_ticks']}")
    member_registration = ",".join(members)
    safe_case = case_id
    # Device parser accepts lowercase hexadecimal case suffixes only.
    prefix, number = safe_case.rsplit("_c", 1)
    token = prefix[3:]
    if not token or any(c not in "0123456789abcdef" for c in token) or len(number) != 4:
        raise ValueError("case id must use lowercase hex and a four-digit case number")
    plan_id = case_id + "_peak_transition"
    ready_at = latest["device_boottime_seconds"]
    content = ("device_browser_donor_plan_v1\n" +
               f"{plan_id}|{case_id}|{launch_boottime:.9f}|{needed_mib * 1024 * 1024}|1\n" +
               f"com.huawei.hmos.browser|{data['uid']}|{main_pid}|{process['start_ticks']}|{ready_at:.9f}|{member_registration}\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="ascii", newline="\n")
    return {"output": str(output.resolve()), "sha256": hashlib.sha256(content.encode("ascii")).hexdigest(),
            "event_id": plan_id, "launch_boottime": launch_boottime,
            "needed_mib": needed_mib, "bundle": data["bundle"], "uid": data["uid"],
            "main_pid": main_pid, "start_ticks": process["start_ticks"],
            "group_member_count": latest["member_count"],
            "anon_bytes": latest.get("anon_bytes"), "cgroup_leaf": latest["cgroup_leaf"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--launch-boottime", required=True, type=float,
                        help="future device CLOCK_BOOTTIME T")
    parser.add_argument("--needed-mib", required=True, type=int,
                        help="registered peak-demand increment for the scenario")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(create_plan(args.manifest, args.case_id, args.launch_boottime,
                                     args.needed_mib, args.output), ensure_ascii=False, indent=2))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
