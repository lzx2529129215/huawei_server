from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from memscenes.device import Device
from memscenes.runner import ROOT, Session, read, save, scene1


def load_config(path: Path, hdc_override: Path | None, device_override: str | None) -> dict:
    config_path = path.resolve()
    if not config_path.is_file():
        raise RuntimeError(f"场景配置不存在：{config_path}")
    config = read(config_path)
    hdc = hdc_override if hdc_override is not None else Path(config["hdc"])
    if not hdc.is_absolute():
        hdc = (config_path.parent / hdc).resolve()
    if not hdc.is_file():
        raise RuntimeError(f"HDC不存在：{hdc}")
    config["hdc"] = str(hdc)
    if device_override:
        config["device"] = device_override
    return config


def offline_preflight(config: dict, config_path: Path, output: Path) -> int:
    hap_dir = ROOT / "bin" / "apps"
    haps = sorted(hap_dir.glob("a*.hap"))
    required = [ROOT / "bin" / "pressure", ROOT / "tools" / "libusb_shared.dll"]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    app_total = int(config["scene1"]["background_mib_per_app"]) * len(config["test_apps"])
    app_ratio = app_total / (float(config["dram_gib"]) * 1024)
    if app_ratio < 1.5:
        missing.append(f"应用触碰预算仅为名义DRAM的{app_ratio:.3f}倍（要求至少1.5；pressure不计）")
    if not haps:
        missing.append("bin/apps/a*.hap")
    if len(haps) != len(config["test_apps"]):
        missing.append(f"HAP数{len(haps)}与负载app数{len(config['test_apps'])}不符")
    result = {"offline": True, "device_checked": False, "capacity_checked": False,
              "swap_checked": False, "config": config_path.resolve().relative_to(ROOT).as_posix(),
              "nominal_dram_gib": config["dram_gib"], "app_load_total_mib": app_total,
              "app_load_ratio_to_nominal_dram": app_ratio,
              "hdc": str(Path(config["hdc"]).resolve().relative_to(ROOT)).replace("\\", "/"),
              "hap_files": [path.relative_to(ROOT).as_posix() for path in haps],
              "pressure_binary": "bin/pressure", "missing": missing}
    save(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 2


def inspect_device(config: dict) -> tuple[Device, dict, dict]:
    device = Device(config["hdc"], config.get("device"))
    uid_text = device.shell("id -u", check=False).strip()
    env = device.environment()
    actual = int(env.get("meminfo", {}).get("MemTotal") or 0)
    expected = float(config["dram_gib"]) * 1024**3
    capacity_ok = bool(actual and 0.90 * expected <= actual <= 1.02 * expected)
    app_total = int(config["scene1"]["background_mib_per_app"]) * len(config["test_apps"])
    app_ratio = app_total / (float(config["dram_gib"]) * 1024)
    swap_total = int(env.get("meminfo", {}).get("SwapTotal") or 0)
    root_ok = uid_text == "0"
    vmstat = env.get("vmstat", {})
    requested_counters = ("workingset_refault_anon", "pagefault_major_anon",
                          "pagefault_minor_anon", "pagefault_major_file", "pagefault_minor_file")
    missing_counters = [name for name in requested_counters if name not in vmstat]
    status = {"selected_device": device.target, "shell_uid": uid_text,
              "engineering_root_available": root_ok, "MemTotal_bytes": actual,
              "nominal_dram_gib": config["dram_gib"], "capacity_match": capacity_ok,
              "app_load_total_mib": app_total, "app_load_ratio_to_nominal_dram": app_ratio,
              "app_load_at_least_150_percent": app_ratio >= 1.5,
              "SwapTotal_bytes": swap_total, "swap_available": swap_total > 0,
              "missing_global_vmstat_counters": missing_counters}
    return device, env, status


def device_preflight(config: dict, config_path: Path, output: Path) -> int:
    device, env, status = inspect_device(config)
    apps = {}
    bundles = [item["bundle"] for item in config["real_apps"].values()] + config["test_apps"]
    for bundle in bundles:
        try:
            apps[bundle] = {"installed": True, **device.app(bundle)}
        except Exception as exc:
            apps[bundle] = {"installed": False, "reason": str(exc)}
    root_accessible = status["engineering_root_available"]
    required_ok = (root_accessible and status["capacity_match"] and status["swap_available"]
                   and status["app_load_at_least_150_percent"])
    result = {"offline": False, "device_checked": True,
              "config": config_path.resolve().relative_to(ROOT).as_posix(),
              "environment": env, "capacity_and_permissions": status, "apps": apps,
              "ready_for_run": required_ok and all(row["installed"] for row in apps.values()),
              "warnings": (["匿名/文件四类fault计数缺字段：" + ",".join(status["missing_global_vmstat_counters"])]
                           if status["missing_global_vmstat_counters"] else [])}
    save(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_run"] and all(row["installed"] for row in apps.values()) else 2


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="场景1：固定六应用序列、受控48GiB/容量生成负载与释放后复用")
    parser.add_argument("--config", type=Path, default=ROOT / "config/scene1_full48_pressure12.json")
    parser.add_argument("--device", help="HDC serial；省略时仅在恰有一个在线目标时自动选择")
    parser.add_argument("--hdc", type=Path, help="覆盖配置中的HDC executable；默认使用包内tools/hdc.exe")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight", help="检查包资源或设备容量、权限、交换空间与应用安装状态")
    pre.add_argument("--offline", action="store_true", help="仅校验包内资源，不连接设备")
    pre.add_argument("--output", type=Path, default=ROOT / "validation/preflight.json")
    run = sub.add_parser("run", help="执行场景1开发/预验证单轮或固定重复轮")
    run.add_argument("--phase", choices=("baseline", "optimized"), default="baseline")
    run.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    config = load_config(args.config, args.hdc, args.device)
    if args.command == "preflight":
        if args.offline:
            return offline_preflight(config, args.config, args.output)
        return device_preflight(config, args.config, args.output)
    if not 1 <= args.rounds <= 100:
        raise ValueError("rounds must be between 1 and 100")
    device, _env, status = inspect_device(config)
    if not status["engineering_root_available"]:
        raise RuntimeError("该场景需要工程root/HDC权限读取进程、系统计数与kernel evidence")
    if not status["capacity_match"]:
        raise RuntimeError(f"名义DRAM与实际MemTotal不匹配：{status['nominal_dram_gib']}GiB / {status['MemTotal_bytes']} bytes")
    if not status["swap_available"]:
        raise RuntimeError("设备SwapTotal为0；拒绝开始要求验证真实换出的场景")
    if not status["app_load_at_least_150_percent"]:
        raise RuntimeError("应用映射触碰预算低于名义DRAM的150%；请重新生成容量配置，压力进程不计入应用预算")
    failed = False
    for _ in range(args.rounds):
        session = Session(device, config, scene=1, phase=args.phase, profile="development")
        output = scene1(session)
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
