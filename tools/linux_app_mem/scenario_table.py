#!/usr/bin/env python3
"""Section 5 of the stage-4 doc: all 17 measured apps (6 official, 11 substitutes) open at once,
counting shared system files (libraries, fonts, icons ...) only once.

For every app:
    total    = cgroup total of its daily state (S02 steady) or of its peak operation
    charged  = shared system files that were charged to the app's cgroup, from mapped_files.json
               (smaps Rss per file) of its daily-operation run:
                 full-clear apps: every shared file it maps (all were dropped before the run);
                 stage-3-method apps: only the shared files on its own drop list (ldd libraries,
                 its plugin dirs, fonts); the other system libraries were already cached and were
                 charged elsewhere, so they are not in its total in the first place
    own      = total - charged
Scenario = sum of own + union of shared files (per file the largest Rss of any app: a page of libgtk
is in memory once however many apps use it). The Chrome program files count as shared too: the
Bilibili, Douyin and Imgur tests run the same Chrome program. The 6 official apps have no mapped-file record (they
were measured before it existed): charged = 0, and the system files only they use are missing.
Usage: scenario_table.py RESULTS_DIR [--only-stage4 APP,APP...] [--exclude APP,APP...]
  --exclude imgur        leave Imgur out (its 6 GB feed is not representative of 小红书)
  --only-stage4 a,b,...  keep only these substitute apps (plus the 6 official apps)
"""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import residency  # noqa: E402

R = Path(sys.argv[1])
opt = dict(zip(sys.argv[2::2], sys.argv[3::2]))
EXCLUDE = set(filter(None, opt.get("--exclude", "").split(",")))
ONLY = set(filter(None, opt.get("--only-stage4", "").split(","))) or None
GiB = 1024
LIMIT = 16 * GiB * 1.2   # 120% of the 16 GiB simulated machine

# Official apps (stage 3): numbers from 阶段3_结果.md sections 2.1 / 2.2 (MiB).
#   name, daily op, daily total, daily anon, peak op, peak total, peak anon
STAGE3 = [("WPS", "S02 编辑普通文档后静置", 1098, 296, "S06 大文档导出 PDF", 2506, 1674),
          ("亿图脑图 MindMaster", "S02 编辑普通脑图后静置", 705, 248, "S02 编辑普通脑图", 726, 259),
          ("亿图图示", "S02 编辑普通流程图后静置", 1017, 524, "S04 加载 8 类符号库", 1190, 654),
          ("微信", "S02 打开 8 个会话后", 1056, 360, "S04 发图片并看大图", 1629, 833),
          ("飞书", "S02 打开 8 个会话后静置", 2292, 1115, "S03 打开云文档主页并浏览", 3192, 1765),
          ("钉钉", "S02 打开 8 个会话后静置", 773, 212, "S03 打开文档页", 1178, 350)]
FONTS = ["/usr/share/fonts"]
# The Bilibili test runs the Chrome program, so with both open Chrome's own files are in memory once.
CHROME = "/opt/google/chrome/"
SHARED = residency.SHARED_PREFIXES + (CHROME,)
#   name, results dir, daily-op mapped_files run, peak op row, charged: "all" | module | prefix list
STAGE4 = [("剪映 → Shotcut", "shotcut", "shotcut_trial_mapped/s02_edit_m1", "S06", "shotcut_trial"),
          ("悟空图像 → GIMP", "gimp", "gimp_trial_mapped/s02_edit_m1", "S05", "gimp_trial"),
          ("华为视频 → VLC", "vlc", "vlc_trial_mapped/s02_play_m1", "S03", "vlc_trial"),
          ("中望CAD → LibreCAD", "librecad", "librecad_trial_mapped/s02_edit_m1", "S06", "librecad_trial"),
          ("备忘录 → Gnote", "gnote", "gnote_trial_mapped/s02_edit_m1", "S02", "all"),
          ("华为音乐 → Rhythmbox", "rhythmbox", "rhythmbox_trial_mapped/s02_play_m1", "S05", "all"),
          ("coremail → Thunderbird", "thunderbird", "thunderbird_trial_mapped/s02_read_m1", "S06", "all"),
          ("日历 → GNOME 日历", "calendar", "calendar_trial/s02_event_w3", "S05", "all"),
          ("华为浏览器 → Chrome", "chrome", "chrome_trial/s02_browse_w1", "S06", FONTS + [CHROME]),
          ("海泰浏览器 → Edge", "edge", "edge_trial/s02_browse_w1", "S06", FONTS),
          ("哔哩哔哩 → B 站网页版", "bilibili", "bilibili_trial/s02_play_w1", "S06", FONTS + [CHROME]),
          ("抖音 → 抖音网页版", "douyin", "douyin_trial/s02_feed_w1", "S02", FONTS + [CHROME]),
          ("小红书 → Imgur 网页版", "imgur", "imgur_trial/s02_feed_w1", "S02", FONTS + [CHROME]),
          ("华为邮箱 → Evolution", "evolution", "evolution_trial/s02_read_w1", "S05", "evolution_trial")]


def table_rows(app):
    for d in (R / f"{app}_trial", R / f"{app}_trial_evict_narrow"):
        if (d / "table.md").exists():
            lines = (d / "table.md").read_text().splitlines()
            i = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 次数 | 峰值 总量"))
            out = []
            for l in lines[i + 2:]:
                if not l.startswith("|"):
                    break
                out.append([c.strip() for c in l.strip().strip("|").split("|")])
            return out
    return None


num = lambda c: float(re.split("（", c)[0].replace(",", "").strip())
short = lambda s: re.sub(r"（[^）]*）", "", s).strip()


def charged_prefixes(how):
    if how == "all":
        return None
    if isinstance(how, list):
        return how
    return [os.path.realpath(p) for p in __import__(how).evict_list()]


union, apps = {}, []
# background (S07 hidden 2 min, 阶段3_结果.md section 2.3): total, anon
BG3 = {"WPS": (1098, 296), "亿图脑图 MindMaster": (699, 243), "亿图图示": (1019, 526), "微信": (1261, 517),
       "飞书": (2318, 1089), "钉钉": (753, 205)}
for name, daily_txt, daily, daily_anon, peak_txt, peak, peak_anon in STAGE3:
    apps.append(dict(name=name, daily_op=daily_txt, daily=daily, daily_anon=daily_anon, peak_op=peak_txt,
                     peak=peak, peak_anon=peak_anon, bg=BG3[name][0], bg_anon=BG3[name][1],
                     charged=0.0, mapped=None))
for name, app, run, peak_row, how in STAGE4:
    if app in EXCLUDE or (ONLY and app not in ONLY):
        continue
    rows = table_rows(app)
    if not rows:
        apps.append(dict(name=name, missing=True))
        continue
    d = next(x for x in rows if x[0].startswith("S02 "))
    p = next(x for x in rows if x[0].startswith(peak_row + " "))
    b = next(x for x in rows if x[0].startswith("S07 "))
    a = dict(name=name, daily_op=short(d[0]) + "，稳定值", daily=num(d[6]), daily_anon=num(d[7]),
             peak_op=short(p[0]), peak=num(p[2]), peak_anon=num(p[3]), bg=num(b[6]), bg_anon=num(b[7]),
             charged=0.0, mapped=None)
    mf = R / run / "mapped_files.json"
    if mf.exists():
        files = json.loads(mf.read_text())["files_kib"]
        shared = {f: k for f, k in files.items() if f.startswith(SHARED)}
        for f, k in shared.items():
            union[f] = max(union.get(f, 0), k)
        pre = charged_prefixes(how)
        a["mapped"] = sum(shared.values()) / 1024
        a["charged"] = sum(k for f, k in shared.items()
                           if pre is None or any(f == q or f.startswith(q.rstrip("/") + "/") for q in pre)) / 1024
        # never more than the app's file pages: a file on the drop list that another running process
        # keeps mapped (e.g. Evolution's libraries, mapped by the EDS services) cannot be dropped
        a["charged"] = min(a["charged"], num(d[8]))
    apps.append(a)

ok = [a for a in apps if not a.get("missing")]
U = sum(union.values()) / 1024
print("| 应用 | 日常取自 | 日常总量 | 日常匿名 | 其中共享文件 | 日常自己的部分 | 峰值操作 | 峰值总量 | 峰值匿名 | 峰值比日常多 |")
print("| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |")
for a in apps:
    if a.get("missing"):
        print(f"| {a['name']} | （还没测完） | | | | | | | | |")
        continue
    ch = "—" if a["mapped"] is None else f"{a['charged']:,.0f}"
    a["peak"] = max(a["peak"], a["daily"]); a["peak_anon"] = max(a["peak_anon"], a["daily_anon"])
    print(f"| {a['name']} | {a['daily_op']} | {a['daily']:,.0f} | {a['daily_anon']:,.0f} | {ch} | "
          f"{a['daily'] - a['charged']:,.0f} | {a['peak_op']} | {a['peak']:,.0f} | {a['peak_anon']:,.0f} | "
          f"+{a['peak'] - a['daily']:,.0f} |")
S = sum(a["daily"] for a in ok)
C = sum(a["charged"] for a in ok)
own = S - C
D = own + U
fmt = lambda v: f"{v:,.0f}"
gib = lambda v: f"{v:,.0f}（{v / GiB:.1f} GiB）"
B = sum(a["bg"] for a in ok) - C + U
print(f"\n直接相加 {S:,.0f}；算在各应用上的共享文件合计 {C:,.0f}；共享文件只算一次 {U:,.0f}\n")
print("| 场景 | 应用数 | 直接相加 | 共享文件只算一次 | 其中匿名 |")
print("| --- | ---: | ---: | ---: | ---: |")
print(f"| 都处于日常状态（S02 后静置） | {len(ok)} | {fmt(S)} | {gib(D)} | {fmt(sum(a['daily_anon'] for a in ok))} |")
print(f"| 都处于后台静置状态（S07 隐藏 2 分钟） | {len(ok)} | {fmt(sum(a['bg'] for a in ok))} | {gib(B)} | "
      f"{fmt(sum(a['bg_anon'] for a in ok))} |")
print(f"\n120% 线 {LIMIT:,.0f}；日常离它还差 {LIMIT - D:,.0f}\n")
print("| 峰值操作 | 比日常多 | 其中匿名 | 日常 + 这一个峰值 | 超过 19.2 GiB？ |")
print("| --- | ---: | ---: | ---: | --- |")
for a in sorted(ok, key=lambda a: a["peak"] - a["daily"], reverse=True):
    inc = a["peak"] - a["daily"]
    if inc < 300:
        continue
    print(f"| {a['name']}：{a['peak_op']} | +{inc:,.0f} | +{a['peak_anon'] - a['daily_anon']:,.0f} | {gib(D + inc)} | "
          f"{'**是**' if D + inc > LIMIT else '否'} |")
print(f"\nAPPS {len(ok)}  daily_direct {S:,.0f}  charged_sum {C:,.0f}  own {own:,.0f}  union {U:,.0f}  "
      f"daily_scenario {D:,.0f} ({D / GiB:.1f} GiB)  mapped_sum {sum(a['mapped'] or 0 for a in ok):,.0f}")
print(f"daily_anon {sum(a['daily_anon'] for a in ok):,.0f}")
B = sum(a["bg"] for a in ok) - C + U
print(f"background_direct {sum(a['bg'] for a in ok):,.0f}  background_scenario {B:,.0f} ({B / GiB:.1f} GiB)  "
      f"anon {sum(a['bg_anon'] for a in ok):,.0f}")
print(f"LIMIT {LIMIT:,.0f}  need_extra {LIMIT - D:,.0f}")
inc = sorted(ok, key=lambda a: a["peak"] - a["daily"], reverse=True)
acc = D
for a in inc:
    acc += a["peak"] - a["daily"]
    print(f"  +{a['peak'] - a['daily']:7,.0f} (anon +{a['peak_anon'] - a['daily_anon']:6,.0f})  {acc:9,.0f} "
          f"({acc / GiB:5.1f} GiB)  {a['name']} {a['peak_op']}")
