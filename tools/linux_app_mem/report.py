#!/usr/bin/env python3
"""Tabulate WPS trial runs: peak (split at the moment the total peaks) and steady values.

Columns (MiB): total = memory.current of the app's scope; anon = anon; file = file - shmem
(file-backed page cache); shmem = shared memory / tmpfs (counted inside memory.stat file);
kernel = kernel memory (slab, page tables, ...). total ~= anon + file + shmem + kernel.
Usage: python3 report.py RESULT_DIR > table.md   (also writes RESULT_DIR/table.csv)
"""
import csv
import json
import sys
from pathlib import Path

MiB = 1 << 20
# app -> run -> (label, operation phases whose peak is reported, steady phase, steady seconds from its end)
SPECS = {
    "wps": {
        "s01_blank": ("S01 启动空白文档", ["launch", "settle"], "steady", 20),
        "s02_edit": ("S02 编辑普通文档（日常）", ["edit"], "idle", 30),
        "s03_bigdoc": ("S03 打开大文档并翻到底", ["launch", "settle", "scroll"], "idle", 20),
        "s04_multidoc": ("S04 5 个文档来回切换", ["switch"], "idle", 20),
        "s05_ppt": ("S05 PPT 放映 40 页", ["show"], "idle", 20),
        "s06_export_pdf": ("S06 大文档导出 PDF", ["export"], "idle", 20),
        "s07_s10_background": [("S07 后台驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台", ["restore"], "idle", 20)],
    },
    "mindmaster": {
        "s01_blank": ("S01 启动空白脑图", ["launch", "settle", "steady"], "steady", 20),  # keeps loading after settle
        "s02_edit": ("S02 编辑普通脑图（日常）", ["edit"], "idle", 30),
        "s03_bigmap": ("S03 打开大型脑图并浏览", ["launch", "settle", "browse"], "idle", 20),
        "s04_expand_all": ("S04 展开全图", ["expand"], "idle", 20),
        "s05_layout": ("S05 切换 4 种布局", ["layout"], "idle", 20),
        "s07_s10_background": [("S07 后台驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台", ["restore"], "idle", 20)],
    },
    "edrawmax": {
        "s01_blank": ("S01 启动空白图", ["launch", "settle", "steady"], "steady", 20),
        "s02_edit": ("S02 编辑普通流程图（日常）", ["edit"], "idle", 30),
        "s03_bigdiagram": ("S03 打开大型网络图并缩放浏览", ["launch", "settle", "browse"], "idle", 20),
        "s04_libraries": ("S04 加载 8 类符号库", ["load_libraries"], "idle", 20),
        "s05_relayout": ("S05 大图全选对齐/分布再撤销", ["relayout"], "idle", 20),
        "s07_s10_background": [("S07 后台驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台", ["restore"], "idle", 20)],
    },
    "feishu": {
        "s01_startup": ("S01 启动并加载消息列表", ["launch", "settle", "steady"], "steady", 20),
        "s02_switch": ("S02 依次打开 8 个会话（只读）", ["switch_chats"], "idle", 20),
        "s03_history": ("往上翻聊天记录（只读）", ["history"], "idle", 20),
        "s04_docs": ("S03 打开云文档主页并浏览（只读）", ["docs"], "idle", 20),
        "s05_view_image": ("发图片给自己并看大图", ["view_image"], "idle", 20),
        "s07_s10_background": [("S07/S08 后台：发 50 MB 文件给自己，窗口隐藏 2 分钟", ["background"], "background", 60),
                               ("S10 切回前台", ["restore"], "idle", 20)],
    },
    "shotcut": {
        "s01_startup": ("S01 启动空项目", ["launch", "settle", "steady"], "steady", 20),
        "s02_edit": ("S02 编辑单轨 1080p 项目（切 5 刀、删 2 段、播放 30 s）", ["edit"], "idle", 20),
        "s03_multitrack": ("S03 3 轨 1080p 预览 60 s", ["preview"], "idle", 20),
        "s04_scrub": ("S04 来回拖动时间线", ["scrub"], "idle", 20),
        "s05_effects": ("S05 叠加特效（高斯模糊 + 辉光）预览 60 s", ["effects"], "idle", 20),
        "s06_export_4k": ("S06 4K 导出", ["export"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并播放 30 s", ["restore"], "idle", 20)],
        "s08_proxy_bg": ("S08 后台生成代理（4 个素材，窗口隐藏 2 分钟）", ["background"], "background", 60),
        "s09_export_bg": ("S09 后台 4K 导出（窗口隐藏直到完成）", ["export_start", "background"], "idle", 20),
    },
    "gimp": {
        "s01_startup": ("S01 启动并新建 1080p 画布", ["launch", "settle", "new_canvas", "steady"], "steady", 20),
        "s02_edit": ("S02 打开照片、裁剪、画 4 笔", ["edit"], "idle", 20),
        "s03_open_big": ("S03 打开 8000×6000 大图", ["open_big"], "idle", 20),
        "s04_layers": ("S04 10 图层项目：复制并移动 5 个图层", ["layers"], "idle", 20),
        "s05_filters": ("S05 大图上运行高斯模糊 + USM 锐化", ["filters"], "idle", 20),
        "s06_export": ("S06 多图层项目导出 PNG", ["export"], "idle", 20),
        "s07_s10_background": [("S07 后台项目驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并缩放画布", ["restore"], "idle", 20)],
        "s09_export_bg": ("S09 后台导出 PNG（窗口隐藏直到完成）", ["export_start", "background"], "idle", 20),
    },
    "vlc": {
        "s01_startup": ("S01 启动（不打开文件）", ["launch", "settle", "steady"], "steady", 20),
        "s02_play": ("S02 播放 1080p 视频 60 s", ["play"], "idle", 20),
        "s03_quality": ("S03 1080p 切到 4K 高码率", ["quality"], "idle", 20),
        "s04_seek": ("S04 连续跳转进度 30 次", ["seek"], "idle", 20),
        "s05_fullscreen": ("S05 进入全屏再返回", ["fullscreen"], "idle", 20),
        "s06_playlist": ("S06 播放列表连续切换 10 个视频", ["playlist"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台继续播放 30 s", ["restore"], "idle", 20)],
        "s09_bg_play": ("S09 后台继续播放（窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "chrome": {
        "s01_startup": ("S01 启动单标签（简单网页）", ["launch", "settle", "steady"], "steady", 20),
        "s02_browse": ("S02 浏览普通网页（3 篇图文页滚动到底）", ["browse"], "idle", 20),
        "s03_tabs": ("S03 打开 10 个标签并轮流切换", ["tabs"], "idle", 20),
        "s04_maps": ("S04 复杂网页（Google 地图缩放拖动 60 s）", ["maps"], "idle", 20),
        "s05_video": ("S05 网页视频（YouTube 4K：清晰度、跳转、全屏）", ["video"], "idle", 20),
        "s06_tab_restore": ("S06 多标签集中恢复（丢弃 9 个后逐个唤醒）", ["discard", "restore_tabs"], "idle", 20),
        "s07_s10_background": [("S07 后台标签驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并操作地图 30 s", ["restore"], "idle", 20)],
        "s08_download": ("S08 后台下载 1 GiB 文件（窗口隐藏）", ["download_start", "background"], "idle", 20),
        "s09_bg_audio": ("S09 后台网页音频（YouTube 播放，窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "edge": {
        "s01_startup": ("S01 启动单标签（简单网页）", ["launch", "settle", "steady"], "steady", 20),
        "s02_browse": ("S02 浏览普通网页（3 篇图文页滚动到底）", ["browse"], "idle", 20),
        "s03_tabs": ("S03 打开 10 个标签并轮流切换", ["tabs"], "idle", 20),
        "s04_maps": ("S04 复杂网页（Google 地图缩放拖动 60 s）", ["maps"], "idle", 20),
        "s05_video": ("S05 网页视频（YouTube 4K：清晰度、跳转、全屏）", ["video"], "idle", 20),
        "s06_tab_restore": ("S06 多标签集中恢复（丢弃 9 个后逐个唤醒）", ["discard", "restore_tabs"], "idle", 20),
        "s07_s10_background": [("S07 后台标签驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并操作地图 30 s", ["restore"], "idle", 20)],
        "s08_download": ("S08 后台下载 1 GiB 文件（窗口隐藏）", ["download_start", "background"], "idle", 20),
        "s09_bg_audio": ("S09 后台网页音频（YouTube 播放，窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "bilibili": {
        "s01_startup": ("S01 启动首页", ["launch", "settle", "steady"], "steady", 20),
        "s02_play": ("S02 播放普通视频 60 s（弹幕开）", ["play"], "idle", 20),
        "s03_quality": ("S03 切换到未登录可用的最高清晰度", ["quality"], "idle", 20),
        "s04_seek": ("S04 连续跳转进度 30 次", ["seek"], "idle", 20),
        "s05_danmaku_fullscreen": ("S05 密集弹幕播放并全屏", ["danmaku"], "idle", 20),
        "s06_live": ("S06 观看直播 60 s", ["live"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台继续播放 30 s", ["restore"], "idle", 20)],
        "s09_bg_play": ("S09 后台继续播放（窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "douyin": {
        "s01_startup": ("S01 启动精选页（不登录）", ["launch", "settle", "steady"], "steady", 20),
        "s02_feed": ("S02 连续刷 8 条固定短视频（每条 15 s）", ["feed"], "idle", 20),
        "s03_comments": ("S03 暂停后打开评论列表并滚动（未登录只显示约 11 条）", ["comments"], "idle", 20),
        "s04_live": ("S04 观看直播 60 s", ["live"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台继续刷 2 条视频", ["restore"], "idle", 20)],
        "s09_bg_play": ("S09 后台继续播放（窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "imgur": {
        "s01_startup": ("S01 启动首页信息流", ["launch", "settle", "steady"], "steady", 20),
        "s02_feed": ("S02 连续滚动图文信息流 60 s", ["feed"], "idle", 20),
        "s03_album": ("S03 浏览 26 张图的多图帖", ["album"], "idle", 20),
        "s04_videos": ("S04 连续播放 10 条短视频", ["videos"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并滚动首页 30 s", ["restore"], "idle", 20)],
    },
    "evolution": {
        "s01_startup": ("S01 启动收件箱（1 万封本地邮件）", ["launch", "settle", "steady"], "steady", 20),
        "s02_read": ("S02 连续阅读 20 封邮件", ["read"], "idle", 20),
        "s04_search": ("S04 在 1 万封邮件中搜索 4 次", ["search"], "idle", 20),
        "s05_attachments": ("S05 内嵌预览 5 封邮件的 1–2 MB 图片附件", ["attachments"], "idle", 20),
        "s07_s10_background": [("S07 后台邮箱驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并打开下一封未读", ["restore"], "idle", 20)],
    },
    "thunderbird": {
        "s01_startup": ("S01 启动收件箱（1 万封本地邮件）", ["launch", "settle", "steady"], "steady", 20),
        "s02_read": ("S02 连续阅读 20 封邮件", ["read"], "idle", 20),
        "s03_search": ("S03 在 1 万封邮件中快速筛选 4 次", ["search"], "idle", 20),
        "s05_attachments": ("S05 预览 5 封大图片附件", ["attachments"], "idle", 20),
        "s06_compose": ("S06 写带 1.9 MB 附件的邮件并存入发件箱", ["compose"], "idle", 20),
        "s07_s10_background": [("S07 后台邮箱驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并打开未读邮件", ["restore"], "idle", 20)],
    },
    "gnote": {
        "s01_startup": ("S01 启动笔记列表（1000 条）", ["launch", "settle", "steady"], "steady", 20),
        "s02_edit": ("S02 长笔记输入、撤销与滚动", ["edit"], "idle", 20),
        "s04_search": ("S04 在 1000 条笔记中全文搜索 4 次", ["search"], "idle", 20),
        "s07_s10_background": [("S07 后台笔记驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并滚动笔记", ["restore"], "idle", 20)],
    },
    "rhythmbox": {
        "s01_startup": ("S01 启动曲库（5000 首）", ["launch", "settle", "steady"], "steady", 20),
        "s02_play": ("S02 前台播放 60 s（显示封面）", ["play"], "idle", 20),
        "s03_covers": ("S03 切换 10 张 3000×3000 高清封面", ["covers"], "idle", 20),
        "s04_skip": ("S04 连续切歌 20 首", ["skip"], "idle", 20),
        "s05_scan": ("S05 扫描导入 5000 首曲库", ["launch", "scan"], "idle", 20),
        "s07_s10_background": [("S07 后台暂停驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台继续播放 30 s", ["restore"], "idle", 20)],
        "s08_bg_play": ("S08 后台音频播放（窗口隐藏 2 分钟）", ["background"], "background", 60),
    },
    "calendar": {
        "s01_startup": ("S01 启动月视图（10,280 条事件）", ["launch", "settle", "steady"], "steady", 20),
        "s02_event": ("S02 新建事件并打开已有事件", ["event"], "idle", 20),
        "s03_week": ("S03 切换到密集周视图并滚动", ["week"], "idle", 20),
        "s04_search": ("S04 在 1 万条事件中搜索 3 次", ["search"], "idle", 20),
        "s05_import": ("S05 导入 5000 条事件的 ICS 文件", ["import"], "idle", 20),
        "s07_s10_background": [("S07 后台日历驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并进入今天", ["restore"], "idle", 20)],
    },
    "librecad": {
        "s01_startup": ("S01 启动空白图纸", ["launch", "settle", "steady"], "steady", 20),
        "s02_edit": ("S02 小图纸上画线、移动、撤销", ["edit"], "idle", 20),
        "s03_open_big": ("S03 打开 15.6 万图元的大图纸", ["open_big"], "idle", 20),
        "s04_zoom_pan": ("S04 大图连续缩放平移", ["zoom_pan"], "idle", 20),
        "s05_redraw": ("S05 大图反复缩放重画 20 次", ["redraw"], "idle", 20),
        "s06_export_pdf": ("S06 大图导出 PDF", ["export"], "idle", 20),
        "s07_s10_background": [("S07 后台图纸驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台并缩放平移", ["restore"], "idle", 20)],
        "s09_export_bg": ("S09 后台导出 PDF（窗口隐藏直到完成）", ["export_start", "background"], "idle", 20),
    },
    "dingtalk": {
        "s01_startup": ("S01 启动并加载消息列表", ["launch", "settle", "steady"], "steady", 20),
        "s02_switch": ("S02 依次打开 8 个会话（只读）", ["switch_chats"], "idle", 20),
        "s03_history": ("打开群聊并往上翻记录（只读）", ["history"], "idle", 20),
        "s04_docs": ("S03 打开文档页（只读）", ["docs"], "idle", 20),
        "s07_s10_background": [("S07 后台驻留（窗口隐藏 2 分钟）", ["background"], "background", 60),
                               ("S10 切回前台", ["restore"], "idle", 20)],
    },
}
SPEC = SPECS["wps"]

def split(r):
    g = lambda k: int(r[k]) / MiB
    return {"total": g("current"), "anon": g("anon"), "file": g("file") - g("shmem"),
            "shmem": g("shmem"), "kernel": g("kernel")}


def app_of(root):
    name = Path(root).resolve().name
    return next((a for a in SPECS if name.startswith(a)), "wps")


def main():
    global SPEC
    root = Path(sys.argv[1])
    SPEC = SPECS[app_of(root)]
    out_rows = []
    for d in sorted(p for p in root.iterdir() if (p / "samples.csv").exists() and not p.name.startswith("explore")):
        run, rep = d.name.rsplit("_", 1)
        if run not in SPEC:
            continue
        rows = list(csv.DictReader(open(d / "samples.csv")))
        errors = [json.loads(l) for l in open(d / "events.jsonl") if '"error"' in l]
        entries = SPEC[run] if isinstance(SPEC[run], list) else [SPEC[run]]
        for label, peak_phases, steady_phase, steady_s in entries:
            out_rows.append(row_for(run, rep, label, rows, errors, peak_phases, steady_phase, steady_s))
    emit(root, out_rows)


def row_for(run, rep, label, rows, errors, peak_phases, steady_phase, steady_s):
    if True:
        pk_rows = [r for r in rows if r["phase"] in peak_phases]
        st_rows = [r for r in rows if r["phase"] == steady_phase]
        if st_rows:
            t_end = float(st_rows[-1]["t"])
            st_rows = [r for r in st_rows if float(r["t"]) >= t_end - steady_s]
        pk = split(max(pk_rows, key=lambda r: int(r["current"]))) if pk_rows else None
        st = {k: sum(split(r)[k] for r in st_rows) / len(st_rows) for k in split(st_rows[0])} if st_rows else None
        squeezed = max((int(r["zswapped"]) + int(r["swap_current"])) / MiB for r in rows)
        return {"run": run, "rep": rep, "label": label, "peak": pk, "steady": st,
                "zswap_or_swap_max_mib": round(squeezed, 1), "errors": errors}


def emit(root, out_rows):
    f = lambda v: "—" if v is None else f"{v:,.0f}"
    q = lambda s: "—" if s is None else " / ".join(f(s[k]) for k in ("total", "anon", "file", "shmem", "kernel"))
    print("| 操作 | 重复 | 峰值：总量 / 匿名 / 文件页 / 共享内存 / 内核 (MiB) | 操作后稳定值：总量 / 匿名 / 文件页 / 共享内存 / 内核 (MiB) | 压缩或换出 | 备注 |")
    print("|---|---|---|---|---|---|")
    for r in out_rows:
        note = "出错：" + r["errors"][0]["error"] if r["errors"] else ""
        print(f"| {r['label']} | {r['rep']} | {q(r['peak'])} | {q(r['steady'])} | {r['zswap_or_swap_max_mib']} MiB | {note} |")
    with open(root / "table.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "rep", "label"] + [f"peak_{k}_mib" for k in ("total", "anon", "file", "shmem", "kernel")]
                   + [f"steady_{k}_mib" for k in ("total", "anon", "file", "shmem", "kernel")] + ["zswap_or_swap_max_mib"])
        for r in out_rows:
            pk, st = r["peak"] or {}, r["steady"] or {}
            w.writerow([r["run"], r["rep"], r["label"]]
                       + [round(pk.get(k, 0), 1) for k in ("total", "anon", "file", "shmem", "kernel")]
                       + [round(st.get(k, 0), 1) for k in ("total", "anon", "file", "shmem", "kernel")]
                       + [r["zswap_or_swap_max_mib"]])



def aggregate(root):
    """Mean and min-max over repeats, per run: peak and steady, as total / anon / file / other."""
    global SPEC
    SPEC = SPECS[app_of(root)]
    rows = list(csv.DictReader(open(Path(root) / "table.csv")))
    by = {}
    for r in rows:
        by.setdefault((r["run"], r["label"]), []).append(r)
    order = list(SPEC)
    print("\n| 操作 | 次数 | 峰值 总量 | 峰值 匿名 | 峰值 文件页 | 峰值 其他 | 稳定 总量 | 稳定 匿名 | 稳定 文件页 | 稳定 其他 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (run, label), rs in sorted(by.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
        def cell(prefix, k):
            if k == "other":
                v = [float(r[f"{prefix}_shmem_mib"]) + float(r[f"{prefix}_kernel_mib"]) for r in rs]
            else:
                v = [float(r[f"{prefix}_{k}_mib"]) for r in rs]
            m = sum(v) / len(v)
            return f"{m:,.0f}" if len(v) == 1 else f"{m:,.0f}（{min(v):,.0f}–{max(v):,.0f}）"
        print(f"| {label} | {len(rs)} | " + " | ".join(cell(p, k) for p in ("peak", "steady")
                                                       for k in ("total", "anon", "file", "other")) + " |")


if __name__ == "__main__":
    main()
    if len(sys.argv) > 2 and sys.argv[2] == "--aggregate":
        aggregate(sys.argv[1])
