#!/usr/bin/env python3
"""One-time QR login to Xiaohongshu web for the Xiaohongshu runs (user's personal account,
2026-10-22, read-only use only: no likes, collects, comments, follows, messages or posts).

Chrome is already running on the Xvfb screen with profile ~/linux_app_mem/browser/xhs and
--remote-debugging-port=9303, on xiaohongshu.com/explore with the login dialog open. This script
copies the dialog's QR code to a PNG, shows it enlarged on the Windows desktop (ImageMagick
`display` on the WSLg screen :0) for the user to scan, follows QR refreshes, and stops when the
login dialog is gone and a profile link is present. It never clicks anything except the dialog's
own "refresh QR" area while the dialog is still open. Then it closes Chrome normally (so the
cookies are written) and saves the profile as profiles/xhs_baseline.
Delete ~/linux_app_mem/browser/xhs and profiles/xhs_baseline after the tests.
Usage: xhs_login.py [TIMEOUT_S]
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cdp  # noqa: E402

PORT = 9303
PROFILE = Path.home() / "linux_app_mem/browser/xhs"
BASE = Path.home() / "linux_app_mem/profiles/xhs_baseline"
OUT = Path(os.environ.get("XHS_QR_DIR", "/tmp")) / "xhs_qr"
timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 600

b = cdp.Browser(PORT, timeout=30)
t = next(p for p in b.pages() if p["type"] == "page" and "xiaohongshu.com" in p["url"])
s = cdp.Session(t["webSocketDebuggerUrl"])
STATE = """(() => {
  const m = document.querySelector('.login-modal');
  const q = document.querySelector('img.qrcode-img');
  const box = document.querySelector('.login-container');
  const me = [...document.querySelectorAll('a[href*="/user/profile/"]')].some(a => a.closest('.side-bar, .side-bar-component, nav, [class*=side]'));
  return {modal: !!m, qr: q ? q.src : null, text: box ? box.innerText.slice(0, 200) : '', me};
})()"""

shown, viewer, last = None, None, time.monotonic()
end = time.monotonic() + timeout
OUT.mkdir(parents=True, exist_ok=True)


def show(src):
    global viewer
    raw = OUT / "qr_raw.png"
    big = OUT / "qr_big.png"
    raw.write_bytes(base64.b64decode(src.split(",", 1)[1]))
    subprocess.run(["convert", str(raw), "-filter", "point", "-resize", "400%", "-bordercolor", "white",
                    "-border", "40", str(big)], check=True)
    if viewer:
        viewer.terminate()
    env = dict(os.environ, DISPLAY=":0")
    env.pop("WAYLAND_DISPLAY", None)
    viewer = subprocess.Popen(["display", "-title", "Xiaohongshu login QR - scan with the Xiaohongshu app",
                               str(big)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


while time.monotonic() < end:
    st = s.js(STATE)
    if not st["modal"] and st["me"]:
        print("logged in", time.strftime("%T"), flush=True)
        break
    if st["modal"] and st["qr"] and st["qr"] != shown:
        show(st["qr"])
        shown = st["qr"]
        print("QR shown", time.strftime("%T"), flush=True)
    if st["modal"] and ("过期" in st["text"] or "失效" in st["text"]):
        # click the expired QR image (still inside the login dialog) to get a new one
        r = s.js("(() => {const q=document.querySelector('img.qrcode-img'); const b=q.getBoundingClientRect();"
                 " return [b.x+b.width/2, b.y+b.height/2]})()")
        for typ in ("mousePressed", "mouseReleased"):
            s.call("Input.dispatchMouseEvent", type=typ, x=r[0], y=r[1], button="left", clickCount=1)
        print("QR expired, refreshed", time.strftime("%T"), flush=True)
        time.sleep(3)
    time.sleep(2)
else:
    if viewer:
        viewer.terminate()
    sys.exit("timeout: not logged in")

if viewer:
    viewer.terminate()
time.sleep(8)                       # let the site finish its post-login requests and set cookies
ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version"))
cdp.Session(ver["webSocketDebuggerUrl"]).call("Browser.close")
for _ in range(60):                 # wait until Chrome has exited (cookies flushed)
    if subprocess.run(["pgrep", "-f", f"[u]ser-data-dir={PROFILE}"], capture_output=True).returncode:
        break
    time.sleep(1)
BASE.mkdir(parents=True, exist_ok=True)
subprocess.run(["rsync", "-a", "--delete", f"{PROFILE}/", f"{BASE}/"], check=True)
print("saved", BASE, time.strftime("%T"), flush=True)
