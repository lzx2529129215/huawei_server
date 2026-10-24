#!/usr/bin/env python3
"""Generate the five aligned-peak scenarios cgA..cgE of the experiment plan
results/linux_oom_lab_rss_plan_20261022/实验设置.md (16 GiB, 25 GiB swap, 2 GiB zswap).

Every app is one memlab_app built from its measured cgroup memory (WSL, Linux
builds; plan §2): anon -> cold anonymous pages (3x compressible), file ->
clean page cache, other (shared + kernel memory) -> anonymous pages locked in
memory, so they count but can never be reclaimed (plan §3: "其他不算").

Timeline (plan §5): apps start every 3 s; each peak app starts its ramp at
T - ramp and grows at increment / ramp, so all peak apps reach the peak at
T = 300 s together; they hold 15 s, then return to daily one every 15 s
(shortest measured hold first, plan §5.2); then the background apps are
switched back one every 15 s (all daily memory touched, file re-read). The
oracle hint goes out 30 s before the first ramp (run_round.py --lead 30).

Background apps are reclaimed and sent to disk most expendable first; that
order is their oom_adj (higher = more expendable), also used by the kill
policy. Rhythmbox plays music (perceptible, 200); peak apps are doing the
user's work (100).

Usage: make_cgroup_scenarios.py --out-dir scenarios [--calibration-dir DIR]
  writes cgA.json .. cgE.json; with --calibration-dir, DIR/calibration_cgX.json
  (calibrate_apps.py) adds the measured synthetic daily/peak of every app.
"""
import argparse
import json
from pathlib import Path

# Copied from results/linux_oom_lab_rss_plan_20261022/实验设置.md §4 (daily tables, peak tables)
# and §5.2 (release order). WSL cgroup measurements of Linux builds (阶段3+4), not HarmonyOS data.
# daily: (app, total, anon, file, other, role)   peaks: (app, operation, increment, ramp_s)   MiB
SCENARIOS = {
    'A': {
        "title": '办公 + 图片视频创作',
        "daily": [
            ('wechat', 1056, 360, 634, 59, 'background'),
            ('feishu', 2292, 1115, 798, 377, 'background'),
            ('dingtalk', 773, 212, 514, 44, 'background'),
            ('wps', 1098, 296, 722, 77, 'peak'),
            ('thunderbird', 1477, 465, 930, 78, 'background'),
            ('shotcut', 1128, 754, 295, 77, 'peak'),
            ('gimp', 256, 208, 41, 4, 'peak'),
            ('calendar', 643, 367, 237, 36, 'peak'),
            ('gnote', 575, 364, 173, 35, 'background'),
            ('rhythmbox', 182, 66, 113, 2, 'background'),
            ('evolution', 192, 141, 26, 21, 'background'),
            ('vlc', 446, 232, 145, 66, 'background'),
        ],
        "peaks": [
            ('shotcut', 'S06 4K 导出', 5306, 62),
            ('gimp', 'S05 大图上运行高斯模糊 + USM 锐化', 1851, 25),
            ('wps', 'S06 大文档导出 PDF', 1408, 20),
            ('calendar', 'S05 导入 5000 条事件的 ICS 文件', 1327, 194),
        ],
        "release": ['gimp', 'wps', 'shotcut', 'calendar'],
    },
    'B': {
        "title": '浏览器 + 创作',
        "daily": [
            ('chrome', 1157, 398, 567, 189, 'peak'),
            ('edge', 1040, 323, 531, 184, 'peak'),
            ('bilibili', 1208, 441, 615, 150, 'background'),
            ('douyin', 2170, 946, 634, 588, 'background'),
            ('shotcut', 1128, 754, 295, 77, 'peak'),
            ('gimp', 256, 208, 41, 4, 'peak'),
            ('wechat', 1056, 360, 634, 59, 'background'),
            ('dingtalk', 773, 212, 514, 44, 'background'),
            ('gnote', 575, 364, 173, 35, 'background'),
            ('rhythmbox', 182, 66, 113, 2, 'background'),
            ('evolution', 192, 141, 26, 21, 'background'),
            ('vlc', 446, 232, 145, 66, 'background'),
        ],
        "peaks": [
            ('shotcut', 'S06 4K 导出', 5306, 62),
            ('gimp', 'S05 大图上运行高斯模糊 + USM 锐化', 1851, 25),
            ('chrome', 'S04 复杂网页（Google 地图缩放拖动 60 s）', 1487, 66),
            ('edge', 'S04 复杂网页（Google 地图缩放拖动 60 s）', 1412, 66),
        ],
        "release": ['gimp', 'chrome', 'edge', 'shotcut'],
    },
    'C': {
        "title": '浏览器多标签 + 办公',
        "daily": [
            ('chrome', 1157, 398, 567, 189, 'peak'),
            ('edge', 1040, 323, 531, 184, 'peak'),
            ('feishu', 2292, 1115, 798, 377, 'background'),
            ('wechat', 1056, 360, 634, 59, 'peak'),
            ('dingtalk', 773, 212, 514, 44, 'background'),
            ('wps', 1098, 296, 722, 77, 'peak'),
            ('thunderbird', 1477, 465, 930, 78, 'background'),
            ('gnote', 575, 364, 173, 35, 'background'),
            ('rhythmbox', 182, 66, 113, 2, 'background'),
            ('calendar', 643, 367, 237, 36, 'background'),
            ('evolution', 192, 141, 26, 21, 'background'),
        ],
        "peaks": [
            ('chrome', 'S06 多标签集中恢复（丢弃 9 个后逐个唤醒）', 3891, 76),
            ('edge', 'S06 多标签集中恢复（丢弃 9 个后逐个唤醒）', 3676, 79),
            ('wps', 'S06 大文档导出 PDF', 1408, 20),
            ('wechat', 'S04 发图片到文件传输助手并看大图', 573, 19),
        ],
        "release": ['wechat', 'chrome', 'edge', 'wps'],
    },
    'D': {
        "title": '影音娱乐 + 创作',
        "daily": [
            ('douyin', 2170, 946, 634, 588, 'background'),
            ('bilibili', 1208, 441, 615, 150, 'peak'),
            ('vlc', 446, 232, 145, 66, 'peak'),
            ('shotcut', 1128, 754, 295, 77, 'peak'),
            ('gimp', 256, 208, 41, 4, 'peak'),
            ('wechat', 1056, 360, 634, 59, 'background'),
            ('rhythmbox', 182, 66, 113, 2, 'background'),
            ('calendar', 643, 367, 237, 36, 'peak'),
            ('gnote', 575, 364, 173, 35, 'background'),
            ('dingtalk', 773, 212, 514, 44, 'background'),
            ('evolution', 192, 141, 26, 21, 'background'),
            ('thunderbird', 1477, 465, 930, 78, 'background'),
        ],
        "peaks": [
            ('shotcut', 'S06 4K 导出', 5306, 62),
            ('gimp', 'S05 大图上运行高斯模糊 + USM 锐化', 1851, 25),
            ('calendar', 'S05 导入 5000 条事件的 ICS 文件', 1327, 194),
            ('bilibili', 'S06 观看直播 60 s', 764, 18),
            ('vlc', 'S03 1080p 切到 4K 高码率', 695, 67),
        ],
        "release": ['gimp', 'bilibili', 'vlc', 'shotcut', 'calendar'],
    },
    'E': {
        "title": '后台导出 + 前台多个操作',
        "daily": [
            ('shotcut', 1128, 754, 295, 77, 'peak'),
            ('gimp', 256, 208, 41, 4, 'peak'),
            ('wps', 1098, 296, 722, 77, 'peak'),
            ('chrome', 1157, 398, 567, 189, 'peak'),
            ('calendar', 643, 367, 237, 36, 'background'),
            ('wechat', 1056, 360, 634, 59, 'background'),
            ('dingtalk', 773, 212, 514, 44, 'background'),
            ('rhythmbox', 182, 66, 113, 2, 'background'),
            ('bilibili', 1208, 441, 615, 150, 'background'),
            ('feishu', 2292, 1115, 798, 377, 'background'),
            ('gnote', 575, 364, 173, 35, 'background'),
        ],
        "peaks": [
            ('shotcut', 'S09 后台 4K 导出（窗口隐藏直到完成）', 5306, 61),
            ('gimp', 'S05 大图上运行高斯模糊 + USM 锐化', 1851, 25),
            ('wps', 'S05 PPT 放映 40 页', 1080, 66),
            ('chrome', 'S04 复杂网页（Google 地图缩放拖动 60 s）', 1487, 66),
        ],
        "release": ['gimp', 'chrome', 'wps', 'shotcut'],
    },
}

# Optional (--hot-set wss): memory a background app keeps using while it sits in the background,
# from the measured background WSS (WSL, Linux builds; 阶段3+4_cgroup与WSS汇总.csv, 120 s
# window, reference only): (anon, file, operation). WSS file includes shared memory mappings,
# so it is capped at the app's file pages. Rhythmbox plays music in the scenarios, so S08.
HOT_WSS = {
    "wechat": (96, 215, "S07/S08 后台：发 50 MB 文件到文件传输助手，窗口隐藏 2 分钟"),
    "feishu": (498, 1458, "S07/S08 后台：发 50 MB 文件给自己，窗口隐藏 2 分钟"),
    "dingtalk": (17, 63, "S07 后台驻留（窗口隐藏 2 分钟）"),
    "thunderbird": (386, 43, "S07 后台邮箱驻留（窗口隐藏 2 分钟）"),
    "gnote": (3, 6, "S07 后台笔记驻留（窗口隐藏 2 分钟）"),
    "rhythmbox": (58, 26, "S08 后台音频播放（窗口隐藏 2 分钟）"),
    "evolution": (6, 30, "S07 后台邮箱驻留（窗口隐藏 2 分钟）"),
    "vlc": (9, 38, "S07 后台暂停驻留（窗口隐藏 2 分钟）"),
    "calendar": (205, 21, "S07 后台日历驻留（窗口隐藏 2 分钟）"),
    "bilibili": (125, 455, "S07 后台暂停驻留（窗口隐藏 2 分钟）"),
    "douyin": (321, 534, "S07 后台暂停驻留（窗口隐藏 2 分钟）"),
}
HOT_PERIOD = 120
# --hot-set wss_all also: file pages a peak app uses during its peak operation, from the measured
# WSS of that operation (same file, reference only; capped at the app's file pages). Read once
# per PEAK_HOT_PERIOD seconds while the peak runs (the period is an assumption).
PEAK_HOT_WSS_FILE = {("shotcut", "S06"): 127, ("shotcut", "S09"): 45, ("gimp", "S05"): 26, ("wps", "S06"): 197,
                     ("wps", "S05"): 199, ("calendar", "S05"): 67, ("chrome", "S04"): 1637, ("chrome", "S06"): 7568,
                     ("edge", "S04"): 1204, ("edge", "S06"): 7063, ("wechat", "S04"): 161, ("bilibili", "S06"): 1278,
                     ("vlc", "S03"): 128}
PEAK_HOT_PERIOD = 10

# Most expendable first (plan §3 "送磁盘的顺序"); value = oom_adj.
EXPENDABLE = {"vlc": 990, "bilibili": 980, "douyin": 970, "gnote": 960, "calendar": 950, "evolution": 940,
              "thunderbird": 930, "dingtalk": 920, "feishu": 910, "wechat": 900, "rhythmbox": 200}
PEAK_ADJ = 100
T, HOLD, GAP, LAUNCH_GAP, TAIL = 300, 15, 15, 3, 30
RATIO = 3.0


def deadline(ramp):
    """A peak counts as an allocation timeout when it finishes 20% (at least 5 s) later than planned."""
    return round(ramp + max(5.0, 0.2 * ramp), 1)


def curve_points(c, inc):
    """Measured rise (make_peak_curves.py) turned into the target peak memory over time: bursts are
    kept as measured; when the measured rise is smaller than the scenario increment the rest is
    added evenly over the ramp, when it is larger the whole curve is scaled down."""
    ramp, rise = c["ramp_s"], c["rise_max_mib"]
    pts = []
    for t, v in zip(c["t"], c["rise_mib"]):
        if rise >= inc:
            m = v * inc / rise
        else:
            m = v + (inc - rise) * (t / ramp if ramp else 1.0)
        pts.append([t, round(m, 1)])
    pts[-1][1] = float(inc)
    return pts


def build(sid, spec, cal, hot=None, suffix="", dirty=0.0, dirty_period=20.0, dirty_scope="background", ramp_cap=None,
          curves=None, partial_ms=200):
    peaks = {p[0]: p for p in spec["peaks"]}
    release = {n: k for k, n in enumerate(spec["release"])}
    assert set(release) == set(peaks), sid
    apps = []
    for name, total, anon, file, other, role in spec["daily"]:
        # Optional dirty share of the file pages (no measurement): a writer thread rewrites the
        # dirty part once per dirty_period, so it stays dirty and must be written back to be freed.
        d = int(round(dirty * file)) if dirty and (dirty_scope == "all" or role == "background") else 0
        a = {"name": name, "role": role, "measured_daily_mib": total, "measured_split_mib": [anon, file, other],
             "anon_mib": anon, "locked_mib": other, "clean_mib": file - d, "dirty_mib": d,
             "dirty_rate_mib_s": round(d / dirty_period, 2) if d else 0,
             "compress_ratio": RATIO, "touch_file": 1,
             "oom_adj": PEAK_ADJ if role == "peak" else EXPENDABLE[name]}
        if role == "peak":
            _, op, inc, measured_ramp = peaks[name]
            # Optional (--ramp-cap): reach the peak faster than measured (an assumption).
            ramp = min(measured_ramp, ramp_cap) if ramp_cap else measured_ramp
            curve = None
            if curves is not None:
                c = curves[f"{name}:{op.split()[0]}"]
                curve = curve_points(c, inc)
                ramp = c["ramp_s"]
            k = release[name]
            if hot == "wss_all":
                a.update({"peak_hot_file_mib": min(PEAK_HOT_WSS_FILE[(name, op.split()[0])], file - d),
                          "peak_hot_period": PEAK_HOT_PERIOD, "peak_hot_source": f"WSS {op}"})
            a.update({"peak_op": op, "peak_mib": inc, "ramp_s": ramp, "measured_ramp_s": measured_ramp, "peak_rate_mib_s": round(inc / ramp, 3),
                      "peak_compress_ratio": RATIO, "hold_s": HOLD + GAP * k, "release_k": k,
                      "peak_deadline_s": deadline(ramp)})
            if curve is not None:
                a.update({"peak_curve": curve, "peak_curve_source": c["run"], "peak_rate_mib_s": 0})
        if hot in ("wss", "wss_all") and role == "background":
            h_anon, h_file, h_op = HOT_WSS[name]
            a.update({"hot_anon_mib": min(h_anon, anon), "hot_file_mib": min(h_file, file), "hot_period": HOT_PERIOD,
                      "hot_source": f"WSS {h_op}"})
        if name in cal:
            a["cal_daily_mib"] = cal[name]["daily_mib"]
            a["cal_peak_mib"] = cal[name]["peak_mib"]
        apps.append(a)
    assert {a["name"] for a in apps if a["role"] == "peak"} == set(peaks), sid
    tl = [{"t": LAUNCH_GAP * i, "op": "launch", "apps": [a["name"]]} for i, a in enumerate(apps)]
    for a in apps:
        if a["role"] == "peak":
            tl.append({"t": round(T - a["ramp_s"], 1), "op": "peak", "apps": [a["name"]]})
    first_ramp = min(T - a["ramp_s"] for a in apps if a["role"] == "peak")
    return_start = T + HOLD + GAP * len(peaks)
    bg = [a["name"] for a in apps if a["role"] == "background"]
    for k, n in enumerate(bg):
        tl.append({"t": return_start + GAP * k, "op": "return", "apps": [n]})
    return {
        "name": f"cg{sid}{suffix}",
        "note": f"场景 {sid}：{spec['title']}. Aligned peaks from WSL cgroup measurements (实验设置.md v2)",
        "scope": {"memory_max_mib": 16384, "swap_max_mib": 25600, "zswap_max_mib": 2048},
        "duration_s": return_start + GAP * len(bg) + TAIL,
        "peak_plan": {"T": T, "hold_s": HOLD, "release_gap_s": GAP, "first_ramp_s": round(first_ramp, 1),
                      "return_start_s": return_start, "release_order": spec["release"], "return_order": bg},
        # wl16 kill policy (plan §1).
        "policy_kill": {"window_s": 1.0, "cooldown_s": 1.0,
                        "levels": [{"some_ms": partial_ms, "min_adj": 800}, {"full_ms": 700, "min_adj": 0}]},
        "popup": {"low_mib": 50, "rearm_mib": 150, "rearm_s": 3.0},
        # Plan §6: watermarks 512/1024 MiB; hinted high = peak still to come + 512, low = high - 256;
        # memory.high pulses on background apps keeping 20% of anon + file; zswap quotas when
        # compressing everything is not enough. Pulses are 64 MiB (plan: at most 256): while a
        # pulse runs, a donor that faults in a page is made to reclaim too (a memory stall), so
        # short pulses keep that stall short, and reclaim is capped at 256 MiB/s so the stalls of
        # back-to-back pulses do not add up past the kill rule (scenario A trial, 2026-10-22).
        "memshiftd": {
            "reclaim_interface": "high", "zswap_quota": "true", "wm_low_mb": 512, "wm_high_mb": 1024,
            "hint_reserve_mb": 512, "hint_gap_mb": 256, "floor_ratio": 0.2, "assumed_compress_ratio": RATIO,
            "pulse_mb": 64, "pulse_rate_mb_s": 256, "pulse_timeout_ms": 5000, "interval_ms": 500, "startup_grace_sec": 5,
            "backoff_sec": 10, "max_boost_mb": 16384,
        },
        "apps": apps,
        "timeline": sorted(tl, key=lambda e: (e["t"], e["op"])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--calibration-dir", default=None)
    ap.add_argument("--only", default="ABCDE")
    ap.add_argument("--dirty-ratio", type=float, default=0.0,
                    help="share of each app's daily file pages kept dirty (no measurement; name gets _dNN)")
    ap.add_argument("--dirty-period", type=float, default=20.0, help="seconds to rewrite the dirty part once")
    ap.add_argument("--dirty-scope", choices=["background", "all"], default="background")
    ap.add_argument("--ramp-cap", type=float, default=None,
                    help="every peak app reaches its peak within this many seconds (faster than measured; name gets _rNN)")
    ap.add_argument("--peak-curve", choices=["measured"], default=None,
                    help="peaks follow the measured WSL curves (make_peak_curves.py; name gets _curve)")
    ap.add_argument("--curves", default=str(Path(__file__).resolve().parent / "scenarios" / "peak_curves.json"))
    ap.add_argument("--partial-stall-ms", type=int, default=200,
                    help="kill rule level 1: some stall per 1 s (lmkd default 200 low-RAM, 70 high-end; name gets _kNN)")
    ap.add_argument("--hot-set", choices=["wss", "wss_all"], default=None,
                    help="background apps keep touching their measured background WSS (scenario name gets _hot)")
    p = ap.parse_args()
    out = Path(p.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for sid in p.only:
        cal = {}
        if p.calibration_dir:
            f = Path(p.calibration_dir) / f"calibration_cg{sid}.json"
            cal = json.loads(f.read_text()) if f.exists() else {}
        suffix = ({"wss": "_hot", "wss_all": "_hotall"}[p.hot_set] if p.hot_set else "") + (f"_d{round(p.dirty_ratio * 100)}" if p.dirty_ratio else "") + \
            ("all" if p.dirty_ratio and p.dirty_scope == "all" else "") + (f"_r{p.ramp_cap:g}" if p.ramp_cap else "") + \
            ("_curve" if p.peak_curve else "") + (f"_k{p.partial_stall_ms}" if p.partial_stall_ms != 200 else "")
        curves = json.loads(Path(p.curves).read_text()) if p.peak_curve else None
        sc = build(sid, SCENARIOS[sid], cal, p.hot_set, suffix, p.dirty_ratio, p.dirty_period, p.dirty_scope, p.ramp_cap,
                   curves, p.partial_stall_ms)
        (out / f"cg{sid}{suffix}.json").write_text(json.dumps(sc, indent=1, ensure_ascii=False))
        print(f"cg{sid}{suffix}: {len(sc['apps'])} apps, first ramp {sc['peak_plan']['first_ramp_s']} s, "
              f"duration {sc['duration_s']} s, calibrated {len(cal)}")


if __name__ == "__main__":
    main()
