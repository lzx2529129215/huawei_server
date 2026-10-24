#!/usr/bin/env python3
"""One table for all 20 apps (stage 3 official builds + stage 4 substitutes), cgroup instead of RSS:
应用名称 | 操作 | cgroup | cgroup匿名 | cgroup文件 | cgroup其他 | WSS | WSS匿名 | WSS文件 | WSS其他   (MiB)

cgroup = memory.current of the app's cgroup at the moment it peaks during the operation (100 ms
  samples), mean of the repeats; split at the same moment from memory.stat:
  匿名 = anon, 文件 = file - shmem, 其他 = shmem + kernel. Taken from each app's table.md
  (stage 3: the runs in 阶段3_结果.md; WeChat: its session table).
WSS = as before (rss_wss_summary.py): pages referenced during the operation's phase, window = the
  phase length shown in brackets; WSS文件 includes shared-memory mappings, WSS其他 = "—".
  Stage 3 WSS comes from the separate RSS/WSS runs (rss_wss/), as in 阶段3_结果.md.
Usage: cgroup_wss_summary.py RESULTS_DIR [--csv]
"""
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import SPECS  # noqa: E402
from report_wss import OP_PHASE  # noqa: E402

R = Path(sys.argv[1])
CSV = "--csv" in sys.argv
#        name, key, cgroup table dir, WSS runs dir
APPS = [("WPS", "wps", "wps_trial", "rss_wss/wps_trial"),
        ("亿图脑图 MindMaster", "mindmaster", "mindmaster_trial", "rss_wss/mindmaster_trial"),
        ("亿图图示", "edrawmax", "edrawmax_trial", "rss_wss/edrawmax_trial"),
        ("微信", "wechat", "wechat_trial", None),
        ("飞书", "feishu", "feishu_trial", "feishu_trial"), ("钉钉", "dingtalk", "dingtalk_trial", "dingtalk_trial"),
        ("剪映 → Shotcut", "shotcut", "shotcut_trial", "shotcut_trial"), ("悟空图像 → GIMP", "gimp", "gimp_trial", "gimp_trial"),
        ("华为视频 → VLC", "vlc", "vlc_trial", "vlc_trial"), ("中望CAD → LibreCAD", "librecad", "librecad_trial", "librecad_trial"),
        ("备忘录 → Gnote", "gnote", "gnote_trial", "gnote_trial"), ("华为音乐 → Rhythmbox", "rhythmbox", "rhythmbox_trial", "rhythmbox_trial"),
        ("coremail → Thunderbird", "thunderbird", "thunderbird_trial", "thunderbird_trial"),
        ("日历 → GNOME 日历", "calendar", "calendar_trial", "calendar_trial"), ("华为浏览器 → Chrome", "chrome", "chrome_trial", "chrome_trial"),
        ("海泰浏览器 → Edge", "edge", "edge_trial", "edge_trial"), ("哔哩哔哩 → B 站网页版", "bilibili", "bilibili_trial", "bilibili_trial"),
        ("抖音 → 抖音网页版", "douyin", "douyin_trial", "douyin_trial"), ("小红书 → Imgur 网页版", "imgur", "imgur_trial", "imgur_trial"),
        ("华为邮箱 → Evolution", "evolution", "evolution_trial", "evolution_trial")]
num = lambda c: re.split("（", c)[0].strip()
avg = lambda xs: sum(xs) / len(xs) if xs else None


def cg_rows(sub):
    """label -> [peak total, anon, file, other] (strings as printed in table.md)"""
    lines = (R / sub / "table.md").read_text(encoding="utf-8").splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 次数 | 峰值 总量"))
    out = {}
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        out.setdefault(c[0], [num(x) for x in c[2:6]])
    return out


def wss(sub, run, op):
    wt, wa, wf, ws = [], [], [], []
    for d in sorted((R / sub).glob(f"{run}_*")):
        if not (d / "events.jsonl").exists():
            continue
        w = next((e for e in map(json.loads, open(d / "events.jsonl")) if e.get("wss") == op), None)
        if w:
            wt.append(w["total_kib"] / 1024); wa.append(w["anon_kib"] / 1024)
            wf.append(w["file_kib"] / 1024); ws.append(w["secs"])
    if not wt:
        return ["—", "—", "—"]
    return [f"{avg(wt):,.0f}（{avg(ws):.0f} s）", f"{avg(wa):,.0f}", f"{avg(wf):,.0f}"]


def wechat_rows():
    lines = (R / "wechat_trial/table_wss.md").read_text(encoding="utf-8").splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 次数 | 峰值：总量"))
    j = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 峰值时 RSS 合计"))
    w = {}
    for l in lines[j + 2:]:
        if not l.startswith("|"):
            break
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        m = re.match(r"([\d,]+) / ([\d,]+) / ([\d,]+)（(\d+) s）", c[3])
        w[c[0]] = [f"{m.group(1)}（{m.group(4)} s）", m.group(2), m.group(3)] if m else ["—"] * 3
    out = []
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        parts = [num(x) for x in c[2].split(" / ")]
        out.append(["微信", c[0]] + parts[:4] + w.get(c[0], ["—"] * 3) + ["—"])
    return out


rows = []
for name, key, sub, wsub in APPS:
    if key == "wechat":
        rows += wechat_rows()
        continue
    cg = cg_rows(sub)
    for run, entries in SPECS[key].items():
        entries = entries if isinstance(entries, list) else [entries]
        for label, _, _, _ in entries:
            if label not in cg:
                continue
            op = "background" if label.startswith("S07") else "restore" if label.startswith("S10") else OP_PHASE[run]
            rows.append([name, label] + cg[label] + wss(wsub, run, op) + ["—"])

HEAD = ["应用名称", "操作", "cgroup", "cgroup匿名", "cgroup文件", "cgroup其他", "WSS(测试时长不准确,仅作参考)", "WSS匿名", "WSS文件", "WSS其他"]
if CSV:
    w = csv.writer(sys.stdout)
    w.writerow(HEAD)
    for r in rows:
        w.writerow([c.replace(",", "") if re.fullmatch(r"-?[\d,]+", c) else c for c in r])
else:
    print("| " + " | ".join(HEAD) + " |")
    print("| --- | --- | " + " | ".join(["---:"] * 8) + " |")
    for r in rows:
        print("| " + " | ".join(r) + " |")
