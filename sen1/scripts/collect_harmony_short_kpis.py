#!/usr/bin/env python3
"""Collect the ten HarmonyOS KPIs with a 16-action, eight-stage UI run.

Each stage consists of one ability launch and one harmless upward swipe.
The application order is the same as the original 460-event experiment.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from collect_harmony_auto_kpis import (
    APPS, Collector, compute_metrics, metric_delta, wall_time,
)


STAGES = ("腾讯会议", "浏览器", "海泰浏览器", "文件管理器", "WPS", "应用市场",
          "腾讯会议", "浏览器")


class ShortCollector(Collector):
    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.actions: list[dict] = []
        self.app_specs: dict = {}
        self.page_helpers: dict[str, dict] = {}
        self.first_processes: dict[str, dict[int, str]] = {}

    def preflight(self) -> dict:
        details = super().preflight()
        self.app_specs = json.loads(self.config.read_text(encoding="utf-8"))["apps"]
        for name in STAGES:
            spec = self.app_specs.get(name)
            if not spec or not spec.get("bundle") or not spec.get("ability"):
                raise RuntimeError(f"缺少 {name} 的启动配置")
        details["action_plan"] = [f"{name}: launch + swipe" for name in STAGES]
        details["action_count"] = 16
        if self.args.page_helper:
            if not self.args.page_helper.is_file():
                raise RuntimeError(f"缺少页负载程序：{self.args.page_helper}")
            assert self.hdc is not None
            free = self.hdc.shell("df -k /data/local/tmp | tail -n 1").split()
            if len(free) < 4 or not free[3].isdigit() or int(free[3]) < 1024 * 1024:
                raise RuntimeError("/data/local/tmp 可用空间不足 1 GiB")
            details["synthetic_pages"] = {
                "per_app_file_mib": 100, "per_app_anon_mib": 100,
                "helper_sha256": hashlib.sha256(self.args.page_helper.read_bytes()).hexdigest(),
                "meaning": "helper-held pages in app memory cgroup, not app-PID allocations",
            }
        return details

    def process_identities(self, bundle: str) -> dict[int, str]:
        assert self.hdc is not None
        identities = {}
        for pid_text in self.hdc.shell(f"pidof {bundle}", check=False).split():
            if pid_text.isdigit():
                raw = self.hdc.shell(f"cat /proc/{pid_text}/stat 2>/dev/null", check=False)
                tail = raw.rpartition(") ")[2].split()
                if len(tail) > 19:
                    identities[int(pid_text)] = tail[19]
        return identities

    def setup_page_helper(self) -> None:
        if self.args.page_helper:
            assert self.hdc is not None
            self.hdc.send(self.args.page_helper, self.remote + "/harmony_workload_pages")
            self.hdc.shell(f"chmod 700 {self.remote}/harmony_workload_pages")

    def start_app_pages(self, name: str) -> dict:
        assert self.hdc is not None
        index = next(i for i, (app, _, _) in enumerate(APPS) if app == name)
        base = f"{self.remote}/pages_{index}"
        self.hdc.shell(f"{self.remote}/harmony_workload_pages {base}.dat 100 100 1200 "
                       f">{base}.log 2>&1 </dev/null & echo $! >{base}.pid")
        pid_text = self.hdc.shell(f"cat {base}.pid").strip()
        if not pid_text.isdigit():
            raise RuntimeError(f"{name} 页负载 PID 无效：{pid_text}")
        pid = int(pid_text)
        info = {"pid": pid, "base": base, "allocated": False, "retouched": False}
        self.page_helpers[name] = info
        for _ in range(30):
            log = self.hdc.shell(f"cat {base}.log 2>/dev/null", check=False)
            if f"READY pid={pid} " in log:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"{name} 页负载未就绪：{log[-300:]}")
        cgroup = self.paths[name].removesuffix("/memory.current")
        self.hdc.shell(f"echo {pid} > {cgroup}/cgroup.procs")
        membership = self.hdc.shell(f"cat /proc/{pid}/cgroup | grep memory:", check=False)
        expected = "/100/" + cgroup.rsplit("/", 1)[1]
        if expected not in membership:
            raise RuntimeError(f"{name} 页负载 cgroup 归属错误：{membership.strip()}")
        self.hdc.shell(f"kill -USR1 {pid}")
        for _ in range(300):
            log = self.hdc.shell(f"cat {base}.log 2>/dev/null", check=False)
            if "ALLOCATED file_bytes=104857600 anon_bytes=104857600" in log:
                info["allocated"] = True
                return {"page_helper_pid": pid, "membership": membership.strip(),
                        "file_mib": 100, "anon_mib": 100}
            if any(error in log for error in ("file_setup", "mmap", "msync")):
                break
            time.sleep(0.2)
        raise RuntimeError(f"{name} 页负载分配失败：{log[-400:]}")

    def retouch_app_pages(self, name: str) -> dict:
        assert self.hdc is not None
        info = self.page_helpers[name]
        pid, base = info["pid"], info["base"]
        cmdline = self.hdc.shell(f"tr '\\0' ' ' </proc/{pid}/cmdline 2>/dev/null", check=False)
        if "harmony_workload_pages" not in cmdline:
            raise RuntimeError(f"{name} 页负载在加压后已退出")
        self.hdc.shell(f"kill -USR2 {pid}")
        for _ in range(100):
            log = self.hdc.shell(f"cat {base}.log 2>/dev/null", check=False)
            match = re.search(r"^RETOUCH file_minor=(\d+) file_major=(\d+) "
                              r"anon_minor=(\d+) anon_major=(\d+) checksum=(\d+)$", log, re.M)
            if match:
                info["retouched"] = True
                info["retouch_faults"] = {
                    "file_minor": int(match.group(1)), "file_major": int(match.group(2)),
                    "anon_minor": int(match.group(3)), "anon_major": int(match.group(4)),
                }
                return {"page_helper_pid": pid, "retouch_faults": info["retouch_faults"]}
            time.sleep(0.2)
        raise RuntimeError(f"{name} 页负载复用超时：{log[-400:]}")

    def stop_page_helpers(self) -> dict:
        assert self.hdc is not None
        result = {}
        for name, info in self.page_helpers.items():
            pid, base = info["pid"], info["base"]
            cmdline = self.hdc.shell(
                f"tr '\\0' ' ' </proc/{pid}/cmdline 2>/dev/null", check=False)
            if "harmony_workload_pages" in cmdline:
                self.hdc.shell(f"kill -TERM {pid}", check=False)
            time.sleep(0.2)
            local = self.output / (Path(base).name + ".log")
            size = self.hdc.shell(f"stat -c %s {base}.log 2>/dev/null", check=False).strip()
            pulled = False
            if size.isdigit():
                self.hdc.recv(base + ".log", local)
                pulled = local.stat().st_size == int(size)
            result[name] = {**info, "log_pulled": pulled,
                            "log_path": str(local) if pulled else None}
        return result

    def record_action(self, stage: int, name: str, action: str, evidence: dict) -> None:
        item = {"stage": stage, "app": name, "action": action,
                "wall_time": wall_time(), **evidence}
        self.actions.append(item)
        print(f"ACTION {len(self.actions):02d}/16 {stage}/8 {name} {action}", flush=True)

    def focused_bundle(self, stage: int, expected: str) -> str:
        assert self.hdc is not None
        last_focused: list[str] = []
        for attempt in range(1, 5):
            layout_path = f"{self.remote}/layout_{stage}_{attempt}.json"
            self.hdc.shell(f"uitest dumpLayout -p {layout_path} -a", timeout=45)
            tree = json.loads(self.hdc.shell(f"cat {layout_path}", timeout=45))
            focused = [node.get("attributes", {}).get("bundleName", "")
                       for node in tree.get("children", [])
                       if node.get("attributes", {}).get("focused") == "true"]
            if focused == [expected]:
                return expected
            last_focused = focused
            time.sleep(1)
        raise RuntimeError(f"第 {stage} 段未切到 {expected}：前台为 {last_focused}")

    def launch_and_swipe(self, stage: int, name: str) -> None:
        assert self.hdc is not None
        spec = self.app_specs[name]
        command = (f"aa start -b {spec['bundle']} -a {spec['ability']} "
                   f"-m {spec['module']}")
        output = self.hdc.shell(command, timeout=45)
        if re.search(r"\b(error|failed|invalid)\b", output, re.I):
            raise RuntimeError(f"第 {stage} 段 {name} 启动失败：{output[:300]}")
        expected = spec["bundle"]
        actual = self.focused_bundle(stage, expected)
        memory_path = self.paths[name]
        current = self.hdc.shell(f"cat {memory_path} 2>/dev/null || echo MISSING").strip()
        if not current.isdigit():
            raise RuntimeError(f"第 {stage} 段 {name} cgroup 不存在：{current}")
        extra: dict = {}
        if stage <= 6:
            if self.args.page_helper:
                extra = self.start_app_pages(name)
            if name in ("腾讯会议", "浏览器"):
                self.first_processes[name] = self.process_identities(expected)
                if not self.first_processes[name]:
                    raise RuntimeError(f"{name} 首次进入后未找到应用主进程")
        elif self.args.page_helper:
            current_processes = self.process_identities(expected)
            original = self.first_processes[name]
            if not any(current_processes.get(pid) == started
                       for pid, started in original.items()):
                raise RuntimeError(f"{name} 应用进程在复用前已重启，不能算热应用复用")
            extra = self.retouch_app_pages(name)
        self.record_action(stage, name, "launch", {"focused_bundle": actual,
                                                   "memory_current_bytes": int(current),
                                                   "aa_output": output.strip(), **extra})
        self.hdc.shell("uitest uiInput swipe 1560 1500 1560 700 350", timeout=30)
        self.record_action(stage, name, "swipe_up", {})

    def write_result(self, result: dict) -> None:
        result["markers"] = self.markers
        result["snapshots"] = self.snapshots
        result["actions"] = self.actions
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

    def run(self) -> Path:
        preflight = self.preflight()
        result: dict = {
            "status": "incomplete", "preflight": preflight,
            "metric_scope": {
                "faults": "device-wide /proc/vmstat delta during two-app reuse",
                "reclaim": "positive net memory.current decrease from before to after pressure; proxy",
            },
            "automation_coverage": {"actions": 16, "stages": list(STAGES), "continuous": True},
        }
        try:
            assert self.hdc is not None
            self.hdc.shell(f"mkdir -p {self.remote}")
            self.setup_page_helper()
            # The prior interrupted run left three target apps alive.  Start
            # from a known cold state, then let the second hot-app visits reuse.
            for name, bundle, _ in APPS:
                self.hdc.shell(f"aa force-stop {bundle}", check=False)
            time.sleep(1)
            self.mark("开始采集")
            self.snapshot("start", memory_keys=("total",))
            for stage, name in enumerate(STAGES[:6], start=1):
                self.launch_and_swipe(stage, name)
            self.mark("首次六应用操作完成")
            before = self.snapshot("before_pressure")
            self.start_pressure()
            self.wait_pressure_allocated()
            deadline = time.monotonic() + self.args.pressure_wait_seconds
            while time.monotonic() < deadline:
                time.sleep(min(1, deadline - time.monotonic()))
                if self.pressure_pid is not None:
                    alive = self.hdc.shell(
                        f"test -d /proc/{self.pressure_pid} && echo yes || echo no",
                        check=False)
                    if "no" in alive:
                        raise RuntimeError("加压进程在观察等待期间退出")
            after = self.snapshot("after_pressure_before_reuse")
            result["pressure"] = self.stop_pressure()
            if result["pressure"]["status"] != "complete":
                raise RuntimeError("加压停止或日志校验不完整")
            fault_start = self.snapshot("reuse_fault_baseline", vmstat_last=True)
            self.mark("开始复用")
            for stage, name in enumerate(STAGES[6:], start=7):
                self.launch_and_swipe(stage, name)
            self.mark("操作完成")
            end = self.snapshot("end", memory_keys=("total",))
            result["metrics"] = compute_metrics(before, after, end, fault_start)
            result["metrics"]["full_window_faults"] = metric_delta(
                self.snapshots[0]["vmstat"], end["vmstat"])
            if (len(self.actions) == 16 and result["pressure"]["status"] == "complete"
                    and (not self.args.page_helper or
                         (len(self.page_helpers) == 6
                          and all(item["allocated"] for item in self.page_helpers.values())
                          and all(self.page_helpers[name]["retouched"]
                                  for name in ("腾讯会议", "浏览器"))))
                    and all(result["metrics"][key]["status"] == "complete"
                            for key in ("anon_refault", "anon_major_fault", "anon_minor_fault",
                                        "file_major_fault", "file_minor_fault"))):
                result["status"] = "complete"
                self.completed = True
        except (Exception, KeyboardInterrupt) as exc:
            result["error"] = str(exc)
            print(f"ERROR {exc}", file=sys.stderr, flush=True)
        finally:
            if self.pressure_pid is not None:
                try:
                    result["pressure"] = self.stop_pressure()
                except Exception as exc:
                    result["pressure_cleanup_error"] = str(exc)
            if self.args.page_helper:
                try:
                    result["synthetic_pages"] = self.stop_page_helpers()
                    if self.completed and not all(
                            info["log_pulled"] for info in result["synthetic_pages"].values()):
                        result["status"] = "incomplete"
                        self.completed = False
                        result["error"] = "页负载日志拉取不完整"
                except Exception as exc:
                    result["synthetic_cleanup_error"] = str(exc)
                    result["status"] = "incomplete"
                    self.completed = False
            self.write_result(result)
            if self.completed and result.get("pressure", {}).get("log_pulled"):
                assert self.remote.startswith("/data/local/tmp/harmony_auto_kpi_")
                self.hdc.shell(f"rm -rf {self.remote}", check=False)
        print(f"DONE status={result['status']} output={self.output}", flush=True)
        return self.output


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--hdc")
    parser.add_argument("--target")
    parser.add_argument("--pressure-helper", type=Path, required=True)
    parser.add_argument("--page-helper", type=Path,
                        help="synthetic 100 MiB file + 100 MiB anon load per app")
    parser.add_argument("--pressure-mib", type=int, default=16384)
    parser.add_argument("--pressure-reserve-mib", type=int, default=4096)
    parser.add_argument("--pressure-max-mib", type=int, default=16384)
    parser.add_argument("--pressure-timeout", type=int, default=1800)
    parser.add_argument("--pressure-allocation-timeout", type=int, default=300)
    parser.add_argument("--pressure-wait-seconds", type=float, default=10)
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--max-sleep", type=float, default=0)
    parser.add_argument("--resume-from-row", type=int, default=1)
    parser.add_argument("--prior-run", type=Path)
    args = parser.parse_args()
    collector = ShortCollector(args)
    collector.run()
    return 0 if collector.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
