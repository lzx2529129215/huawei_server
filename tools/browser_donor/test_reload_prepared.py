from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reload_prepared import (BROWSER_TASKBAR, EXPECTED_WINDOW_RECTS, SCREEN_CAP_PREFIX,
                             SCREEN_HEIGHT, SCREEN_WIDTH,
                             WINDOW_UI, parse_wms_table,
                             png_dimensions, validate_window_state)


def raw_detail(i: int, visible: bool) -> str:
    x, y, width, height = EXPECTED_WINDOW_RECTS[i]
    return ("----------------------------------WindowManagerService----------------------------------\n"
            f"WindowName: browser{i}\nDisplayId: 0\nWinId: {1068+i}\nPid: 18676\n"
            f"IsVisible: {'true' if visible else 'false'}\n"
            f"WindowRect: [ {x}, {y}, {width}, {height} ]\n")


def raw_table() -> str:
    return ("WindowName DisplayId Pid WinId\n" +
            "".join(f"browser{i} 0 18676 {1068+i}\n" for i in range(6)))


class ReloadPreparedTests(unittest.TestCase):
    def test_wms_parser_and_preflight_all_hidden(self) -> None:
        self.assertEqual(len(parse_wms_table(raw_table())), 6)
        state = validate_window_state(raw_table(),
            {f"browser{i}": raw_detail(i, False) for i in range(6)},
            owner_pid=18676, expected_visibility={i: False for i in range(6)})
        self.assertEqual(state["visible_indices"], [])
        self.assertEqual(state["windows"]["browser5"]["win_id"], 1073)

    def test_wms_selected_window_visibility_and_rect_are_checked(self) -> None:
        state = validate_window_state(raw_table(),
            {f"browser{i}": raw_detail(i, i == 2) for i in range(6)},
            owner_pid=18676, expected_visibility={i: i == 2 for i in range(6)})
        self.assertEqual(state["visible_indices"], [2])
        wrong = raw_detail(2, True).replace("648, 414", "649, 414")
        details = {f"browser{i}": raw_detail(i, i == 2) for i in range(6)}
        details["browser2"] = wrong
        with self.assertRaisesRegex(ValueError, "WindowRect mismatch"):
            validate_window_state(raw_table(), details, 18676,
                                  {i: i == 2 for i in range(6)})

    def test_owner_window_id_and_visible_set_must_match(self) -> None:
        details = {f"browser{i}": raw_detail(i, i == 1) for i in range(6)}
        with self.assertRaisesRegex(ValueError, "owner PID mismatch"):
            validate_window_state(raw_table(), details, 18677,
                                  {i: i == 1 for i in range(6)})
        with self.assertRaisesRegex(ValueError, "visibility mismatch"):
            validate_window_state(raw_table(), details, 18676,
                                  {i: i == 0 for i in range(6)})

    def test_png_ihdr_dimension_parser(self) -> None:
        fake = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + \
               SCREEN_WIDTH.to_bytes(4, "big") + SCREEN_HEIGHT.to_bytes(4, "big")
        self.assertEqual(png_dimensions(fake), (3120, 2080))
        with self.assertRaisesRegex(ValueError, "not a complete PNG"):
            png_dimensions(b"not png")

    def test_screenshot_uses_harmony_uitest_command(self) -> None:
        self.assertEqual(SCREEN_CAP_PREFIX, "uitest screenCap -p")

    def test_all_ui_coordinates_fit_calibrated_display(self) -> None:
        points = [BROWSER_TASKBAR, *[(x, 1771) for x in (407, 840, 1280, 1720, 2160, 2610)]]
        for row in WINDOW_UI:
            points.extend(row["tabs"])
            points.extend((row["reload"], row["minimize"]))
        self.assertTrue(all(0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT
                            for x, y in points))


if __name__ == "__main__":
    unittest.main()
