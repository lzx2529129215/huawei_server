#!/usr/bin/env python3
"""Write the Gnote test notebook: 1,000 notes (Tomboy/Gnote .note XML) plus one long note.

  notes/<uuid>.note   999 ordinary notes (5-60 lines, Chinese and English) + "长笔记 Long note"
                      (about 3,000 lines) used for editing and scrolling.
"""
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path.home() / "linux_app_mem/materials/gnote/notes"
random.seed(11)
WORDS = ("会议 纪要 待办 购物 读书 笔记 想法 计划 旅行 菜谱 电影 学习 工作 周末 项目 预算 健身 生日 "
         "meeting todo idea plan travel recipe book movie budget project weekend gym birthday notes").split()


def note(title, lines, when):
    ts = when.strftime("%Y-%m-%dT%H:%M:%S.0000000-04:00")
    body = escape(title + "\n\n" + "\n".join(lines))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<note version="0.3" xmlns:link="http://beatniksoftware.com/tomboy/link" xmlns:size="http://beatniksoftware.com/tomboy/size" xmlns="http://beatniksoftware.com/tomboy">
  <title>{escape(title)}</title>
  <text xml:space="preserve"><note-content version="0.1">{body}</note-content></text>
  <last-change-date>{ts}</last-change-date>
  <last-metadata-change-date>{ts}</last-metadata-change-date>
  <create-date>{ts}</create-date>
  <cursor-position>0</cursor-position>
  <selection-bound-position>-1</selection-bound-position>
  <width>800</width>
  <height>1000</height>
  <x>0</x>
  <y>0</y>
  <open-on-startup>False</open-on-startup>
</note>
"""


def line():
    return " ".join(random.choice(WORDS) for _ in range(random.randint(5, 16)))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.note"):
        f.unlink()
    t0 = datetime(2026, 1, 1)
    for k in range(999):
        title = f"{random.choice(WORDS)} {random.choice(WORDS)} {k:04d}"
        (OUT / f"{uuid.UUID(int=random.getrandbits(128))}.note").write_text(
            note(title, [line() for _ in range(random.randint(5, 60))], t0 + timedelta(hours=7 * k)), encoding="utf-8")
    (OUT / f"{uuid.UUID(int=random.getrandbits(128))}.note").write_text(
        note("长笔记 Long note", [f"{i:04d} " + line() for i in range(3000)], t0 + timedelta(days=300)), encoding="utf-8")
    print(OUT, len(list(OUT.glob("*.note"))), "notes")


if __name__ == "__main__":
    main()
