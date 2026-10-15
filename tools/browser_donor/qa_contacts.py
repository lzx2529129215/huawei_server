#!/usr/bin/env python3
"""Build local-only three-page browser QA contact sheets from captured screenshots.

This utility reads existing PNG captures, the static UI window layout, and the
browser workload manifest. It has no device or UI-control code and never edits
its input files. A sheet is written only for a window with all three captures.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


CAPTURE_RE = re.compile(r"(?:^|_)w(?P<window>[1-6])p(?P<page>[1-3])_navigated\.png$")
EXPECTED_WINDOWS = 6
PAGES_PER_WINDOW = 3
CARD_WIDTH = 1320
HEADER_HEIGHT = 48
GAP = 18
CROP_HEIGHT = 510


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _workload_pages(path: Path) -> dict[tuple[int, int], str]:
    manifest = _load_json(path)
    if (manifest.get("schema") != "browser-donor-workload-v1" or
            manifest.get("window_count") != EXPECTED_WINDOWS or
            manifest.get("pages_per_window") != PAGES_PER_WINDOW):
        raise ValueError("workload manifest is not the expected six-window/three-page browser workload")
    pages: dict[tuple[int, int], str] = {}
    for window_index, window in enumerate(manifest.get("windows", []), start=1):
        if window.get("label") != f"w{window_index}":
            raise ValueError(f"unexpected workload window order at index {window_index}")
        for page_index, page in enumerate(window.get("pages", []), start=1):
            if page.get("label") != f"w{window_index}p{page_index}":
                raise ValueError(f"unexpected workload page order in w{window_index}")
            uri = page.get("ui_uri")
            if not isinstance(uri, str) or not uri.startswith("/"):
                raise ValueError(f"workload page has no absolute UI URI: w{window_index}p{page_index}")
            pages[(window_index, page_index)] = "file://" + uri
    expected = {(w, p) for w in range(1, EXPECTED_WINDOWS + 1)
                for p in range(1, PAGES_PER_WINDOW + 1)}
    if set(pages) != expected:
        raise ValueError("workload manifest does not contain exactly 18 ordered pages")
    return pages


def _captures(directory: Path) -> dict[tuple[int, int], Path]:
    found: dict[tuple[int, int], Path] = {}
    for path in sorted(directory.glob("*_navigated.png")):
        match = CAPTURE_RE.search(path.name)
        if not match:
            continue
        slot = (int(match.group("window")), int(match.group("page")))
        if slot in found:
            raise ValueError(f"duplicate navigation capture for w{slot[0]}p{slot[1]}")
        found[slot] = path
    return found


def _crop_capture(path: Path, rect: list[int], expected_screen: tuple[int, int]) -> Image.Image:
    with Image.open(path) as source:
        source.load()
        if source.size != expected_screen:
            raise ValueError(f"{path.name} is {source.size}, expected full-screen {expected_screen}")
        x, y, width, height = (int(value) for value in rect)
        if min(x, y) < 0 or width <= 0 or height < CROP_HEIGHT:
            raise ValueError(f"invalid browser window crop rect for {path.name}: {rect}")
        box = (x, y, x + width, y + CROP_HEIGHT)
        if box[2] > source.width or box[3] > source.height:
            raise ValueError(f"window crop exceeds screenshot bounds: {path.name}")
        crop = source.crop(box).convert("RGB")
    scaled_height = round(crop.height * CARD_WIDTH / crop.width)
    return crop.resize((CARD_WIDTH, scaled_height), Image.Resampling.LANCZOS)


def build_contact_sheets(screenshots: Path, layout_path: Path,
                         workload_path: Path, output: Path) -> tuple[list[Path], list[str]]:
    layout = _load_json(layout_path)
    if layout.get("schema") != "browser-donor-ui-layout-v1":
        raise ValueError("unknown UI layout schema")
    windows = layout.get("windows")
    if not isinstance(windows, list) or len(windows) != EXPECTED_WINDOWS:
        raise ValueError("UI layout must contain six browser windows")
    for index, row in enumerate(windows):
        if row.get("name") != f"browser{index}" or not isinstance(row.get("rect"), list):
            raise ValueError(f"UI layout window index {index} is not bound to browser{index}")
    screen = layout.get("screen_size")
    if not isinstance(screen, list) or len(screen) != 2:
        raise ValueError("UI layout has no full-screen dimensions")
    expected_screen = (int(screen[0]), int(screen[1]))
    expected_uris = _workload_pages(workload_path)
    captures = _captures(screenshots)
    output.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    pending: list[str] = []
    font = _font(16)
    for window_index in range(1, EXPECTED_WINDOWS + 1):
        slots = [(window_index, page_index) for page_index in range(1, PAGES_PER_WINDOW + 1)]
        missing = [f"w{w}p{p}" for w, p in slots if (w, p) not in captures]
        if missing:
            pending.extend(missing)
            continue

        crops = [_crop_capture(captures[slot], windows[window_index - 1]["rect"], expected_screen)
                 for slot in slots]
        card_height = HEADER_HEIGHT + crops[0].height
        canvas = Image.new("RGB", (CARD_WIDTH * 3 + GAP * 4, card_height + GAP * 2), "white")
        draw = ImageDraw.Draw(canvas)
        for page_index, (slot, crop) in enumerate(zip(slots, crops)):
            left = GAP + page_index * (CARD_WIDTH + GAP)
            uri = expected_uris[slot]
            draw.text((left, GAP), f"w{slot[0]}p{slot[1]}  expected: {uri}", fill="black", font=font)
            canvas.paste(crop, (left, GAP + HEADER_HEIGHT))
        destination = output / f"w{window_index}_p1-p3.png"
        canvas.save(destination, format="PNG", optimize=True)
        generated.append(destination)
    return generated, pending


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, required=True,
                        help="directory containing full-screen *_wNpM_navigated.png captures")
    parser.add_argument("--layout", type=Path, required=True,
                        help="static browser window layout JSON")
    parser.add_argument("--workload", type=Path, required=True,
                        help="browser workload manifest with expected UI URIs")
    parser.add_argument("--output", type=Path, required=True,
                        help="new QA output directory; only derived PNGs are written here")
    args = parser.parse_args()
    generated, pending = build_contact_sheets(args.screenshots, args.layout,
                                               args.workload, args.output)
    print(f"generated {len(generated)} contact sheet(s)")
    for path in generated:
        print(path.resolve())
    if pending:
        print("pending captures: " + ", ".join(pending))
    return 0 if generated else 2


if __name__ == "__main__":
    raise SystemExit(main())
