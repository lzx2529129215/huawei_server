#!/usr/bin/env python3
"""Fixed-window working set table (runs made with APPMEM_WSS=1 APPMEM_WSS_WINDOW=10).

Each "wssw" event is one window: the pages referenced during `secs` seconds (Denning's w(t, tau)).
Windows restart at every phase mark, so a full window (full=true, tau = 10 s) never mixes two
phases. Per operation (labels and phases from report.py / report_wss.py):
  during  = the operation's phase (S07: the hidden 2 minutes, S10: after the window is shown again)
  quiet   = the quiet phase after the operation (idle / steady)
For each, all full windows of all repeats are pooled: mean and max of total / anon / file (MiB).
A phase shorter than one window has only a cut-short window: it is shown with its length, e.g.
"62 (5 s)", and is not comparable with the 10 s values.
Usage: report_wss10.py RESULT_DIR [--md]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import SPECS, app_of  # noqa: E402
from report_wss import OP_PHASE  # noqa: E402

MiB = 1024


def windows(d):
    return [e for e in map(json.loads, open(d / "events.jsonl")) if "wssw" in e]


def pool(dirs, phase):
    full, part = [], []
    for d in dirs:
        for w in windows(d):
            if w["wssw"] == phase:
                (full if w["full"] else part).append(w)
    return full, part


def stats(full, part):
    if full:
        n = len(full)
        mean = [sum(w[k] for w in full) / n / MiB for k in ("total_kib", "anon_kib", "file_kib")]
        mx = [max(w[k] for w in full) / MiB for k in ("total_kib", "anon_kib", "file_kib")]
        return n, mean, mx, None
    if part:                                   # phase shorter than one window
        n = len(part)
        mean = [sum(w[k] for w in part) / n / MiB for k in ("total_kib", "anon_kib", "file_kib")]
        secs = sum(w["secs"] for w in part) / n
        return 0, mean, mean, secs
    return 0, None, None, None


def fmt(v, secs=None):
    if v is None:
        return "—"
    s = f"{v:,.0f}"
    return f"{s}（{secs:.0f} s）" if secs is not None else s


root = Path(sys.argv[1])
spec = SPECS[app_of(root)]
print("| 操作 | 窗口数 | 期间平均 总 | 期间平均 匿名 | 期间平均 文件 | 期间最大 总 | 期间最大 匿名 | 静置平均 总 | 静置平均 匿名 |")
print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
for run, entries in spec.items():
    entries = entries if isinstance(entries, list) else [entries]
    dirs = [d for d in sorted(root.glob(f"{run}_*")) if (d / "events.jsonl").exists()]
    if not dirs:
        continue
    for label, peak_phases, steady_phase, _ in entries:
        op = "background" if label.startswith("S07") else "restore" if label.startswith("S10") else OP_PHASE[run]
        n, mean, mx, secs = stats(*pool(dirs, op))
        if label.startswith("S07"):
            qm = None
        else:
            qn, qm, _, qsecs = stats(*pool(dirs, steady_phase))
        cells = [label, str(n) if n else "—"]
        cells += [fmt(mean[i], secs) for i in range(3)] if mean else ["—"] * 3
        cells += [fmt(mx[i], secs) for i in range(2)] if mx else ["—"] * 2
        cells += [fmt(qm[i], qsecs) for i in range(2)] if qm else ["—"] * 2
        print("| " + " | ".join(cells) + " |")
