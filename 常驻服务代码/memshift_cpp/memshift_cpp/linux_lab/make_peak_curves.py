#!/usr/bin/env python3
"""Measured peak curves for the aligned-peak scenarios (WSL, Linux builds; 100 ms samples of the
app's cgroup memory.current, results/linux_app_mem_20261021/<app>_trial/<run>/samples.csv).

For every peak operation of make_cgroup_scenarios.SCENARIOS:
  - the operation starts at the first sample of its phases (tools/linux_app_mem/report.py SPECS)
    and peaks at the highest memory.current inside them (same definition as 实验设置.md §2,
    "实测到峰时间");
  - of the 3 repeats, the one whose time to peak is closest to their mean is used;
  - its rise above the lowest value between the operation start and the peak is kept as a running
    maximum (the synthetic app only allocates until the hold; dips are not replayed). The lowest
    value is the start value for most operations; for the multi-tab restore it is the point
    where the 9 discarded tabs have been freed, so the curve is the wake-up part.
Output: JSON {"app:OPCODE": {"run": ..., "t": [...], "rise_mib": [...], "ramp_s": ..., "rise_max_mib": ...}}
(t in seconds from the operation start, 0.1 s steps).
Usage: make_peak_curves.py --out curves.json
"""
import argparse
import csv
import glob
import json
import statistics
import sys
from pathlib import Path

MEAS = Path("/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021")
sys.path.insert(0, "/home/boyi/HNUST/huawei_server/tools/linux_app_mem")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import SPECS  # noqa: E402
import make_cgroup_scenarios as gen  # noqa: E402

MiB = 1 << 20
# Phases whose start is the operation start, when the report's peak phases begin earlier than the
# measured ramp in 实验设置.md (multi-tab restore: measured from when the 9 tabs are discarded).
START_PHASE = {}


def run_for(app, op):
    code = op.split()[0]
    if app == "wechat":
        return "session", {"S04": ["view_image"]}[code]
    for run, ent in SPECS[app].items():
        for e in (ent if isinstance(ent, list) else [ent]):
            if e[0].split()[0] == code or e[0].startswith(code + "/") or e[0].startswith(code + " "):
                return run, e[1]
    raise SystemExit(f"no run for {app} {op}")


def repeats(app, run):
    if app == "wechat":
        return sorted(glob.glob(str(MEAS / "wechat_trial" / "session_w*")))
    dirs = sorted(glob.glob(str(MEAS / f"{app}_trial" / f"{run}_w*")))
    return dirs or sorted(glob.glob(str(MEAS / f"{app}_trial" / f"{run}_r*")))


def curve(d, phases, start_phase=None):
    rows = [r for r in csv.DictReader(open(Path(d) / "samples.csv")) if r["phase"] in phases and r["current"]]
    if start_phase:
        i = next(k for k, r in enumerate(rows) if r["phase"] == start_phase)
        rows = rows[i:]
    t0 = float(rows[0]["t"])
    pk = max(range(len(rows)), key=lambda k: int(rows[k]["current"]))
    mn = min(range(pk + 1), key=lambda k: int(rows[k]["current"]))
    c0 = int(rows[mn]["current"])
    # Nothing is allocated before the lowest point (memory there was being freed, not grown).
    pts = [(float(r["t"]) - t0, 0.0 if k < mn else (int(r["current"]) - c0) / MiB) for k, r in enumerate(rows[:pk + 1])]
    return pts, pts[-1][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = {}
    for sid, spec in gen.SCENARIOS.items():
        for app, op, inc, ramp in spec["peaks"]:
            key = f"{app}:{op.split()[0]}"
            if key in out:
                continue
            run, phases = run_for(app, op)
            cs = [(d, *curve(d, phases, START_PHASE.get(key))) for d in repeats(app, run)]
            mean_t = statistics.mean(c[2] for c in cs)
            d, pts, t_peak = min(cs, key=lambda c: abs(c[2] - mean_t))
            # 0.1 s grid, linear interpolation, running maximum.
            grid, vals, j, best = [], [], 0, 0.0
            n = int(round(t_peak * 10))
            for k in range(n + 1):
                t = k / 10
                while j + 1 < len(pts) and pts[j + 1][0] <= t:
                    j += 1
                if j + 1 < len(pts):
                    (ta, va), (tb, vb) = pts[j], pts[j + 1]
                    v = va + (vb - va) * (t - ta) / (tb - ta) if tb > ta else vb
                else:
                    v = pts[-1][1]
                best = max(best, v)
                grid.append(round(t, 1))
                vals.append(round(max(0.0, best), 1))
            out[key] = {"run": str(Path(d).relative_to(MEAS)), "phases": phases, "t": grid, "rise_mib": vals,
                        "ramp_s": round(t_peak, 1), "rise_max_mib": vals[-1],
                        "repeat_ramps_s": [round(c[2], 1) for c in cs], "doc_ramp_s": ramp, "doc_increment_mib": inc}
            v0 = [0.0] + vals
            jump = max(b - a for a, b in zip(v0, v0[11:])) if len(v0) > 11 else vals[-1]
            print(f"{key:14s} {out[key]['run']:34s} ramp {t_peak:6.1f} (doc {ramp}) repeats {out[key]['repeat_ramps_s']} "
                  f"rise {vals[-1]:6.0f} (doc inc {inc}) max 1 s rise {jump:5.0f}")
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
