#!/usr/bin/env python3
"""Web versions in Google Chrome as same-category substitutes for 哔哩哔哩 / 抖音 / 小红书 (no login).

Same setup as browser_trial.py (Chrome, throwaway profile reset before every run, DevTools
protocol for input), plus Chinese UI/Accept-Language (zh-CN) so the sites serve their Chinese
pages. Nothing is logged in; login pop-ups are closed. Read-only: only play, pause, seek, scroll,
open the comment list, fullscreen. No likes, comments, follows or uploads.
The numbers are "Chrome + this site" (browser and site together), measured from WSL in the US.

Bilibili (A19): s01_startup home, s02_play video 60 s (danmaku on), s03_quality highest quality
  the player allows without login, s04_seek 30 jumps, s05_danmaku_fullscreen, s06_live live room,
  s07_s10_background pause + hide 2 min, restore and play 30 s, s09_bg_play play hidden 2 min.
  S08 (offline download) does not exist on the web.
Douyin (A01), no login (2026-10-22): only the 精选 page's featured video plays and every card click
  opens the login dialog, but single video pages play and show comments. s01_startup 精选 page,
  s02_feed 8 fixed videos 15 s each, s03_comments pause and scroll the comment list, s04_live a
  live room 60 s (gift panel not opened), s07_s10_background, s09_bg_play.
  S05/S06/S08 (camera effects, editing/publishing, uploading) need the app or a login.
Imgur (A17, substitute for 小红书, whose web version needs a QR login): s01_startup home feed,
  s02_feed scroll 60 s, s03_album a 26-image post, s04_videos 10 fixed video posts, 10 s each,
  s07_s10_background hide 2 min, restore and scroll the feed. S05/S06/S08 (editing, publishing,
  uploading) are writes; S09 (notifications) needs an account.

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 web_trial.py bilibili|douyin|imgur OUT_DIR [--rep=r1] [run ...]
"""
import os
import sys
import time
from pathlib import Path

SITE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("bilibili", "douyin", "imgur") else "bilibili"
sys.argv = [sys.argv[0], "chrome"] + sys.argv[2:]       # browser_trial reads the browser name from argv[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_trial as bt  # noqa: E402
from trial_common import X, run_trials, settle  # noqa: E402

ZH = ["--lang=zh-CN", "--accept-lang=zh-CN,zh"]
HOME = {"bilibili": "https://www.bilibili.com/", "douyin": "https://www.douyin.com/", "imgur": "https://imgur.com/"}[SITE]
BILI_VIDEO = "https://www.bilibili.com/video/BV1xrnz6gEVD/"   # 影视飓风 iPhone Duo review: 25 min, 2160p, ~38k danmaku
BILI_LIVE = "https://live.bilibili.com/"


class Abort(RuntimeError):
    pass


def prepare(r, url=HOME):
    r.launch(bt.NO_BUS + [bt.B["bin"]] + bt.FLAGS + ZH + [url])
    w = bt.main_window(r)
    b = bt.browser()
    tid, p = bt.page_of(b)
    r.mark("settle"); bt.wait_load(p, 60); time.sleep(5); dismiss(w, p); settle(r, timeout=90)
    if "WIDTH=3840" not in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
        r.maximize(w); time.sleep(2)
    r.shot("loaded")
    return w, b, tid, p


def dismiss(w, p):
    """Close login pop-ups (never log in) and Chrome's 'open external app?' prompt."""
    X(["xdotool", "key", "--window", str(w), "Escape"], check=False)
    p.js("""(() => { const sel = ['.login-panel-popover .close', '.bili-mini-close-icon', '.close-btn',
        '[class*=login] [class*=close]', '[class*=Login] [class*=close]', '.dy-account-close'];
        let n = 0; for (const s of sel) for (const e of document.querySelectorAll(s)) { try { e.click(); n++ } catch (_) {} }
        return n })()""")


def video_state(p):
    return p.js("(() => {const v=document.querySelector('video'); return v ? {paused: v.paused, t: Math.round(v.currentTime), w: v.videoWidth, h: v.videoHeight} : null})()")


def ensure_playing(p, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = video_state(p)
        if st and not st["paused"]:
            return st
        if st:
            p.js("document.querySelector('video').play().catch(() => 0)")
        time.sleep(1)
    return video_state(p)


def pause(p):
    p.js("(() => {const v=document.querySelector('video'); v && v.pause()})()")


# ------------------------------------------------------------ bilibili ---
def bili_quality(p):
    return p.js("(() => { try { const q = window.player.getQuality(); return [q.nowQ, q.realQ] } catch (e) { return null } })()")


def b01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def b02_play(r, d):
    w, b, tid, p = prepare(r)
    r.mark("play"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); st = ensure_playing(p)
    time.sleep(60); end = video_state(p); r.shot("playing"); pause(p)
    r.mark("idle", start=st, end=end, quality=bili_quality(p)); time.sleep(30)


def b03_quality(r, d):
    w, b, tid, p = prepare(r)
    r.mark("quality"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); ensure_playing(p); time.sleep(15)
    before = bili_quality(p)
    for q in (120, 116, 80, 64, 32):                     # highest the player grants without login
        p.js(f"(() => {{ try {{ window.player.requestQuality({q}) }} catch (e) {{}} }})()"); time.sleep(6)
        now = bili_quality(p)
        if now and now[1] and now[1] >= q:
            break
    dismiss(w, p); ensure_playing(p); time.sleep(30); after = bili_quality(p); r.shot("quality"); pause(p)
    r.mark("idle", before=before, after=after, size=video_state(p)); time.sleep(30)


def b04_seek(r, d):
    w, b, tid, p = prepare(r)
    r.mark("seek"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); ensure_playing(p); time.sleep(3)
    for t in (970, 120, 640, 330, 1110, 50, 810, 490, 1180, 260, 720, 30, 900, 570, 150, 1010, 400, 680, 80, 1150,
              450, 920, 200, 770, 10, 590, 1050, 360, 840, 100):
        p.js(f"document.querySelector('video').currentTime = {t}"); time.sleep(0.5)
    time.sleep(3); r.shot("seeked"); pause(p)
    r.mark("idle", end=video_state(p)); time.sleep(30)


def b05_danmaku_fullscreen(r, d):
    w, b, tid, p = prepare(r)
    r.mark("danmaku"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); ensure_playing(p)
    p.js("document.querySelector('video').currentTime = 300"); time.sleep(20)
    p.js("(document.querySelector('.bpx-player-video-area') || document.querySelector('video')).focus()")
    bt.key(p, "f"); time.sleep(20); r.shot("fullscreen"); bt.key(p, "f"); time.sleep(5); pause(p)
    n = p.js("document.querySelectorAll('.bili-danmaku-x-dm, .b-danmaku').length")
    r.mark("idle", danmaku_nodes=n); time.sleep(30)


def b06_live(r, d):
    w, b, tid, p = prepare(r)
    r.mark("live"); bt.navigate(p, BILI_LIVE); time.sleep(8); dismiss(w, p)
    room = p.js("""(() => { const a = Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
        .find(h => /^https:\\/\\/live\\.bilibili\\.com\\/\\d+/.test(h)); return a || null })()""")
    if not room:
        raise Abort("no live room link on live.bilibili.com")
    bt.navigate(p, room); time.sleep(5); dismiss(w, p); st = ensure_playing(p); time.sleep(60); r.shot("live")
    r.mark("idle", room=room, video=st); pause(p); time.sleep(30)


def b07_s10_background(r, d):
    w, b, tid, p = prepare(r)
    r.mark("idle_fg"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); ensure_playing(p); time.sleep(20); pause(p); time.sleep(10)
    r.mark("background"); bt.hide(w); time.sleep(120)
    r.mark("restore"); bt.show(w); time.sleep(2); dismiss(w, p); ensure_playing(p); time.sleep(30); r.shot("restored"); pause(p)
    r.mark("idle"); time.sleep(30)


def b09_bg_play(r, d):
    w, b, tid, p = prepare(r)
    r.mark("idle_fg"); bt.navigate(p, BILI_VIDEO); time.sleep(3); dismiss(w, p); ensure_playing(p); time.sleep(10)
    r.mark("background"); bt.hide(w); time.sleep(120)
    st = video_state(p)
    r.mark("restore", video=st); bt.show(w); time.sleep(5); r.shot("restored"); pause(p)
    r.mark("idle"); time.sleep(30)


# ------------------------------------------------------------ douyin ---
# Without login only the 精选 page's featured video plays; clicking a card opens the login dialog.
# Single video pages (/video/<id>) play without login and show comments, so the feed is a fixed
# list of videos taken from the 精选 page on 2026-10-22, opened one after another.
DY_VIDEOS = ["7697283722604776745", "7696040469277696889", "7685928416142560512", "7693844311170993417",
             "7689731341721564658", "7698098558267919667", "7697180425155808527", "7697905078228847915",
             "7694206162778017058", "7681664597752827188"]
DY_LIVE = "https://live.douyin.com/"
DY_DIALOG = """(() => {
  const ds = [...document.querySelectorAll('div')].filter(d => { const b = d.getBoundingClientRect();
    return /扫码登录|Log in to Douyin|登录后|手机号登录|验证码登录|Scan to Log In/.test(d.innerText || '') &&
           b.width > 500 && b.width < 1100 && b.height > 300 && b.height < 900 });
  if (!ds.length) return null;
  const area = e => { const b = e.getBoundingClientRect(); return b.width * b.height };
  const dlg = ds.sort((a, b) => area(a) - area(b))[0], r = dlg.getBoundingClientRect();
  const c = [...dlg.querySelectorAll('*')].filter(e => { const b = e.getBoundingClientRect();
    return b.width > 8 && b.width < 50 && b.height > 8 && b.height < 50 && b.top < r.top + 70 && b.right > r.right - 90 });
  if (!c.length) return null;
  const b = c[0].getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2] })()"""


def dy_close_login(p):
    """Close Douyin's login dialog with its own close icon (only when the dialog is on screen)."""
    for _ in range(3):
        xy = p.js(DY_DIALOG)
        if not xy:
            return
        for typ in ("mousePressed", "mouseReleased"):
            p.call("Input.dispatchMouseEvent", type=typ, x=xy[0], y=xy[1], button="left", clickCount=1)
        time.sleep(1.5)


def dy_clear(w, p):
    """Douyin re-opens its login dialog after a while and asks Chrome to open the desktop app
    (xdg-open prompt, cancelled with Escape); clear both. Retries while the page navigates."""
    for _ in range(3):
        try:
            dismiss(w, p); dy_close_login(p)
            return
        except RuntimeError:
            time.sleep(3)


def dy_state(p):
    for _ in range(3):
        try:
            return video_state(p)
        except RuntimeError:
            time.sleep(3)
    return None


def dy_open(w, p, vid):
    bt.navigate(p, f"https://www.douyin.com/video/{vid}"); time.sleep(3)
    for _ in range(3):
        try:
            dy_clear(w, p)
            return ensure_playing(p)
        except RuntimeError:                               # "Inspected target navigated or closed"
            time.sleep(3)
    return dy_state(p)


def d01_startup(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("steady", video=dy_state(p)); time.sleep(15); dy_clear(w, p); time.sleep(15); r.shot("steady")


def d02_feed(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("feed"); played = []
    for vid in DY_VIDEOS[:8]:                              # Excel: a fixed sequence, next one every 15 s
        dy_open(w, p, vid); time.sleep(6); dy_clear(w, p); time.sleep(6); played.append(dy_state(p))
    r.shot("feed"); pause(p)
    r.mark("idle", played=played); time.sleep(30)


def d03_comments(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("comments")
    for attempt in range(2):                               # the comment list sometimes is not there at once
        dy_open(w, p, DY_VIDEOS[0]); time.sleep(5); pause(p); dy_clear(w, p)
        if p.js("!!document.querySelector('[data-e2e=comment-list]')"):
            break
        time.sleep(5)
    else:
        raise Abort("no comment list on the video page")
    for i in range(30):                                    # the comments are below the player: scroll the page
        bt.wheel(p, 800, x=1500, y=1400); time.sleep(1)
        if i % 5 == 4:
            dy_clear(w, p)
    n = p.js("document.querySelectorAll('[data-e2e=comment-item]').length"); r.shot("comments")
    r.mark("idle", comments=n); time.sleep(30)


def d04_live(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("live"); bt.navigate(p, DY_LIVE); time.sleep(8); dy_clear(w, p)
    room = p.js("""(() => { const a = Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
        .find(h => /^https:\\/\\/live\\.douyin\\.com\\/\\d+/.test(h)); return a || null })()""")
    if not room:
        raise Abort("no live room link on live.douyin.com")
    bt.navigate(p, room); time.sleep(5); dy_clear(w, p); st = ensure_playing(p)
    time.sleep(30); dy_clear(w, p); time.sleep(30); r.shot("live")
    r.mark("idle", room=room, video=st); pause(p); time.sleep(30)


def d07_s10_background(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("idle_fg"); dy_open(w, p, DY_VIDEOS[0]); time.sleep(20); dy_clear(w, p); pause(p); time.sleep(10)
    r.mark("background"); bt.hide(w); time.sleep(120)
    r.mark("restore"); bt.show(w); time.sleep(2); dy_clear(w, p)
    for vid in DY_VIDEOS[1:3]:                             # back to the foreground and keep swiping
        dy_open(w, p, vid); time.sleep(13)
    r.shot("restored"); pause(p)
    r.mark("idle"); time.sleep(30)


def d09_bg_play(r, d):
    w, b, tid, p = prepare(r)
    dy_clear(w, p)
    r.mark("idle_fg"); dy_open(w, p, DY_VIDEOS[0]); time.sleep(5); dy_clear(w, p); ensure_playing(p); time.sleep(5)
    r.mark("background"); bt.hide(w); time.sleep(120)
    st = dy_state(p)
    r.mark("restore", video=st); bt.show(w); time.sleep(5); r.shot("restored"); dy_clear(w, p); pause(p)
    r.mark("idle"); time.sleep(30)


# ------------------------------------------------------------ imgur ---
# Same-category substitute for 小红书 (A17), chosen 2026-10-22 because it needs no login: a
# waterfall feed of image and short-video posts, multi-image posts. Fixed posts taken from the
# home feed on 2026-10-22 (non-political ones).
IG_ALBUM = "https://imgur.com/gallery/arts-music-stuff-21-october-2026-izq5WYX"      # 26 images
IG_VIDEOS = ["stranger-finds-abandoned-dog-out-road-tgDLJKJ", "reunited-jGB8wqq", "latte-gets-caught-being-bad-qHKPqKZ",
             "just-some-amazing-cosplay-6JtFIQi", "two-minutes-lap-HtRYEop", "footballoxing-A1ZUNXO",
             "mrw-i-see-breakfast-post-XYONfBA", "i-need-ppe-mdo3YXM", "tommy-brennan-mTIVi0w",
             "ornj-live-interesting-lives-y50MbhG"]
BIG_VIDEO = """(() => { const area = e => { const b = e.getBoundingClientRect(); return b.height > 100 ? b.width * b.height : 0 };
  const v = [...document.querySelectorAll('video')].sort((a, b) => area(b) - area(a))[0]; if (!v || !area(v)) return null;
  if (v.paused) v.play().catch(() => 0);
  return {paused: v.paused, t: Math.round(v.currentTime), w: v.videoWidth, h: v.videoHeight} })()"""
PAUSE_ALL = "document.querySelectorAll('video').forEach(v => v.pause())"
# Measured as the site behaves: Imgur's grid autoplays the visible video tiles (18 at once on the
# 3840x2160 window, about 10 columns, software decoding without a GPU) and carries about 20 ad
# iframes. Switching autoplay off (a script that paused every video, trial 2026-10-22) only took
# the start from 4.0 to 3.5 GB and broke the video posts, so it is not used.


def i01_startup(r, d):
    w, b, tid, p = prepare(r)
    r.mark("steady", playing=p.js("[...document.querySelectorAll('video')].filter(v => !v.paused).length"))
    time.sleep(30); r.shot("steady")


def i02_feed(r, d):
    w, b, tid, p = prepare(r)
    r.mark("feed")
    for _ in range(40):                                    # about 60 s of continuous scrolling
        bt.wheel(p, 1200); time.sleep(1.5)
    n = p.js("document.querySelectorAll('a[href*=\"/gallery/\"]').length"); r.shot("feed")
    r.mark("idle", posts=n); time.sleep(30)


def i03_album(r, d):
    w, b, tid, p = prepare(r)
    r.mark("album"); bt.navigate(p, IG_ALBUM); time.sleep(5)
    for _ in range(40):                                    # page through the 26 images
        bt.wheel(p, 900); time.sleep(1.5)
    n = p.js("[...document.images].filter(i => i.getBoundingClientRect().width > 300 && i.complete).length")
    r.shot("album"); r.mark("idle", images=n); time.sleep(30)


def i04_videos(r, d):
    w, b, tid, p = prepare(r)
    r.mark("videos"); played = []
    for v in IG_VIDEOS:                                    # Excel: 10 short videos one after another
        bt.navigate(p, f"https://imgur.com/gallery/{v}"); time.sleep(2); p.js(BIG_VIDEO); time.sleep(8)
        played.append(p.js(BIG_VIDEO))
    r.shot("videos"); p.js(PAUSE_ALL)
    r.mark("idle", played=played); time.sleep(30)


def i07_s10_background(r, d):
    w, b, tid, p = prepare(r)
    r.mark("idle_fg")
    for _ in range(10):
        bt.wheel(p, 1200); time.sleep(1.5)
    p.js(PAUSE_ALL); time.sleep(10)
    r.mark("background"); bt.hide(w); time.sleep(120)
    r.mark("restore"); bt.show(w); time.sleep(2)
    for _ in range(20):                                    # back to the foreground and scroll the feed
        bt.wheel(p, 1200); time.sleep(1.5)
    r.shot("restored"); p.js(PAUSE_ALL)
    r.mark("idle"); time.sleep(30)


RUNS = {"bilibili": [b01_startup, b02_play, b03_quality, b04_seek, b05_danmaku_fullscreen, b06_live,
                     b07_s10_background, b09_bg_play],
        "douyin": [d01_startup, d02_feed, d03_comments, d04_live, d07_s10_background, d09_bg_play],
        "imgur": [i01_startup, i02_feed, i03_album, i04_videos, i07_s10_background]}

if __name__ == "__main__":
    for f in RUNS[SITE]:                                   # report names: s01_startup, s02_play, ...
        f.__name__ = "s" + f.__name__[1:]
    run_trials(SITE, RUNS[SITE], bt.M, bt.evict_list(), bt.B["running"], argv=sys.argv[2:], before_each=bt.restore_profile)
