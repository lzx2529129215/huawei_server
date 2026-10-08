#!/usr/bin/env python3
"""Replay the first 460 HarmonyOS events and collect vmstat/cgroup proxies.

The first 383 events are the initial-use window.  Memory pressure is applied
between event 383 and event 384; the last 77 events are the reuse window.
"""

from __future__ import annotations

import argparse
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
from datetime import datetime, timezone
from pathlib import Path


APPS = (
    ("腾讯会议", "com.tencent.meeting.app", "hot"),
    ("浏览器", "com.huawei.hmos.browser", "hot"),
    ("海泰浏览器", "com.haitai.htbrowser", "cold"),
    ("文件管理器", "com.huawei.hmos.filemanager", "cold"),
    ("WPS", "cn.wps.office.hap", "cold"),
    ("应用市场", "com.huawei.hmsapp.appgallery", "cold"),
)
STAGES = (("腾讯会议", 11), ("浏览器", 58), ("海泰浏览器", 112),
          ("文件管理器", 58), ("WPS", 90), ("应用市场", 54),
          ("腾讯会议", 11), ("浏览器", 66))
FIRST_COUNT = sum(count for _, count in STAGES[:6])
TOTAL_COUNT = sum(count for _, count in STAGES)
VM_FIELDS = ("workingset_refault_anon", "pagefault_major_anon",
             "pagefault_minor_anon", "pagefault_major_file", "pagefault_minor_file")
CGROUP_ROOT = "/dev/cgroup/memory/default/100"


def wall_time() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def find_hdc(explicit: str | None, replay_root: Path) -> Path:
    candidates = [explicit, os.environ.get("HDC_PATH"),
                  str(replay_root / "tools" / "hdc.exe"),
                  shutil.which("hdc"), shutil.which("hdc.exe"),
                  r"D:\lzx\app\devceo studio\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("找不到 hdc.exe；请指定 --hdc")


class Hdc:
    def __init__(self, executable: Path, target: str):
        self.base = [str(executable), "-t", target]

    def call(self, *args: str, timeout: int = 30, check: bool = True) -> str:
        p = subprocess.run(self.base + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        if check and p.returncode:
            raise RuntimeError(f"hdc {args[0]} 失败({p.returncode}): {p.stdout.strip()}")
        return p.stdout.replace("\r\n", "\n")

    def shell(self, command: str, *, timeout: int = 30, check: bool = True) -> str:
        return self.call("shell", command, timeout=timeout, check=check)

    def send(self, source: Path, target: str) -> None:
        self.call("file", "send", str(source), target, timeout=180)

    def recv(self, source: str, target: Path) -> None:
        self.call("file", "recv", source, str(target), timeout=180)


def parse_vmstat(raw: str) -> dict[str, int]:
    values = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in VM_FIELDS and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    missing = set(VM_FIELDS) - values.keys()
    if missing:
        raise RuntimeError(f"/proc/vmstat 缺少字段：{sorted(missing)}")
    return values


def parse_memory(raw: str, paths: dict[str, str]) -> dict[str, int]:
    found = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in paths and value.strip().isdigit():
            found[key] = int(value.strip())
    missing = set(paths) - found.keys()
    if missing:
        raise RuntimeError(f"无法读取 memory.current：{sorted(missing)}；原始输出：{raw[:500]}")
    return found


def replay_rows(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and re.match(r"^\d{4}-\d\d-\d\d ", parts[0]):
            result.append((parts[0], parts[1]))
    return result


def verify_plan(output: str) -> str:
    match = re.search(r"^rows=(\d+) supported=(\d+) skipped=(\d+)$", output, re.M)
    if not match or tuple(map(int, match.groups())) != (TOTAL_COUNT, TOTAL_COUNT, 0):
        raise RuntimeError("自动化预演未覆盖全部 460 条操作")
    rows = replay_rows(output)
    expected = [name for name, count in STAGES for _ in range(count)]
    if len(rows) != TOTAL_COUNT or [name for _, name in rows] != expected:
        raise RuntimeError("回放应用顺序或分界与预期不符，拒绝执行")
    cutoff = rows[FIRST_COUNT][0]
    if rows[FIRST_COUNT - 1][0] >= cutoff:
        raise RuntimeError("第 383/384 条时间戳不能唯一分隔两段")
    return cutoff


def metric_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, dict]:
    result = {}
    for key in VM_FIELDS:
        delta = after[key] - before[key]
        result[key] = {"count": delta if delta >= 0 else None,
                       "status": "complete" if delta >= 0 else "counter_decreased",
                       "before": before[key], "after": after[key]}
    return result


def reclaim_metric(before: int, after: int) -> dict:
    signed_drop = before - after
    # memory.current is a gauge, not a reclaim event counter.  A net increase
    # means there is no measurable net decrease in this two-snapshot window.
    decrease = max(0, signed_drop)
    return {"bytes": decrease, "mib": decrease / 1048576,
            "signed_decrease_bytes": signed_drop, "before_bytes": before,
            "after_bytes": after, "status": "proxy_net_decrease"}


def compute_metrics(before: dict, after_pressure: dict, end: dict,
                    fault_start: dict | None = None) -> dict:
    faults = metric_delta((fault_start or after_pressure)["vmstat"], end["vmstat"])
    app_memory = {}
    for name, _, _ in APPS:
        app_memory[name] = reclaim_metric(before["memory"][name],
                                          after_pressure["memory"][name])
    hot = sum(app_memory[name]["bytes"] for name, _, group in APPS if group == "hot")
    cold = sum(app_memory[name]["bytes"] for name, _, group in APPS if group == "cold")
    total = reclaim_metric(before["memory"]["total"], after_pressure["memory"]["total"])
    return {
        "anon_refault": faults["workingset_refault_anon"],
        "anon_major_fault": faults["pagefault_major_anon"],
        "anon_minor_fault": faults["pagefault_minor_anon"],
        "file_major_fault": faults["pagefault_major_file"],
        "file_minor_fault": faults["pagefault_minor_file"],
        "hot_reclaim": {"bytes": hot, "mib": hot / 1048576, "status": "proxy_net_decrease"},
        "cold_reclaim": {"bytes": cold, "mib": cold / 1048576, "status": "proxy_net_decrease"},
        "total_reclaim": total,
        "hot_reclaim_ratio": {"value": hot / total["bytes"] if total["bytes"] else None,
                              "status": "proxy" if total["bytes"] else "undefined_zero_denominator"},
        "cold_reclaim_ratio": {"value": cold / total["bytes"] if total["bytes"] else None,
                               "status": "proxy" if total["bytes"] else "undefined_zero_denominator"},
        "per_app_reclaim": app_memory,
        "full_window_faults": metric_delta(before["vmstat"], end["vmstat"]),
    }


class Collector:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.replay_root = args.replay_root.resolve()
        self.replay = self.replay_root / "bin" / "replay_user_events.exe"
        self.events = self.replay_root / "data" / "user_events合并.xlsx"
        self.source_config = self.replay_root / "config" / "replay_config.json"
        self.helper = args.pressure_helper.resolve()
        self.output = args.output_root.resolve() / ("harmony_auto_kpi_" +
                      datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3))
        self.session = "harmony_auto_kpi_" + secrets.token_hex(10)
        self.remote = "/data/local/tmp/" + self.session
        self.hdc: Hdc | None = None
        self.paths: dict[str, str] = {}
        self.pressure_pid: int | None = None
        self.markers: list[dict] = []
        self.snapshots: list[dict] = []
        self.completed = False
        self.plan_rows: list[tuple[str, str]] = []

    def mark(self, name: str) -> None:
        marker = {"name": name, "wall_time": wall_time(), "monotonic_ns": time.monotonic_ns()}
        self.markers.append(marker)
        print(f"MARKER {name} {marker['wall_time']}", flush=True)

    def preflight(self) -> dict:
        for path in (self.replay, self.events, self.source_config, self.helper):
            if not path.is_file():
                raise RuntimeError(f"缺少依赖文件：{path}")
        if self.args.pressure_mib is not None and self.args.pressure_mib < 64:
            raise RuntimeError("--pressure-mib 至少为 64 MiB")
        if self.args.pressure_reserve_mib < 1024 or self.args.pressure_timeout < 1:
            raise RuntimeError("加压保留内存至少 1024 MiB，超时至少 1 秒")
        hdc_path = find_hdc(self.args.hdc, self.replay_root)
        targets = subprocess.run([str(hdc_path), "list", "targets"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=15)
        online = [line.split()[0] for line in targets.stdout.splitlines()
                  if line.strip() and "Empty" not in line and "[" not in line]
        target = self.args.target or (online[0] if len(online) == 1 else None)
        if target is None or target not in online:
            raise RuntimeError(f"目标设备未唯一确定，在线设备：{online}")
        self.hdc = Hdc(hdc_path, target)
        if self.hdc.shell("getconf PAGE_SIZE").strip() != "4096":
            raise RuntimeError("设备页大小不是 4096 B")
        arch = self.hdc.shell("uname -m").strip()
        if arch not in ("aarch64", "arm64"):
            raise RuntimeError(f"加压 helper 仅支持 ARM64，当前：{arch}")
        uids = {}
        for name, bundle, _ in APPS:
            dump = self.hdc.shell(f"bm dump -n {bundle}")
            match = re.search(r'"uid"\s*:\s*([1-9]\d*)', dump)
            if not match:
                raise RuntimeError(f"无法识别 {name} 的 UID")
            uids[name] = int(match.group(1))
            self.paths[name] = f"{CGROUP_ROOT}/{bundle}_{uids[name]}/memory.current"
        self.paths["total"] = CGROUP_ROOT + "/memory.current"
        self.hdc.shell(f"cat {self.paths['total']}")
        meminfo = self.hdc.shell("grep '^MemAvailable:' /proc/meminfo").split()
        if len(meminfo) < 2 or not meminfo[1].isdigit():
            raise RuntimeError("无法读取 MemAvailable")
        available_mib = int(meminfo[1]) // 1024
        target_mib = (self.args.pressure_mib if self.args.pressure_mib is not None
                      else min(self.args.pressure_max_mib,
                               available_mib - self.args.pressure_reserve_mib))
        target_mib = target_mib // 64 * 64
        if target_mib < 64 or target_mib > available_mib - self.args.pressure_reserve_mib:
            raise RuntimeError("加压量超出 MemAvailable 扣除保留内存后的安全范围")
        self.pressure_mib = target_mib
        config = json.loads(self.source_config.read_text(encoding="utf-8"))
        config["hdc_path"] = str(hdc_path)
        config["device_id"] = target
        self.output.mkdir(parents=True, exist_ok=False)
        self.config = self.output / "replay_config.json"
        self.config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"target": target, "hdc": str(hdc_path), "uids": uids, "cgroup_paths": self.paths,
                "page_size": 4096, "pressure_mib": target_mib,
                "pressure_wait_seconds": self.args.pressure_wait_seconds,
                "pressure_helper_sha256": hashlib.sha256(self.helper.read_bytes()).hexdigest(),
                "replay_sha256": hashlib.sha256(self.replay.read_bytes()).hexdigest()}

    def snapshot(self, label: str, *, vmstat_last: bool = False,
                 memory_keys: tuple[str, ...] | None = None) -> dict:
        assert self.hdc is not None
        start = time.monotonic_ns()
        vmstat = (None if vmstat_last else parse_vmstat(self.hdc.shell("cat /proc/vmstat")))
        # One shell round trip minimizes skew among the seven cgroup gauges.
        selected_paths = (self.paths if memory_keys is None else
                          {key: self.paths[key] for key in memory_keys})
        command = "; ".join(f"printf '{name}='; cat {path} 2>/dev/null || echo MISSING"
                            for name, path in selected_paths.items())
        memory = parse_memory(self.hdc.shell(command), selected_paths)
        if vmstat_last:
            vmstat = parse_vmstat(self.hdc.shell("cat /proc/vmstat"))
        snap = {"label": label, "wall_time": wall_time(), "monotonic_start_ns": start,
                "monotonic_end_ns": time.monotonic_ns(), "vmstat": vmstat, "memory": memory}
        self.snapshots.append(snap)
        print(f"SNAPSHOT {label} total={memory['total']} bytes", flush=True)
        return snap

    def replay_command(self, *extra: str) -> list[str]:
        return [str(self.replay), "--events", str(self.events), "--config", str(self.config),
                *extra]

    def dry_run(self) -> str:
        p = subprocess.run(self.replay_command("--limit", str(TOTAL_COUNT)),
                           cwd=self.replay_root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        (self.output / "replay_preview.txt").write_text(p.stdout + p.stderr, encoding="utf-8")
        if p.returncode:
            raise RuntimeError(f"回放预演失败：{p.stderr[-1000:]}")
        cutoff = verify_plan(p.stdout)
        self.plan_rows = replay_rows(p.stdout)
        suffix = subprocess.run(self.replay_command("--start", cutoff, "--limit",
                                                    str(TOTAL_COUNT - FIRST_COUNT)),
                                cwd=self.replay_root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
        suffix_rows = replay_rows(suffix.stdout)
        if (suffix.returncode or len(suffix_rows) != TOTAL_COUNT - FIRST_COUNT
                or suffix_rows[0][0] != cutoff
                or [name for _, name in suffix_rows] !=
                [name for name, count in STAGES[6:] for _ in range(count)]):
            raise RuntimeError("复用段预演不是准确的后 77 条")
        if self.args.resume_from_row > 1:
            start_index = self.args.resume_from_row - 1
            start_time = self.plan_rows[start_index][0]
            if self.plan_rows[start_index - 1][0] >= start_time:
                raise RuntimeError("接续位置时间戳不能唯一分隔")
            expected = self.plan_rows[start_index:FIRST_COUNT]
            resume = subprocess.run(self.replay_command(
                "--start", start_time, "--limit", str(len(expected))),
                cwd=self.replay_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180)
            if resume.returncode or replay_rows(resume.stdout) != expected:
                raise RuntimeError("接续预演未能准确覆盖剩余首次操作")
        print(f"预演通过：前 {FIRST_COUNT} 条 + 后 {TOTAL_COUNT-FIRST_COUNT} 条；分界 {cutoff}")
        return cutoff

    def execute(self, label: str, *extra: str) -> None:
        command = self.replay_command(*extra, "--execute", "--speed", str(self.args.speed),
                                      "--max-sleep", str(self.args.max_sleep), "--allow-protected")
        print(f"开始自动操作：{label}", flush=True)
        log_path = self.output / f"replay_{label}.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(command, cwd=self.replay_root, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                fields = line.split("\t", 3)
                if len(fields) == 4 and re.match(r"^\d{4}-", fields[0]):
                    print(f"  {fields[1]}: {fields[2]}", flush=True)
            rc = proc.wait()
        if rc:
            raise RuntimeError(f"{label} 回放失败，退出码 {rc}；查看 {log_path}")

    def start_pressure(self) -> None:
        assert self.hdc is not None
        self.hdc.shell(f"mkdir -p {self.remote}")
        remote_bin = self.remote + "/harmony_mem_pressure"
        self.hdc.send(self.helper, remote_bin)
        self.hdc.shell(f"chmod 700 {remote_bin}")
        self.hdc.shell(f"{remote_bin} {self.pressure_mib} {self.args.pressure_timeout} 2 "
                       f">{self.remote}/pressure.log 2>&1 & echo $! >{self.remote}/pressure.pid")
        pid_text = self.hdc.shell(f"cat {self.remote}/pressure.pid").strip()
        if not pid_text.isdigit():
            raise RuntimeError(f"加压进程 PID 无效：{pid_text}")
        self.pressure_pid = int(pid_text)
        for _ in range(40):
            log = self.hdc.shell(f"cat {self.remote}/pressure.log 2>/dev/null", check=False)
            if "READY " in log:
                self.mark("加压就绪")
                return
            alive = self.hdc.shell(f"test -d /proc/{self.pressure_pid} && echo yes || echo no",
                                   check=False)
            if "no" in alive:
                break
            time.sleep(0.1)
        raise RuntimeError(f"内存加压未就绪：{log[-500:]}")

    def wait_pressure_allocated(self) -> None:
        """Do not begin the post-pressure observation clock before allocation."""
        assert self.hdc is not None and self.pressure_pid is not None
        deadline = time.monotonic() + self.args.pressure_allocation_timeout
        while time.monotonic() < deadline:
            log = self.hdc.shell(f"cat {self.remote}/pressure.log 2>/dev/null", check=False)
            allocations = [int(value) for value in re.findall(
                r"^ALLOCATED_MIB\s+(\d+)$", log, re.M)]
            if allocations and max(allocations) >= self.pressure_mib:
                self.mark("加压分配完成")
                return
            if "MMAP_FAILED" in log or "STOP " in log:
                raise RuntimeError(f"加压未达到目标：{log[-500:]}")
            alive = self.hdc.shell(
                f"test -d /proc/{self.pressure_pid} && echo yes || echo no", check=False)
            if "no" in alive:
                raise RuntimeError(f"加压进程提前退出：{log[-500:]}")
            time.sleep(0.5)
        raise RuntimeError("加压进程分配内存超时")

    def stop_pressure(self) -> dict:
        assert self.hdc is not None
        if self.pressure_pid is None:
            return {"status": "not_started"}
        pid = self.pressure_pid
        self.hdc.shell(f"kill -2 {pid} 2>/dev/null || true", check=False)
        exited = False
        for _ in range(30):
            alive = self.hdc.shell(f"test -d /proc/{pid} && echo yes || echo no", check=False)
            if "no" in alive:
                exited = True
                break
            stat = self.hdc.shell(f"cat /proc/{pid}/stat 2>/dev/null", check=False)
            tail = stat.rpartition(") ")[2].split()
            if tail and tail[0] == "Z":
                exited = True
                break
            time.sleep(0.5)
        if not exited:
            self.hdc.shell(f"kill -15 {pid} 2>/dev/null || true", check=False)
        self.pressure_pid = None
        remote_log = self.remote + "/pressure.log"
        local_log = self.output / "pressure.log"
        size = self.hdc.shell(f"stat -c %s {remote_log} 2>/dev/null", check=False).strip()
        pulled = False
        if size.isdigit():
            self.hdc.recv(remote_log, local_log)
            pulled = local_log.stat().st_size == int(size)
        content = local_log.read_text(encoding="utf-8", errors="replace") if pulled else ""
        clean_stop = bool(re.search(r"^STOP .*reason=signal$", content, re.M))
        return {"status": "complete" if exited and pulled and clean_stop else "incomplete",
                "pid": pid, "exited": exited, "log_pulled": pulled,
                "clean_stop": clean_stop,
                "allocated_mib": max([int(x) for x in re.findall(r"^ALLOCATED_MIB\s+(\d+)$",
                                                                   content, re.M)] or [0])}

    def run(self) -> Path:
        preflight = self.preflight()
        result: dict = {"status": "incomplete", "preflight": preflight,
                        "metric_scope": {"faults": "reuse window: after pressure stop to operation end; device-wide",
                                         "reclaim": "positive net memory.current decrease: before pressure to after wait"},
                        "automation_coverage": {
                            "resume_from_row": self.args.resume_from_row,
                            "prior_run": str(self.args.prior_run.resolve()) if self.args.prior_run else None,
                            "continuous": self.args.resume_from_row == 1}}
        try:
            cutoff = self.dry_run()
            self.mark("开始采集" if self.args.resume_from_row == 1 else "接续采集")
            # Cold app cgroups may not exist until those apps have been launched.
            self.snapshot("start", memory_keys=("total",))
            if self.args.resume_from_row == 1:
                self.execute("initial", "--limit", str(FIRST_COUNT))
            else:
                start_time = self.plan_rows[self.args.resume_from_row - 1][0]
                remaining = FIRST_COUNT - self.args.resume_from_row + 1
                self.execute("initial", "--start", start_time, "--limit", str(remaining))
            self.mark("首次六应用操作完成")
            before = self.snapshot("before_pressure")
            self.start_pressure()
            self.wait_pressure_allocated()
            deadline = time.monotonic() + self.args.pressure_wait_seconds
            while time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
                if self.pressure_pid is not None:
                    alive = self.hdc.shell(
                        f"test -d /proc/{self.pressure_pid} && echo yes || echo no", check=False)
                    if "no" in alive:
                        raise RuntimeError("加压进程在观察等待期间退出")
            after_pressure = self.snapshot("after_pressure_before_reuse")
            result["pressure"] = self.stop_pressure()
            if result["pressure"]["status"] != "complete":
                raise RuntimeError("加压停止或日志校验不完整")
            fault_start = self.snapshot("reuse_fault_baseline", vmstat_last=True)
            self.mark("开始复用")
            self.execute("reuse", "--start", cutoff, "--limit",
                         str(TOTAL_COUNT - FIRST_COUNT))
            self.mark("操作完成")
            end = self.snapshot("end")
            result["metrics"] = compute_metrics(before, after_pressure, end, fault_start)
            result["metrics"]["full_window_faults"] = metric_delta(
                self.snapshots[0]["vmstat"], end["vmstat"])
            result["status"] = (("complete" if self.args.resume_from_row == 1
                                  else "complete_with_resume")
                                if result["pressure"]["status"] == "complete"
                                and all(result["metrics"][key]["status"] == "complete"
                                        for key in ("anon_refault", "anon_major_fault",
                                                    "anon_minor_fault", "file_major_fault",
                                                    "file_minor_fault")) else "incomplete")
            self.completed = result["status"].startswith("complete")
        except (Exception, KeyboardInterrupt) as exc:
            result["error"] = str(exc)
            print(f"ERROR {exc}", file=sys.stderr, flush=True)
        finally:
            if self.pressure_pid is not None:
                try:
                    result["pressure"] = self.stop_pressure()
                except Exception as exc:
                    result["pressure_cleanup_error"] = str(exc)
            result["markers"] = self.markers
            result["snapshots"] = self.snapshots
            (self.output / "metrics.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            with (self.output / "metrics.csv").open("w", newline="", encoding="utf-8-sig") as out:
                writer = csv.writer(out)
                writer.writerow(("metric", "value", "unit", "status"))
                for key, metric in result.get("metrics", {}).items():
                    if key in ("per_app_reclaim", "full_window_faults"):
                        continue
                    value = metric.get("count", metric.get("mib", metric.get("value")))
                    unit = "count" if "count" in metric else ("MiB" if "mib" in metric else "ratio")
                    writer.writerow((key, value, unit, metric.get("status")))
            if self.completed and result.get("pressure", {}).get("log_pulled"):
                # The random, session-specific directory is the only delete target.
                assert self.remote.startswith("/data/local/tmp/harmony_auto_kpi_")
                self.hdc.shell(f"rm -rf {self.remote}", check=False)
        print(f"DONE status={result['status']} output={self.output}", flush=True)
        if "metrics" in result:
            print(json.dumps({k: v for k, v in result["metrics"].items()
                              if k not in ("per_app_reclaim", "full_window_faults")},
                             ensure_ascii=False), flush=True)
        return self.output


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-root", type=Path, required=True,
                        help="root of the portable user_events replay package")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/harmony_auto_kpi"))
    parser.add_argument("--hdc", help="hdc.exe path; defaults to replay package tools/hdc.exe")
    parser.add_argument("--target", help="HDC device ID; required if multiple devices online")
    parser.add_argument("--pressure-helper", type=Path,
                        default=Path(__file__).with_name("harmony_mem_pressure"))
    parser.add_argument("--pressure-mib", type=int)
    parser.add_argument("--pressure-reserve-mib", type=int, default=4096)
    parser.add_argument("--pressure-max-mib", type=int, default=16384)
    parser.add_argument("--pressure-timeout", type=int, default=1800)
    parser.add_argument("--pressure-allocation-timeout", type=int, default=300)
    parser.add_argument("--pressure-wait-seconds", type=float, default=30.0)
    parser.add_argument("--speed", type=float, default=20.0)
    parser.add_argument("--max-sleep", type=float, default=3.0)
    parser.add_argument("--preflight-only", action="store_true",
                        help="只检查设备、依赖和 460 条自动化预演，不执行操作或加压")
    parser.add_argument("--resume-from-row", type=int, default=1,
                        help="从第 N 条事件接续首次阶段，仅用于失败后的有证据恢复")
    parser.add_argument("--prior-run", type=Path,
                        help="接续运行时链接上一次失败结果目录")
    args = parser.parse_args()
    if (args.pressure_wait_seconds < 0 or args.speed <= 0 or args.max_sleep < 0
            or args.pressure_allocation_timeout < 1):
        parser.error("等待时间不能为负，回放速度必须大于 0")
    if not 1 <= args.resume_from_row <= FIRST_COUNT:
        parser.error("--resume-from-row 必须在 1 到 383 之间")
    if args.resume_from_row > 1 and (args.prior_run is None or not args.prior_run.is_dir()):
        parser.error("接续运行必须指定现存的 --prior-run 目录")
    try:
        collector = Collector(args)
        if args.preflight_only:
            preflight = collector.preflight()
            cutoff = collector.dry_run()
            (collector.output / "preflight.json").write_text(
                json.dumps({"status": "ready", "preflight": preflight,
                            "first_count": FIRST_COUNT, "reuse_count": TOTAL_COUNT - FIRST_COUNT,
                            "reuse_cutoff": cutoff}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"READY output={collector.output}")
            return 0
        output = collector.run()
        return 0 if collector.completed else 1
    except Exception as exc:
        print(f"PRECHECK ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
