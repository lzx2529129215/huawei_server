#!/usr/bin/env python3
"""RSS / PSS / WSS table for runs made with APPMEM_WSS=1.

Per operation:
  * cgroup peak (memory.current, 100 ms samples) in the operation's phases, and its time t*;
  * RSS and PSS summed over the app's processes, from the 1 s smaps_rollup sample nearest t*
    (RSS double-counts pages shared between processes; PSS splits them);
  * WSS of the operation phase: pages referenced during that phase (clear_refs at its start,
    smaps Referenced at its end), split anon / file; only mapped pages are visible to WSS;
  * PSS and WSS of the quiet phase after the operation.
Usage: python3 report_wss.py RESULT_DIR   (e.g. .../rss_wss/wps_trial)
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import SPECS, app_of  # noqa: E402

MiB = 1 << 20
OP_PHASE = {"s01_blank": "settle", "s02_edit": "edit", "s03_bigdoc": "scroll", "s03_bigmap": "browse",
            "s03_bigdiagram": "browse", "s04_multidoc": "switch", "s04_expand_all": "expand",
            "s04_libraries": "load_libraries", "s05_ppt": "show", "s05_layout": "layout", "s05_relayout": "relayout",
            "s06_export_pdf": "export", "s01_startup": "settle", "s02_switch": "switch_chats",
            "s03_history": "history", "s04_docs": "docs", "s05_view_image": "view_image",
            "s03_multitrack": "preview", "s04_scrub": "scrub", "s05_effects": "effects", "s06_export_4k": "export",
            "s08_proxy_bg": "background", "s09_export_bg": "background", "s03_open_big": "open_big",
            "s04_layers": "layers", "s05_filters": "filters", "s06_export": "export",
            "s02_play": "play", "s03_quality": "quality", "s04_seek": "seek", "s05_fullscreen": "fullscreen",
            "s06_playlist": "playlist", "s09_bg_play": "background",
            "s02_browse": "browse", "s03_tabs": "tabs", "s04_maps": "maps", "s05_video": "video",
            "s06_tab_restore": "restore_tabs", "s08_download": "background", "s09_bg_audio": "background",
            "s05_danmaku_fullscreen": "danmaku", "s06_live": "live", "s02_read": "read", "s03_search": "search",
            "s05_attachments": "attachments", "s06_compose": "compose",
            "s04_search": "search", "s03_covers": "covers", "s04_skip": "skip", "s05_scan": "scan",
            "s08_bg_play": "background", "s02_event": "event", "s03_week": "week", "s05_import": "import", "s04_zoom_pan": "zoom_pan", "s02_feed": "feed", "s03_comments": "comments", "s04_live": "live", "s03_album": "album", "s04_videos": "videos", "s05_redraw": "redraw", "s06_export_pdf": "export"}


def table(root):
    spec = SPECS[app_of(root)]
    out = []
    for run, entries in spec.items():
        entries = entries if isinstance(entries, list) else [entries]
        for d in sorted(root.glob(f"{run}_*")):
            if not (d / "rss.csv").exists():
                continue
            samples = list(csv.DictReader(open(d / "samples.csv")))
            rss = list(csv.DictReader(open(d / "rss.csv")))
            wss = {e["wss"]: e for e in map(json.loads, open(d / "events.jsonl")) if "wss" in e}
            for label, peak_phases, steady_phase, _ in entries:
                op_phase = "background" if label.startswith("S07") else "restore" if label.startswith("S10") \
                    else OP_PHASE[run]
                quiet = "background" if label.startswith("S07") else steady_phase
                pk_rows = [r for r in samples if r["phase"] in peak_phases]
                pk = max(pk_rows, key=lambda r: int(r["current"]))
                t_pk = float(pk["t"])
                near = min(rss, key=lambda r: abs(float(r["t"]) - t_pk))
                q_rss = [r for r in rss if r["phase"] == quiet]
                q_pss = sum(int(r["pss_kib"]) for r in q_rss) / len(q_rss) / 1024 if q_rss else None
                w_op, w_q = wss.get(op_phase), wss.get(quiet) if quiet != op_phase else None
                out.append({
                    "label": label, "rep": d.name.rsplit("_", 1)[1],
                    "cg": (int(pk["current"]) / MiB, int(pk["anon"]) / MiB, (int(pk["file"]) - int(pk["shmem"])) / MiB),
                    "rss": int(near["rss_kib"]) / 1024, "pss": int(near["pss_kib"]) / 1024,
                    "pss_anon": int(near["pss_anon_kib"]) / 1024, "pss_file": int(near["pss_file_kib"]) / 1024,
                    "near_dt": abs(float(near["t"]) - t_pk),
                    "wss_op": w_op, "q_pss": q_pss, "wss_q": w_q, "quiet": quiet,
                })
    return out


def fmt_w(w):
    if not w:
        return "—"
    return f"{w['total_kib'] / 1024:,.0f} / {w['anon_kib'] / 1024:,.0f} / {w['file_kib'] / 1024:,.0f}（{w['secs']:.0f} s）"


def main():
    root = Path(sys.argv[1])
    rows = table(root)
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)
    avg = lambda xs: sum(xs) / len(xs)

    def rng(xs):
        return f"{avg(xs):,.0f}" if len(xs) == 1 else f"{avg(xs):,.0f}（{min(xs):,.0f}–{max(xs):,.0f}）"

    def w(rs, key):
        ws = [r[key] for r in rs if r[key]]
        if not ws:
            return "—"
        tot = [x["total_kib"] / 1024 for x in ws]
        return (f"{rng(tot)} / {avg([x['anon_kib'] for x in ws]) / 1024:,.0f} / {avg([x['file_kib'] for x in ws]) / 1024:,.0f}"
                f"（{avg([x['secs'] for x in ws]):.0f} s）")

    print("| 操作 | 次数 | cgroup 峰值：总量 / 匿名 / 文件页 | 峰值时 RSS 合计 | 峰值时 PSS 合计（匿名 / 文件） | 操作期间 WSS：总 / 匿名 / 文件（时长） | 操作后 PSS | 操作后 WSS：总 / 匿名 / 文件（时长） |")
    print("|---|---|---|---|---|---|---|---|")
    for label, rs in groups.items():
        cg = f"{rng([r['cg'][0] for r in rs])} / {avg([r['cg'][1] for r in rs]):,.0f} / {avg([r['cg'][2] for r in rs]):,.0f}"
        qp = [r["q_pss"] for r in rs if r["q_pss"] is not None]
        print(f"| {label} | {len(rs)} | {cg} | {avg([r['rss'] for r in rs]):,.0f} "
              f"| {rng([r['pss'] for r in rs])}（{avg([r['pss_anon'] for r in rs]):,.0f} / {avg([r['pss_file'] for r in rs]):,.0f}） "
              f"| {w(rs, 'wss_op')} | {rng(qp) if qp else '—'} | {w(rs, 'wss_q')} |")


if __name__ == "__main__":
    main()
