#!/usr/bin/env python3
"""Generate fixed test documents for the WPS memory measurement.

Run with the venv that has python-docx / python-pptx / Pillow:
  ~/linux_app_mem/venv/bin/python make_materials.py ~/linux_app_mem/materials/wps
Every file comes from a fixed seed, so a rerun gives the same content.
A manifest.json with sizes and SHA-256 is written next to the files.
"""
import hashlib
import io
import json
import random
import sys
from pathlib import Path

from docx import Document
from docx.shared import Cm
from PIL import Image, ImageFilter
from pptx import Presentation
from pptx.util import Inches, Pt

# Common Chinese characters used to build filler text.
CHARS = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所"
         "民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处理府研质")


def text(rng, n_chars):
    out, line = [], 0
    for _ in range(n_chars):
        out.append(rng.choice(CHARS))
        line += 1
        if line > rng.randint(20, 60):
            out.append("，" if rng.random() < 0.7 else "。")
            line = 0
    return "".join(out) + "。"


def photo(rng, w, h, noise):
    """A photo-like JPEG: smooth colour field plus grain, so it compresses like a real photo."""
    small = Image.new("RGB", (16, 12))
    small.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(16 * 12)])
    img = small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(4))
    grain = Image.merge("RGB", [Image.effect_noise((w, h), noise) for _ in range(3)])
    img = Image.blend(img, grain, 0.25)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def make_docx(path, seed, paragraphs, chars_per_par, images, img_size, noise):
    rng = random.Random(seed)
    doc = Document()
    doc.add_heading(f"测试文档 {path.stem}", 0)
    every = max(1, paragraphs // max(1, images))
    placed = 0
    for i in range(paragraphs):
        if i % 25 == 0:
            doc.add_heading(f"第 {i // 25 + 1} 节", 1)
        doc.add_paragraph(text(rng, chars_per_par))
        if placed < images and i % every == every - 1:
            doc.add_picture(io.BytesIO(photo(rng, *img_size, noise)), width=Cm(15))
            placed += 1
    doc.save(path)


def make_pptx(path, seed, slides, img_size, noise):
    rng = random.Random(seed)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for i in range(slides):
        s = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout
        s.shapes.add_picture(io.BytesIO(photo(rng, *img_size, noise)), Inches(0.5), Inches(0.5), height=Inches(5.6))
        tb = s.shapes.add_textbox(Inches(0.5), Inches(6.2), Inches(12), Inches(1)).text_frame
        tb.text = f"第 {i + 1} 页  " + text(rng, 40)
        tb.paragraphs[0].runs[0].font.size = Pt(20)
        # Fade transition on every slide (object animations are not generated).
        from lxml import etree
        tr = etree.SubElement(s._element, "{http://schemas.openxmlformats.org/presentationml/2006/main}transition")
        etree.SubElement(tr, "{http://schemas.openxmlformats.org/presentationml/2006/main}fade")
    prs.save(path)


def main():
    out = Path(sys.argv[1]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    # normal: about 20 pages with a few pictures; 4 variants for multi-document switching.
    for k in range(5):
        make_docx(out / f"normal{k + 1}.docx", 100 + k, paragraphs=60, chars_per_par=250, images=5,
                  img_size=(1600, 1200), noise=40)
    # big: about 50k+ characters of text and 40 high-resolution pictures (target ~100 MB, like evidence E13).
    make_docx(out / "big.docx", 200, paragraphs=400, chars_per_par=300, images=40, img_size=(4000, 3000), noise=25)
    # deck: 40 slides, one high-resolution picture each, fade transitions.
    make_pptx(out / "deck.pptx", 300, slides=40, img_size=(4000, 2250), noise=60)
    manifest = {}
    for f in sorted(out.glob("*.*x")):
        manifest[f.name] = {"bytes": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    for k, v in manifest.items():
        print(f"{k:14s} {v['bytes'] / 2**20:8.1f} MiB  {v['sha256'][:16]}")


if __name__ == "__main__":
    main()
