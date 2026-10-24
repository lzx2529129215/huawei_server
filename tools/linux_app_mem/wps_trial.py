#!/usr/bin/env python3
"""Stage 2: WPS (Linux build) memory, one cold start per operation, on a private Xvfb screen.

Usage: python3 wps_trial.py OUT_DIR [--rep=r1] [run ...]   (default: every run)
Runs: s01_blank s02_edit s03_bigdoc s04_multidoc s05_ppt s06_export_pdf s07_s10_background
Each run opens fresh copies of the materials (so WPS never restores an earlier run's edits),
and the copies are deleted afterwards.
"""
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import appmem  # noqa: E402

M = Path.home() / "linux_app_mem/materials/wps"
EVICT = ["/opt/kingsoft/wps-office", "~/.config/Kingsoft", "~/.local/share/Kingsoft", "/usr/share/fonts"]
WPS, WPP = "/usr/bin/wps", "/usr/bin/wpp"
X = appmem.sh


def filler(n, seed=7):
    rng = random.Random(seed)
    words = "memory pressure reclaim anonymous file page cache document editing paragraph table figure".split()
    out = []
    while sum(len(w) + 1 for w in out) < n:
        out.append(rng.choice(words))
    return " ".join(out)


def settle(r, quiet_mib=4, window_s=5, timeout=90):
    """Wait until memory.current moves less than quiet_mib over window_s seconds."""
    hist, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        hist.append((time.monotonic(), int((r.cg / "memory.current").read_text())))
        hist = [h for h in hist if h[0] >= hist[-1][0] - window_s]
        if hist[-1][0] - hist[0][0] >= window_s - 0.3 and \
                (max(v for _, v in hist) - min(v for _, v in hist)) < quiet_mib * appmem.MiB:
            return True
        time.sleep(0.25)
    return False


def no_wps_running():
    return subprocess.run(["pgrep", "-f", "/opt/kingsoft/wps-office"], capture_output=True).returncode != 0


def new_windows(r, before):
    return [x for x in r.windows() if x[0] not in before]


def open_main(r, argv, pattern, timeout=90):
    r.launch(argv)
    w = r.wait_window(pattern, timeout=timeout)
    r.maximize(w)
    return w


# ---------------------------------------------------------------- runs ---
def click_from_bottom_right(w, dx, dy):
    """Click a button placed relative to a dialog's bottom-right corner; Qt may restore a
    different dialog size, so absolute coordinates are not stable."""
    g = X(["xdotool", "getwindowgeometry", "--shell", str(w)]).stdout
    kv = dict(line.split("=") for line in g.split() if "=" in line)
    X(["xdotool", "mousemove", "--window", str(w), str(int(kv["WIDTH"]) - dx), str(int(kv["HEIGHT"]) - dy), "click", "1"])


def s01_blank(r, d):
    w = open_main(r, [WPS, str(d / "blank.docx")], "blank")
    r.mark("settle"); settle(r); r.shot("loaded")
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_edit(r, d):
    w = open_main(r, [WPS, str(d / "normal1.docx")], "normal1")
    r.mark("settle"); settle(r); r.shot("loaded")
    r.mark("edit"); r.key(w, "ctrl+End", "Return")
    for k in range(6):
        r.type(w, filler(160, seed=k) + " ", delay_ms=45); r.key(w, "Return")
    r.shot("edited")
    r.mark("idle"); time.sleep(60); r.shot("idle")


def s03_bigdoc(r, d):
    w = open_main(r, [WPS, str(d / "big.docx")], "big")
    r.mark("settle"); settle(r, timeout=120); r.shot("loaded")
    r.mark("scroll")
    for _ in range(15):
        r.key(w, *(["Next"] * 10), delay_ms=150)
    r.key(w, "ctrl+End"); time.sleep(3); r.shot("end")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s04_multidoc(r, d):
    w = open_main(r, [WPS] + [str(d / f"normal{k}.docx") for k in range(1, 6)], "normal")
    time.sleep(10); r.mark("settle"); settle(r, timeout=120); r.shot("loaded")
    r.mark("switch")
    # Ctrl+Tab in WPS only toggles between the two most recent tabs, so click the tabs in turn.
    tab_x = [250, 497, 740, 984, 1228]  # tab centres at 3840x2160 (normal1..normal5)
    for i in range(60):
        X(["xdotool", "mousemove", "--window", w, str(tab_x[i % 5]), "20", "click", "1"]); time.sleep(1)
        if i in (1, 22, 43):  # expect normal2, normal3, normal4 active
            r.shot(f"switch{i + 1}")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s05_ppt(r, d):
    w = open_main(r, [WPP, str(d / "deck.pptx")], "deck")
    r.mark("settle"); settle(r, timeout=120); r.shot("loaded")
    before = {x[0] for x in r.windows()}
    r.mark("show"); r.key(w, "F5"); time.sleep(4)
    nw = new_windows(r, before)
    sw = nw[0][0] if nw else w
    r.events.write(json.dumps({"show_window": nw}, ensure_ascii=False) + "\n")
    for i in range(40):
        r.key(sw, "Right"); time.sleep(1.5)
        if i in (5, 30):
            r.shot(f"slide{i}")
    r.key(sw, "Escape"); time.sleep(2)
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s06_export_pdf(r, d):
    pdf = d / "big_export.pdf"
    w = open_main(r, [WPS, str(d / "big.docx")], "big")
    r.mark("settle"); settle(r, timeout=120); r.shot("loaded")
    before = {x[0] for x in r.windows()}
    r.mark("export")
    X(["xdotool", "mousemove", "--window", w, "50", "52", "click", "1"]); time.sleep(2)
    r.shot("file_menu")
    menu = new_windows(r, before)[0][0]
    X(["xdotool", "mousemove", "--window", menu, "97", "208", "click", "1"]); time.sleep(3)
    dlg = r.wait_window("Export PDF", timeout=30)
    r.type(dlg, str(pdf), delay_ms=20); time.sleep(1); r.shot("dialog")
    click_from_bottom_right(dlg, 50, 26)        # OK (was 480,465 in a 530x491 dialog)
    # Some fonts in the document may not be embedded; WPS then asks "Embed as Image" or
    # "Replace with Similar Font". "Replace" only swaps the fonts and does not export, so we
    # click the default "Embed as Image", which exports in one step.
    for _ in range(60):  # the prompt can take a while to appear on a loaded machine
        prompt = [x for x in r.windows() if x[1] == "Prompt"]
        if prompt:
            r.shot("font_prompt")
            click_from_bottom_right(prompt[0][0], 282, 31)  # "Embed as Image" (303,404 in 585x435)
            r.events.write(json.dumps({"font_prompt": "Embed as Image"}) + "\n")
            break
        time.sleep(0.5)
    t0, last, stable = time.monotonic(), -1, 0
    while time.monotonic() < t0 + 600:
        size = pdf.stat().st_size if pdf.exists() else -1
        if size < 0 and time.monotonic() > t0 + 120:
            raise RuntimeError("no PDF file 120 s after clicking OK")
        stable = stable + 1 if size == last and size > 0 else 0
        if stable >= 5:  # file size unchanged for 5 s: export finished
            break
        last = size; time.sleep(1)
    r.events.write(json.dumps({"export_seconds": round(time.monotonic() - t0, 1)}) + "\n")
    done = [x for x in r.windows() if x[1] == "Export to PDF File"]
    if done:  # "Export completed" box: Close
        r.shot("export_completed")
        click_from_bottom_right(done[0][0], 147, 30); time.sleep(1)  # Close (175,69 in 322x99)
    r.mark("idle", pdf_bytes=pdf.stat().st_size if pdf.exists() else None); r.shot("exported")
    time.sleep(30)


def s07_s10_background(r, d):
    w = open_main(r, [WPS, str(d / "normal1.docx")], "normal1")
    r.mark("settle"); settle(r); r.shot("loaded")
    r.mark("edit"); r.key(w, "ctrl+End", "Return"); r.type(w, filler(300) + " ", delay_ms=45)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", w], check=False)  # hidden, like minimized
    time.sleep(5); r.shot("background")
    time.sleep(115)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", w], check=False); time.sleep(1)
    r.key(w, "ctrl+Home"); r.key(w, *(["Next"] * 20), delay_ms=200); r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_blank, s02_edit, s03_bigdoc, s04_multidoc, s05_ppt, s06_export_pdf, s07_s10_background]


def main():
    out = Path(sys.argv[1])
    args = sys.argv[2:]
    rep = "r1"
    if args and args[0].startswith("--rep="):
        rep, args = args[0][6:], args[1:]
    names = args or [f.__name__ for f in RUNS]
    appmem.use_xvfb()
    for name in names:
        fn = globals()[name]
        if not no_wps_running():
            sys.exit(f"WPS is already running; close it before {name}")
        r = appmem.Run("wps", f"{name}_{rep}", out)
        work = r.out / "work"
        work.mkdir(exist_ok=True)
        for f in M.glob("*.*x"):
            shutil.copyfile(f, work / f.name)
        n = appmem.evict(EVICT + [str(work)])
        r.mark("evicted", files=n, display=appmem.XVFB_DISPLAY)
        try:
            fn(r, work)
        except Exception as e:  # keep the samples, record the failure
            r.mark("error", error=repr(e))
            print(f"{name}: ERROR {e!r}", flush=True)
        finally:
            r.stop()
        shutil.rmtree(work)  # our own copies; the exported PDF size is in events.jsonl
        s = appmem.summarize(r.out)
        (r.out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
        print(f"{name}: " + ", ".join(f"{p}: peak {v['peak']['current']} (anon {v['peak']['anon']}, file {v['peak']['file']})"
                                      for p, v in s.items() if p not in ("start", "stop", "evicted")), flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
