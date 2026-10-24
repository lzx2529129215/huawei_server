#!/usr/bin/env python3
"""Single-app calibration, like the Harmony framework's per-app calibration:
run every app of a scenario alone inside memlab.slice (no other apps, no
memshiftd, no kill policy) and measure its daily footprint and its peak from
its own cgroup (memory.current after DAILY_READY, memory.peak before it exits).

Usage: calibrate_apps.py --scenario S.json --out calibration.json
"""
import argparse
import json
import shutil
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_round as rr  # noqa: E402


def calibrate(a, data, logdir, timeout=90.0, hold=None):
    log = logdir / f"{a['name']}.log"
    cmd = ["systemd-run", "--user", "-q", f"--unit={rr.unit(a['name'])}", "--slice=memlab",
           str(rr.BUILD / "memlab_app"), "--name", a["name"], "--log", str(log), "--data-dir", str(data),
           "--anon-mib", str(a.get("anon_mib", 0)), "--clean-mib", str(a.get("clean_mib", 0)),
           "--dirty-mib", str(a.get("dirty_mib", 0)), "--dirty-rate", str(a.get("dirty_rate_mib_s", 0)),
           "--compress-ratio", str(a.get("compress_ratio", 1)), "--ttl", str(int(timeout) + 30),
           "--seed", str(zlib.crc32(a["name"].encode()) % 100000)]
    for key, flag in (("locked_mib", "--locked-mib"), ("peak_compress_ratio", "--peak-compress-ratio")):
        if a.get(key):
            cmd += [flag, str(a[key])]
    if a.get("peak_mib"):
        cmd += ["--peak-mib", str(a["peak_mib"]), "--peak-page-only", str(a.get("peak_page_only", 0)),
                "--hold", str(a.get("hold_s", 0) if hold is None else hold), "--peak-after", str(a.get("peak_after_s", 2)),
                "--exit-after-peak", "1"]
    rr.sh(["systemctl", "--user", "reset-failed", rr.unit(a["name"])], check=False)
    rr.sh(cmd)
    cg = rr.app_cg(a["name"])
    t0 = time.monotonic()
    daily = peak = 0
    ready_at = None
    while time.monotonic() - t0 < timeout:
        cur = int(rr.read(cg / "memory.current", "0") or 0)
        peak = max(peak, int(rr.read(cg / "memory.peak", "0") or 0), cur)
        text = rr.read(log)
        if ready_at is None and "DAILY_READY" in text:
            ready_at = time.monotonic()
        # Sample the daily footprint before any task starts (peak_after can be 1 s; apps
        # without one wait 2 s, so the sample never includes the peak).
        if ready_at is not None and not daily and time.monotonic() - ready_at >= 0.3:
            daily = cur
        if "STARTUP_READY" in text and daily:
            if not a.get("peak_mib"):
                break
            if "PEAK_RELEASE" in text:
                break
        time.sleep(0.1)
    rr.sh(["systemctl", "--user", "stop", rr.unit(a["name"])], check=False)
    ev = rr.parse_app_logs(log)
    startup = next((float(e["ms"]) for e in ev if e["event"] == "STARTUP_READY"), None)
    return {"daily_mib": round(daily / rr.MiB), "peak_mib": round(peak / rr.MiB), "startup_ms": startup,
            "nominal_daily_mib": rr.daily_mib(a), "nominal_peak_mib": rr.daily_mib(a) + a.get("peak_mib", 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hold", type=float, default=None, help="override every peak's hold (seconds)")
    args = ap.parse_args()
    sc = json.loads(Path(args.scenario).read_text())
    work = Path(args.out).with_suffix(".work")
    shutil.rmtree(work, ignore_errors=True)
    (work / "data").mkdir(parents=True)
    rr.cleanup_units()
    scope = sc["scope"]
    props = [f"MemoryMax={scope['memory_max_mib']}M", f"MemorySwapMax={scope['swap_max_mib']}M"]
    if "zswap_max_mib" in scope:
        props.append(f"MemoryZSwapMax={scope['zswap_max_mib']}M")
    rr.sh(["systemctl", "--user", "set-property", "--runtime", "memlab.slice"] + props)
    rr.sh(["systemd-run", "--user", "-q", f"--unit={rr.unit('anchor')}", "--slice=memlab", "sleep", "3600"])
    out = {}
    try:
        for a in sc["apps"]:
            out[a["name"]] = calibrate(a, work / "data", work, hold=args.hold)
            print(a["name"], out[a["name"]], flush=True)
            for f in (work / "data").iterdir():
                f.unlink()
    finally:
        rr.cleanup_units()
        shutil.rmtree(work / "data", ignore_errors=True)
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
