#!/usr/bin/env python3
"""Tabulate session-style runs (one cold start, operations as consecutive phases), e.g. WeChat.

For every phase: peak (split at the moment the total peaks) and the level at the end of the
phase (mean of its last 10 s), averaged over repeats (session_r1, session_r2, ...).
"delta" = this phase's end level minus the previous phase's end level.
Usage: python3 report_session.py RESULT_DIR [label-file.json]
"""
import csv
import json
import sys
from pathlib import Path

MiB = 1 << 20
LABELS = {
    "startup": "S01 启动并加载会话列表", "steady_list": "会话列表静置 30 s", "switch_chats": "S02 依次打开 8 个会话（只读）",
    "history": "S03 往上翻聊天记录（只读）", "moments": "S05 刷朋友圈（只读）", "channels": "看视频号 5 个视频（只读）",
    "view_image": "S04 发图片到文件传输助手并看大图", "background_transfer": "S07/S08 后台：发 50 MB 文件到文件传输助手，窗口隐藏 2 分钟",
    "restore": "S10 切回前台", "idle": "切回后静置 30 s",
}


def split(r):
    g = lambda k: int(r[k]) / MiB
    return {"total": g("current"), "anon": g("anon"), "file": g("file") - g("shmem"), "other": g("shmem") + g("kernel")}


def one(run_dir):
    rows = list(csv.DictReader(open(run_dir / "samples.csv")))
    rss = list(csv.DictReader(open(run_dir / "rss.csv"))) if (run_dir / "rss.csv").exists() else []
    wss = {e["wss"]: e for e in map(json.loads, open(run_dir / "events.jsonl")) if "wss" in e}
    phases, res = [], {}
    for r in rows:
        if not phases or phases[-1] != r["phase"]:
            phases.append(r["phase"])
    for ph in phases:
        rs = [r for r in rows if r["phase"] == ph]
        t_end = float(rs[-1]["t"])
        tail = [r for r in rs if float(r["t"]) >= t_end - 10]
        pk_row = max(rs, key=lambda r: int(r["current"]))
        near = min(rss, key=lambda r: abs(float(r["t"]) - float(pk_row["t"]))) if rss else None
        res[ph] = {"rss": int(near["rss_kib"]) / 1024 if near else None,
                   "pss": (int(near["pss_kib"]) / 1024, int(near["pss_anon_kib"]) / 1024, int(near["pss_file_kib"]) / 1024) if near else None,
                   "wss": wss.get(ph),
                   "peak": split(pk_row),
                   "end": {k: sum(split(r)[k] for r in tail) / len(tail) for k in ("total", "anon", "file", "other")},
                   "squeezed": max((int(r["zswapped"]) + int(r["swap_current"])) / MiB for r in rs)}
    return phases, res


def main():
    root = Path(sys.argv[1])
    labels = dict(LABELS, **(json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else {}))
    pattern = sys.argv[3] if len(sys.argv) > 3 else "session_r*"
    runs = sorted(p for p in root.glob(pattern) if (p / "samples.csv").exists())
    data = [one(p) for p in runs]
    order = [p for p in data[0][0] if p in labels]
    f = lambda v: f"{v:,.0f}"
    print(f"| 操作 | 次数 | 峰值：总量（范围） / 匿名 / 文件页 / 其他 | 结束时：总量 / 匿名 / 文件页 / 其他 | 比上一步 |")
    print("|---|---|---|---|---|")
    prev = None
    for ph in order:
        got = [res[ph] for _, res in data if ph in res]
        pk = {k: sum(g["peak"][k] for g in got) / len(got) for k in ("total", "anon", "file", "other")}
        en = {k: sum(g["end"][k] for g in got) / len(got) for k in ("total", "anon", "file", "other")}
        rng = [g["peak"]["total"] for g in got]
        delta = "" if prev is None else f"{en['total'] - prev:+,.0f}"
        print(f"| {labels[ph]} | {len(got)} | {f(pk['total'])}（{f(min(rng))}–{f(max(rng))}） / {f(pk['anon'])} / {f(pk['file'])} / {f(pk['other'])}"
              f" | {f(en['total'])} / {f(en['anon'])} / {f(en['file'])} / {f(en['other'])} | {delta} |")
        prev = en["total"]
    if any(g.get("wss") for _, res in data for g in res.values()):
        print("\n| 操作 | 峰值时 RSS 合计 | 峰值时 PSS 合计（匿名 / 文件） | 这一步的 WSS：总 / 匿名 / 文件（时长） |")
        print("|---|---|---|---|")
        for ph in order:
            got = [res[ph] for _, res in data if ph in res and res[ph].get("pss")]
            ws = [g["wss"] for g in got if g.get("wss")]
            if not got:
                continue
            a = lambda xs: sum(xs) / len(xs)
            pss = f"{a([g['pss'][0] for g in got]):,.0f}（{a([g['pss'][1] for g in got]):,.0f} / {a([g['pss'][2] for g in got]):,.0f}）"
            w = "—" if not ws else (f"{a([x['total_kib'] for x in ws]) / 1024:,.0f} / {a([x['anon_kib'] for x in ws]) / 1024:,.0f}"
                                    f" / {a([x['file_kib'] for x in ws]) / 1024:,.0f}（{a([x['secs'] for x in ws]):.0f} s）")
            print(f"| {labels[ph]} | {a([g['rss'] for g in got]):,.0f} | {pss} | {w} |")
    sq = max(g["squeezed"] for _, res in data for g in res.values())
    print(f"\n压缩区 + swap 最大用量：{sq:.1f} MiB；重复次数：{len(runs)}")


if __name__ == "__main__":
    main()
