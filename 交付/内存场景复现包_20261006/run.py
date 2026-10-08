from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from memscenes.device import Device
from memscenes.runner import (ROOT, Session, read, save, effective_settings, scene1, scene2, scene3, scene4,
                              calibrate, calibrate_scene4, freeze, compare)

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="鸿蒙PC应用内存场景")
    parser.add_argument("--config", type=Path, default=ROOT / "config/scenarios.json")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight", help="只读检查设备与应用")
    check.add_argument("--output", type=Path, default=ROOT / "results/preflight.json")
    check.add_argument("--offline", action="store_true",
                        help="只校验可重定位交付包资源，不连接设备、不判断实际内存容量")
    for name in ("run", "calibrate"):
        command = sub.add_parser(name)
        command.add_argument("--scene", type=int, choices=(1, 2, 3, 4), default=2)
        command.add_argument("--phase", choices=("baseline", "optimized"), default="baseline")
        command.add_argument("--profile", choices=("smoke", "development", "qualification", "acceptance"), default="development")
        command.add_argument("--subcase", choices=("combined", "startup", "oom", "popup"), default="combined")
        command.add_argument("--rounds", type=int)
        command.add_argument("--frozen", type=Path)
        command.add_argument("--calibration-output", type=Path, default=ROOT / "config/calibration.json")
    freezing = sub.add_parser("freeze")
    freezing.add_argument("--baseline", type=Path, required=True)
    freezing.add_argument("--calibration", type=Path, required=True)
    freezing.add_argument("--audit", type=Path,
                          help="hash-bound offline UI evidence audit for 32GiB prevalidation qualification")
    freezing.add_argument("--output", type=Path, default=ROOT / "config/frozen_scene2.json")
    comparison = sub.add_parser("compare")
    comparison.add_argument("--baseline", type=Path, nargs="+", required=True)
    comparison.add_argument("--optimized", type=Path, nargs="+", required=True)
    comparison.add_argument("--baseline-audit", type=Path, nargs="+",
                            help="与每个场景2 baseline result一一对应的hash-bound离线审计")
    comparison.add_argument("--optimized-audit", type=Path, nargs="+",
                            help="与每个场景2 optimized result一一对应的hash-bound离线审计")
    comparison.add_argument("--output", type=Path, default=ROOT / "results/comparison.json")
    args = parser.parse_args()
    if args.command == "compare":
        report = compare(args.baseline, args.optimized, args.output,
                         args.baseline_audit, args.optimized_audit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 2
    if args.command == "freeze":
        freeze(args.baseline, args.calibration, args.output, args.audit)
        print(f"冻结用例：{args.output}")
        return 0
    config_path = args.config.resolve()
    config = read(config_path)
    hdc_path = Path(config["hdc"])
    if not hdc_path.is_absolute():
        # Portable deliveries express the collector path relative to their
        # config file; this avoids consulting the source workspace or SDK path.
        config["hdc"] = str((config_path.parent / hdc_path).resolve())
    if args.command == "preflight" and args.offline:
        test_apps = config.get("test_apps", [])
        hap_dir = ROOT / ("bin/scene4_apps" if config.get("scene4") else "bin/apps")
        haps = sorted(hap_dir.glob("a*.hap"))
        missing = []
        if not Path(config["hdc"]).is_file():
            missing.append("hdc=" + config["hdc"])
        if not haps:
            missing.append(str(hap_dir.relative_to(ROOT)) + "/*.hap")
        if len(haps) != len(test_apps):
            missing.append(f"HAP数量{len(haps)}与test_apps数量{len(test_apps)}不一致")
        if not (ROOT / "bin/pressure").is_file():
            missing.append("bin/pressure")
        try:
            config_display = config_path.relative_to(ROOT).as_posix()
        except ValueError:
            config_display = str(config_path)
        try:
            hdc_display = Path(config["hdc"]).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            hdc_display = str(Path(config["hdc"]).resolve())
        report = {"offline": True, "device_checked": False,
                  "mem_capacity_checked": False, "config": config_display,
                  "hdc": hdc_display,
                  "hap_files": [str(path.relative_to(ROOT)) for path in haps],
                  "pressure_binary": "bin/pressure",
                  "missing": missing}
        save(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not missing else 2
    d = Device(config["hdc"], config.get("device"))
    if args.command == "preflight":
        env = d.environment()
        apps = {}
        for bundle in [x["bundle"] for x in config["real_apps"].values()] + config["test_apps"]:
            try:
                apps[bundle] = {"installed": True, **d.app(bundle)}
            except Exception as exc:
                apps[bundle] = {"installed": False, "reason": str(exc)}
        report = {"environment": env, "apps": apps,
                  "scene2_16gb_ready": config["dram_gib"] == 16 and 0.9 * 16 * 1024**3 <= env["meminfo"]["MemTotal"] <= 16 * 1024**3}
        save(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(x["installed"] for x in apps.values()) else 2
    config = effective_settings(config, args.profile)
    rounds = args.rounds if args.rounds is not None else (
        (10 if args.scene == 1 else 3) if args.profile == "acceptance" else 1)
    if not 1 <= rounds <= 100:
        raise ValueError("rounds must be between 1 and 100")
    if args.command == "calibrate" and args.scene not in (2, 4):
        raise ValueError("单应用峰值校准属于场景2；场景1用development运行校准")
    if args.scene == 3 and (args.command != "run" or args.phase != "baseline" or
                            args.profile != "development" or args.subcase != "combined" or args.frozen):
        raise ValueError("场景3仅支持development baseline combined单轮运行，不使用场景2冻结/验收参数")
    if args.scene == 3 and "scene3" not in config:
        raise ValueError("场景3配置缺少scene3固定用例定义")
    if args.scene == 4 and (args.phase not in ("baseline", "optimized") or args.profile != "development" or
                            args.subcase != "combined" or args.frozen):
        raise ValueError("场景4仅支持独立的baseline或optimized/development敏感性诊断与校准，不接受scene2冻结参数")
    if args.scene == 4 and "scene4" not in config:
        raise ValueError("场景4配置缺少固定负载定义")
    if args.command == "run" and args.scene == 2 and args.profile == "acceptance" and not args.frozen:
        raise ValueError("正式场景2必须提供冻结用例；先完成qualification基线、单应用校准和freeze")
    frozen = read(args.frozen) if args.frozen else None
    failed = False
    for _ in range(rounds):
        session = Session(d, config, args.scene, args.phase, args.profile)
        if args.command == "calibrate":
            output = calibrate_scene4(session, args.calibration_output) if args.scene == 4 else calibrate(session, args.calibration_output)
        elif args.scene == 1:
            output = scene1(session)
        elif args.scene == 2:
            output = scene2(session, args.subcase, frozen)
        elif args.scene == 3:
            output = scene3(session)
        else:
            output = scene4(session)
        result = read(output / "result.json")
        if result["status"] == "invalid":
            failed = True
            break
    return 2 if failed else 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
