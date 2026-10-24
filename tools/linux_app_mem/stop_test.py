#!/usr/bin/env python3
"""Stop a running trial script and its app scopes without matching the calling shell.

Usage: stop_test.py SCRIPT_NAME [APP]   e.g. stop_test.py rhythmbox_trial.py rhythmbox
Only python processes whose argv contains SCRIPT_NAME as a separate argument are killed.
"""
import os
import signal
import subprocess
import sys
import time

script = sys.argv[1]
app = sys.argv[2] if len(sys.argv) > 2 else None
me = {os.getpid(), os.getppid()}
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) in me:
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        continue
    if "python" in os.path.basename(exe) and script.encode() in argv:
        os.kill(int(pid), signal.SIGTERM)
        print("killed", pid)
time.sleep(1)
if app:
    units = subprocess.run(["systemctl", "--user", "list-units", "--no-legend", "--plain", f"appmem-{app}-*"],
                           capture_output=True, text=True).stdout.split()
    for u in [x for x in units if x.endswith(".scope")]:
        subprocess.run(["systemctl", "--user", "stop", u])
        print("stopped", u)
    subprocess.run(["systemctl", "--user", "reset-failed"], capture_output=True)
