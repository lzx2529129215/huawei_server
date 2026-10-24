#!/usr/bin/env python3
"""Stage-4 summary: the Excel's four items for every substitute app, from each app's table.md.
Each item names the operation row (label prefix) and whether its peak or its steady value is used.
Usage: doc_summary.py RESULTS_DIR
"""
import re
import sys
from pathlib import Path

R = Path(sys.argv[1])
#        whitelist app, substitute, dir, daily, peak, background, switch back   (row prefix, "peak"|"steady")
APPS = [
    ("剪映", "Shotcut", "shotcut_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("悟空图像", "GIMP", "gimp_trial", ("S02", "steady"), ("S05", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("华为视频", "VLC", "vlc_trial", ("S02", "steady"), ("S03", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("中望CAD", "LibreCAD", "librecad_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("备忘录", "Gnote", "gnote_trial", ("S02", "steady"), ("S02", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("华为音乐", "Rhythmbox", "rhythmbox_trial", ("S02", "steady"), ("S05", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("coremail", "Thunderbird", "thunderbird_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("日历", "GNOME 日历", "calendar_trial", ("S02", "steady"), ("S05", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("华为浏览器", "Chrome", "chrome_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("海泰浏览器", "Edge", "edge_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("哔哩哔哩", "B 站网页版", "bilibili_trial", ("S02", "steady"), ("S06", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("抖音", "抖音网页版", "douyin_trial", ("S02", "steady"), ("S02", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("小红书", "Imgur 网页版", "imgur_trial", ("S02", "steady"), ("S02", "peak"), ("S07", "steady"), ("S10", "peak")),
    ("华为邮箱", "Evolution", "evolution_trial", ("S02", "steady"), ("S05", "peak"), ("S07", "steady"), ("S10", "peak")),
]
ITEMS = [("前台日常基线", 3), ("前台最大操作峰值", 4), ("后台静置基线", 5), ("切回前台恢复峰值", 6)]


def rows(path):
    lines = path.read_text().splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 次数 | 峰值 总量"))
    out = []
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        out.append([c.strip() for c in l.strip().strip("|").split("|")])
    return out


num = lambda c: re.split("（", c)[0].strip()
data = {}
for app in APPS:
    t = R / app[2] / "table.md"
    if t.exists():
        data[app[2]] = rows(t)
for title, k in ITEMS:
    print(f"\n### {title}\n")
    print("| 白名单应用 → 替代 | 取自 | 总量 | 匿名 | 文件页 | 其他 |")
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    tot = [0.0] * 4
    for app in APPS:
        if app[2] not in data:
            print(f"| {app[0]} → {app[1]} | （未测） | | | | |")
            continue
        prefix, which = app[k]
        r = next((x for x in data[app[2]] if x[0].startswith(prefix + " ")), None)
        if not r:
            print(f"| {app[0]} → {app[1]} | {prefix}（无此行） | | | | |")
            continue
        cols = (r[2], r[3], r[4], r[5]) if which == "peak" else (r[6], r[7], r[8], r[9])
        vals = [num(c) for c in cols]
        for i, v in enumerate(vals):
            tot[i] += float(v.replace(",", ""))
        label = r[0] + ("（峰值）" if which == "peak" else "（稳定值）")
        print(f"| {app[0]} → {app[1]} | {label} | " + " | ".join(vals) + " |")
    print("| **合计** | | " + " | ".join(f"**{v:,.0f}**" for v in tot) + " |")
