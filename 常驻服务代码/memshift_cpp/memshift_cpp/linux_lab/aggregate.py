#!/usr/bin/env python3
"""Aggregate round summaries into comparison tables (Markdown + CSV).

Usage: aggregate.py --out DIR --label NAME ROUND_DIR [ROUND_DIR ...]
Rounds are grouped by (mode, tag). The first group is the baseline unless
--baseline names one ("observe" or "observe<tag>").
"""
import argparse
import csv
import json
import statistics
import subprocess
from pathlib import Path

LAB = Path(__file__).resolve().parent
BUILD = LAB.parent / "build"


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 1) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.stdev(xs), 1) if len(xs) > 1 else 0.0


def row(s):
    ev = s["events"]
    return {
        "group": s["mode"] + s.get("tag", ""), "round": s["round_id"],
        "events_total": s.get("events_total", s["kills"]["total"]), "popups": s.get("popups", 0),
        "kills_total": s["kills"]["total"], "kills_kernel": s["kills"]["kernel_oom"],
        "kills_policy": s["kills"]["policy"], "relaunches": s["relaunches"], "peak_fail": s["peak_fail"],
        "psi_some_ms": s["psi_ms"]["some"], "psi_full_ms": s["psi_ms"]["full"],
        "peak_ms_max": max((e["peak_ms_max"] or 0) for e in ev) if ev else None,
        "pswpout_mib": s["pswpout_mib"], "pswpin_mib": s["pswpin_mib"],
        "touch_ms_mean": s["touch_ms"]["mean"],
        "reclaimed_mib": s["memshiftd_reclaim"]["submitted_mib"],
        "reclaim_write_ms": s["memshiftd_reclaim"]["write_ms"],
        "would_reclaim_mib": s["memshiftd_reclaim"]["planned_mib"],
        "drop_caches_in_peak": sum(1 for e in ev if e["drop_caches_in_window"]),
        "events": "; ".join(f"t{e['t']}:k{e['policy_kills'] + e['kernel_kills']}/s{e['stall_some_ms']}ms"
                            f"/pre{e['reclaimed_before_mib']:.0f}MiB" for e in ev),
    }


def compare(base_dirs, opt_dirs, name, out):
    """Run tools/compare_runs on the concatenated per-round CSVs."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for label, dirs in (("baseline", base_dirs), ("optimized", opt_dirs)):
        p = out / f"{label}_{name}"
        with open(p, "w", newline="") as f:
            w = None
            for d in dirs:
                with open(d / name) as src:
                    r = list(csv.reader(src))
                if w is None:
                    w = csv.writer(f)
                    w.writerow(r[0])
                w.writerows(r[1:])
        paths.append(p)
    res = subprocess.run([str(BUILD / "compare_runs"), str(paths[0]), str(paths[1])],
                         text=True, capture_output=True)
    return (res.stdout + res.stderr).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("rounds", nargs="+")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dirs = [Path(d) for d in a.rounds]
    summaries = [json.loads((d / "summary.json").read_text()) for d in dirs]
    rows = [row(s) for s in summaries]
    groups = []
    for r in rows:
        if r["group"] not in groups:
            groups.append(r["group"])
    base = a.baseline or groups[0]
    cols = ["group", "round", "events_total", "popups", "kills_total", "kills_kernel", "kills_policy", "relaunches", "peak_fail",
            "psi_some_ms", "psi_full_ms", "peak_ms_max", "pswpout_mib", "pswpin_mib", "touch_ms_mean",
            "reclaimed_mib", "reclaim_write_ms", "would_reclaim_mib", "drop_caches_in_peak", "events"]
    with open(out / f"{a.label}_rounds.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    md = [f"## {a.label}", "", "### Per round", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        md.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    md += ["", "### Mean ± sd per group", "",
           "| group | rounds | events total | popups | kills total | kernel | policy | relaunches | PSI some ms | peak ms max | swap-out MiB | touch ms | reclaimed MiB | events vs baseline | kills vs baseline |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    base_kills = mean([r["kills_total"] for r in rows if r["group"] == base])
    base_events = mean([r["events_total"] for r in rows if r["group"] == base])
    for g in groups:
        rs = [r for r in rows if r["group"] == g]
        k = mean([r["kills_total"] for r in rs])
        ev = mean([r["events_total"] for r in rs])
        change = "" if g == base or not base_kills else f"{(base_kills - k) / base_kills * 100:+.1f}% fewer"
        echange = "" if g == base or not base_events else f"{(base_events - ev) / base_events * 100:+.1f}% fewer"
        md.append(f"| {g} | {len(rs)} | {ev} ± {sd([r['events_total'] for r in rs])} | "
                  f"{mean([r['popups'] for r in rs])} | {k} ± {sd([r['kills_total'] for r in rs])} | "
                  f"{mean([r['kills_kernel'] for r in rs])} | {mean([r['kills_policy'] for r in rs])} | "
                  f"{mean([r['relaunches'] for r in rs])} | {mean([r['psi_some_ms'] for r in rs])} | "
                  f"{mean([r['peak_ms_max'] for r in rs])} | {mean([r['pswpout_mib'] for r in rs])} | "
                  f"{mean([r['touch_ms_mean'] for r in rs])} | {mean([r['reclaimed_mib'] for r in rs])} | {echange} | {change} |")
    by_group = {g: [d for d, r in zip(dirs, rows) if r["group"] == g] for g in groups}
    for g in groups:
        if g == base:
            continue
        for name in ("compare_total.csv", "compare_kernel.csv"):
            text = compare(by_group[base], by_group[g], name, out / f"{a.label}_{g}")
            md += ["", f"### compare_runs: {base} vs {g} ({name})", "", "```", text, "```"]
    (out / f"{a.label}.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
