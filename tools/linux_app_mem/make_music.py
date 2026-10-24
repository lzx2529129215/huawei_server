#!/usr/bin/env python3
"""Write the music test library for Rhythmbox (substitute for 华为音乐).

  library/      5,000 short MP3s (10 s, 128 kbit/s, different tones), tagged
                artist / album / title / track; 250 albums of 20 tracks, 50 artists
  covers/       the first 10 albums carry an embedded 3000x3000 JPEG cover (high-res cover art)
  play/         the 20 tracks of album 1 (30 s each) for playback / track switching
"""
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path.home() / "linux_app_mem/materials/music"
LIB = OUT / "library"
N_ALBUMS, PER_ALBUM = 250, 20


def cover(i):
    p = OUT / f"cover_{i}.jpg"
    if not p.exists():
        subprocess.run(["convert", "-size", "3000x3000", "-seed", str(100 + i), "plasma:fractal",
                        "-quality", "90", str(p)], check=True)
    return p


def track(a, t, seconds=10, dest=None):
    artist = f"艺术家 {a % 50:02d}"
    album = f"专辑 {a:03d}"
    title = f"第 {t + 1} 首 Track {t + 1}"
    out = dest or (LIB / f"{a % 50:02d}" / f"album_{a:03d}" / f"{t + 1:02d}.mp3")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return
    freq = 200 + (a * 7 + t * 13) % 800
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}"]
    if a < 10:
        cmd += ["-i", str(cover(a)), "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "128k", "-id3v2_version", "3",
            "-metadata", f"artist={artist}", "-metadata", f"album={album}", "-metadata", f"title={title}",
            "-metadata", f"track={t + 1}/{PER_ALBUM}", "-metadata", "genre=Test", str(out)]
    subprocess.run(cmd, check=True)


def main():
    LIB.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        cover(i)
    jobs = [(a, t) for a in range(N_ALBUMS) for t in range(PER_ALBUM)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda j: track(*j), jobs))
    play = OUT / "play"
    play.mkdir(parents=True, exist_ok=True)
    for t in range(PER_ALBUM):
        track(1, t, seconds=30, dest=play / f"{t + 1:02d}.mp3")
    n = len(list(LIB.rglob("*.mp3")))
    print(LIB, n, "tracks;", play, len(list(play.glob("*.mp3"))), "tracks")


if __name__ == "__main__":
    main()
