#!/usr/bin/env python3
"""Add several apps' memory for a "many apps at once" scenario, counting shared system files once.

For each app give one run directory (e.g. its daily operation, rep w1) whose mapped_files.json was
recorded right before the app was stopped. For every app:
    total, anon, file, other      = cgroup memory at the chosen point (peak or steady, from summary.json)
    shared                        = shared system files it maps (libraries, fonts, icons ...; smaps Rss)
    own                           = total - shared
Scenario total = sum of own + union of shared, where the union takes, for every shared file, the
largest resident size any app maps (a page of libgtk is in memory once however many apps use it).
Limits: smaps counts mapped pages only, so read-ahead parts of shared files stay in "own" (the sum is
slightly too high); the union per file is the maximum, not a page-exact union (slightly too low).
Usage: scenario_sum.py [--steady PHASE] RUN_DIR [RUN_DIR ...]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import residency  # noqa: E402

MiB = 1 << 20
args = sys.argv[1:]
phase = None
if args and args[0] == "--steady":
    phase, args = args[1], args[2:]
union, rows = {}, []
for d in map(Path, args):
    mf = json.loads((d / "mapped_files.json").read_text())
    files = mf["files_kib"]
    shared = {p: k for p, k in files.items() if p.startswith(residency.SHARED_PREFIXES)}
    for p, k in shared.items():
        union[p] = max(union.get(p, 0), k)
    s = json.loads((d / "summary.json").read_text())
    if phase:
        pk = s[phase]["peak"]
    else:                                   # largest total over all phases except stop
        pk = max((v["peak"] for k, v in s.items() if k not in ("stop", "evicted")), key=lambda v: v["current"])
    sh = sum(shared.values()) / 1024
    rows.append((d.name, pk["current"], pk["anon"], pk["file"] - pk["shmem"], pk["shmem"] + pk["kernel"], sh))
print("| 运行 | 总量 | 匿名 | 文件页 | 其他 | 其中共享系统文件 | 应用自己的部分 |")
print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
for name, tot, an, fi, ot, sh in rows:
    print(f"| {name} | {tot:,.0f} | {an:,.0f} | {fi:,.0f} | {ot:,.0f} | {sh:,.0f} | {tot - sh:,.0f} |")
naive = sum(r[1] for r in rows)
own = sum(r[1] - r[5] for r in rows)
u = sum(union.values()) / 1024
print(f"\n直接相加 {naive:,.0f} MiB；各应用自己的部分合计 {own:,.0f} + 共享系统文件只算一次 {u:,.0f} = {own + u:,.0f} MiB"
      f"（共享文件逐个应用相加是 {sum(r[5] for r in rows):,.0f}，重复计算了 {sum(r[5] for r in rows) - u:,.0f}）")
