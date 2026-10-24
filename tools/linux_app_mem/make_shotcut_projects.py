#!/usr/bin/env python3
"""Write the Shotcut test projects (MLT XML in Shotcut 24.04's own layout).

The layout was copied from projects that Shotcut itself saved (one track; three tracks with
"Size, Position & Rotate", Gaussian blur and Glow filters). Media stay in MEDIA (not copied
per run); the small .mlt files go next to it and are copied into each run's work dir.

  p_single.mlt  1080p30, V1 = clip_a (120 s)                      S02 edit, S07/S10
  p_multi.mlt   1080p30, V1 = clip_a, V2/V3 = clip_b/clip_c as     S03 multi-track preview,
                picture-in-picture (half size, top-right / bottom-left)   S04 scrubbing
  p_effect.mlt  1080p30, V1 = clip_a with Gaussian blur + Glow     S05 effects
  p_4k.mlt      2160p30, V1 = clip_4k (60 s)                       S06/S09 4K export
  p_proxy.mlt   2160p30, V1..V4 = clip_4k + the three 1080p clips  S08 background proxy generation
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path.home() / "linux_app_mem/materials/shotcut"
MEDIA = OUT / "media"
FPS = 30


def tc(frames):
    s = frames / FPS
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def profile(w, h):
    name = "HD 1080p 30 fps" if h == 1080 else "UHD 2160p 30 fps"
    return (f'  <profile description="{name}" width="{w}" height="{h}" progressive="1" sample_aspect_num="1" '
            f'sample_aspect_den="1" display_aspect_num="16" display_aspect_den="9" frame_rate_num="{FPS}" '
            f'frame_rate_den="1" colorspace="709"/>')


def props(d, indent="    "):
    return "\n".join(f'{indent}<property name="{k}">{escape(str(v))}</property>' for k, v in d.items())


def chain(cid, path, frames, filters=()):
    body = props({"length": tc(frames), "eof": "pause", "resource": str(path), "mlt_service": "avformat-novalidate",
                  "seekable": 1, "audio_index": 1, "video_index": 0, "shotcut:caption": path.name})
    fl = ""
    for i, f in enumerate(filters):
        fl += f'\n    <filter id="{cid}_f{i}" out="{tc(frames - 1)}">\n{props(f, "      ")}\n    </filter>'
    return f'  <chain id="{cid}" out="{tc(frames - 1)}">\n{body}{fl}\n  </chain>'


def affine(rect):
    return {"background": "color:#00000000", "mlt_service": "affine", "shotcut:filter": "affineSizePosition",
            "transition.fill": 1, "transition.distort": 0, "transition.rect": rect, "transition.valign": "middle",
            "transition.halign": "center", "shotcut:animIn": "00:00:00.000", "shotcut:animOut": "00:00:00.000",
            "transition.threads": 0}


GBLUR = {"mlt_service": "avfilter.gblur", "shotcut:filter": "blur_gaussian_av", "av.sigma": 4, "av.sigmaV": 4,
         "av.planes": "0xf"}
GLOW = {"version": "0.1", "mlt_service": "frei0r.glow", "0": 0.5}


def project(w, h, tracks, frames):
    """tracks: list of (media file, filters[, frames]) from V1 upwards; frames = the longest track."""
    last = tc(frames - 1)
    parts = [f'<?xml version="1.0" standalone="no"?>',
             f'<mlt LC_NUMERIC="C" version="7.22.0" title="Shotcut version 24.04.01" producer="main_bin">',
             profile(w, h),
             '  <playlist id="main_bin">\n    <property name="xml_retain">1</property>\n  </playlist>',
             f'  <producer id="black" in="00:00:00.000" out="{last}">\n' + props(
                 {"length": tc(frames), "eof": "pause", "resource": 0, "aspect_ratio": 1, "mlt_service": "color",
                  "mlt_image_format": "rgba", "set.test_audio": 0}) + '\n  </producer>',
             f'  <playlist id="background">\n    <entry producer="black" in="00:00:00.000" out="{last}"/>\n  </playlist>']
    for i, (media, filters, *n) in enumerate(tracks):
        n = n[0] if n else frames
        parts.append(chain(f"chain{i}", MEDIA / media, n, filters))
        parts.append(f'  <playlist id="playlist{i}">\n' + props({"shotcut:video": 1, "shotcut:name": f"V{i + 1}"})
                     + f'\n    <entry producer="chain{i}" in="00:00:00.000" out="{tc(n - 1)}"/>\n  </playlist>')
    tr = [f'  <tractor id="tractor0" title="Shotcut version 24.04.01" in="00:00:00.000" out="{last}">',
          props({"shotcut": 1, "shotcut:projectAudioChannels": 2, "shotcut:projectFolder": 0}),
          '    <track producer="background"/>']
    tr += [f'    <track producer="playlist{i}"/>' for i in range(len(tracks))]
    n = 0
    for i in range(len(tracks)):
        b = i + 1
        tr.append(f'    <transition id="transition{n}">\n' + props(
            {"a_track": 0, "b_track": b, "mlt_service": "mix", "always_active": 1, "sum": 1}, "      ") + "\n    </transition>")
        tr.append(f'    <transition id="transition{n + 1}">\n' + props(
            {"a_track": 0 if b == 1 else 1, "b_track": b, "version": "0.1", "mlt_service": "frei0r.cairoblend",
             "threads": 0, "disable": 1 if b == 1 else 0}, "      ") + "\n    </transition>")
        n += 2
    tr.append("  </tractor>")
    parts.append("\n".join(tr))
    parts.append("</mlt>\n")
    return "\n".join(parts)


def main():
    m = 120 * FPS
    files = {
        "p_single.mlt": project(1920, 1080, [("clip_a_1080p.mp4", ())], m),
        "p_multi.mlt": project(1920, 1080, [("clip_a_1080p.mp4", ()),
                                            ("clip_b_1080p.mp4", (affine("960 0 960 540 1"),)),
                                            ("clip_c_1080p.mp4", (affine("0 540 960 540 1"),))], m),
        "p_effect.mlt": project(1920, 1080, [("clip_a_1080p.mp4", (GBLUR, GLOW))], m),
        "p_4k.mlt": project(3840, 2160, [("clip_4k.mp4", ())], 60 * FPS),
        "p_proxy.mlt": project(3840, 2160, [("clip_4k.mp4", (), 60 * FPS), ("clip_a_1080p.mp4", ()),
                                            ("clip_b_1080p.mp4", ()), ("clip_c_1080p.mp4", ())], m),
    }
    for name, xml in files.items():
        (OUT / name).write_text(xml)
        print(OUT / name)


if __name__ == "__main__":
    main()
