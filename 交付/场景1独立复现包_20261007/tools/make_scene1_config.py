from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIB_PER_GIB = 1024
APP_COUNT = 8
QUANTUM_MIB = 64


def ceil_to(value: int, quantum: int) -> int:
    return ((value + quantum - 1) // quantum) * quantum


def generate(dram_gib: float, pressure_mib: int | None, device: str | None,
             template: Path, output: Path) -> dict:
    if not math.isfinite(dram_gib) or dram_gib < 16:
        raise ValueError("名义物理内存必须是有限数值且不少于16GiB")
    if pressure_mib is not None and pressure_mib <= 0:
        raise ValueError("pressure-mib必须为正整数；压力不计入应用150%负载")
    per_app = ceil_to(math.ceil(dram_gib * MIB_PER_GIB * 1.5 / APP_COUNT), QUANTUM_MIB)
    pressure = pressure_mib if pressure_mib is not None else (12288 if dram_gib == 32 else 6144)
    raw = template.read_bytes()
    config = json.loads(raw)
    config["dram_gib"] = dram_gib
    config["device"] = device
    config["version_label"] = f"scene1-generated-{dram_gib:g}GiB-app150pct-pressure{pressure}MiB"
    scene = config["scene1"]
    scene["background_mib_per_app"] = per_app
    scene["pressure_mib"] = pressure
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = {
        "schema_version": 1,
        "template": template.resolve().relative_to(ROOT).as_posix(),
        "template_sha256": hashlib.sha256(raw).hexdigest(),
        "nominal_dram_gib": dram_gib,
        "actual_MemTotal_checked_at_run": False,
        "apps": APP_COUNT,
        "app_total_mib": per_app * APP_COUNT,
        "app_target_per_app_mib": per_app,
        "app_target_ratio_to_nominal_dram": per_app * APP_COUNT / (dram_gib * MIB_PER_GIB),
        "file_ratio": scene["file_ratio"],
        "file_target_mib_per_app": round(per_app * scene["file_ratio"]),
        "anon_target_mib_per_app": per_app - round(per_app * scene["file_ratio"]),
        "pressure_mib_separate_from_app_budget": pressure,
        "pressure_is_calibrated_for_target_device": False,
        "note": "必须通过设备preflight核对MemTotal/Swap/工程权限；pressure仅是候选，不自动增压。"
    }
    output.with_suffix(output.suffix + ".generation.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"config": str(output), **sidecar}


def main() -> int:
    parser = argparse.ArgumentParser(description="为Scene 1生成名义DRAM匹配的8应用配置；不修改模板")
    parser.add_argument("--dram-gib", type=float, required=True, help="设备名义物理内存GiB；运行时仍会核对实际MemTotal")
    parser.add_argument("--pressure-mib", type=int, help="独立pressure候选MiB；不计入应用负载")
    parser.add_argument("--device", help="HDC serial；省略时要求设备端唯一在线")
    parser.add_argument("--template", type=Path, default=ROOT / "config/scene1_full48_pressure12.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    template = args.template if args.template.is_absolute() else (ROOT / args.template).resolve()
    output = args.output if args.output.is_absolute() else (ROOT / args.output).resolve()
    print(json.dumps(generate(args.dram_gib, args.pressure_mib, args.device, template, output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
