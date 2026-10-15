"""Cross compile the local 3-target controller for the Harmony aarch64 device."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = HERE / "native"
DEFAULT_COMPILER = Path(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\native\llvm\bin\clang++.exe")
DEFAULT_SYSROOT = Path(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\native\sysroot")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=DEFAULT_COMPILER)
    parser.add_argument("--sysroot", type=Path, default=DEFAULT_SYSROOT)
    args = parser.parse_args()
    source = NATIVE / "controller.cpp"
    binary = NATIVE / "device_retry_controller"
    if not args.compiler.is_file() or not args.sysroot.is_dir():
        raise SystemExit("Harmony clang++ or native sysroot is unavailable")
    command = [str(args.compiler), "--target=aarch64-linux-ohos", f"--sysroot={args.sysroot}",
               "-D__MUSL__", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wpedantic",
               "-pthread", str(source), "-o", str(binary)]
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode:
        return result.returncode
    headers = {path.name: sha(path) for path in sorted(NATIVE.glob("*.hpp"))}
    manifest = {"schema": "browser_donor_native_controller_build_85g_v1",
        "role_schema": "device_retry_plan_browser_donor_3target_85g_v1",
        "target_count": 3, "target_bundles": [f"io.memscene.scene4.workload.a{i}" for i in (14, 15, 16)],
        "measurement_version": "device-retry-browser-donor-3target-85g-lead90-v1",
        "controller_source": str(source), "controller_source_sha256": sha(source),
        "headers_sha256": headers, "binary": str(binary), "binary_sha256": sha(binary),
        "build_command": command, "build_returncode": result.returncode,
        "cross_compiled": True, "device_execution": "not_run"}
    (NATIVE / "build_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
