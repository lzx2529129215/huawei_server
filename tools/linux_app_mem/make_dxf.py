#!/usr/bin/env python3
"""Write the LibreCAD test drawings (plain DXF R12, readable by LibreCAD's libdxfrw).

A "block plan": a grid of buildings; each building has 16 wall lines, 4 window lines, 4 circles
(columns), a closed polyline (outline) and a text label, on separate layers.
  small.dxf    5 x 4 buildings   (~530 entities)
  large.dxf  100 x 60 buildings  (~156,000 entities)
"""
from pathlib import Path

OUT = Path.home() / "linux_app_mem/materials/librecad"


def building(e, bx, by, n):
    w, h = 40.0, 30.0
    x0, y0 = bx * 60.0, by * 50.0
    # outer walls (two lines per side, with a door gap) and inner walls
    for (a, b) in [((0, 0), (18, 0)), ((22, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0)),
                   ((0, 12), (25, 12)), ((25, 0), (25, 12)), ((25, 18), (w, 18)), ((15, 12), (15, h)),
                   ((30, 18), (30, h)), ((0.5, 0.5), (17.5, 0.5)), ((22.5, 0.5), (w - 0.5, 0.5)),
                   ((w - 0.5, 0.5), (w - 0.5, h - 0.5)), ((w - 0.5, h - 0.5), (0.5, h - 0.5)),
                   ((0.5, h - 0.5), (0.5, 0.5)), ((18, 0), (22, 4))]:
        e.append(("LINE", "WALL", (x0 + a[0], y0 + a[1]), (x0 + b[0], y0 + b[1])))
    for i in range(4):                                     # windows
        e.append(("LINE", "WINDOW", (x0 + 5 + i * 9, y0 + h), (x0 + 9 + i * 9, y0 + h)))
    for (cx, cy) in [(5, 5), (35, 5), (5, 25), (35, 25)]:  # columns
        e.append(("CIRCLE", "COLUMN", (x0 + cx, y0 + cy), 1.2))
    e.append(("POLYLINE", "OUTLINE", [(x0 - 2, y0 - 2), (x0 + w + 2, y0 - 2), (x0 + w + 2, y0 + h + 2), (x0 - 2, y0 + h + 2)]))
    e.append(("TEXT", "LABEL", (x0 + 3, y0 + 14), f"B-{n:05d} 办公/OFFICE"))


def dxf(entities):
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    for ent in entities:
        kind, layer = ent[0], ent[1]
        if kind == "LINE":
            (x1, y1), (x2, y2) = ent[2], ent[3]
            out += ["0", "LINE", "8", layer, "10", f"{x1:.3f}", "20", f"{y1:.3f}", "30", "0.0",
                    "11", f"{x2:.3f}", "21", f"{y2:.3f}", "31", "0.0"]
        elif kind == "CIRCLE":
            (x, y), r = ent[2], ent[3]
            out += ["0", "CIRCLE", "8", layer, "10", f"{x:.3f}", "20", f"{y:.3f}", "30", "0.0", "40", f"{r:.3f}"]
        elif kind == "POLYLINE":
            out += ["0", "POLYLINE", "8", layer, "66", "1", "70", "1"]
            for (x, y) in ent[2]:
                out += ["0", "VERTEX", "8", layer, "10", f"{x:.3f}", "20", f"{y:.3f}", "30", "0.0"]
            out += ["0", "SEQEND", "8", layer]
        elif kind == "TEXT":
            (x, y), s = ent[2], ent[3]
            out += ["0", "TEXT", "8", layer, "10", f"{x:.3f}", "20", f"{y:.3f}", "30", "0.0", "40", "2.5", "1", s]
    out += ["0", "ENDSEC", "0", "EOF"]
    return "\n".join(out) + "\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, nx, ny in (("small.dxf", 5, 4), ("large.dxf", 100, 60)):
        e = []
        for by in range(ny):
            for bx in range(nx):
                building(e, bx, by, by * nx + bx)
        (OUT / name).write_text(dxf(e), encoding="utf-8")
        print(OUT / name, len(e), "entities")


if __name__ == "__main__":
    main()
