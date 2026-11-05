"""Resizable / collapsible three column container.

``compute_layout`` is pure arithmetic (no Tk) so the geometry rules can be unit
tested; ``ThreePane`` wires those rules to a Tk frame with draggable sashes, a
``✕`` collapse button on each side pane and a clickable edge strip that brings a
collapsed pane back.  Both the lobby and the game screen use it.

``compute_vlayout`` / ``VSplit`` are the vertical sibling: two stacked sections
with a draggable divider (the game's log and chat share the right column).
"""
from __future__ import annotations

import tkinter as tk
from typing import Any, Callable

SASH = 8
STRIP = 24
VSPLIT = 6
SIDES = ("left", "right")


def _clamp(value: float, low: float, high: float) -> int:
    if high < low:
        high = low
    return int(round(max(low, min(high, value))))


def compute_layout(
    width: int,
    left: int,
    right: int,
    *,
    collapsed_left: bool = False,
    collapsed_right: bool = False,
    min_left: int = 160,
    min_center: int = 380,
    min_right: int = 240,
    sash: int = SASH,
    strip: int = STRIP,
) -> dict[str, tuple[int, int]]:
    """Return ``{name: (x, width)}`` for each visible piece of the row.

    Names: ``left``, ``sash_left``, ``center``, ``sash_right``, ``right`` and,
    for a collapsed side, ``left_strip`` / ``right_strip``.  The centre column
    absorbs the slack; when the window gets too narrow the side panes are
    squeezed down to their minimums first, then the centre keeps whatever is
    left (it never steals space from the sides below their minimums).
    """
    width = max(0, int(width))
    want_left = min_left if collapsed_left else max(min_left, int(left))
    want_right = min_right if collapsed_right else max(min_right, int(right))

    def reserved(lw: int, rw: int) -> int:
        total = 0
        total += strip if collapsed_left else lw + sash
        total += strip if collapsed_right else rw + sash
        return total

    center = width - reserved(want_left, want_right)
    deficit = min_center - center
    if deficit > 0 and not collapsed_right:
        cut = min(deficit, max(0, want_right - min_right))
        want_right -= cut
        deficit -= cut
    if deficit > 0 and not collapsed_left:
        want_left -= min(deficit, max(0, want_left - min_left))
    center = max(0, width - reserved(want_left, want_right))

    rects: dict[str, tuple[int, int]] = {}
    x = 0
    if collapsed_left:
        rects["left_strip"] = (0, strip)
        x = strip
    else:
        rects["left"] = (0, want_left)
        x = want_left
        rects["sash_left"] = (x, sash)
        x += sash
    rects["center"] = (x, center)
    x += center
    if collapsed_right:
        rects["right_strip"] = (min(x, max(0, width - strip)), strip)
    else:
        rects["sash_right"] = (x, sash)
        rects["right"] = (x + sash, want_right)
    return rects


def max_side_width(
    width: int,
    *,
    other: int,
    other_collapsed: bool,
    min_center: int = 380,
    sash: int = SASH,
    strip: int = STRIP,
) -> int:
    """Largest width the dragged side may take without starving the centre."""
    reserved = (strip if other_collapsed else int(other) + sash) + sash + min_center
    return max(0, int(width) - reserved)


def compute_vlayout(
    height: int,
    top: int,
    *,
    min_top: int = 70,
    min_bottom: int = 150,
    sash: int = VSPLIT,
) -> dict[str, tuple[int, int]]:
    """Return ``{name: (y, height)}`` for ``top``, ``sash`` and ``bottom``.

    The divider sits at ``top`` and the remaining space goes to ``bottom``; the
    two sections are clamped so neither ever disappears unless the container
    itself is too short to hold both minimums (then ``bottom`` gives way first).
    """
    height = max(0, int(height))
    sash_h = min(max(0, int(sash)), height)
    room = height - sash_h
    low = min(min_top, max(0, room - min_bottom))
    high = max(low, room - min_bottom)
    top = _clamp(top, low, high)
    return {
        "top": (0, top),
        "sash": (top, sash_h),
        "bottom": (top + sash_h, max(0, room - top)),
    }


class ThreePane(tk.Frame):
    """Three columns with draggable sashes, collapsible sides and persistence.

    The caller owns the pane contents (``.left``, ``.center``, ``.right``).
    Sizes/collapsed flags are stored in ``prefs`` under ``layout.<key>.*``.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        key: str,
        prefs: Any = None,
        defaults: dict[str, int] | None = None,
        mins: dict[str, int] | None = None,
        titles: dict[str, str] | None = None,
        bg: str = "#0d1a21",
        strip_bg: str = "#1c3a46",
        strip_hover: str = "#25505f",
        fg: str = "#93aaa6",
        accent: str = "#3ddc97",
        font: Callable[[int], tuple] | None = None,
        sash: int = SASH,
        strip: int = STRIP,
    ) -> None:
        super().__init__(master, bg=bg)
        self._key = key
        self._prefs = prefs
        self._defaults = {"left": 300, "right": 320}
        self._defaults.update(defaults or {})
        self._mins = {"left": 160, "center": 380, "right": 240}
        self._mins.update(mins or {})
        self._titles = {"left": "", "right": ""}
        self._titles.update(titles or {})
        self._bg = bg
        self._strip_bg = strip_bg
        self._strip_hover = strip_hover
        self._fg = fg
        self._accent = accent
        self._font = font or (lambda size: ("TkDefaultFont", size))
        self._sash_w = sash
        self._strip_w = strip
        self._drag: dict[str, Any] | None = None

        self._width = {"left": int(self._defaults["left"]),
                       "right": int(self._defaults["right"])}
        self._collapsed = {"left": False, "right": False}
        self._load()

        self.left = tk.Frame(self, bg=bg)
        self.center = tk.Frame(self, bg=bg)
        self.right = tk.Frame(self, bg=bg)
        self._sashes = {side: self._make_sash(side) for side in SIDES}
        self._strips = {side: self._make_strip(side) for side in SIDES}
        self._closers = {side: self._make_closer(side) for side in SIDES}
        for side in SIDES:
            self._sync_title(side)

        self.bind("<Configure>", lambda _e: self._relayout())

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self._prefs is None:
            return
        for side in SIDES:
            stored = self._prefs.get_int(f"layout.{self._key}.{side}",
                                         self._width[side])
            self._width[side] = max(self._mins[side], int(stored))
            self._collapsed[side] = self._prefs.get_bool(
                f"layout.{self._key}.{side}_collapsed", False)

    def save(self) -> None:
        if self._prefs is None:
            return
        for side in SIDES:
            self._prefs.set(f"layout.{self._key}.{side}", self._width[side])
            self._prefs.set(f"layout.{self._key}.{side}_collapsed",
                            self._collapsed[side])
        self._prefs.save()

    # ---------------------------------------------------------------- widgets
    def _make_sash(self, side: str) -> tk.Canvas:
        canvas = tk.Canvas(self, width=self._sash_w, bg=self._bg,
                           highlightthickness=0, bd=0, cursor="sb_h_double_arrow")
        canvas.pane_side = side  # type: ignore[attr-defined]
        canvas.hot = False  # type: ignore[attr-defined]
        canvas.bind("<Configure>", lambda _e, c=canvas: self._draw_sash(c))
        canvas.bind("<Enter>", lambda _e, c=canvas: self._sash_hot(c, True))
        canvas.bind("<Leave>", lambda _e, c=canvas: self._sash_hot(c, False))
        canvas.bind("<ButtonPress-1>", self._sash_press)
        canvas.bind("<B1-Motion>", self._sash_drag)
        canvas.bind("<ButtonRelease-1>", self._sash_release)
        canvas.bind("<Double-Button-1>", self._sash_reset)
        return canvas

    def _make_strip(self, side: str) -> tk.Frame:
        frame = tk.Frame(self, bg=self._strip_bg, cursor="hand2")
        arrow = tk.Label(frame, text="▶" if side == "left" else "◀",
                         bg=self._strip_bg, fg=self._accent,
                         font=self._font(9), bd=0, padx=0, pady=0)
        arrow.pack(fill="x", pady=(10, 4))
        label = tk.Label(frame, text="", bg=self._strip_bg, fg=self._fg,
                         font=self._font(9), bd=0, justify="center")
        label.pack(fill="both", expand=True, pady=(0, 10))
        frame._arrow = arrow  # type: ignore[attr-defined]
        frame._label = label  # type: ignore[attr-defined]
        for widget in (frame, arrow, label):
            widget.bind("<Button-1>", lambda _e, s=side: self.expand(s))
            widget.bind("<Enter>", lambda _e, f=frame: self._strip_hot(f, True))
            widget.bind("<Leave>", lambda _e, f=frame: self._strip_hot(f, False))
        return frame

    def _make_closer(self, side: str) -> tk.Label:
        # Child of the pane frame so the ✕ sits in that pane's top-right
        # corner, not at the far right of the whole container.
        label = tk.Label(getattr(self, side), text="✕", bg=self._strip_bg,
                         fg=self._fg, font=self._font(10), bd=0, padx=4,
                         pady=0, cursor="hand2")
        label.bind("<Button-1>", lambda _e, s=side: self.collapse(s))
        label.bind("<Enter>", lambda _e, w=label: w.configure(
            bg=self._strip_hover, fg="#ff6b6b"))
        label.bind("<Leave>", lambda _e, w=label: w.configure(
            bg=self._strip_bg, fg=self._fg))
        return label

    def _strip_hot(self, frame: tk.Frame, hot: bool) -> None:
        color = self._strip_hover if hot else self._strip_bg
        frame.configure(bg=color)
        for child in frame.winfo_children():
            child.configure(bg=color)

    def _sash_hot(self, canvas: tk.Canvas, hot: bool) -> None:
        canvas.hot = hot  # type: ignore[attr-defined]
        self._draw_sash(canvas)

    def _draw_sash(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        width = canvas.winfo_width() or self._sash_w
        height = canvas.winfo_height() or 1
        hot = bool(getattr(canvas, "hot", False))
        color = self._accent if hot else "#2b4a57"
        x = width / 2
        canvas.create_line(x, 6, x, max(7, height - 6), fill=color, width=2)
        if height > 60:
            middle = height / 2
            for offset in (-16, 0, 16):
                canvas.create_oval(x - 1.5, middle + offset - 1.5,
                                   x + 1.5, middle + offset + 1.5,
                                   fill=color, outline="")

    def _sync_title(self, side: str) -> None:
        title = str(self._titles.get(side) or "")
        if len(title) > 6:
            title = title[:5] + "…"
        self._strips[side]._label.configure(text="\n".join(title))  # type: ignore[attr-defined]

    def set_titles(self, **titles: str) -> None:
        for side in SIDES:
            if side in titles:
                self._titles[side] = titles[side]
                self._sync_title(side)

    # -------------------------------------------------------------- geometry
    def _relayout(self) -> None:
        width = self.winfo_width()
        if width <= 1:
            return
        height = self.winfo_height()
        rects = compute_layout(
            width, self._width["left"], self._width["right"],
            collapsed_left=self._collapsed["left"],
            collapsed_right=self._collapsed["right"],
            min_left=self._mins["left"], min_center=self._mins["center"],
            min_right=self._mins["right"], sash=self._sash_w,
            strip=self._strip_w)
        for name, widget in (("left", self.left), ("center", self.center),
                             ("right", self.right)):
            rect = rects.get(name)
            if rect is None:
                widget.place_forget()
            else:
                widget.place(x=rect[0], y=0, width=rect[1], height=height)
        for side in SIDES:
            canvas = self._sashes[side]
            rect = rects.get(f"sash_{side}")
            if rect is None:
                canvas.place_forget()
            else:
                canvas.place(x=rect[0], y=0, width=rect[1], height=height)
            strip = self._strips[side]
            strip_rect = rects.get(f"{side}_strip")
            if strip_rect is None:
                strip.place_forget()
            else:
                strip.place(x=strip_rect[0], y=0, width=strip_rect[1],
                            height=height)
            closer = self._closers[side]
            if self._collapsed[side]:
                closer.place_forget()
            else:
                closer.place(relx=1.0, x=-4, y=4, anchor="ne")
                closer.lift()

    # ---------------------------------------------------------------- actions
    def collapse(self, side: str, collapsed: bool = True) -> None:
        if side not in SIDES or self._collapsed[side] == collapsed:
            return
        self._collapsed[side] = bool(collapsed)
        self._relayout()
        self.save()

    def expand(self, side: str) -> None:
        self.collapse(side, False)

    def toggle(self, side: str) -> None:
        self.collapse(side, not self._collapsed[side])

    def reset(self) -> None:
        for side in SIDES:
            self._width[side] = int(self._defaults[side])
            self._collapsed[side] = False
        self._relayout()
        self.save()

    def is_collapsed(self, side: str) -> bool:
        return bool(self._collapsed.get(side))

    # ------------------------------------------------------------------ drag
    def _sash_press(self, event: "tk.Event") -> None:
        side = getattr(event.widget, "pane_side", "")
        if side in SIDES:
            self._drag = {"side": side, "x": event.x_root,
                          "width": self._width[side]}

    def _sash_drag(self, event: "tk.Event") -> None:
        if not self._drag:
            return
        side = self._drag["side"]
        delta = event.x_root - self._drag["x"]
        wanted = self._drag["width"] + (delta if side == "left" else -delta)
        other = "right" if side == "left" else "left"
        ceiling = max_side_width(
            self.winfo_width(), other=self._width[other],
            other_collapsed=self._collapsed[other],
            min_center=self._mins["center"], sash=self._sash_w,
            strip=self._strip_w)
        self._width[side] = _clamp(wanted, self._mins[side],
                                   max(self._mins[side], ceiling))
        self._relayout()

    def _sash_release(self, _event: "tk.Event") -> None:
        if self._drag:
            self._drag = None
            self.save()

    def _sash_reset(self, _event: "tk.Event") -> None:
        self.reset()


class VSplit(tk.Frame):
    """Two stacked sections with a draggable divider.

    ``.top`` / ``.bottom`` are plain frames the caller fills.  The divider
    offset is kept in ``prefs`` under ``layout.<key>.top`` (pixels) and restored
    on the next run; double-clicking the divider goes back to the default.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        key: str,
        prefs: Any = None,
        default_top: int = 180,
        min_top: int = 70,
        min_bottom: int = 150,
        bg: str = "#0d1a21",
        accent: str = "#3ddc97",
        sash: int = VSPLIT,
        anchor: str = "top",
        default_bottom: int = 340,
    ) -> None:
        super().__init__(master, bg=bg)
        self._key = key
        self._prefs = prefs
        self._anchor = anchor if anchor in ("top", "bottom") else "top"
        self._default_bottom = int(default_bottom)
        self._bot_h = int(default_bottom)
        self._default_top = int(default_top)
        self._min_top = int(min_top)
        self._min_bottom = int(min_bottom)
        self._bg = bg
        self._accent = accent
        self._sash_h = int(sash)
        self._drag: dict[str, Any] | None = None
        self._top_h = self._load()
        if self._anchor == "bottom" and self._prefs is not None:
            self._bot_h = max(self._min_bottom, int(self._prefs.get_int(
                f"layout.{self._key}.bottom", self._default_bottom)))

        self.top = tk.Frame(self, bg=bg)
        self.bottom = tk.Frame(self, bg=bg)
        self.sash = self._make_sash()
        self.bind("<Configure>", lambda _e: self._relayout())

    # ------------------------------------------------------------ persistence
    def _load(self) -> int:
        if self._prefs is None:
            return self._default_top
        stored = self._prefs.get_int(f"layout.{self._key}.top",
                                     self._default_top)
        return max(self._min_top, int(stored))

    def save(self) -> None:
        if self._prefs is None:
            return
        if self._anchor == "bottom":
            self._prefs.set(f"layout.{self._key}.bottom", int(self._bot_h))
        else:
            self._prefs.set(f"layout.{self._key}.top", int(self._top_h))
        self._prefs.save()

    def reset(self) -> None:
        self._top_h = self._default_top
        self._bot_h = self._default_bottom
        self._relayout()
        self.save()

    # ---------------------------------------------------------------- widgets
    def _make_sash(self) -> tk.Canvas:
        canvas = tk.Canvas(self, bg=self._bg, height=self._sash_h,
                           highlightthickness=0, bd=0,
                           cursor="sb_v_double_arrow")
        canvas.hot = False  # type: ignore[attr-defined]
        canvas.bind("<Configure>", lambda _e, c=canvas: self._draw_sash(c))
        canvas.bind("<Enter>", lambda _e, c=canvas: self._sash_hot(c, True))
        canvas.bind("<Leave>", lambda _e, c=canvas: self._sash_hot(c, False))
        canvas.bind("<ButtonPress-1>", self._press)
        canvas.bind("<B1-Motion>", self._drag_move)
        canvas.bind("<ButtonRelease-1>", self._release)
        canvas.bind("<Double-Button-1>", lambda _e: self.reset())
        return canvas

    def _sash_hot(self, canvas: tk.Canvas, hot: bool) -> None:
        canvas.hot = hot  # type: ignore[attr-defined]
        self._draw_sash(canvas)

    def _draw_sash(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        width = canvas.winfo_width() or 1
        height = canvas.winfo_height() or self._sash_h
        color = self._accent if bool(getattr(canvas, "hot", False)) else "#2b4a57"
        y = height / 2
        canvas.create_line(6, y, max(7, width - 6), y, fill=color, width=2)
        if width > 80:
            middle = width / 2
            for offset in (-16, 0, 16):
                canvas.create_oval(middle + offset - 1.5, y - 1.5,
                                   middle + offset + 1.5, y + 1.5,
                                   fill=color, outline="")

    # -------------------------------------------------------------- geometry
    def _relayout(self) -> None:
        height = self.winfo_height()
        if height <= 1:
            return
        width = self.winfo_width()
        wanted = (height - self._sash_h - self._bot_h) if self._anchor == "bottom" else self._top_h
        rects = compute_vlayout(height, wanted, min_top=self._min_top,
                                min_bottom=self._min_bottom, sash=self._sash_h)
        self.top.place(x=0, y=rects["top"][0], width=width,
                       height=rects["top"][1])
        self.sash.place(x=0, y=rects["sash"][0], width=width,
                        height=rects["sash"][1])
        self.bottom.place(x=0, y=rects["bottom"][0], width=width,
                          height=rects["bottom"][1])

    def top_height(self) -> int:
        return int(self._top_h)

    # ------------------------------------------------------------------ drag
    def _press(self, event: "tk.Event") -> None:
        self._drag = {"y": event.y_root, "top": self.top.winfo_height() or self._top_h}

    def _drag_move(self, event: "tk.Event") -> None:
        if not self._drag:
            return
        wanted = self._drag["top"] + (event.y_root - self._drag["y"])
        rects = compute_vlayout(self.winfo_height() or 1, wanted,
                                min_top=self._min_top,
                                min_bottom=self._min_bottom,
                                sash=self._sash_h)
        self._top_h = rects["top"][1]
        if self._anchor == "bottom":
            self._bot_h = rects["bottom"][1]
        self._relayout()

    def _release(self, _event: "tk.Event") -> None:
        if self._drag:
            self._drag = None
            self.save()
