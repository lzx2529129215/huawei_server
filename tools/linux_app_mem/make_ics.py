#!/usr/bin/env python3
"""Write the calendar test data for GNOME Calendar (substitute for 日历).

  calendar.ics  the local "Personal" calendar: 10,000 events over two years (2025-11 .. 2027-10),
                plus a dense week (the week of 2026-10-19): 40 events per day
  import.ics    5,000 more events (2027) for the import operation
"""
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path.home() / "linux_app_mem/materials/calendar"
random.seed(5)
WORDS = ("会议 评审 周会 出差 培训 面试 午餐 健身 体检 生日 发布 上线 复盘 讨论 客户 拜访 "
         "meeting review standup trip training interview lunch gym checkup launch retro sync demo").split()


def ev(uid, start, minutes, summary, desc):
    f = "%Y%m%dT%H%M%S"
    end = start + timedelta(minutes=minutes)
    return ("BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20261001T000000Z\r\nCREATED:20261001T000000Z\r\nLAST-MODIFIED:20261001T000000Z\r\n"
            f"DTSTART;TZID=America/New_York:{start.strftime(f)}\r\nDTEND;TZID=America/New_York:{end.strftime(f)}\r\n"
            f"SUMMARY:{summary}\r\nDESCRIPTION:{desc}\r\nLOCATION:Room {random.randint(100, 999)}\r\n"
            "TRANSP:OPAQUE\r\nEND:VEVENT\r\n")


TZ = ("BEGIN:VTIMEZONE\r\nTZID:America/New_York\r\nBEGIN:DAYLIGHT\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\n"
      "TZNAME:EDT\r\nDTSTART:19700308T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\nEND:DAYLIGHT\r\n"
      "BEGIN:STANDARD\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\nTZNAME:EST\r\nDTSTART:19701101T020000\r\n"
      "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n")


def cal(events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//linux_app_mem//test//EN\r\n" + TZ + "".join(events)
            + "END:VCALENDAR\r\n")


def summary(k):
    return f"{random.choice(WORDS)} {random.choice(WORDS)} #{k}"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    events = []
    t0 = datetime(2025, 11, 1, 8, 0)
    for k in range(10000):
        day = t0 + timedelta(days=k * 730 // 10000)
        start = day.replace(hour=random.randint(7, 20), minute=random.choice((0, 15, 30, 45)))
        events.append(ev(f"cal-{k}@linux-app-mem", start, random.choice((15, 30, 45, 60, 90)), summary(k),
                         " ".join(random.choice(WORDS) for _ in range(12))))
    week = datetime(2026, 10, 19, 7, 0)
    for d in range(7):
        for j in range(40):
            start = week + timedelta(days=d, minutes=20 * j)
            events.append(ev(f"dense-{d}-{j}@linux-app-mem", start, 30, f"密集 dense {d}-{j}", "dense week"))
    (OUT / "calendar.ics").write_text(cal(events), encoding="utf-8")
    imp = []
    t1 = datetime(2027, 1, 1, 9, 0)
    for k in range(5000):
        start = (t1 + timedelta(days=k * 365 // 5000)).replace(hour=random.randint(7, 20))
        imp.append(ev(f"imp-{k}@linux-app-mem", start, 60, "导入 import " + summary(k), "imported"))
    (OUT / "import.ics").write_text(cal(imp), encoding="utf-8")
    print(OUT, len(events), "events +", len(imp), "to import")


if __name__ == "__main__":
    main()
