#!/usr/bin/env python3
"""Reload the prepared six-window browser workload without creating tabs.

The default is a no-input dry run. Device input requires both --execute and
--confirm-ui-reload. Before clicking thumbnails or page controls, the tool
checks the known browser window IDs, owner PID, visibility, and WindowRect via
WindowManagerService. It stops immediately when observed state differs.
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
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from prepare import DEFAULT_BUNDLE, Hdc  # noqa: E402

SCREEN_WIDTH = 3120
SCREEN_HEIGHT = 2080
SCREEN_CAP_PREFIX = "uitest screenCap -p"
BROWSER_TASKBAR = (1508, 2015)
THUMBNAIL_X = (407, 840, 1280, 1720, 2160, 2610)
THUMBNAIL_Y = 1771
EXPECTED_WINDOW_RECTS = (
    (515, 281, 2090, 1394),
    (582, 348, 2090, 1394),
    (648, 414, 2090, 1394),
    (715, 481, 2090, 1394),
    (781, 76, 2090, 1394),
    (848, 143, 2090, 1394),
)

# Each coordinate tuple is (x, y), in 3120x2080 screenshot coordinates.
WINDOW_UI = (
    {"tabs": ((800, 320), (1350, 320), (1900, 320)), "reload": (718, 405), "minimize": (2488, 320)},
    {"tabs": ((880, 385), (1430, 385), (1980, 385)), "reload": (787, 472), "minimize": (2555, 385)},
    {"tabs": ((945, 451), (1495, 451), (2045, 451)), "reload": (853, 537), "minimize": (2620, 452)},
    {"tabs": ((1010, 519), (1560, 519), (2110, 519)), "reload": (920, 606), "minimize": (2688, 519)},
    {"tabs": ((1070, 114), (1620, 114), (2170, 114)), "reload": (986, 200), "minimize": (2754, 114)},
    {"tabs": ((1140, 180), (1690, 180), (2240, 180)), "reload": (1053, 268), "minimize": (2821, 180)},
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def png_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ValueError("screenshot is not a complete PNG with an IHDR header")
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def parse_wms_table(raw: str) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        match = re.match(r"^\s*(browser\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s|$)", line)
        if match:
            name = match.group(1)
            if name in found:
                raise ValueError(f"WMS has duplicate {name} rows")
            found[name] = (int(match.group(3)), int(match.group(4)))
    return found


def parse_wms_detail(raw: str) -> dict:
    def field(name: str) -> str:
        match = re.search(rf"(?mi)^{re.escape(name)}:\s*(.*?)\s*$", raw)
        if not match:
            raise ValueError(f"WMS detail lacks {name}")
        return match.group(1)

    rect_match = re.search(r"(?mi)^WindowRect:\s*\[\s*([\d,\s]+)\]", raw)
    visible = field("IsVisible").lower()
    if visible not in ("true", "false") or not rect_match:
        raise ValueError("WMS detail visibility or WindowRect malformed")
    rect = tuple(int(part.strip()) for part in rect_match.group(1).split(","))
    if len(rect) != 4:
        raise ValueError("WMS WindowRect must contain x,y,width,height")
    return {"name": field("WindowName"), "win_id": int(field("WinId")),
            "pid": int(field("Pid")), "visible": visible == "true", "rect": rect,
            "raw": raw}


def validate_window_state(raw_table: str, details_raw: dict[str, str],
                          owner_pid: int, expected_visibility: dict[int, bool]) -> dict:
    table = parse_wms_table(raw_table)
    expected_names = {f"browser{i}" for i in range(6)}
    if set(table) != expected_names:
        raise ValueError(f"expected exactly browser0..browser5 in WMS, observed {sorted(table)}")
    expected_ids = set(range(1068, 1074))
    observed_ids = {win_id for _, win_id in table.values()}
    if observed_ids != expected_ids:
        raise ValueError(f"expected browser WinIds 1068..1073, observed {sorted(observed_ids)}")
    out = {}
    for i in range(6):
        name = f"browser{i}"
        row_pid, row_id = table[name]
        detail = parse_wms_detail(details_raw[name])
        expected_rect = EXPECTED_WINDOW_RECTS[i]
        if row_pid != owner_pid or detail["pid"] != owner_pid:
            raise ValueError(f"{name} owner PID mismatch: expected {owner_pid}, row={row_pid}, detail={detail['pid']}")
        if detail["name"] != name or detail["win_id"] != row_id or row_id != 1068 + i:
            raise ValueError(f"{name} WMS row/detail identity mismatch")
        if detail["rect"] != expected_rect:
            raise ValueError(f"{name} WindowRect mismatch: expected {expected_rect}, observed {detail['rect']}")
        if i in expected_visibility and detail["visible"] is not expected_visibility[i]:
            raise ValueError(f"{name} visibility mismatch: expected {expected_visibility[i]}, observed {detail['visible']}")
        out[name] = {k: detail[k] for k in ("pid", "win_id", "visible", "rect")}
    visible = [i for i in range(6) if out[f"browser{i}"]["visible"]]
    should_be_visible = [i for i, value in expected_visibility.items() if value]
    if set(visible) != set(should_be_visible):
        raise ValueError(f"visible browser window set mismatch: expected {should_be_visible}, observed {visible}")
    return {"windows": out, "visible_indices": visible}


class Runner:
    def __init__(self, hdc_path: Path, target: str, output: Path, remote_root: str,
                 owner_pid: int, guided: bool, hdc: Hdc | None = None):
        self.hdc_path = hdc_path.resolve(strict=True)
        self.target = target
        self.output = output.resolve()
        self.remote_root = remote_root
        self.owner_pid = owner_pid
        self.guided = guided
        self.hdc = hdc or Hdc(str(self.hdc_path), target)
        self.actions: list[dict] = []
        self.screenshot_sequence = 0

    def shell_checked(self, command: str, timeout: int = 25, marker: str | None = None) -> str:
        result = subprocess.run([str(self.hdc_path), "-t", self.target, "shell", command],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
        output = result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        if result.returncode:
            raise RuntimeError(f"HDC shell rc={result.returncode}: {output[-1000:]}")
        if marker:
            match = re.search(rf"(?m)^__{re.escape(marker)}=(\d+)\s*$", output)
            if not match:
                raise RuntimeError(f"device command lacks __{marker}= exit marker: {output[-1000:]}")
            if int(match.group(1)) != 0:
                raise RuntimeError(f"device command __{marker}={match.group(1)}: {output[-1000:]}")
            output = output[:match.start()].rstrip()
        return output.strip()

    def record(self, name: str, command: str, fn) -> object:
        row = {"name": name, "command": command,
               "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "started_monotonic": time.monotonic()}
        self.actions.append(row)
        try:
            result = fn()
            row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            row["duration_seconds"] = round(time.monotonic() - row["started_monotonic"], 3)
            row["ok"] = True
            if isinstance(result, dict):
                row["result"] = result
            elif result is not None:
                row["result"] = str(result)
            return result
        except Exception as exc:
            row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            row["duration_seconds"] = round(time.monotonic() - row["started_monotonic"], 3)
            row["ok"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise

    def wms(self, expected_visibility: dict[int, bool]) -> dict:
        table = self.hdc.shell("hidumper -s WindowManagerService -a '-a'", timeout=20)
        rows = parse_wms_table(table)
        details_raw = {}
        for name, (_, win_id) in sorted(rows.items()):
            details_raw[name] = self.hdc.shell(
                "hidumper -s WindowManagerService -a " + shlex.quote(f"-w {win_id}"), timeout=15)
        state = validate_window_state(table, details_raw, self.owner_pid, expected_visibility)
        state["raw_table"] = table
        state["raw_details"] = details_raw
        return state

    def screenshot(self, label: str) -> dict:
        self.screenshot_sequence += 1
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        name = f"{self.screenshot_sequence:03d}_{safe_label}.png"
        remote = f"{self.remote_root}/{name}"
        local = self.output / "screenshots" / name
        local.parent.mkdir(parents=True, exist_ok=True)
        capture = self.shell_checked(f"{SCREEN_CAP_PREFIX} {shlex.quote(remote)}; __rc=$?; echo __CAP_RC=$__rc",
                                    timeout=30, marker="CAP_RC")
        if "__CAP_RC=" in capture:
            raise RuntimeError("unparsed uitest screenCap result")
        remote_sha = self.hdc.shell("sha256sum " + shlex.quote(remote), timeout=15).split()
        if not remote_sha or not re.fullmatch(r"[0-9a-fA-F]{64}", remote_sha[0]):
            raise RuntimeError(f"remote screenshot hash is malformed for {remote}")
        if local.exists():
            raise RuntimeError(f"refusing to overwrite prior screenshot {local}")
        result = subprocess.run([str(self.hdc_path), "-t", self.target, "file", "recv",
                                 remote, str(local)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=90, check=False)
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode or not local.is_file() or local.stat().st_size <= 24:
            raise RuntimeError(f"HDC screenshot receive failed (rc={result.returncode}): {output[-1000:]}")
        local_sha = sha256_file(local)
        if local_sha.lower() != remote_sha[0].lower():
            raise RuntimeError(f"screenshot hash mismatch for {remote}: {local_sha} != {remote_sha[0]}")
        width, height = png_dimensions(local.read_bytes()[:32])
        if (width, height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
            raise RuntimeError(f"screenshot resolution mismatch: expected {SCREEN_WIDTH}x{SCREEN_HEIGHT}, got {width}x{height}")
        self.shell_checked(f"rm -f {shlex.quote(remote)}; __rc=$?; echo __RM_RC=$__rc",
                           timeout=15, marker="RM_RC")
        return {"local_path": str(local), "sha256": local_sha, "bytes": local.stat().st_size,
                "width": width, "height": height, "device_capture_output": capture,
                "hdc_receive_output": output.strip()}

    def click(self, name: str, x: int, y: int) -> str:
        if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
            raise ValueError(f"click outside calibrated display: ({x},{y})")
        cmd = f"uitest uiInput click {x} {y}; __rc=$?; echo __UI_RC=$__rc"
        return self.shell_checked(cmd, timeout=20, marker="UI_RC")

    def guided_checkpoint(self, message: str) -> bool:
        if self.guided:
            row = {"name": "guided_operator_confirmation",
                   "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "prompt": message}
            self.actions.append(row)
            answer = input(message + " Review the saved screenshot and type CONTINUE: ").strip()
            if answer != "CONTINUE":
                row["ok"] = False
                row["answer"] = answer
                row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
                raise RuntimeError("operator stopped at guided checkpoint")
            row["ok"] = True
            row["answer"] = "CONTINUE"
            row["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            return True
        return False


def validate_preparation(manifest_path: Path, workload_path: Path) -> tuple[dict, dict, int]:
    prep = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    workload = json.loads(workload_path.read_text(encoding="utf-8-sig"))
    if prep.get("schema") != "browser-donor-preparation-v1" or prep.get("bundle") != DEFAULT_BUNDLE:
        raise ValueError("preparation manifest is not the reviewed browser donor schema/bundle")
    if prep.get("eligible_for_review") is not True or prep.get("expected_windows") != 6:
        raise ValueError("preparation manifest is not reviewed as eligible for six windows")
    if workload.get("schema") != "browser-donor-workload-v1" or workload.get("window_count") != 6 or workload.get("pages_per_window") != 3:
        raise ValueError("workload manifest must describe exactly six windows by three pages")
    uid = prep.get("uid")
    if not isinstance(uid, int) or uid <= 0:
        raise ValueError("preparation manifest lacks a valid browser package UID")
    samples = prep.get("samples") or []
    if not samples:
        raise ValueError("preparation manifest has no reviewed sample")
    latest = samples[-1]
    if latest.get("identity_verified") is not True or latest.get("browser_window_count") != 6:
        raise ValueError("latest preparation sample lacks stable browser identity/six windows")
    pids = {int(row.get("pid", -1)) for row in latest.get("processes", [])}
    window_rows = latest.get("browser_windows", [])
    owners = {int(row.get("pid", -1)) for row in window_rows
              if row.get("is_browser_window") is True}
    if len(owners) != 1 or len(pids) < 1:
        raise ValueError("reviewed WMS sample does not identify one browser window owner")
    owner_pid = next(iter(owners))
    windows = sorted((row for row in window_rows if row.get("is_browser_window") is True),
                     key=lambda row: row.get("name", ""))
    expected = []
    if len(windows) != 6:
        raise ValueError("preparation sample must contain six browser windows")
    for i, row in enumerate(windows):
        rect_match = re.search(r"(?mi)^WindowRect:\s*\[\s*([\d,\s]+)\]", row.get("detail_raw", ""))
        if not rect_match:
            raise ValueError(f"reviewed preparation sample lacks {row.get('name')} WindowRect")
        rect = tuple(int(part.strip()) for part in rect_match.group(1).split(","))
        if (row.get("name") != f"browser{i}" or row.get("win_id") != 1068 + i or
                int(row.get("pid", -1)) != owner_pid or rect != EXPECTED_WINDOW_RECTS[i]):
            raise ValueError(f"reviewed window identity/position differs from calibrated browser{i}")
        expected.append({"name": f"browser{i}", "win_id": 1068 + i,
                         "owner_pid": owner_pid, "rect": list(rect)})
    if (workload.get("expected_page_sha256") != "2a791ac3d6567f14d5613f03cad6504b1f6fe9050e5e28851d5c6f3184db0506" or
            len(workload.get("windows", [])) != 6 or
            any(len(window.get("pages", [])) != 3 for window in workload.get("windows", []))):
        raise ValueError("workload manifest page count/hash differs from the reviewed 6x3 inputs")
    workload_dir = workload_path.resolve().parent
    for window in workload["windows"]:
        for page in window["pages"]:
            local_page = (workload_dir / page["path"]).resolve(strict=True)
            if sha256_file(local_page).lower() != page["sha256"].lower():
                raise ValueError(f"local source HTML hash mismatch for {page['label']}: {local_page}")
    return prep, workload, owner_pid


def verify_device_pages(runner: Runner, workload: dict) -> list[dict]:
    verified = []
    expected_sha = workload["expected_page_sha256"].lower()
    for window in workload["windows"]:
        for page in window["pages"]:
            device_path = page["device_path"]
            output = runner.hdc.shell("sha256sum " + shlex.quote(device_path), timeout=15)
            tokens = output.split()
            if not tokens or tokens[0].lower() != expected_sha:
                observed = tokens[0] if tokens else "<no hash output>"
                raise ValueError(f"device page hash mismatch for {device_path}: {observed}")
            verified.append({"label": page["label"], "device_path": device_path,
                             "ui_uri": page["ui_uri"], "sha256": tokens[0].lower()})
    return verified


def run(args: argparse.Namespace) -> dict:
    if not args.execute or not args.confirm_ui_reload:
        raise ValueError("UI input requires both --execute and --confirm-ui-reload")
    prep, workload, owner_pid = validate_preparation(args.preparation_manifest, args.workload_manifest)
    if args.target != prep.get("device_target"):
        raise ValueError(f"selected HDC target differs from preparation manifest: {args.target!r}")
    window_filter = args.window
    if window_filter is not None and not 1 <= window_filter <= 6:
        raise ValueError("--window must be 1..6")
    if args.target != prep.get("device_target"):
        raise ValueError("selected HDC target differs from preparation manifest")
    output = args.output.resolve()
    hdc = Hdc(str(args.hdc.resolve(strict=True)), args.target)
    output.mkdir(parents=True, exist_ok=False)
    (output / "screenshots").mkdir()
    run_id = uuid.uuid4().hex[:12]
    remote_root = "/data/local/tmp/browser_reload_" + run_id
    runner = Runner(args.hdc, args.target, output, remote_root, owner_pid, args.guided, hdc=hdc)
    report = {"schema": "browser-donor-reload-v1", "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "target": args.target, "bundle": DEFAULT_BUNDLE, "owner_pid": owner_pid,
              "preparation_manifest": str(args.preparation_manifest.resolve()),
              "workload_manifest": str(args.workload_manifest.resolve()),
              "workload_sha256": workload.get("expected_page_sha256"),
              "window_filter": window_filter, "guided": args.guided,
              "device_mutations": ["UI clicks only", "temporary uitest screenCap files under unique /data/local/tmp directory"],
              "new_tabs_created": 0, "browser_closed": False, "system_settings_changed": False,
              "status": "preflight", "actions": runner.actions, "verified_pages": [],
              "screenshots": []}
    report_path = output / "reload_report.json"
    write_report(report_path, report)
    try:
        runner.shell_checked("mkdir " + shlex.quote(remote_root) + "; __rc=$?; echo __MKDIR_RC=$__rc",
                             timeout=15, marker="MKDIR_RC")
        report["verified_pages"] = verify_device_pages(runner, workload)
        (output / "device_page_hashes.json").write_text(
            json.dumps(report["verified_pages"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        initial = runner.record("initial_screenshot", SCREEN_CAP_PREFIX, lambda: runner.screenshot("initial"))
        report["screenshots"].append(initial)
        hidden_state = runner.record("preflight_hidden_wms", "WMS identity/position/visibility check",
                                     lambda: runner.wms({i: False for i in range(6)}))
        report["preflight_wms"] = hidden_state
        report["status"] = "running"
        windows = range(6) if window_filter is None else (window_filter - 1,)
        for i in windows:
            layout = WINDOW_UI[i]
            runner.record("open_browser_taskbar", f"uitest click {BROWSER_TASKBAR}",
                          lambda: runner.click("open_browser_taskbar", *BROWSER_TASKBAR))
            time.sleep(1)
            shot = runner.record("browser_thumbnail_switcher", SCREEN_CAP_PREFIX, lambda: runner.screenshot(f"w{i+1}_switcher"))
            report["screenshots"].append(shot)
            runner.guided_checkpoint(f"w{i+1} thumbnail switcher saved at {shot['local_path']}; verify thumbnails before selection.")

            thumb = (THUMBNAIL_X[i], THUMBNAIL_Y)
            runner.record("select_window_thumbnail", f"uitest click {thumb}",
                          lambda: runner.click("select_window_thumbnail", *thumb))
            time.sleep(1)
            shot = runner.record("selected_window", SCREEN_CAP_PREFIX, lambda: runner.screenshot(f"w{i+1}_selected"))
            report["screenshots"].append(shot)
            visible_state = runner.record("selected_window_wms", "verify selected browser window visible/in-place",
                                          lambda: runner.wms({j: j == i for j in range(6)}))
            report.setdefault("window_checks", []).append({"window": i + 1, "phase": "selected", "wms": visible_state})
            runner.guided_checkpoint(f"w{i+1} selected screenshot saved at {shot['local_path']}; verify URL/window before reload.")

            for page_i, tab_xy in enumerate(layout["tabs"]):
                page = workload["windows"][i]["pages"][page_i]
                runner.record("select_tab", f"uitest click {tab_xy}",
                              lambda xy=tab_xy: runner.click("select_tab", *xy))
                time.sleep(0.25)
                runner.record("verify_tab_window", "WMS owner/visible/WindowRect check",
                              lambda: runner.wms({j: j == i for j in range(6)}))
                runner.record("reload_tab", f"uitest click {layout['reload']}",
                              lambda xy=layout["reload"]: runner.click("reload_tab", *xy))
                time.sleep(2)
                shot = runner.record("loaded_page_screenshot", SCREEN_CAP_PREFIX, lambda: runner.screenshot(f"w{i+1}_p{page_i+1}_reloaded"))
                report["screenshots"].append(shot)
                check = runner.record("verify_reloaded_window", "WMS owner/visible/WindowRect check",
                                      lambda: runner.wms({j: j == i for j in range(6)}))
                page_check = {"window": i + 1, "page": page["label"],
                    "device_path": page["device_path"], "ui_uri": page["ui_uri"],
                    "expected_sha256": page["sha256"], "phase": "reloaded", "wms": check,
                    "screenshot": shot["local_path"],
                    "page_url_visually_confirmed": False,
                    "url_verification": "not_automatically_verified"}
                report.setdefault("window_checks", []).append(page_check)
                if args.guided:
                    page_check["page_url_visually_confirmed"] = runner.guided_checkpoint(
                        f"{page['label']} reload screenshot saved at {shot['local_path']}; visually confirm the address then continue.")
                    page_check["url_verification"] = "operator_confirmed_from_screenshot"

            runner.record("minimize_window", f"uitest click {layout['minimize']}",
                          lambda: runner.click("minimize_window", *layout["minimize"]))
            time.sleep(1)
            shot = runner.record("minimized_window_screenshot", SCREEN_CAP_PREFIX, lambda: runner.screenshot(f"w{i+1}_minimized"))
            report["screenshots"].append(shot)
            hidden_check = runner.record("verify_window_minimized", "WMS visibility/position check",
                                         lambda: runner.wms({j: False for j in range(6)}))
            report.setdefault("window_checks", []).append({"window": i + 1, "phase": "minimized", "wms": hidden_check})
            if args.window is not None:
                report["partial_window_only"] = True

        report["status"] = "complete" if window_filter is None else "partial_complete"
    except (Exception, KeyboardInterrupt) as exc:
        report["status"] = "stopped_on_error"
        report["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            shot = runner.record("error_screenshot", SCREEN_CAP_PREFIX, lambda: runner.screenshot("error"))
            report["screenshots"].append(shot)
            report["error"]["screenshot"] = shot["local_path"]
        except Exception as capture_exc:
            report["error"]["screenshot_error"] = f"{type(capture_exc).__name__}: {capture_exc}"
    finally:
        report["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        report["actions"] = runner.actions
        write_report(report_path, report)
    if report["status"] == "stopped_on_error":
        raise RuntimeError(f"reload stopped; inspect {report_path}: {report['error']['message']}")
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dry_run(args: argparse.Namespace) -> dict:
    prep, workload, owner_pid = validate_preparation(args.preparation_manifest, args.workload_manifest)
    if args.target != prep.get("device_target"):
        raise ValueError("selected HDC target differs from preparation manifest")
    steps = []
    windows = range(6) if args.window is None else (args.window - 1,)
    for i in windows:
        steps += [{"action": "click", "name": "open_browser_taskbar", "xy": BROWSER_TASKBAR},
                  {"action": "wait", "seconds": 1},
                  {"action": "screenshot", "name": f"w{i+1}_switcher"},
                  {"action": "click", "name": "select_window_thumbnail", "xy": (THUMBNAIL_X[i], THUMBNAIL_Y)},
                  {"action": "wait", "seconds": 1},
                  {"action": "wms_assert", "window": i+1, "owner_pid": owner_pid,
                   "win_id": 1068+i, "visible": True, "rect": EXPECTED_WINDOW_RECTS[i]}]
        for page_i, tab in enumerate(WINDOW_UI[i]["tabs"]):
            steps += [{"action": "click", "name": f"select_{i+1}p{page_i+1}", "xy": tab},
                      {"action": "wms_assert", "window": i+1},
                      {"action": "click", "name": "reload", "xy": WINDOW_UI[i]["reload"]},
                      {"action": "wait", "seconds": 2},
                      {"action": "screenshot", "name": f"w{i+1}_p{page_i+1}_reloaded"}]
        steps += [{"action": "click", "name": "minimize", "xy": WINDOW_UI[i]["minimize"]},
                  {"action": "wait", "seconds": 1},
                  {"action": "wms_assert", "window": i+1, "visible": False}]
    return {"status": "dry_run_only_no_device_input", "target": prep.get("device_target"),
            "owner_pid": owner_pid, "verified_preparation_windows": prep.get("expected_windows"),
            "workload_pages": sum(len(row["pages"]) for row in workload["windows"]),
            "steps": steps, "new_tabs_created": 0, "browser_closed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--workload-manifest", type=Path,
                        default=HERE / "workload_6x3_20261013.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="new, empty local output directory")
    parser.add_argument("--window", type=int, help="run only this window 1..6, for guided inspection")
    parser.add_argument("--guided", action="store_true",
                        help="pause after screenshots until the operator types CONTINUE")
    parser.add_argument("--execute", action="store_true", help="allow UI clicks; omitted means dry-run")
    parser.add_argument("--confirm-ui-reload", action="store_true",
                        help="confirm intended tab reloads on the prepared six-window browser")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output directory already exists; choose a new run directory")
        if not args.hdc.is_file() or not args.preparation_manifest.is_file() or not args.workload_manifest.is_file():
            raise ValueError("HDC executable and both manifests must exist")
        if args.window is not None and not 1 <= args.window <= 6:
            raise ValueError("--window must be in 1..6")
        if args.execute:
            report = run(args)
        else:
            report = dry_run(args)
        if not args.execute:
            args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.output.resolve().mkdir(parents=False, exist_ok=False)
            write_report(args.output.resolve() / "reload_report.json", report)
        print(json.dumps({"status": report["status"], "owner_pid": report.get("owner_pid"),
                          "steps": len(report.get("steps", [])), "output": str(args.output.resolve())}, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"browser workload reload failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
