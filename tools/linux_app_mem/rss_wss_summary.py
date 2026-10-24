#!/usr/bin/env python3
"""One table for all 20 apps (stage 3 official builds + stage 4 substitutes):
应用名称 | 操作 | RSS | RSS匿名 | RSS文件 | RSS其他 | WSS | WSS匿名 | WSS文件 | WSS其他   (MiB, mean of the repeats)

RSS = sum over the app's processes of smaps Rss (VmRSS = RssAnon + RssFile + RssShmem).
  Stage 4: the 1 s sample (rss.csv) with the largest RSS during the operation's phases:
    RSS匿名 = smaps "Anonymous" summed, RSS文件 = RSS - RSS匿名 (this includes shared memory,
    RssShmem, which was not sampled separately), RSS其他 = "—".
  Stage 3 (WPS, MindMaster, EdrawMax, Feishu, DingTalk): anon RSS was not sampled yet, so all three
    come from the snapshot taken when the run ended (operation done + 30 s quiet); marked "*".
  WeChat (one session per repeat): only the RSS at each step's peak; no per-step anon split.
WSS = pages referenced during the operation's phase (clear_refs at the phase start, smaps
  Referenced at its end, summed over processes); the window is the phase length, shown in brackets.
  WSS文件 includes shared memory mappings; WSS其他 = "—".
Usage: rss_wss_summary.py RESULTS_DIR [--csv]
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
STAGE3 = [("WPS", "rss_wss/wps_trial"), ("亿图脑图 MindMaster", "rss_wss/mindmaster_trial"),
          ("亿图图示", "rss_wss/edrawmax_trial"), ("微信", "wechat_trial"), ("飞书", "feishu_trial"),
          ("钉钉", "dingtalk_trial")]
STAGE4 = [("剪映 → Shotcut", "shotcut_trial"), ("悟空图像 → GIMP", "gimp_trial"), ("华为视频 → VLC", "vlc_trial"),
          ("中望CAD → LibreCAD", "librecad_trial"), ("备忘录 → Gnote", "gnote_trial"),
          ("华为音乐 → Rhythmbox", "rhythmbox_trial"), ("coremail → Thunderbird", "thunderbird_trial"),
          ("日历 → GNOME 日历", "calendar_trial"), ("华为浏览器 → Chrome", "chrome_trial"),
          ("海泰浏览器 → Edge", "edge_trial"), ("哔哩哔哩 → B 站网页版", "bilibili_trial"),
          ("抖音 → 抖音网页版", "douyin_trial"), ("小红书 → Imgur 网页版", "imgur_trial"),
          ("华为邮箱 → Evolution", "evolution_trial")]
avg = lambda xs: sum(xs) / len(xs) if xs else None


def app_key(dirname):
    return dirname.rsplit("/", 1)[-1].replace("_trial", "")


def rows_for(name, sub, stage3):
    root = R / sub
    spec = SPECS[app_key(sub)]
    out = []
    for run, entries in spec.items():
        entries = entries if isinstance(entries, list) else [entries]
        dirs = [d for d in sorted(root.glob(f"{run}_*")) if (d / "rss.csv").exists()]
        if not dirs:
            continue
        for label, peak_phases, steady_phase, _ in entries:
            op = "background" if label.startswith("S07") else "restore" if label.startswith("S10") else OP_PHASE[run]
            rss, an, wt, wa, wf, ws = [], [], [], [], [], []
            for d in dirs:
                ev = [json.loads(l) for l in open(d / "events.jsonl")]
                if stage3:
                    snap = next((e for e in ev if e.get("smaps") == "before_stop"), None)
                    if snap:
                        rss.append(sum(p["Rss"] for p in snap["procs"]) / 1024)
                        an.append(sum(p["Anonymous"] for p in snap["procs"]) / 1024)
                else:
                    rr = [r for r in csv.DictReader(open(d / "rss.csv")) if r["phase"] in peak_phases]
                    if rr:                         # the 1 s sample with the largest RSS during the operation
                        pk = max(rr, key=lambda r: int(r["rss_kib"]))
                        rss.append(int(pk["rss_kib"]) / 1024)
                        an.append(int(pk["anonymous_kib"]) / 1024)
                w = next((e for e in ev if e.get("wss") == op), None)
                if w:
                    wt.append(w["total_kib"] / 1024); wa.append(w["anon_kib"] / 1024)
                    wf.append(w["file_kib"] / 1024); ws.append(w["secs"])
            r, a = avg(rss), avg(an)
            star = "*" if stage3 else ""
            out.append([name, label,
                        f"{r:,.0f}{star}" if r is not None else "—",
                        f"{a:,.0f}{star}" if a is not None else "—",
                        f"{r - a:,.0f}{star}" if r is not None and a is not None else "—", "—",
                        f"{avg(wt):,.0f}（{avg(ws):.0f} s）" if wt else "—",
                        f"{avg(wa):,.0f}" if wa else "—", f"{avg(wf):,.0f}" if wf else "—", "—"])
    return out


def wechat_rows():
    lines = (R / "wechat_trial/table_wss.md").read_text(encoding="utf-8").splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("| 操作 | 峰值时 RSS 合计"))
    out = []
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        m = re.match(r"([\d,]+) / ([\d,]+) / ([\d,]+)（(\d+) s）", c[3])
        w = (f"{m.group(1)}（{m.group(4)} s）", m.group(2), m.group(3)) if m else ("—", "—", "—")
        out.append(["微信", c[0], c[1], "—", "—", "—", w[0], w[1], w[2], "—"])
    return out


HEAD = ["应用名称", "操作", "RSS", "RSS匿名", "RSS文件", "RSS其他", "WSS(测试时长不准确,仅作参考)", "WSS匿名", "WSS文件", "WSS其他"]
rows = []
for name, sub in STAGE3:
    rows += wechat_rows() if name == "微信" else rows_for(name, sub, True)
for name, sub in STAGE4:
    rows += rows_for(name, sub, False)
if CSV:
    w = csv.writer(sys.stdout)
    w.writerow(HEAD)
    for r in rows:
        w.writerow([c.replace(",", "") if re.fullmatch(r"[\d,]+\*?", c) else c for c in r])
else:
    print("| " + " | ".join(HEAD) + " |")
    print("| --- | --- | " + " | ".join(["---:"] * 8) + " |")
    for r in rows:
        print("| " + " | ".join(r) + " |")
