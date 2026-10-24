#!/usr/bin/env python3
"""Turn an app's table.md / table_wss.md (report.py / report_wss.py output) into the three tables
used in the stage-3/4 docs: cgroup per operation, the three totals at the peak, and WSS.
One number per cell; only the peak total keeps its 3-run range.
Usage: doc_tables.py RESULT_DIR
"""
import re
import sys
from pathlib import Path

d = Path(sys.argv[1])


def rows(text, header_start):
    lines = text.splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith(header_start))
    out = []
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        out.append([c.strip() for c in l.strip().strip("|").split("|")])
    return out


def first(cell):
    return re.split(r"（", cell)[0].strip()


agg = rows((d / "table.md").read_text(), "| 操作 | 次数 | 峰值 总量")
print("**每个操作（cgroup）**\n")
print("| 操作 | 峰值总量（范围） | 峰值匿名 | 峰值文件页 | 峰值其他 | 稳定总量 | 稳定匿名 | 稳定文件页 | 稳定其他 |")
print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
for r in agg:
    print(f"| {r[0]} | {r[2]} | {first(r[3])} | {first(r[4])} | {first(r[5])} | {first(r[6])} | {first(r[7])} | {first(r[8])} | {first(r[9])} |")

wss = rows((d / "table_wss.md").read_text(), "| 操作 | 次数 | cgroup 峰值")
print("\n**三种口径（峰值时刻）**\n")
print("| 操作 | cgroup 总量 | cgroup 匿名 | RSS 合计 | PSS 合计 | PSS 匿名 | PSS 文件 |")
print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
for r in wss:
    cg = [first(x) for x in r[2].split("/")]
    m = re.match(r"([\d,]+)（[^）]*）（([\d,]+) / ([\d,]+)）", r[4])
    pss, pa, pf = (m.group(1), m.group(2), m.group(3)) if m else (r[4], "", "")
    print(f"| {r[0]} | {cg[0]} | {cg[1]} | {r[3]} | {pss} | {pa} | {pf} |")

print("\n**工作集 WSS**\n")
print("| 操作 | 期间时长 | 期间总 | 期间匿名 | 期间文件 | 静置时长 | 静置总 | 静置匿名 | 静置文件 |")
print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")


def wss_cell(c):
    m = re.match(r"([\d,]+)(?:（[^）]*）)? / ([\d,]+) / ([\d,]+)（(\d+) s）", c)
    return (f"{m.group(4)} s", m.group(1), m.group(2), m.group(3)) if m else ("—", "—", "—", "—")


for r in wss:
    a, b = wss_cell(r[5]), wss_cell(r[7])
    print(f"| {r[0]} | {a[0]} | {a[1]} | {a[2]} | {a[3]} | {b[0]} | {b[1]} | {b[2]} | {b[3]} |")
