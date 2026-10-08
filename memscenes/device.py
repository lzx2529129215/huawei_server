from __future__ import annotations
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from .model import parse_stat, parse_proc_identity, key_values

def quote(value) -> str:
    return shlex.quote(str(value))

class Device:
    def __init__(self, executable: str, target: str | None = None):
        self.executable = str(executable)
        self.base = [self.executable]
        self.command_timeout = 45
        targets = self.call("list", "targets")
        online = [x.strip().split()[0] for x in targets.splitlines()
                  if x.strip() and "Empty" not in x and "[" not in x]
        if target is None and len(online) == 1:
            target = online[0]
        if target not in online:
            raise RuntimeError(f"设备未唯一确定或离线：{online}")
        self.target = target
        self.base += ["-t", target]
        self.apps: dict[str, dict] = {}

    def call(self, *args, timeout=None, check=True) -> str:
        timeout = self.command_timeout if timeout is None else timeout
        result = subprocess.run(self.base + [str(x) for x in args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        text = result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if check and result.returncode:
            raise RuntimeError(f"hdc操作失败({result.returncode}): {text[:500]}")
        return text

    def shell(self, command: str, **kwargs) -> str:
        return self.call("shell", command, **kwargs)

    def receive(self, source: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.call("file", "recv", source, str(path), timeout=180)
        if not path.is_file():
            raise RuntimeError(f"证据文件未取回：{source}: {text}")

    def app(self, bundle: str) -> dict:
        if bundle not in self.apps:
            raw = self.shell("bm dump -n " + quote(bundle))
            match = re.search(r'"uid"\s*:\s*(\d+)', raw)
            if not match:
                raise RuntimeError(f"应用未安装：{bundle}")
            uid = int(match[1])
            version_code = re.search(r'"versionCode"\s*:\s*(\d+)', raw)
            version_name = re.search(r'"versionName"\s*:\s*"([^"]+)"', raw)
            self.apps[bundle] = {"uid": uid,
                                 "version_code": int(version_code[1]) if version_code else None,
                                 "version_name": version_name[1] if version_name else None,
                                 "cgroup": f"/dev/cgroup/memory/default/100/{bundle}_{uid}",
                                 "files": f"/data/app/el2/100/base/{bundle}/haps/entry/files"}
        return self.apps[bundle]

    def app_processes(self, bundle: str) -> dict[str, dict]:
        spec = self.app(bundle)
        cgroup = spec["cgroup"]
        script = (
            f"for p in $(cat {quote(cgroup + '/cgroup.procs')} 2>/dev/null); do "
            'test -r /proc/$p/stat && cat /proc/$p/stat; done'
        )
        result = {}
        for line in self.shell(script, check=False).splitlines():
            try:
                item = parse_stat(line)
                result[f"{item['pid']}:{item['start_ticks']}"] = item
            except (ValueError, IndexError):
                pass
        if not result:
            # A cgroup can disappear while an app is restarting.
            for p in self.shell("pidof " + quote(bundle), check=False).split():
                if p.isdigit():
                    raw = self.shell(f"cat /proc/{p}/stat", check=False).strip()
                    try:
                        item = parse_stat(raw)
                        result[f"{item['pid']}:{item['start_ticks']}"] = item
                    except (ValueError, IndexError):
                        pass
        return result

    def inspect_process_identity(self, pid: int, timeout: float = 20) -> dict:
        """Capture raw stat/status from a journal-known PID without cgroup lookup."""
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("invalid process id")
        raw = self.shell(
            f"echo __MEMSCENE_UPTIME__; cat /proc/uptime 2>&1; "
            f"echo __MEMSCENE_STAT__; cat /proc/{pid}/stat 2>&1; "
            f"echo __MEMSCENE_STATUS__; cat /proc/{pid}/status 2>&1",
            timeout=timeout, check=False)
        result = {"raw": raw, "identity": None, "uptime_seconds": None, "parse_error": None}
        try:
            uptime_section, remainder = raw.split("__MEMSCENE_STAT__\n", 1)
            stat_raw, separator, status_raw = remainder.partition("__MEMSCENE_STATUS__\n")
            if not separator:
                raise ValueError("direct proc response lacks status section")
            uptime_text = uptime_section.rpartition("__MEMSCENE_UPTIME__\n")[2].strip()
            result["uptime_seconds"] = float(uptime_text.split()[0])
            result["identity"] = parse_proc_identity(stat_raw.strip(), status_raw)
        except (IndexError, ValueError) as exc:
            result["parse_error"] = str(exc)
        return result

    def private_memory(self, bundle: str) -> int:
        total = 0
        for row in self.app_processes(bundle).values():
            raw = self.shell(f"cat /proc/{row['pid']}/smaps_rollup", check=False)
            values = key_values(raw)
            if "Pss" in values:
                total += values["Pss"] + values.get("SwapPss", values.get("Swap", 0))
            else:
                raw = self.shell(f"cat /proc/{row['pid']}/status", check=False)
                values = key_values(raw)
                total += values.get("VmRSS", 0) + values.get("VmSwap", 0)
        return total

    def launch(self, bundle: str, ability="EntryAbility", module="entry", params=None,
               timeout: int = 60) -> str:
        command = f"aa start -b {quote(bundle)} -a {quote(ability)} -m {quote(module)}"
        for key, value in (params or {}).items():
            if isinstance(value, int):
                command += f" --pi {quote(key)} {value}"
            else:
                command += f" --ps {quote(key)} {quote(value)}"
        output = self.shell(command, timeout=timeout)
        if re.search(r"\b(error|failed|invalid)\b", output, re.I):
            raise RuntimeError(output.strip())
        return output

    def promote(self, bundle: str, case_id: str) -> str:
        return self.launch(bundle, params={"op": "promote", "case_id": case_id},
                           timeout=min(self.command_timeout, 15))

    def focus(self, bundle: str) -> str:
        return self.launch(bundle, params={"op": "focus"}, timeout=min(self.command_timeout, 15))

    def force_stop(self, bundle: str) -> None:
        self.shell("aa force-stop " + quote(bundle), check=False)

    def journal(self, bundle: str, case_id: str) -> tuple[str, list[dict]]:
        from .model import json_lines
        path = self.app(bundle)["files"] + f"/memscene_{case_id}.jsonl"
        raw = self.shell("cat " + quote(path), check=False)
        return raw, [row for row in json_lines(raw) if row.get("case_id") == case_id]

    def remove_owned_case_pages(self, bundle: str, case_id: str) -> None:
        if not re.fullmatch(r"s2_[a-f0-9]+_c\d{4}|s2_[a-f0-9]+_single\d+_l\d+|s1_[a-f0-9]+_background", case_id):
            raise ValueError("refusing to remove an unowned workload file")
        path = self.app(bundle)["files"] + "/memscene_" + case_id + ".pages"
        self.shell("rm -f " + quote(path), check=False)
        check = self.shell("if test -e " + quote(path) + "; then echo present; else echo absent; fi", check=False).strip()
        if check != "absent":
            raise RuntimeError(f"owned backing file cleanup not verified: {path} ({check})")
        return {"path": path, "absent": True, "verification": check}

    def dump_ui(self, remote: str, timeout: int = 45) -> dict:
        message = self.shell("uitest dumpLayout -a -p " + quote(remote), timeout=timeout)
        raw = self.shell("cat " + quote(remote), timeout=timeout, check=False)
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeError("界面证据获取失败：" + message[:300])

    def dismiss_owned_install_prompts(self, remote: str) -> None:
        # Newly installed workload packages prompt for desktop icons. Only dismiss
        # prompts bearing our exact app label, never unrelated application dialogs.
        for _ in range(12):
            tree = self.dump_ui(remote)
            nodes = list(ui_nodes(tree))
            texts = " ".join(str(row.get("text", "")) for row in nodes)
            if "内存场景负载" not in texts or "应用图标到桌面" not in texts:
                return
            button = next((row for row in nodes if row.get("text") == "不添加"), None)
            if button is None:
                return
            coords = [int(number) for number in re.findall(r"\d+", button.get("bounds", ""))]
            if len(coords) != 4:
                return
            self.shell(f"uitest uiInput click {(coords[0]+coords[2])//2} {(coords[1]+coords[3])//2}")
            time.sleep(0.3)

    def screenshot(self, remote: str, destination: Path) -> None:
        self.shell("uitest screenCap -p " + quote(remote), timeout=45)
        self.receive(remote, destination)

    def now_ms(self) -> int:
        value = self.shell("date +%s").strip()
        if not value.isdigit():
            raise RuntimeError("无法读取设备时间")
        return int(value) * 1000

    def clock_pair(self) -> tuple[float, int]:
        lines = self.shell("cat /proc/uptime; date +%s").splitlines()
        if len(lines) < 2 or not lines[1].strip().isdigit():
            raise RuntimeError("无法读取同一查询内的设备单调时间与墙钟")
        return float(lines[0].split()[0]), int(lines[1].strip()) * 1000

    def environment(self) -> dict:
        meminfo = self.shell("cat /proc/meminfo")
        values = key_values(meminfo)
        kernel = self.shell("uname -a").strip()
        nominal = self.shell("param get ohos.boot.vendor.ddrsize", check=False).strip()
        storage = self.shell("df -k /data", check=False)
        return {"target": self.target, "kernel": kernel, "meminfo": values,
                "data_storage_df": storage,
                "reported_dram_gib": int(nominal) if nominal.isdigit() else None,
                "device_time_ms": self.now_ms(), "hdc_version": self.call("-v").strip()}

def ui_nodes(tree: dict):
    stack = [(tree, "")]
    while stack:
        node, inherited_bundle = stack.pop()
        if isinstance(node, dict):
            attributes = node.get("attributes", {})
            owner = attributes.get("bundleName") or inherited_bundle
            yield {**attributes, "__ownerBundleName": owner}
            stack.extend((child, owner) for child in node.get("children", []))

def focused_bundles(tree: dict) -> set[str]:
    return {row.get("bundleName", "") for row in ui_nodes(tree) if str(row.get("focused")).lower() == "true"}
