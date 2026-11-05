"""Tests for the resizable three column layout and the prefs store.

The geometry rules are pure arithmetic, so most tests never touch Tk.  The
widget level checks run in a child interpreter on purpose: creating and
destroying a Tk root inside the test process leaves a stale Tcl async handler
that later kills the threaded ``test_net`` cases.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import panes
import prefs

HERE = Path(__file__).resolve().parents[1]

# Runs with a private Tk root, verifies layout, collapse, drag clamp and that
# the state survives a fresh root, then prints "ok".
CHILD = r"""
import sys, tkinter as tk
import panes, prefs

path = sys.argv[1]

class Event:
    pass

def build():
    root = tk.Tk()
    root.geometry("1200x700")
    widget = panes.ThreePane(
        root, key="t", prefs=prefs.Prefs(path),
        defaults={"left": 300, "right": 320},
        mins={"left": 200, "center": 380, "right": 250},
        titles={"left": "房间", "right": "聊天"})
    widget.pack(fill="both", expand=True)
    root.update_idletasks()
    widget._relayout()
    root.update_idletasks()
    return root, widget

root, w = build()
assert w.left.winfo_ismapped() and w.right.winfo_ismapped()
assert w.left.winfo_width() >= 200, w.left.winfo_width()
assert w.center.winfo_width() >= 380, w.center.winfo_width()

w.collapse("right")
root.update_idletasks()
assert w.is_collapsed("right")
assert not w.right.winfo_ismapped()
assert w._strips["right"].winfo_ismapped()
assert w.center.winfo_width() >= 380

w.expand("right")
root.update_idletasks()
assert w.right.winfo_ismapped()

sash = w._sashes["left"]
press = Event(); press.widget = sash; press.x_root = 0
w._sash_press(press)
drag = Event(); drag.widget = sash; drag.x_root = 5000
w._sash_drag(drag)
release = Event(); release.widget = sash
w._sash_release(release)
root.update_idletasks()
assert w.center.winfo_width() >= 380, w.center.winfo_width()

w.collapse("right")
root.destroy()

root, again = build()
assert again.is_collapsed("right"), "collapsed state was not restored"
root.destroy()

# vertical split: clamp, drag, persistence
def vsplit():
    top = tk.Tk()
    top.geometry("420x600")
    split = panes.VSplit(top, key="v", prefs=prefs.Prefs(path),
                         default_top=200, min_top=80, min_bottom=170)
    split.pack(fill="both", expand=True)
    top.update_idletasks()
    split._relayout()
    top.update_idletasks()
    return top, split

r2, sp = vsplit()
assert sp.top.winfo_height() >= 80, sp.top.winfo_height()
assert sp.bottom.winfo_height() >= 170, sp.bottom.winfo_height()
press = Event(); press.y_root = 0
sp._press(press)
drag = Event(); drag.y_root = 4000
sp._drag_move(drag)
sp._release(Event())
r2.update_idletasks()
assert sp.bottom.winfo_height() >= 170, sp.bottom.winfo_height()
moved = sp.top_height()
r2.destroy()

r3, sp2 = vsplit()
assert sp2.top_height() == moved, (sp2.top_height(), moved)
r3.destroy()
print("ok")
"""


class ComputeLayoutTest(unittest.TestCase):
    def test_three_columns_fill_the_width(self) -> None:
        rects = panes.compute_layout(1200, 300, 320, sash=8)
        self.assertEqual(rects["left"], (0, 300))
        self.assertEqual(rects["sash_left"], (300, 8))
        self.assertEqual(rects["center"], (308, 1200 - 308 - 328))
        self.assertEqual(rects["sash_right"], (872, 8))
        self.assertEqual(rects["right"], (880, 320))
        self.assertEqual(rects["center"][0] + rects["center"][1],
                         rects["sash_right"][0])

    def test_center_absorbs_extra_width(self) -> None:
        narrow = panes.compute_layout(1100, 300, 320)
        wide = panes.compute_layout(1500, 300, 320)
        self.assertEqual(narrow["left"][1], wide["left"][1])
        self.assertEqual(narrow["right"][1], wide["right"][1])
        self.assertGreater(wide["center"][1], narrow["center"][1])
        self.assertEqual(wide["center"][1] - narrow["center"][1], 400)

    def test_shrinks_sides_before_starving_the_center(self) -> None:
        rects = panes.compute_layout(900, 400, 400, min_center=400,
                                     min_left=250, min_right=300)
        self.assertEqual(rects["right"][1], 300)
        self.assertEqual(rects["left"][1], 250)
        self.assertEqual(rects["center"][1], 900 - (250 + 8 + 300 + 8))

    def test_collapsed_right_keeps_a_strip(self) -> None:
        rects = panes.compute_layout(1200, 300, 320, collapsed_right=True)
        self.assertNotIn("right", rects)
        self.assertNotIn("sash_right", rects)
        self.assertEqual(rects["right_strip"], (1200 - panes.STRIP, panes.STRIP))
        self.assertEqual(rects["center"][0] + rects["center"][1],
                         1200 - panes.STRIP)

    def test_collapsed_left_keeps_a_strip(self) -> None:
        rects = panes.compute_layout(1200, 300, 320, collapsed_left=True)
        self.assertNotIn("left", rects)
        self.assertNotIn("sash_left", rects)
        self.assertEqual(rects["left_strip"], (0, panes.STRIP))
        self.assertEqual(rects["center"][0], panes.STRIP)

    def test_max_side_width_leaves_room_for_the_center(self) -> None:
        limit = panes.max_side_width(1200, other=320, other_collapsed=False,
                                     min_center=380)
        self.assertEqual(limit, 1200 - (320 + 8) - 8 - 380)
        collapsed = panes.max_side_width(1200, other=320, other_collapsed=True,
                                         min_center=380)
        self.assertEqual(collapsed, 1200 - panes.STRIP - 8 - 380)


class ComputeVLayoutTest(unittest.TestCase):
    def test_two_sections_fill_the_height(self) -> None:
        rects = panes.compute_vlayout(600, 180, sash=6)
        self.assertEqual(rects["top"], (0, 180))
        self.assertEqual(rects["sash"], (180, 6))
        self.assertEqual(rects["bottom"], (186, 600 - 186))

    def test_bottom_keeps_its_minimum_when_short(self) -> None:
        rects = panes.compute_vlayout(300, 400, min_top=80, min_bottom=170,
                                      sash=6)
        self.assertEqual(rects["top"][1] + rects["sash"][1]
                         + rects["bottom"][1], 300)
        self.assertEqual(rects["bottom"][1], 170)

    def test_tiny_container_gives_way_to_the_bottom(self) -> None:
        rects = panes.compute_vlayout(120, 400, min_top=80, min_bottom=170,
                                      sash=6)
        self.assertEqual(rects["top"][1], 0)
        self.assertEqual(rects["bottom"][0] + rects["bottom"][1], 120)

    def test_top_never_goes_below_its_minimum(self) -> None:
        rects = panes.compute_vlayout(800, 10, min_top=80, min_bottom=170,
                                      sash=6)
        self.assertEqual(rects["top"][1], 80)


class PrefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "prefs.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        store = prefs.Prefs(self.path)
        store.set("lang", "zh")
        store.set("layout.game.left", 240)
        store.set("layout.game.right_collapsed", True)
        self.assertTrue(store.save())
        again = prefs.Prefs(self.path)
        self.assertEqual(again.get("lang"), "zh")
        self.assertEqual(again.get_int("layout.game.left", 0), 240)
        self.assertTrue(again.get_bool("layout.game.right_collapsed"))

    def test_missing_file_is_empty(self) -> None:
        store = prefs.Prefs(self.path)
        self.assertEqual(len(store), 0)
        self.assertEqual(store.get_int("nope", 7), 7)
        self.assertIsNone(store.error)

    def test_corrupt_file_does_not_raise(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = prefs.Prefs(self.path)
        self.assertEqual(len(store), 0)
        self.assertIn("parse", store.error or "")

    def test_non_object_root_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps([1, 2]), encoding="utf-8")
        store = prefs.Prefs(self.path)
        self.assertEqual(len(store), 0)

    def test_bad_numeric_value_falls_back(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"x": "wide"}), encoding="utf-8")
        store = prefs.Prefs(self.path)
        self.assertEqual(store.get_int("x", 12), 12)
        self.assertFalse(store.get_bool("x", False))


class ThreePaneWidgetTest(unittest.TestCase):
    def test_widget_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "prefs.json"
            env = dict(os.environ, UNO_PREFS=str(store))
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", CHILD, str(store)],
                    cwd=str(HERE), env=env, capture_output=True, text=True,
                    timeout=120)
            except subprocess.TimeoutExpired:
                self.fail("three pane smoke test timed out")
        if proc.returncode != 0 and "no display" in (proc.stderr or ""):
            self.skipTest("no display available")
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
