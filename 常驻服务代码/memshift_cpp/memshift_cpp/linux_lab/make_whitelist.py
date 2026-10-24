#!/usr/bin/env python3
"""Generate the whitelist scenario: the 20 whitelisted HarmonyOS PC apps,
launched the way the acceptance test describes ("模拟用户点击并发启动").

Per-app memory profiles are ESTIMATES by app category (no per-app device data
yet); replace them with measured values when available. Calibrate first with
calibrate_apps.py and pass --calibration so every case is checked on measured
daily/peak values.

Residents stay open as background apps for the whole round. Each case clicks a
group of launch-pool apps at (almost) the same time: every app loads its daily
footprint, runs its typical task (a peak) peak_after_s seconds later, holds it,
then the user closes it. Cases use different app combinations, and each case
must meet the three acceptance conditions on its own group:
  (1) every app's peak <= M
  (2) daily usage of all open apps (and of all 20 apps) <= M
  (3) the group's peak sum >= 1.2 M
"""
import argparse
import itertools
import json

# id: (display name, category, role, anon, clean, dirty, dirty_rate, task peak, peak_after_s, hold_s)
# Sizes in MiB at the 16 GiB scale. Roles: resident = open in the background all
# round; launch = cold-started inside cases.
APPS = {
    "hw_music":       ("华为音乐",   "light",    "resident", 180,  40,   0,   0.0,    0,  0,  0),
    "calendar":       ("日历",       "light",    "resident", 120,  30,   0,   0.0,    0,  0,  0),
    "notes":          ("备忘录",     "light",    "resident", 120,  30,   0,   0.0,    0,  0,  0),
    "wechat":         ("微信",       "chromium", "resident", 600, 150, 100,   0.5,    0,  0,  0),
    "dingtalk":       ("钉钉",       "chromium", "resident", 600, 120,  80,   0.4,    0,  0,  0),
    "feishu":         ("飞书",       "chromium", "resident", 700, 150,  80,   0.4,    0,  0,  0),
    "hw_browser":     ("华为浏览器", "chromium", "resident", 800, 200,   0,   0.0,    0,  0,  0),
    "xhs":            ("小红书",     "chromium", "resident", 600, 200,   0,   0.0,    0,  0,  0),
    "jianying":       ("剪映",       "heavy",    "launch",   500, 300,   0,   0.0, 4300,  8, 15),
    "zwcad":          ("中望CAD",    "heavy",    "launch",   500, 200,   0,   0.0, 4300,  4, 15),
    "wukong":         ("悟空图像",   "heavy",    "launch",   400, 200,   0,   0.0, 4200,  6, 12),
    "wps":            ("WPS",        "medium",   "launch",   350, 150,   0,   0.0, 2300,  2, 10),
    "edrawmax":       ("亿图图示",   "medium",   "launch",   300, 100,   0,   0.0, 2100,  3, 10),
    "mindmaster":     ("mindmaster", "medium",   "launch",   300, 100,   0,   0.0, 1900,  3,  8),
    "hw_video":       ("华为视频",   "medium",   "launch",   300, 300,   0,   0.0, 1800,  1, 20),
    "bilibili":       ("哔哩哔哩",   "chromium", "launch",   600, 200,   0,   0.0, 1800,  2, 20),
    "douyin":         ("抖音",       "chromium", "launch",   700, 250,   0,   0.0, 1700,  1, 15),
    "haitai_browser": ("海泰浏览器", "chromium", "launch",   700, 150,   0,   0.0, 1500,  2, 10),
    "coremail":       ("coremail",   "chromium", "launch",   400, 150,   0,   0.0, 1200,  3,  6),
    "hw_email":       ("华为邮箱",   "medium",   "launch",   300, 150,   0,   0.0, 1100,  3,  6),
}


def daily(a):
    return a.get("cal_daily_mib", a["anon_mib"] + a["clean_mib"] + a["dirty_mib"] + 4)


def peak(a):
    return a.get("cal_peak_mib", a["anon_mib"] + a["clean_mib"] + a["dirty_mib"] + 4 + a.get("peak_mib", 0))


def build(p):
    cal = json.load(open(p.calibration)) if p.calibration else {}
    apps = []
    for name, (display, cat, role, anon, clean, dirty, rate, task, after, hold) in APPS.items():
        a = {"name": name, "display": display, "kind": cat, "role": role,
             "anon_mib": int(anon * p.scale), "clean_mib": int(clean * p.scale), "dirty_mib": int(dirty * p.scale),
             "dirty_rate_mib_s": rate, "compress_ratio": p.compress_ratio,
             "oom_adj": 200 if name == "hw_music" else (900 if role == "resident" else 700)}
        if role == "launch":
            a.update({"peak_mib": int(task * p.scale), "peak_page_only": 1, "peak_after_s": after, "hold_s": hold,
                      "peak_deadline_s": 60, "exit_after_s": p.close_after, "fg_adj": 100})
        if name in cal:
            a["cal_daily_mib"] = cal[name]["daily_mib"]
            a["cal_peak_mib"] = cal[name]["peak_mib"]
        apps.append(a)
    by = {a["name"]: a for a in apps}
    residents = [a for a in apps if a["role"] == "resident"]
    pool = [a["name"] for a in apps if a["role"] == "launch"]
    m = p.memory_max
    open_base = sum(daily(a) for a in residents)

    def valid(group):
        g = [by[n] for n in group]
        return (max(peak(a) for a in g) <= m and open_base + sum(daily(a) for a in g) <= m and
                sum(peak(a) for a in g) >= 1.2 * m)

    combos = [c for c in itertools.combinations(pool, p.group_size) if valid(c)]
    if len(combos) < p.cases:
        raise SystemExit(f"only {len(combos)} valid {p.group_size}-app combinations; need {p.cases}")
    # Different combinations per case: greedily cover apps not used yet, then keep
    # the group peak sum near target_ratio * M so measured values keep a margin over 1.2 M.
    chosen, used = [], set()
    for _ in range(p.cases):
        best = max((c for c in combos if c not in chosen),
                   key=lambda c: (len(set(c) - used), -abs(sum(peak(by[n]) for n in c) / m - p.target_ratio), c))
        chosen.append(best)
        used |= set(best)
    cases = []
    for k, c in enumerate(chosen):
        # Heavy apps last so the last click (foreground) differs between cases.
        order = sorted(c, key=lambda n: (peak(by[n]), n))
        order = order[k % len(order):] + order[:k % len(order)]
        cases.append({"id": f"case{k + 1}", "apps": order})

    tl = []
    for i, a in enumerate(residents):
        tl.append({"t": 3 * i, "op": "launch", "apps": [a["name"]]})
    t = p.first_case
    for k, c in enumerate(cases):
        tl.append({"t": t, "op": "launch_group", "case": c["id"], "apps": c["apps"], "click_gap_s": p.click_gap})
        if k + 1 < len(cases):
            tl.append({"t": t + p.return_offset, "op": "return", "apps": [a["name"] for a in residents]})
        t += p.case_gap
    s = p.memory_max / 5120.0
    return {
        "name": p.name,
        "note": "20 whitelisted apps (estimated profiles), Harmony scene-2 style concurrent-launch cases",
        "generator_args": vars(p),
        "scope": {"memory_max_mib": p.memory_max, "swap_max_mib": p.swap_max, "zswap_max_mib": p.zswap_max},
        "duration_s": p.first_case + (len(cases) - 1) * p.case_gap + p.tail,
        "policy_kill": {"window_s": 1.0, "cooldown_s": 1.0,
                        "levels": [{"some_ms": p.medium_ms, "min_adj": 800},
                                   {"full_ms": p.critical_ms, "min_adj": 0}]},
        "memshiftd": {
            "reserve_mb": int(round(128 * s)), "ready_free_mb": int(round(32 * s)), "startup_grace_sec": 5,
            "io_some_limit_pct": 50, "max_boost_mb": max(4096, p.memory_max), "batch_mb": 256,
            "max_per_minute_mb": 16384, "app_per_minute_mb": 4096, "alpha": 1.0, "anon_weight": 1.0,
            "keep_ratio": 0.2, "episode_fraction": 0.5, "cooldown_sec": 1,
            # A launch group's demand exceeds half the machine by design (condition 3),
            # so the hint cap must allow up to memory minus reserve.
            "boost_cap_fraction": 1.0,
        },
        "apps": apps,
        "cases": cases,
        "timeline": sorted(tl, key=lambda e: e["t"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="wl16")
    ap.add_argument("--memory-max", type=int, default=16384)
    ap.add_argument("--swap-max", type=int, default=25600)
    ap.add_argument("--zswap-max", type=int, default=2048)
    ap.add_argument("--compress-ratio", type=float, default=3.0)
    ap.add_argument("--scale", type=float, default=1.0, help="multiply every app size")
    ap.add_argument("--calibration", default=None, help="calibrate_apps.py output")
    ap.add_argument("--cases", type=int, default=4)
    ap.add_argument("--group-size", type=int, default=6)
    ap.add_argument("--target-ratio", type=float, default=1.3, help="preferred group peak sum / M")
    ap.add_argument("--click-gap", type=float, default=2.0, help="seconds between clicks inside a case")
    ap.add_argument("--close-after", type=float, default=5.0, help="seconds an app stays open after its task")
    ap.add_argument("--first-case", type=int, default=60)
    ap.add_argument("--case-gap", type=int, default=110)
    ap.add_argument("--return-offset", type=int, default=47)
    ap.add_argument("--tail", type=int, default=60)
    ap.add_argument("--medium-ms", type=float, default=200, help="lmkd-like partial stall threshold per 1 s")
    ap.add_argument("--critical-ms", type=float, default=700, help="lmkd-like complete stall threshold per 1 s")
    ap.add_argument("--out", required=True)
    p = ap.parse_args()
    sc = build(p)
    with open(p.out, "w") as f:
        json.dump(sc, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
