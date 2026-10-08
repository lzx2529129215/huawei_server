from __future__ import annotations

"""Scene4-only service lifecycle wrapper for no-target oracle protocol checks.

This wrapper deliberately delegates device identity/switch/restore work to the
reviewed donor diagnostic driver.  Its extra hard gate binds the Scene4 runner
configuration to the exact control/schedule/ledger paths in the daemon INI.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from tools import scene4_donor_mechanism_diagnostic as driver


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_daemon_ini(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    with path.open("r", encoding="utf-8") as stream:
        parser.read_file(stream)
    if not parser.has_section("daemon"):
        raise ValueError("daemon ini missing [daemon]")
    required = ("oracle_mode", "control_file", "oracle_schedule_file", "oracle_ledger_file")
    values = {key: parser.get("daemon", key, fallback="").strip() for key in required}
    missing = [key for key, value in values.items() if not value]
    if missing or values["oracle_mode"].lower() != "true":
        raise ValueError(f"daemon oracle configuration incomplete: missing={missing}, mode={values['oracle_mode']!r}")
    return values


def bind_runner_paths(base_config: dict, daemon_ini: Path) -> tuple[dict, dict]:
    """Return a Scene4 config whose mutable protocol paths equal daemon paths."""
    ini = parse_daemon_ini(daemon_ini)
    config = json.loads(json.dumps(base_config))
    scene4 = config.get("scene4")
    if not isinstance(scene4, dict):
        raise ValueError("base config has no scene4 object")
    runner_values = {
        "control_file": ini["control_file"],
        "oracle_schedule_remote_path": ini["oracle_schedule_file"],
        "oracle_ledger_remote_path": ini["oracle_ledger_file"],
    }
    scene4["oracle_schedule_remote_path"] = runner_values["oracle_schedule_remote_path"]
    scene4["oracle_ledger_remote_path"] = runner_values["oracle_ledger_remote_path"]
    schedule_parent = runner_values["oracle_schedule_remote_path"].rsplit("/", 1)[0]
    runner_values["control_file"] = schedule_parent + "/control.conf"
    if runner_values["control_file"] != ini["control_file"]:
        raise ValueError("Scene4 control path is not the daemon's configured control_file")
    if not scene4.get("oracle_clock_probe_remote_path"):
        raise ValueError("Scene4 clock probe path missing")
    return config, {"daemon_ini": ini, "scene4_runner": runner_values,
                    "paths_match": all(ini[k] == runner_values[name] for k, name in (
                        ("control_file", "control_file"),
                        ("oracle_schedule_file", "oracle_schedule_remote_path"),
                        ("oracle_ledger_file", "oracle_ledger_remote_path")))}


def make_run_id() -> str:
    return "s4_" + secrets.token_hex(6) + "_c0001"


def expected_remote_root(run_id: str) -> str:
    if not re.fullmatch(r"s4_[0-9a-f]{12}_c0001", run_id):
        raise ValueError("run id malformed")
    return "/data/local/tmp/memshift_validation_20261008/oracle_followup_" + run_id[3:15]


def child_command(python: str, run_id: str, out: Path, *, prepare_only: bool,
                  hdc: str, target: str, daemon: str, template: str, mode: str = "paused-a",
                  phase: str = "baseline", runner_config: Path | None = None) -> list[str]:
    command = [python, str(PACKAGE_ROOT / "tools" / "scene4_donor_mechanism_diagnostic.py"),
               "--hdc", hdc, "--target", target, "--daemon", daemon, "--config-template", template,
               "--run-id", run_id, "--output-dir", str(out), "--mode", mode, "--phase", phase]
    if runner_config is not None:
        command.extend(["--runner-config", str(runner_config)])
    if prepare_only:
        command.append("--prepare-only")
    return command


def scene4_runner_command(python: str, config_path: Path, phase: str) -> list[str]:
    if phase not in ("baseline", "optimized"):
        raise ValueError("Scene4 runner phase must be baseline or optimized")
    return [python, str(PACKAGE_ROOT / "run.py"), "--config", str(config_path),
            "run", "--scene", "4", "--phase", phase,
            "--profile", "development", "--rounds", "1"]


def runner_lifecycle_succeeded(driver_returncode: int, final: dict) -> bool:
    """Execution/restore success is separate from later manual measurement review."""
    run = final.get("scene4_run", {})
    restore = final.get("service_restore", {})
    return bool(driver_returncode == 0 and run.get("execution_complete") is True
                and run.get("result_path") and restore.get("verified") is True)


def launch_driver(command: list[str], out: Path) -> dict:
    log_path = out / "driver_stdout_stderr.log"
    started = datetime.now(timezone(timedelta(hours=8))).isoformat()
    progress = {"state": "starting", "host_pid": os.getpid(), "child_argv": command,
                "started_local": started, "log_path": str(log_path)}
    (out / "wrapper_progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    with log_path.open("ab", buffering=0) as log:
        child = subprocess.Popen(command, cwd=str(PACKAGE_ROOT), stdout=log, stderr=subprocess.STDOUT)
        progress.update({"state": "running", "child_pid": child.pid,
                         "child_created_monotonic": time.monotonic()})
        (out / "wrapper_progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
        code = child.wait()
    progress.update({"state": "exited", "child_returncode": code,
                     "finished_local": datetime.now(timezone(timedelta(hours=8))).isoformat()})
    (out / "wrapper_progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    return progress


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scene4-only daemon lifecycle and bound runner")
    parser.add_argument("--mode", choices=("prepare", "paused-a", "scene4-baseline", "scene4-optimized"), required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--hdc", default=str(driver.DEFAULT_HDC))
    parser.add_argument("--target", default="3QC0124C03000514")
    parser.add_argument("--daemon", default=str(driver.DEFAULT_DAEMON))
    parser.add_argument("--template", default=str(PACKAGE_ROOT.parents[1] / "results" /
        "scene4_oracle_cold_20261007_luna01" / "A1024_service_run2" / "resident_oracle_run2.ini"))
    parser.add_argument("--base-config", default=str(PACKAGE_ROOT / "config" / "scenarios_scene4_oracle_cold_A2048_followup_20261008.json"))
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    run_id = args.run_id or make_run_id()
    remote_root = expected_remote_root(run_id)
    out = args.output_dir or (PACKAGE_ROOT / "results" / "oracle_followup_20261008_service_wrapper" / run_id)
    out.mkdir(parents=True, exist_ok=False)
    template = Path(args.template)
    base_config = json.loads(Path(args.base_config).read_text(encoding="utf-8"))
    # The diagnostic helper stages its generated service INI during prepare-only.
    prep_cmd = child_command(args.python, run_id, out, prepare_only=True,
                             hdc=args.hdc, target=args.target, daemon=args.daemon, template=str(template))
    prep_log = out / "prepare_stdout_stderr.log"
    with prep_log.open("wb") as log:
        prepared = subprocess.run(prep_cmd, cwd=str(PACKAGE_ROOT), stdout=log,
                                  stderr=subprocess.STDOUT, check=False)
    if prepared.returncode != 0:
        raise RuntimeError(f"driver prepare-only failed with exit {prepared.returncode}; see {prep_log}")
    local_ini = out / "resident_donor_diag.ini"
    runner_config, contract = bind_runner_paths(base_config, local_ini)
    if not contract["paths_match"]:
        raise RuntimeError("service/Scene4 runner path contract failed")
    runner_config["hdc"] = str(Path(args.hdc).resolve())
    runner_config["device"] = args.target
    clock_local = (PACKAGE_ROOT.parents[1] / "results" / "scene4_oracle_cold_20261007_luna01" /
                   "scene4_clock_domain_probe_clkinfo")
    if not clock_local.is_file():
        raise RuntimeError("shared Scene4 clock helper is missing")
    runner_config["scene4"]["oracle_clock_probe_remote_path"] = remote_root + "/clock_probe_clkinfo"
    runner_config["scene4"]["oracle_clock_probe_sha256"] = sha256_file(clock_local)
    expected_paths = {
        "remote_root": remote_root,
        "daemon_ini": contract["daemon_ini"],
        "driver_run_id": run_id,
        "control_file": contract["daemon_ini"]["control_file"],
        "schedule_file": contract["daemon_ini"]["oracle_schedule_file"],
        "ledger_file": contract["daemon_ini"]["oracle_ledger_file"],
    }
    if any(not value.startswith(remote_root + "/") for key, value in expected_paths.items()
           if key in ("control_file", "schedule_file", "ledger_file")):
        raise RuntimeError("daemon mutable protocol path escapes this run's unique remote root")
    runner_config_path = out / "scene4_runner_config_bound.json"
    runner_config_path.write_text(json.dumps(runner_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actual_run = args.mode in ("scene4-baseline", "scene4-optimized")
    phase = "optimized" if args.mode == "scene4-optimized" else "baseline"
    frozen = {"schema": "scene4-service-wrapper-contract-v2", "mode": args.mode,
              "run_id": run_id, "remote_root": remote_root,
              "contract": contract, "runner_config_path": str(runner_config_path),
              "runner_config_sha256": sha256_file(runner_config_path),
              "daemon_ini_path": str(local_ini), "daemon_ini_sha256": sha256_file(local_ini),
              "template_path": str(template), "template_sha256": sha256_file(template),
              "daemon_binary": str(Path(args.daemon).resolve()),
              "daemon_binary_sha256": sha256_file(Path(args.daemon)),
              "no_target_launch": not actual_run, "pressure_run": actual_run,
              "intended_control": "paused=false" if phase == "optimized" else "paused=true",
              "runner_entry": str(PACKAGE_ROOT / "run.py") if actual_run else None,
              "runner_phase": phase if actual_run else None}
    contract_path = out / "service_path_contract.json"
    contract_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": run_id, "output_dir": str(out), "contract": contract,
                      "contract_path": str(contract_path), "prepared_only": args.mode == "prepare"},
                     ensure_ascii=False, indent=2))
    if args.mode == "prepare":
        return 0
    run_cmd = child_command(args.python, run_id, out, prepare_only=False,
                            hdc=args.hdc, target=args.target, daemon=args.daemon, template=str(template),
                            mode="runner" if actual_run else "paused-a", phase=phase,
                            runner_config=runner_config_path if actual_run else None)
    (out / "driver_argv.json").write_text(json.dumps(run_cmd, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_state = launch_driver(run_cmd, out)
    final_path = out / "diagnostic_final.json"
    if run_state["child_returncode"] != 0 or not final_path.is_file():
        return 2
    final = json.loads(final_path.read_text(encoding="utf-8"))
    restore = final.get("service_restore", {})
    if actual_run:
        return 0 if runner_lifecycle_succeeded(run_state["child_returncode"], final) else 2
    paused_check = final.get("paused_a_verification", {})
    return 0 if restore.get("verified") and paused_check.get("valid") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
