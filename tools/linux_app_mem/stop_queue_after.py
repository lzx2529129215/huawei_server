#!/usr/bin/env python3
"""Wait until APP's formal runs are done, then stop run_formal.sh so the apps queued after it never
start (user decision 2026-10-22: keep the stage-3 method, no reruns). Anything of the next app that
already started is stopped and its partial results removed; the first results kept under
<app>_trial_evict_narrow are moved back to <app>_trial. Finally a Windows toast.
Usage: stop_queue_after.py APP NEXT_APP [NEXT_APP ...]
"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = Path("/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021")
app, later = sys.argv[1], sys.argv[2:]
log = R / f"{app}_trial/run.log"
while not (log.exists() and f" done {app}" in log.read_text()):
    time.sleep(3)
me = {os.getpid(), os.getppid()}
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) in me:
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    if len(argv) > 1 and argv[0] == b"bash" and argv[1] == b"run_formal.sh":
        os.kill(int(pid), signal.SIGTERM)
        print("stopped queue", pid, flush=True)
time.sleep(2)
for nxt in later:
    subprocess.run(["python3", "-I", str(HERE / "stop_test.py"), f"{nxt}_trial.py", nxt])
    new, old = R / f"{nxt}_trial", R / f"{nxt}_trial_evict_narrow"
    if old.exists():
        if new.exists():
            shutil.rmtree(new)          # partial rerun, never used
        old.rename(new)
        print("restored", new, flush=True)
subprocess.run(["python3", "-I", str(HERE / "notify_windows.py"), "全部测完",
                f"{app} 已完成，队列已停止（不再重测）"], capture_output=True)
print("done", time.strftime("%T"), flush=True)
