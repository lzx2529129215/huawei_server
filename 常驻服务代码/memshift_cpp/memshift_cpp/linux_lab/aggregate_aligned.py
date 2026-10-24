#!/usr/bin/env python3
"""Tables for the aligned-peak experiment (scenarios cgA..cgE, run_round.py
"peak_plan"): observe vs apply per scenario.

Main metric per round: oom_events = kills (kernel OOM + policy) + peak
allocation timeouts. Popups (available < 50 MiB) are reported separately.
Benefit: kills and stall during the peak; cost: switch-back time of the
background apps (all daily memory touched again, file re-read).

Usage: aggregate_aligned.py --out DIR --label NAME ROUND_DIR [ROUND_DIR ...]
"""
import argparse
import csv
import json
import statistics
from pathlib import Path


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def f(x, nd=1):
    return "—" if x is None else f"{x:,.{nd}f}"


def row(s):
    al = s.get("aligned", {})
    ph = {p["phase"]: p for p in s["events"]}
    peak_ph = ("ramp", "hold")
    touch = [r["touch_ms"] for r in al.get("returns", []) if r.get("touch_ms") is not None]
    anon = [r["anon_ms"] for r in al.get("returns", []) if r.get("anon_ms") is not None]
    filems = [r["file_ms"] for r in al.get("returns", []) if r.get("file_ms") is not None]
    late = [p["late_ms"] for p in al.get("peaks", []) if p.get("late_ms") is not None]
    pul = al.get("pulses", {})
    return {
        "scenario": s["scenario"], "group": s["mode"] + s.get("tag", ""), "round": s["round_id"],
        "oom_events": s.get("oom_events"), "kills": s["kills"]["total"], "kills_policy": s["kills"]["policy"],
        "kills_kernel": s["kills"]["kernel_oom"], "peak_timeouts": s["peak_fail"], "popups": s["popups"],
        "kills_before_T": sum(ph[p]["policy_kills"] + ph[p]["kernel_kills"] for p in ("daily", "prep", "ramp") if p in ph),
        "kills_peak": sum(ph[p]["policy_kills"] + ph[p]["kernel_kills"] for p in ("hold", "release") if p in ph),
        "kills_return": sum(ph[p]["policy_kills"] + ph[p]["kernel_kills"] for p in ("return",) if p in ph),
        "killed": ";".join(a for p in s["events"] for a in p.get("killed", [])),
        "stall_some_ms_peak": sum(ph[p]["stall_some_ms"] for p in peak_ph if p in ph),
        "stall_full_ms_peak": sum(ph[p]["stall_full_ms"] for p in peak_ph if p in ph),
        "stall_some_ms_prep": ph["prep"]["stall_some_ms"] if "prep" in ph else None,
        "stall_some_ms_return": ph["return"]["stall_some_ms"] if "return" in ph else None,
        "psi_some_ms_total": s["psi_ms"]["some"],
        "free_at_T_mib": al.get("free_at_T_mib"),
        "min_free_mib_peak": min((ph[p]["min_free_mib"] for p in peak_ph if p in ph and ph[p]["min_free_mib"] is not None), default=None),
        "peak_late_ms_max": max(late) if late else None,
        "pulse_freed_mib": pul.get("net_freed_mib"),
        "freed_before_T_mib": sum(ph[p]["reclaimed_before_mib"] for p in ("daily", "prep", "ramp") if p in ph), "pulses": pul.get("n"), "pulse_write_ms": pul.get("write_ms"),
        "pulse_max_write_ms": pul.get("max_write_ms"), "pulses_not_restored": pul.get("not_restored"),
        "pulse_to_zswap_mib": pul.get("zswpout_mib"), "pulse_to_disk_mib": pul.get("pswpout_mib"),
        "quota_apps": sum(1 for q in al.get("quota_plan", []) if q["action"] in ("set", "would_set")),
        "scope_pswpout_mib": s["pswpout_mib"], "scope_pswpin_mib": s["pswpin_mib"],
        "zswap_pool_peak_mib": s.get("zswap_pool_peak_mib"),
        "touch_ms_mean": mean(touch), "touch_ms_max": max(touch) if touch else None,
        "touch_anon_ms_mean": mean(anon), "touch_file_ms_mean": mean(filems),
        "relaunches": s["relaunches"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("rounds", nargs="+")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in a.rounds:
        p = Path(d) / "summary.json"
        if p.exists():
            rows.append(row(json.loads(p.read_text())))
    rows.sort(key=lambda r: (r["scenario"], r["group"], int(r["round"]) if str(r["round"]).isdigit() else 0))
    cols = list(rows[0].keys())
    with open(out / f"{a.label}_rounds.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    md = [f"## {a.label}", "",
          "每个场景 observe 与 apply 的每轮平均值 ± 标准差。OOM 事件 = 杀进程（内核 OOM + 杀进程规则）+ 峰值应用分配超时；弹窗另列。",
          "峰值期间 = ramp + hold（第一个应用开始上涨到 T + 15 s）。切回耗时 = 后台应用切回时把日常内存访问一遍、文件读一遍的耗时。", "",
          "峰前回收 = memshiftd 在 T 之前用 memory.high 净腾出的量；写磁盘 = 整机 swap 写出量（内核回收 + memshiftd 脉冲）。", "",
          "| 场景 | 组 | 轮数 | OOM 事件/轮 | 比 observe 少 | 0 事件的轮 | 杀进程/轮 | 峰值超时/轮 | 弹窗/轮 | 峰值期间 PSI some ms | T 时空闲 MiB | 峰前回收 MiB | 写磁盘 MiB | 切回耗时 ms（均值） | 切回耗时 ms（最大） |",
          "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for sc in sorted({r["scenario"] for r in rows}):
        groups = []
        for r in rows:
            if r["scenario"] == sc and r["group"] not in groups:
                groups.append(r["group"])
        base = mean([r["oom_events"] for r in rows if r["scenario"] == sc and r["group"].startswith("observe")])
        for g in groups:
            rs = [r for r in rows if r["scenario"] == sc and r["group"] == g]
            ev = [r["oom_events"] for r in rs]
            m = mean(ev)
            less = "" if g.startswith("observe") or not base else f"{(base - m) / base * 100:.0f}%"
            md.append(f"| {sc} | {g} | {len(rs)} | {f(m)} ± {f(sd(ev))} | {less} | {sum(1 for x in ev if x == 0)}/{len(rs)} | "
                      f"{f(mean([r['kills'] for r in rs]))} | {f(mean([r['peak_timeouts'] for r in rs]))} | {f(mean([r['popups'] for r in rs]))} | "
                      f"{f(mean([r['stall_some_ms_peak'] for r in rs]), 0)} | {f(mean([r['free_at_T_mib'] for r in rs]), 0)} | "
                      f"{f(mean([r['freed_before_T_mib'] for r in rs]), 0)} | {f(mean([r['scope_pswpout_mib'] for r in rs]), 0)} | "
                      f"{f(mean([r['touch_ms_mean'] for r in rs]), 0)} | {f(mean([r['touch_ms_max'] for r in rs]), 0)} |")
    md += ["", "### 杀进程发生在哪个阶段（每轮平均）", "",
           "| 场景 | 组 | 峰前（日常 + 准备 + 上涨） | 峰值（保持 + 回落） | 切回 | 被杀的应用（所有轮） |", "| --- | --- | ---: | ---: | ---: | --- |"]
    for sc in sorted({r["scenario"] for r in rows}):
        for g in sorted({r["group"] for r in rows if r["scenario"] == sc}, key=lambda x: (not x.startswith("observe"), x)):
            rs = [r for r in rows if r["scenario"] == sc and r["group"] == g]
            names = {}
            for r in rs:
                for n in filter(None, r["killed"].split(";")):
                    names[n] = names.get(n, 0) + 1
            md.append(f"| {sc} | {g} | {f(mean([r['kills_before_T'] for r in rs]))} | {f(mean([r['kills_peak'] for r in rs]))} | "
                      f"{f(mean([r['kills_return'] for r in rs]))} | {', '.join(f'{k}×{v}' for k, v in sorted(names.items(), key=lambda x: -x[1])) or '—'} |")
    (out / f"{a.label}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
