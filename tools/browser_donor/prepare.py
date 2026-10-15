#!/usr/bin/env python3
"""Read-only inventory and capacity sampler for a real Harmony browser donor.

The operator opens the offline pages in the browser and minimizes every window.
This program records one application/memcg identity and samples it repeatedly;
it never starts/stops apps, writes device state, or changes memory policy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BUNDLE = "com.huawei.hmos.browser"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def parse_proc_stat(raw: str) -> tuple[int, int, str]:
    """Return PID, start ticks, and state despite spaces or ')' in comm."""
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 1 or right <= left:
        raise ValueError("malformed /proc/PID/stat")
    pid = int(raw[:left].strip())
    tail = raw[right + 1 :].split()  # field 3 is index 0; starttime is field 22.
    if len(tail) <= 19:
        raise ValueError("short /proc/PID/stat")
    return pid, int(tail[19]), tail[0]


def key_values(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z_]+):?\s+(-?\d+)(?:\s+kB)?\s*$", line)
        if match:
            value = int(match.group(2))
            # Linux proc kB values are reported as KiB.
            out[match.group(1)] = value * 1024 if line.rstrip().endswith("kB") else value
    return out


class Hdc:
    def __init__(self, executable: str, target: str | None):
        self.executable = executable
        result = subprocess.run([executable, "list", "targets"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15)
        if result.returncode:
            raise RuntimeError("hdc list targets failed: " + result.stdout + result.stderr)
        online = [line.strip().split()[0] for line in result.stdout.splitlines()
                  if line.strip() and "Empty" not in line and "[" not in line]
        if target is None and len(online) == 1:
            target = online[0]
        if not target or target not in online:
            raise RuntimeError(f"select one online target with --target; online={online}")
        self.target = target

    def shell(self, command: str, timeout: int = 20) -> str:
        result = subprocess.run([self.executable, "-t", self.target, "shell", command],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        output = result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError(f"device read failed ({result.returncode}): {output[:1000]}")
        return output.replace("\r\n", "\n")


def section(raw: str, name: str) -> str:
    match = re.search(rf"(?ms)^__{re.escape(name)}__\n(.*?)(?=^__[A-Z0-9_]+__\s*$|\Z)", raw)
    return match.group(1).strip() if match else ""


def proc_rows(raw: str, bundle: str, leaf_name: str) -> list[dict]:
    chunks = re.split(r"(?m)^__PROC_BEGIN__=(\d+)\s*$", raw)
    rows = []
    for i in range(1, len(chunks), 2):
        if i + 1 >= len(chunks):
            break
        pid = int(chunks[i])
        body = chunks[i + 1]
        def get(label: str) -> str:
            m = re.search(rf"(?ms)^__{label}__\n(.*?)(?=^__(?:STAT_BEFORE|STAT_AFTER|STATUS|CGROUP|OOM_ADJ)__|^__PROC_END__|\Z)", body)
            return m.group(1).strip() if m else ""
        stat_before, status, cgroups = get("STAT_BEFORE"), get("STATUS"), get("CGROUP")
        try:
            stat_pid, start_before, state = parse_proc_stat(stat_before)
            pid_after, start_after, _ = parse_proc_stat(get("STAT_AFTER"))
        except (ValueError, TypeError):
            continue
        uid_match = re.search(r"(?m)^Uid:\s+(\d+)", status)
        adj = get("OOM_ADJ")
        leaf_ok = any(":memory:" in line and line.strip().endswith("/" + leaf_name)
                      for line in cgroups.splitlines())
        rows.append({"pid": stat_pid, "start_ticks": str(start_before),
                     "start_ticks_after": str(start_after), "state": state,
                     "uid": int(uid_match.group(1)) if uid_match else None,
                     "oom_score_adj": int(adj) if adj.lstrip("-").isdigit() else None,
                     "cgroup_matches_expected_leaf": leaf_ok,
                     "raw_cgroup": cgroups,
                     "verified": stat_pid == pid and pid_after == pid and
                                start_before == start_after and bool(uid_match) and leaf_ok and
                                adj.lstrip("-").isdigit()})
    return sorted(rows, key=lambda row: row["pid"])


def browser_windows(hdc: Hdc, member_pids: set[int]) -> tuple[list[dict], str]:
    table = hdc.shell("hidumper -s WindowManagerService -a '-a'", timeout=20)
    ids = []
    for line in table.splitlines():
        match = re.match(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+", line)
        if match and int(match.group(3)) in member_pids:
            ids.append({"name": match.group(1), "pid": int(match.group(3)),
                        "win_id": int(match.group(4)),
                        "is_browser_window": bool(re.fullmatch(r"browser\d+", match.group(1)))})
    details = []
    for row in ids:
        raw = hdc.shell("hidumper -s WindowManagerService -a " +
                        shlex.quote(f"-w {row['win_id']}"), timeout=15)
        visible = re.search(r"(?mi)^IsVisible:\s*(true|false)\s*$", raw)
        pid = re.search(r"(?mi)^Pid:\s*(\d+)\s*$", raw)
        detail_id = re.search(r"(?mi)^WinId:\s*(\d+)\s*$", raw)
        details.append({**row, "detail_pid": int(pid.group(1)) if pid else None,
                        "detail_win_id": int(detail_id.group(1)) if detail_id else None,
                        "is_visible": visible.group(1).lower() == "true" if visible else None,
                        "detail_raw": raw, "detail_sha256": sha256(raw)})
    return details, table


def sample(hdc: Hdc, bundle: str, uid: int) -> dict:
    leaf_name = f"{bundle}_{uid}"
    leaf = f"/dev/memcg/100/{leaf_name}"
    command = (
        "echo __CLOCK__; date +%s; cat /proc/uptime; "
        "echo __MEMINFO__; cat /proc/meminfo; "
        "echo __VMSTAT__; cat /proc/vmstat; "
        "echo __ZRAM__; for f in /sys/block/zram*/mm_stat; do "
        "test -r \"$f\" && echo \"$f: $(cat $f)\"; done; "
        f"echo __MEMBER_PIDS__; cat {shlex.quote(leaf + '/cgroup.procs')}; "
        f"echo __MEMORY_STAT__; cat {shlex.quote(leaf + '/memory.stat')}; "
        f"echo __MEMORY_CURRENT__; cat {shlex.quote(leaf + '/memory.current')}; "
        f"echo __APP_SCORE__; cat {shlex.quote(leaf + '/memory.app_score')}; "
        f"for p in $(cat {shlex.quote(leaf + '/cgroup.procs')}); do "
        "echo __PROC_BEGIN__=$p; echo __STAT_BEFORE__; cat /proc/$p/stat; "
        "echo __STATUS__; cat /proc/$p/status; echo __OOM_ADJ__; cat /proc/$p/oom_score_adj; "
        "echo __CGROUP__; cat /proc/$p/cgroup; echo __STAT_AFTER__; cat /proc/$p/stat; "
        "echo __PROC_END__; done; echo __MEMBER_PIDS_AFTER__; "
        f"cat {shlex.quote(leaf + '/cgroup.procs')}"
    )
    raw = hdc.shell(command, timeout=35)
    memstat = key_values(section(raw, "MEMORY_STAT"))
    meminfo = key_values(section(raw, "MEMINFO"))
    vmstat = key_values(section(raw, "VMSTAT"))
    member_pids = sorted({int(x) for x in section(raw, "MEMBER_PIDS").split() if x.isdecimal()})
    member_pids_after = sorted({int(x) for x in section(raw, "MEMBER_PIDS_AFTER").split() if x.isdecimal()})
    processes = proc_rows(raw, bundle, leaf_name)
    windows, wms_table = browser_windows(hdc, set(member_pids))
    uptime = section(raw, "CLOCK").splitlines()
    try:
        wall_seconds = int(uptime[0].strip())
        boottime_seconds = float(uptime[1].split()[0])
    except (IndexError, ValueError):
        raise RuntimeError("device clock sample is incomplete")
    return {
        "sampled_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "device_wall_seconds": wall_seconds, "device_boottime_seconds": boottime_seconds,
        "browser_bundle": bundle, "uid": uid, "cgroup_leaf": leaf,
        "member_pids": member_pids, "member_pids_after": member_pids_after,
        "member_count": len(member_pids),
        "processes": processes,
        "identity_verified": bool(member_pids) and member_pids == member_pids_after and
                             member_pids == [p["pid"] for p in processes]
                             and all(p["verified"] and isinstance(p["uid"], int) and p["uid"] > 0
                                     for p in processes),
        "memory_stat_bytes_or_counts": memstat,
        "anon_bytes": memstat.get("Anon"), "zram_bytes": memstat.get("Zram"),
        "eswap_bytes": memstat.get("Eswap"),
        "memory_current_bytes": int(section(raw, "MEMORY_CURRENT").split()[0])
                                if section(raw, "MEMORY_CURRENT").split() and
                                   section(raw, "MEMORY_CURRENT").split()[0].isdigit() else None,
        "app_score": int(section(raw, "APP_SCORE").split()[0])
                     if section(raw, "APP_SCORE").split() and
                        section(raw, "APP_SCORE").split()[0].lstrip("-").isdigit() else None,
        "meminfo_bytes_or_counts": meminfo,
        "system_memfree_bytes": meminfo.get("MemFree"),
        "system_memavailable_bytes": meminfo.get("MemAvailable"),
        "system_swapfree_bytes": meminfo.get("SwapFree"),
        "vmstat_counts": {k: vmstat.get(k) for k in
                          ("pswpin", "pswpout", "oom_kill", "pgmajfault")},
        "zram_mm_stat_raw": section(raw, "ZRAM"),
        "browser_windows": windows,
        "all_browser_windows_hidden": bool(windows) and all(w["is_visible"] is False and
              w["detail_pid"] == w["pid"] and w["detail_win_id"] == w["win_id"] for w in windows),
        "browser_window_count": sum(1 for w in windows if w["is_browser_window"]),
        "app_window_count": len(windows),
        "wms_table_sha256": sha256(wms_table),
        "raw_device_snapshot": raw,
    }


def local_inputs(args: argparse.Namespace) -> list[dict]:
    manifest_path = args.workload_manifest.resolve(strict=True)
    workload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if workload.get("schema") != "browser-donor-workload-v1":
        raise ValueError("unsupported workload manifest schema")
    if workload.get("bundle") != args.bundle:
        raise ValueError("workload manifest bundle differs from --bundle")
    windows = workload.get("windows")
    if not isinstance(windows, list) or len(windows) != args.expected_windows:
        raise ValueError("workload manifest window count differs from --expected-windows")
    if workload.get("window_count") != args.expected_windows or workload.get("pages_per_window") != args.pages_per_window:
        raise ValueError("workload manifest dimensions differ from command-line dimensions")
    files = []
    labels = set()
    for window in windows:
        label = str(window.get("label", ""))
        pages = window.get("pages")
        if not label or label in labels or not isinstance(pages, list) or len(pages) != args.pages_per_window:
            raise ValueError("each workload window needs a unique label and exact page count")
        labels.add(label)
        for page in pages:
            raw_path = Path(page["path"])
            path = (manifest_path.parent / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
            path = path.resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"input is not a file: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected_hash = page.get("sha256")
            if expected_hash and digest.lower() != str(expected_hash).lower():
                raise ValueError(f"input hash mismatch: {path}")
            files.append({"window_label": label, "page_label": page.get("label"),
                          "path": str(path), "size_bytes": path.stat().st_size,
                          "sha256": digest, "device_path": page.get("device_path"),
                          "ui_uri": page.get("ui_uri")})
    if len(files) != args.expected_windows * args.pages_per_window:
        raise ValueError("workload manifest must enumerate every intended browser tab")
    return files


def verify_device_inputs(hdc: Hdc, inputs: list[dict]) -> list[dict]:
    destinations = [item["device_path"] for item in inputs]
    if any(not isinstance(path, str) or not path.startswith("/storage/") for path in destinations):
        raise ValueError("every page needs an explicit /storage/... device_path")
    if len(set(destinations)) != len(destinations):
        raise ValueError("device page paths must be unique")
    verified = []
    for item in inputs:
        remote = item["device_path"]
        raw = hdc.shell("sha256sum " + shlex.quote(remote), timeout=15)
        match = re.search(r"(?m)^([0-9a-fA-F]{64})\s+(.+?)\s*$", raw)
        if not match or match.group(1).lower() != item["sha256"].lower():
            raise RuntimeError(f"device HTML hash missing or mismatched: {remote}; output={raw[:200]!r}")
        verified.append({"device_path": remote, "sha256": match.group(1).lower(),
                         "verified": True, "raw": raw.strip()})
    return verified


def build_manifest(args: argparse.Namespace) -> dict:
    inputs = local_inputs(args)
    hdc = Hdc(args.hdc, args.target)
    bm = hdc.shell("bm dump -n " + shlex.quote(args.bundle), timeout=20)
    match = re.search(r'"uid"\s*:\s*(\d+)', bm)
    if not match:
        raise RuntimeError(f"browser package is not installed or UID unavailable: {args.bundle}")
    uid = int(match.group(1))
    device_inputs = verify_device_inputs(hdc, inputs)
    samples = []
    for index in range(args.samples):
        if index:
            time.sleep(args.interval)
        samples.append(sample(hdc, args.bundle, uid))
    counts = [row["browser_window_count"] for row in samples]
    member_sets = [row["member_pids"] for row in samples]
    member_identity_sets = [tuple((p["pid"], p["start_ticks"]) for p in row["processes"])
                            for row in samples]
    member_uid_sets = [tuple((p["pid"], p["uid"]) for p in row["processes"])
                       for row in samples]
    primary_identity = []
    for row in samples:
        primary_pids = {w["pid"] for w in row["browser_windows"] if w["is_browser_window"]}
        by_pid = {p["pid"]: p for p in row["processes"]}
        primary_identity.append(len(primary_pids) == 1 and
                                next(iter(primary_pids), None) in by_pid and
                                by_pid[next(iter(primary_pids))]["uid"] == uid)
    stable_window_count = len(set(counts)) == 1 and counts[0] == args.expected_windows
    hidden = all(row["all_browser_windows_hidden"] and
                 row["browser_window_count"] == args.expected_windows for row in samples)
    verified = all(row["identity_verified"] for row in samples)
    all_scores_positive = all(row["app_score"] is not None and row["app_score"] > 0
                              for row in samples)
    all_members_background = all(bool(row["processes"]) and
                                 all(p["oom_score_adj"] is not None and p["oom_score_adj"] > 0
                                     for p in row["processes"]) for row in samples)
    windows_owned = all(all(w["detail_pid"] in row["member_pids"] and
                            w["pid"] in row["member_pids"] for w in row["browser_windows"])
                        for row in samples)
    anon_values = [row["anon_bytes"] for row in samples if row["anon_bytes"] is not None]
    anon_stable = bool(anon_values) and max(anon_values) - min(anon_values) <= max(anon_values) // 50
    return {
        "schema": "browser-donor-preparation-v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "device_target": hdc.target, "bundle": args.bundle, "uid": uid,
        "expected_windows": args.expected_windows,
        "workload_manifest": str(args.workload_manifest.resolve()),
        "pages_per_window_operator_assertion": args.pages_per_window,
        "page_count_operator_assertion": args.expected_windows * args.pages_per_window,
        "local_inputs": inputs,
        "device_inputs": device_inputs,
        "operator_assertions": {
            "pages_loaded_and_settled": args.pages_loaded,
            "all_windows_minimized": args.all_minimized,
            "input_files_match_expected_page_count": len(inputs) == args.expected_windows * args.pages_per_window,
        },
        "sample_interval_seconds": args.interval, "samples": samples,
        "readiness": {
            "sample_count_sufficient": args.samples >= 2,
            "identity_verified_each_sample": verified,
            "all_browser_windows_hidden_each_sample": hidden,
            "all_group_app_scores_positive_each_sample": all_scores_positive,
            "all_group_oom_score_adj_positive_each_sample": all_members_background,
            "every_browser_window_pid_is_group_member": windows_owned,
            "expected_window_count_stable": stable_window_count,
            "member_pid_sets_equal_across_samples": len(set(tuple(x) for x in member_sets)) == 1,
            "member_pid_start_sets_equal_across_samples": len(set(member_identity_sets)) == 1,
            "member_pid_uid_sets_equal_across_samples": len(set(member_uid_sets)) == 1,
            "browser_primary_pid_uses_package_uid_each_sample": all(primary_identity),
            "anon_capacity_within_two_percent_across_samples": anon_stable,
            "capacity_anon_min_bytes": min(anon_values) if anon_values else None,
            "capacity_anon_max_bytes": max(anon_values) if anon_values else None,
            "capacity_anon_delta_bytes": (max(anon_values) - min(anon_values)) if anon_values else None,
        },
        "eligible_for_review": bool(args.samples >= 2 and verified and hidden and
                                     all_scores_positive and all_members_background and windows_owned and
                                     len(set(tuple(x) for x in member_sets)) == 1 and
                                     len(set(member_identity_sets)) == 1 and len(set(member_uid_sets)) == 1 and
                                     all(primary_identity) and anon_stable and
                                     stable_window_count and args.pages_loaded and
                                     args.all_minimized and
                                     len(inputs) == args.expected_windows * args.pages_per_window and
                                     len(device_inputs) == len(inputs) and
                                     all(item.get("verified") for item in device_inputs)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", required=True, help="path to the Harmony device command-line tool")
    parser.add_argument("--target", help="explicit device target if more than one is online")
    parser.add_argument("--bundle", default=DEFAULT_BUNDLE)
    parser.add_argument("--expected-windows", type=int, required=True)
    parser.add_argument("--pages-per-window", type=int, default=3)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workload-manifest", required=True, type=Path,
                        help="JSON with each browser window and the local pages assigned to it")
    parser.add_argument("--pages-loaded", action="store_true",
                        help="operator confirms all intended offline pages finished loading")
    parser.add_argument("--all-minimized", action="store_true",
                        help="operator confirms every browser window is minimized")
    args = parser.parse_args()
    if args.expected_windows < 1 or args.pages_per_window < 1 or args.samples < 2 or args.interval < 0:
        parser.error("expected positive window/page counts, at least two samples, and nonnegative interval")
    try:
        manifest = build_manifest(args)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"browser donor sample failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()),
                      "eligible_for_review": manifest["eligible_for_review"],
                      "readiness": manifest["readiness"]}, ensure_ascii=False, indent=2))
    return 0 if manifest["eligible_for_review"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
