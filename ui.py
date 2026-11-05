#!/usr/bin/env python3
"""Tkinter UI for the UNO LAN game.

One UI drives two transports that share the same tiny client surface
(``pid``, ``welcome``, ``send``, ``poll``, ``alive``, ``close``):
``net.NetClient`` for LAN rooms.  There is no AI/offline mode: every seat
is a real person on the network.
single-machine game.  All drawing is done on canvases so the cards can be the
pre-rendered SVG sprites from ``cards/``.
"""
from __future__ import annotations

import math
import os
import queue
import random
import sys
import threading
import time
import base64
import struct
import tkinter as tk
import tkinter.font as tkfont
import zlib
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable

import avatars
import cards as card_assets
import faulthandler
import lanip
import qrcode_min
import webserver
import i18n
import panes
import prefs
from engine import Rules, card_value, is_wild

BG = "#0d1a21"
BG2 = "#10232c"
PANEL = "#152b35"
PANEL2 = "#1c3a46"
FELT = "#125340"
FELT2 = "#0e4033"
OV_BG = "#17333c"
INK = "#eef7f4"
MUTED = "#93aaa6"
ACCENT = "#ffc93c"
ACCENT2 = "#3ddc97"
DANGER = "#ff6b6b"
BLUE = "#59a5ff"
PURPLE = "#b98bff"
GOLD = "#ffd166"
COLOR_HEX = {"R": "#e84a5f", "Y": "#f2a900", "G": "#2ecc71", "B": "#3b82f6"}
SEAT_COLORS = ["#f97316", "#a78bfa", "#f472b6", "#38bdf8", "#facc15",
               "#4ade80", "#fb7185", "#22d3ee", "#c084fc", "#fbbf24"]


def ui_font(size: int = 13, bold: bool = False) -> tuple[str, int, str]:
    return ("PingFang TC", size, "bold" if bold else "normal")


def mono_font(size: int = 12) -> tuple[str, int]:
    return ("Menlo", size)


# emoji reactions: the same palette as net.REACTIONS (the host rejects anything else)
REACTIONS = ("👍", "😂", "😮", "😡", "😭", "🎉", "🤔", "👏")
SPECIAL_REACTIONS = ("👑", "🔥", "😎", "🏆")      # only for whoever leads the match score
REACT_SECONDS = 2.6


def emoji_font(size: int = 24) -> tuple[str, int]:
    if sys.platform == "darwin":
        return ("Apple Color Emoji", size)
    if os.name == "nt":
        return ("Segoe UI Emoji", size)
    return ("Noto Color Emoji", size)


def rounded(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
            r: float = 16, **kw: Any) -> int:
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    # Tk polygons default to systemTextColor, which paints cards white; a
    # canvas path with no explicit fill must stay transparent.
    kw.setdefault("fill", "")
    return canvas.create_polygon(pts, smooth=True, splinesteps=20, **kw)


class FlatButton(tk.Canvas):
    """A rounded canvas pill button (macOS-safe) with a tiny hover animation."""

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None] | None,
                 bg: str = ACCENT, fg: str = "#1b1205", hover: str | None = None,
                 font: tuple | None = None, padx: int = 18, pady: int = 9,
                 width: int | None = None, radius: int | None = None,
                 keycap: str | None = None, **pack: Any) -> None:
        self._font = font or ui_font(13, True)
        self._measure = tkfont.Font(font=self._font)
        self._text = text
        self._keycap = keycap
        self._cap_font = ui_font(9, True)
        self._tw = self._measure.measure(text)
        self._th = self._measure.metrics("linespace")
        self._fixed_width = width is not None
        cap_w = (tkfont.Font(font=self._cap_font).measure(keycap) + 10 + 8) if keycap else 0
        w = width or (self._tw + cap_w + padx * 2)
        h = self._th + pady * 2
        super().__init__(parent, width=w, height=h, bg=_parent_bg(parent),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bg = bg
        self._fg = fg
        self._hover = hover or _lighten(bg)
        self._cmd = command
        self._command = command  # legacy alias used by a few call sites
        self._enabled = True
        self._active = False
        self._fill = bg
        self._radius = radius
        self._padx, self._pady = padx, pady
        self._packinfo = pack
        self._hover_job: Any = None
        self.bind("<Configure>", lambda _e=None: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()
        if pack:
            self.pack(**pack)

    # --------------------------------------------------------------- drawing
    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        w = max(8, self.winfo_width())
        h = max(8, self.winfo_height())
        self.delete("all")
        pbg = str(self.cget("bg"))
        # disabled = the same colour, faded towards the background (like the web's opacity)
        fill = self._fill if self._enabled else _mix(pbg, self._bg, 0.34)
        r = self._radius if self._radius is not None else h / 2
        outline = ACCENT if self._active and self._enabled else ""
        rounded(self, 1, 1, w - 1, h - 1, r=min(r, (h - 2) / 2), fill=fill,
                outline=outline, width=2 if outline else 0)
        fg = self._fg if self._enabled else _mix(pbg, self._fg, 0.55)
        if self._keycap:
            cap_w = max(16, tkfont.Font(font=self._cap_font).measure(self._keycap) + 10)
            total = self._tw + 8 + cap_w
            x0 = (w - total) / 2
            self.create_text(x0, h / 2, text=self._text, fill=fg, font=self._font, anchor="w")
            cx = x0 + self._tw + 8
            soft = _mix(fill, fg, 0.55)
            rounded(self, cx, h / 2 - 8, cx + cap_w, h / 2 + 8, r=4, fill="", outline=soft, width=1)
            self.create_text(cx + cap_w / 2, h / 2, text=self._keycap, fill=soft, font=self._cap_font)
        else:
            self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)
        self._fill_w = w

    def _fade_to(self, target: str) -> None:
        if self._hover_job is not None:
            try:
                self.after_cancel(self._hover_job)
            except tk.TclError:
                pass
        start = self._fill
        app = self.winfo_toplevel()
        anim = getattr(app, "animate", None)
        if anim is None or not self.winfo_exists():
            self._fill = target
            self._draw()
            return
        self._hover_job = None

        def step(t: float) -> None:
            if not self.winfo_exists():
                raise tk.TclError("gone")
            self._fill = _mix(start, target, t)
            self._draw()

        anim(0.14, step)

    # ---------------------------------------------------------------- events
    def _on_click(self, _e: Any) -> None:
        if self._enabled and self._cmd:
            self._cmd()

    def _on_enter(self, _e: Any) -> None:
        if self._enabled:
            self._fade_to(self._hover)

    def _on_leave(self, _e: Any) -> None:
        if self._enabled:
            self._fade_to(self._bg)

    # ----------------------------------------------------------------- state
    def set_enabled(self, ok: bool) -> None:
        self._enabled = bool(ok)
        try:
            self.configure(cursor="hand2" if ok else "arrow")
        except tk.TclError:
            return
        self._fill = self._bg
        self._draw()

    def set_active(self, on: bool) -> None:
        self._active = bool(on)
        self._bg = "#2b4a57" if on else PANEL
        self._fg = ACCENT if on else INK
        self._hover = _lighten(self._bg)
        self._fill = self._bg
        self._draw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._tw = self._measure.measure(text)
        if not self._fixed_width:
            w = self._tw + self._padx * 2
            try:
                self.configure(width=w)
            except tk.TclError:
                return
        self._draw()

    def set_visible(self, ok: bool) -> None:
        if ok:
            if self._packinfo:
                self.pack(**self._packinfo)
            else:
                self.pack()
        else:
            self.pack_forget()


def rounded_entry(parent: tk.Misc, textvariable: tk.StringVar | None = None, *,
                  font: tuple, width: int = 20, inner: str = PANEL2, outer: str | None = None,
                  radius: int = 12, **entry_kw: Any) -> tuple[tk.Canvas, tk.Entry]:
    """A text field with rounded corners. Returns (container to pack, the Entry)."""
    outer = outer or _parent_bg(parent)
    box = tk.Canvas(parent, bg=outer, highlightthickness=0, bd=0, width=60)
    entry = tk.Entry(box, textvariable=textvariable, width=width, font=font, bg=inner, fg=INK,
                     insertbackground=INK, relief="flat", bd=0, highlightthickness=0, **entry_kw)
    box.configure(height=entry.winfo_reqheight() + 14)
    win = box.create_window(14, 20, window=entry, anchor="w", width=60)
    state = {"focus": False}

    def layout(_e: Any = None) -> None:
        w, h = max(60, box.winfo_width()), max(24, box.winfo_height())
        box.delete("bg")
        rounded(box, 1, 1, w - 1, h - 1, r=min(radius, (h - 2) / 2), fill=inner,
                outline=ACCENT if state["focus"] else "#2b4a57", width=2 if state["focus"] else 1,
                tags=("bg",))
        box.tag_lower("bg")
        box.itemconfigure(win, width=max(20, w - 28))
        box.coords(win, 14, h / 2)

    def focus(on: bool) -> None:
        state["focus"] = on
        layout()

    box.bind("<Configure>", layout)
    entry.bind("<FocusIn>", lambda _e=None: focus(True), add="+")
    entry.bind("<FocusOut>", lambda _e=None: focus(False), add="+")
    return box, entry


def _lighten(hex_color: str, amount: float = 0.16) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r = min(255, int(r + (255 - r) * amount))
    g = min(255, int(g + (255 - g) * amount))
    b = min(255, int(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(c2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{int(round(x + (y - x) * t)):02x}" for x, y in zip(a, b))


def _parent_bg(widget: tk.Misc | None) -> str:
    while widget is not None:
        try:
            return str(widget.cget("bg"))
        except tk.TclError:
            widget = getattr(widget, "master", None)
    return BG


def ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


class Switch(tk.Canvas):
    """Rounded on/off switch with a sliding knob."""

    def __init__(self, parent: tk.Misc, value: bool,
                 command: Callable[[bool], None] | None = None,
                 width: int = 54, height: int = 28, **pack: Any) -> None:
        super().__init__(parent, width=width, height=height, bg=_parent_bg(parent),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._value = bool(value)
        self._cmd = command
        self._pos = 1.0 if self._value else 0.0
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", lambda _e=None: self._draw())
        self._draw()
        if pack:
            self.pack(**pack)

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        w = max(24, self.winfo_width())
        h = max(18, self.winfo_height())
        self.delete("all")
        pad = 3
        knob = h - pad * 2
        track = ACCENT2 if self._value else "#2b4a57"
        rounded(self, 0, 0, w, h, r=h / 2, fill=track, outline="")
        x = pad + (w - pad * 2 - knob) * self._pos
        self.create_oval(x, pad, x + knob, pad + knob,
                         fill="#f4fffa" if self._value else "#8fa3a0", outline="")

    def _toggle(self, _e: Any = None) -> None:
        self._value = not self._value
        target = 1.0 if self._value else 0.0
        start = self._pos

        def step(i: int = 0) -> None:
            if not self.winfo_exists():
                return
            t = min(1.0, i / 7)
            self._pos = start + (target - start) * ease_out(t)
            self._draw()
            if i < 7:
                self.after(14, lambda: step(i + 1))

        step()
        if self._cmd:
            self._cmd(self._value)

    def set_value(self, value: bool) -> None:
        self._value = bool(value)
        self._pos = 1.0 if self._value else 0.0
        self._draw()


class Toast:
    """Compact rounded transient message that slides in under the header."""

    def __init__(self, app: "UnoApp") -> None:
        self.app = app
        self.holder = tk.Frame(app, bg=BG)
        self.canvas = tk.Canvas(self.holder, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.label = tk.Label(self.canvas, text="", bg=PANEL, fg=INK,
                              font=ui_font(12, True), justify="left",
                              wraplength=520, padx=16, pady=9)
        self._win = self.canvas.create_window(2, 2, anchor="nw", window=self.label)
        self._job: Any = None
        self._anim: Any = None

    def show(self, text: str, ms: int = 2600, danger: bool = False) -> None:
        if not text:
            return
        text = str(text)
        bg = "#3a1113" if danger else "#12331c"
        fg = "#ffd9d9" if danger else "#d8ffe4"
        width = max(240, min(420, self.app.winfo_width() - 120))
        self.label.configure(text=text, bg=bg, fg=fg, wraplength=width - 36)
        self.label.update_idletasks()
        w = min(width, self.label.winfo_reqwidth() + 4)
        h = self.label.winfo_reqheight() + 4
        self.label.configure(wraplength=w - 36)
        self.canvas.configure(width=w, height=h)
        self.label.update_idletasks()
        h = self.label.winfo_reqheight() + 4
        self.canvas.configure(height=h)
        self.canvas.delete("bg")
        rounded(self.canvas, 0, 0, w, h, r=14, fill=bg,
                outline=_lighten(bg, 0.22), width=1, tags="bg")
        self.canvas.tag_lower("bg")
        self.canvas.coords(self._win, 2, 2)
        self.holder.place(relx=1.0, x=-16, y=-h, anchor="ne")
        self.holder.lift()

        def slide(t: float) -> None:
            if self.holder.winfo_exists():
                self.holder.place_configure(y=int(-h + (h + 88) * t))

        self.app.animate(0.20, slide)
        if self._job:
            self.app.after_cancel(self._job)
        self._job = self.app.after(ms, self._hide)

    def _hide(self) -> None:
        h = self.canvas.winfo_height() or 40

        def slide(t: float) -> None:
            if self.holder.winfo_exists():
                self.holder.place_configure(y=int(88 - (88 + h) * t))

        def done() -> None:
            if self.holder.winfo_exists():
                self.holder.place_forget()

        self.app.animate(0.18, slide, done)


def rounded_panel(parent: tk.Misc, bg: str = PANEL, radius: int = 16, pad: int = 8,
                  grid: dict[str, Any] | None = None, stretch: bool = False,
                  **pack: Any) -> tuple[tk.Canvas, tk.Frame]:
    """A rounded canvas card with a normal Frame inside for content."""
    holder = tk.Canvas(parent, bg=_parent_bg(parent), highlightthickness=0, bd=0)
    inner = tk.Frame(holder, bg=bg)
    drawn: list[tuple[int, int]] = [(0, 0)]

    def redraw(_e: Any = None) -> None:
        if not holder.winfo_exists():
            return
        w = holder.winfo_width()
        h = holder.winfo_height()
        if w <= 1 or h <= 1:
            return
        if drawn[0] != (w, h):
            drawn[0] = (w, h)
        holder.delete("bg")
        rounded(holder, 0, 0, w, h, r=radius, fill=bg, outline="", tags="bg")
        holder.tag_lower("bg")
        # the last <Configure> can arrive before the geometry manager settles
        if (holder.winfo_width(), holder.winfo_height()) != (w, h):
            holder.after(16, redraw)

    holder.bind("<Configure>", redraw)

    if stretch:
        inner.place(x=pad, y=pad, relwidth=1.0, relheight=1.0,
                    width=-pad * 2, height=-pad * 2)
    else:
        def fit(_e: Any = None) -> None:
            if not holder.winfo_exists() or not inner.winfo_exists():
                return
            want = inner.winfo_reqheight() + pad * 2
            if want > 1 and int(holder.cget("height")) != want:
                holder.configure(height=want)
            want_w = inner.winfo_reqwidth() + pad * 2
            if want_w > 1 and int(holder.cget("width")) != want_w:
                holder.configure(width=want_w)
            redraw()

        inner.bind("<Configure>", fit)
        inner.place(x=pad, y=pad, relwidth=1.0, width=-pad * 2)
    if grid:
        holder.grid(**grid)
    elif pack:
        holder.pack(**pack)
    else:
        holder.pack()
    return holder, inner


def make_shade(app: Any, w: int, h: int, alpha: int = 120) -> tk.PhotoImage:
    """A rounded black overlay with alpha, drawn over a card to dim it (cached per size)."""
    cache = app.__dict__.setdefault("_shade_cache", {})
    img = cache.get((w, h, alpha))
    if img is not None:
        return img
    r = max(3.0, w * 0.09)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        py = y + 0.5
        cy = min(max(py, r), h - r)
        for x in range(w):
            px = x + 0.5
            cx = min(max(px, r), w - r)
            dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
            cover = max(0.0, min(1.0, r + 0.5 - dist))
            raw += bytes((0, 0, 0, int(alpha * cover)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    img = tk.PhotoImage(master=app, data=base64.b64encode(png).decode("ascii"))
    cache[(w, h, alpha)] = img
    return img


GAME_TEXT = {
    "zh": {"sort": "整理手牌", "menu_lang": "English", "menu_avatar": "我的頭像",
           "rename": "改名字", "ok": "確定"},
    "en": {"sort": "Sort hand", "menu_lang": "中文", "menu_avatar": "My avatar",
           "rename": "Change name", "ok": "OK"},
}
_COLOR_ORDER = {"R": 0, "Y": 1, "G": 2, "B": 3}
_VALUE_ORDER = {**{str(i): i for i in range(10)}, "skip": 10, "reverse": 11, "draw2": 12}


def gt(app: "UnoApp", key: str) -> str:
    return GAME_TEXT.get(app.lang, GAME_TEXT["en"]).get(key, key)


def ordered_hand(hand: list[str], sort: bool) -> list[str]:
    """The hand in display order (colour, then value; wilds last) when sorting is on."""
    if not sort:
        return list(hand)

    def key(card: str) -> tuple:
        if card in ("wild", "wild4"):
            return (1, 0, 0 if card == "wild" else 1)
        return (0, _COLOR_ORDER.get(card[:1], 9), _VALUE_ORDER.get(card[1:], 99))

    return sorted(hand, key=key)


class PillLabel(tk.Canvas):
    """A rounded 'label' (the turn banner). configure(text=, bg=, fg=) works like a Label."""

    def __init__(self, parent: tk.Misc, text: str = "", bg: str = PANEL2, fg: str = MUTED,
                 font: tuple | None = None, height: int = 38, radius: int = 10) -> None:
        super().__init__(parent, height=height, bg=_parent_bg(parent), highlightthickness=0, bd=0)
        self._pill = {"text": text, "bg": bg, "fg": fg}
        self._timer: tuple[str, float, str] | None = None     # (seconds text, fraction left, colour)
        self._timer_key: tuple | None = None
        self._pfont = font or ui_font(13, True)
        self._pradius = radius
        self.bind("<Configure>", lambda _e=None: self._redraw())

    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        mine = {k: kw.pop(k) for k in ("text", "bg", "fg") if k in kw}
        if mine:
            self._pill.update(mine)
            self._redraw()
        if kw or cnf:
            return super().configure(cnf, **kw)
        return None

    config = configure  # type: ignore[assignment]

    def set_timer(self, text: str | None, frac: float = 0.0, color: str = "") -> None:
        """Turn-timer readout inside the pill: seconds on the right and a draining bar underneath."""
        key = None if text is None else (text, round(max(0.0, min(1.0, frac)) * 60), color)
        if key == self._timer_key:
            return                                   # nothing visible changed: no redraw
        self._timer_key = key
        self._timer = None if text is None else (text, max(0.0, min(1.0, frac)), color)
        self._redraw()

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        w, h = max(20, self.winfo_width()), max(20, self.winfo_height())
        self.delete("all")
        rounded(self, 0, 0, w, h, r=self._pradius, fill=self._pill["bg"], outline="")
        pad = 56 if self._timer else 16
        self.create_text(w / 2 - (20 if self._timer else 0), h / 2 - (2 if self._timer else 0),
                         text=self._pill["text"], fill=self._pill["fg"],
                         font=self._pfont, width=max(40, w - pad))
        if self._timer:
            text, frac, color = self._timer
            self.create_text(w - 16, h / 2 - 2, text=text, anchor="e", fill=color,
                             font=ui_font(15, True))
            x0, x1, y = 14, w - 14, h - 6
            self.create_rectangle(x0, y, x1, y + 3, fill=_mix(self._pill["bg"], "#000000", 0.35), outline="")
            if frac > 0:
                self.create_rectangle(x0, y, x0 + (x1 - x0) * frac, y + 3, fill=color, outline="")


SEC_TEXT = {
    "zh": {
        "title": "房間安全",
        "password": "房間密碼",
        "password_hint": "留空 = 不用密碼",
        "approval": "有人加入要我同意",
        "locked": "🔒 有密碼",
        "ask_pw": "這個房間有密碼",
        "enter_pw": "輸入房間密碼",
        "join": "加入",
        "wants_join": "{name} 想加入",
        "wants_watch": "{name} 想旁觀",
        "allow": "同意",
        "deny": "拒絕",
        "waiting": "等房主同意…",
    },
    "en": {
        "title": "Room security",
        "password": "Room password",
        "password_hint": "empty = no password",
        "approval": "Ask me before anyone joins",
        "locked": "🔒 password",
        "ask_pw": "This room has a password",
        "enter_pw": "Enter the room password",
        "join": "Join",
        "wants_join": "{name} wants to join",
        "wants_watch": "{name} wants to watch",
        "allow": "Allow",
        "deny": "Deny",
        "waiting": "Waiting for the host to let you in…",
    },
}


def sx(app: "UnoApp", key: str, **kw: Any) -> str:
    text = SEC_TEXT.get(app.lang, SEC_TEXT["en"]).get(key, key)
    return text.format(**kw) if kw else text


class PasswordDialog(tk.Toplevel):
    """Asks for a room's password, then retries the join."""

    def __init__(self, app: "UnoApp", host: str, port: int, spectate: bool, wrong: bool = False) -> None:
        super().__init__(app)
        self.app, self.host, self.port, self.spectate = app, host, port, spectate
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app)
        self.title(sx(app, "ask_pw"))
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=26, pady=22)
        tk.Label(body, text="🔒 " + sx(app, "ask_pw"), bg=PANEL, fg=INK, font=ui_font(16, True)).pack(pady=(0, 4))
        tk.Label(body, text=i18n.error_text("bad_password", app.lang) if wrong else sx(app, "enter_pw"),
                 bg=PANEL, fg=DANGER if wrong else MUTED, font=ui_font(11)).pack(pady=(0, 10))
        self.var = tk.StringVar()
        box, self.entry = rounded_entry(body, self.var, font=ui_font(14), outer=PANEL, width=18, show="•")
        box.pack(fill="x")
        self.entry.bind("<Return>", lambda _e=None: self._ok())
        self.entry.bind("<Escape>", lambda _e=None: self.destroy())
        row = tk.Frame(body, bg=PANEL)
        row.pack(pady=(14, 0))
        FlatButton(row, sx(app, "join"), self._ok, bg=ACCENT2, fg="#02160c", padx=22, pady=8, radius=12,
                   font=ui_font(13, True)).pack(side="left", padx=4)
        FlatButton(row, i18n.t("cancel", app.lang), self.destroy, bg=PANEL2, fg=INK, padx=18, pady=8,
                   radius=12, font=ui_font(13, True)).pack(side="left", padx=4)
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        self.after(60, self._focus)

    def _focus(self) -> None:
        try:
            self.grab_set()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _ok(self) -> None:
        pw = self.var.get()
        self.destroy()
        self.app.join_room(self.host, self.port, self.spectate, password=pw)


class RenameDialog(tk.Toplevel):
    """A small window to change the player's name."""

    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app)
        self.app = app
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app)
        self.title(gt(app, "rename"))
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=26, pady=22)
        tk.Label(body, text=gt(app, "rename"), bg=PANEL, fg=INK, font=ui_font(16, True)).pack(pady=(0, 12))
        self.var = tk.StringVar(value=app.player_name)
        box, self.entry = rounded_entry(body, self.var, font=ui_font(14), outer=PANEL, width=18)
        box.pack(fill="x")
        self.entry.bind("<Return>", lambda _e=None: self._ok())
        self.entry.bind("<Escape>", lambda _e=None: self.destroy())
        row = tk.Frame(body, bg=PANEL)
        row.pack(pady=(14, 0))
        FlatButton(row, gt(app, "ok"), self._ok, bg=ACCENT2, fg="#02160c", padx=20, pady=8,
                   radius=12, font=ui_font(13, True)).pack(side="left", padx=4)
        FlatButton(row, i18n.t("cancel", app.lang), self.destroy, bg=PANEL2, fg=INK, padx=18, pady=8,
                   radius=12, font=ui_font(13, True)).pack(side="left", padx=4)
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        self.after(60, self._focus)

    def _focus(self) -> None:
        try:
            self.grab_set()
            self.entry.focus_set()
            self.entry.select_range(0, "end")
        except tk.TclError:
            pass

    def _ok(self) -> None:
        name = self.var.get()
        self.destroy()
        self.app.rename_me(name)


class GameMenuDialog(tk.Toplevel):
    """The '...' menu: language, avatar, phone/web address, leave."""

    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app)
        self.app = app
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app)
        self.title("UNO")
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=26, pady=22)

        def add(text: str, cmd: Callable[[], None], bg: str, fg: str) -> None:
            FlatButton(body, text, cmd, bg=bg, fg=fg, width=240, padx=18, pady=10, radius=14,
                       font=ui_font(13, True)).pack(pady=5)

        add(gt(app, "menu_lang"), lambda: self._do(app.toggle_lang), PANEL2, INK)
        add("✎  " + gt(app, "rename"), lambda: self._do(lambda: RenameDialog(app)), PANEL2, INK)
        add("◉  " + gt(app, "menu_avatar"), lambda: self._do(lambda: AvatarDialog(app)), PANEL2, INK)
        if app.web_server is not None:
            add("📱  " + wt(app, "button"), lambda: self._do(app.show_web_info), PANEL2, INK)
        add(i18n.t("game_btn_leave", app.lang), lambda: self._do(app.leave_game), DANGER, "#250707")
        add(i18n.t("close", app.lang), self.destroy, PANEL, MUTED)
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        self.after(60, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _do(self, fn: Callable[[], None]) -> None:
        self.destroy()
        fn()


AV_TEXT = {
    "zh": {
        "title": "我的頭像",
        "choose": "選擇照片…",
        "remove": "移除照片",
        "done": "完成",
        "hint": "照片會自動裁成圓形，同桌的玩家都看得到。",
        "pick": "選一張頭像照片",
        "images": "圖片",
        "all": "所有檔案",
        "unsupported": "這個檔案讀不了，請換一張 PNG 或 JPG。",
        "too_big": "圖片處理後還是太大，請換一張。",
        "missing": "找不到這個檔案。",
        "empty": "這張圖片是空的。",
    },
    "en": {
        "title": "My avatar",
        "choose": "Choose photo…",
        "remove": "Remove photo",
        "done": "Done",
        "hint": "Your photo is cropped to a circle and shown to everyone at the table.",
        "pick": "Pick an avatar photo",
        "images": "Images",
        "all": "All files",
        "unsupported": "That file can't be read - try a PNG or JPG.",
        "too_big": "The picture is still too large - try another one.",
        "missing": "File not found.",
        "empty": "That picture is empty.",
    },
}


def av_text(app: "UnoApp", key: str) -> str:
    return AV_TEXT.get(app.lang, AV_TEXT["en"]).get(key, key)


def make_avatar(parent: tk.Misc, app: "UnoApp", grid: Any, name: str, color: str,
                d: int, bg: str) -> tk.Canvas:
    """A small round avatar widget: the player's photo, or a coloured initial."""
    cv = tk.Canvas(parent, width=d, height=d, bg=bg, highlightthickness=0, bd=0)
    if grid is not None:
        cv.create_image(d / 2, d / 2, image=grid.circle(app, d - 3))
        cv.create_oval(1, 1, d - 1, d - 1, outline=color, width=2)
    else:
        cv.create_oval(1, 1, d - 1, d - 1, fill=color, outline="")
        cv.create_text(d / 2, d / 2, text=(str(name).strip()[:1] or "?").upper(),
                       fill="#101c22", font=ui_font(max(9, int(d * 0.46)), True))
    return cv


class AvatarDialog(tk.Toplevel):
    """Small window to pick or remove the player's own avatar photo."""

    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app)
        self.app = app
        self.title(av_text(app, "title"))
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app)
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=30, pady=(24, 22))
        self.preview = tk.Canvas(body, width=132, height=132, bg=PANEL,
                                 highlightthickness=0, bd=0)
        self.preview.pack()
        tk.Label(body, text=av_text(app, "hint"), bg=PANEL, fg=MUTED, font=ui_font(11),
                 wraplength=250, justify="center").pack(pady=(12, 6))
        self.msg = tk.Label(body, text="", bg=PANEL, fg=DANGER, font=ui_font(11),
                            wraplength=250, justify="center")
        self.msg.pack(pady=(0, 6))
        row = tk.Frame(body, bg=PANEL)
        row.pack()
        FlatButton(row, av_text(app, "choose"), self.choose, bg=ACCENT2, fg="#02160c",
                   padx=14, pady=7, font=ui_font(12, True)).pack(side="left", padx=4)
        self.remove_btn = FlatButton(row, av_text(app, "remove"), self.remove, bg=PANEL2,
                                     fg=MUTED, padx=14, pady=7, font=ui_font(12))
        self.remove_btn.pack(side="left", padx=4)
        FlatButton(body, av_text(app, "done"), self.destroy, bg=PANEL2, fg=INK,
                   padx=22, pady=7, font=ui_font(12, True)).pack(pady=(14, 0))
        self._draw_preview()
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        self.after(60, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_set()
        except tk.TclError:
            pass

    def _draw_preview(self) -> None:
        cv = self.preview
        cv.delete("all")
        grid = self.app.my_avatar
        name = self.app.player_name
        if grid is not None:
            cv.create_image(66, 66, image=grid.circle(self.app, 120))
            cv.create_oval(5, 5, 127, 127, outline=ACCENT2, width=3)
        else:
            cv.create_oval(5, 5, 127, 127, fill=ACCENT2, outline="")
            cv.create_text(66, 66, text=(name.strip()[:1] or "?").upper(),
                           fill="#10202a", font=ui_font(54, True))
        self.remove_btn.set_enabled(grid is not None)

    def choose(self) -> None:
        app = self.app
        app._modal_depth += 1                     # tells the freeze watchdog this wait is expected
        try:
            path = filedialog.askopenfilename(
                parent=self, title=av_text(app, "pick"),
                filetypes=[(av_text(app, "images"),
                            "*.png *.gif *.jpg *.jpeg *.heic *.tif *.tiff *.bmp *.webp"),
                           (av_text(app, "all"), "*.*")])
        finally:
            app._modal_depth -= 1
            app._last_pump = time.monotonic()
        if not path:
            return
        try:
            app.set_my_avatar(path)
            self.msg.configure(text="")
        except avatars.AvatarError as exc:
            self.msg.configure(text=av_text(app, str(exc)) if str(exc) in AV_TEXT["en"]
                               else av_text(app, "unsupported"))
        self._draw_preview()

    def remove(self) -> None:
        self.app.clear_my_avatar()
        self.msg.configure(text="")
        self._draw_preview()


WEB_TEXT = {
    "zh": {
        "button": "手機／網頁版",
        "title": "手機／網頁版",
        "hint": "朋友的手機或電腦連到同一個 Wi-Fi，掃 QR code 或在瀏覽器輸入網址就能加入同一桌，不用裝任何東西。",
        "copy": "複製網址",
        "copied": "已複製網址",
        "others": "其他可用網址",
        "done": "完成",
        "off": "網頁版沒有開啟（只有房主的電腦會提供網頁版）",
        "failed": "網頁版無法啟動：{msg}",
    },
    "en": {
        "button": "Phone / web",
        "title": "Phone / web version",
        "hint": "Friends on the same Wi-Fi can scan the QR code or type the address in any browser to join this table - nothing to install.",
        "copy": "Copy link",
        "copied": "Link copied",
        "others": "Other addresses",
        "done": "Done",
        "off": "The web version is off (only the host's computer serves it)",
        "failed": "Web version could not start: {msg}",
    },
}


def wt(app: "UnoApp", key: str, **kw: Any) -> str:
    text = WEB_TEXT.get(app.lang, WEB_TEXT["en"]).get(key, key)
    return text.format(**kw) if kw else text


class WebInfoDialog(tk.Toplevel):
    """Shows the address and a QR code that phones can scan to join in a browser."""

    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app)
        self.app = app
        self.title(wt(app, "title"))
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app)
        urls = list(app.web_urls)
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=30, pady=(22, 20))
        tk.Label(body, text="📱 " + wt(app, "title"), bg=PANEL, fg=INK,
                 font=ui_font(16, True)).pack(pady=(0, 12))
        try:
            matrix = qrcode_min.qr_matrix(urls[0], "M")
        except ValueError:
            matrix = None
        if matrix:
            cell, quiet = 6, 3
            side = (len(matrix) + 2 * quiet) * cell
            qr = tk.Canvas(body, width=side, height=side, bg="#ffffff", highlightthickness=0, bd=0)
            qr.pack()
            for y, row in enumerate(matrix):
                for x, dark in enumerate(row):
                    if dark:
                        qr.create_rectangle((x + quiet) * cell, (y + quiet) * cell,
                                            (x + quiet + 1) * cell, (y + quiet + 1) * cell,
                                            fill="#0b1218", outline="")
        tk.Label(body, text=urls[0], bg=PANEL, fg=ACCENT2, font=mono_font(15)).pack(pady=(12, 4))
        tk.Label(body, text=wt(app, "hint"), bg=PANEL, fg=MUTED, font=ui_font(11),
                 wraplength=300, justify="center").pack(pady=(0, 6))
        if len(urls) > 1:
            tk.Label(body, text=wt(app, "others") + ": " + "  ".join(urls[1:]), bg=PANEL, fg="#5f7a77",
                     font=ui_font(10), wraplength=300, justify="center").pack(pady=(0, 6))
        row = tk.Frame(body, bg=PANEL)
        row.pack(pady=(8, 0))
        FlatButton(row, wt(app, "copy"), lambda: self._copy(urls[0]), bg=ACCENT2, fg="#02160c",
                   padx=14, pady=7, font=ui_font(12, True)).pack(side="left", padx=4)
        FlatButton(row, wt(app, "done"), self.destroy, bg=PANEL2, fg=INK,
                   padx=18, pady=7, font=ui_font(12, True)).pack(side="left", padx=4)
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

    def _copy(self, text: str) -> None:
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(text)
        except tk.TclError:
            return
        self.app.toast.show(wt(self.app, "copied"))


class MenuFrame(tk.Frame):
    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app, bg=BG)
        self.app = app
        lang = app.lang
        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        logo = tk.Canvas(wrap, width=420, height=150, bg=BG, highlightthickness=0)
        logo.pack(pady=(0, 6))
        lf = ("Arial Black", 78)
        logo.create_text(213, 82, text="UNO", fill="#b3202a", font=lf)
        for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, -2), (-2, 2), (2, 2)):
            logo.create_text(210 + dx, 76 + dy, text="UNO", fill="#ffffff", font=lf)
        logo.create_text(210, 76, text="UNO", fill="#ffe14d", font=lf)

        tk.Label(wrap, text=i18n.t("app_subtitle", lang), bg=BG, fg=MUTED,
                 font=ui_font(14)).pack(pady=(0, 26))

        _fh, field = rounded_panel(wrap, bg=PANEL, radius=18, pad=8, fill="x", pady=(0, 14))
        self.avatar_btn = tk.Canvas(field, width=60, height=60, bg=PANEL, highlightthickness=0,
                                    bd=0, cursor="hand2")
        self.avatar_btn.pack(side="left", padx=(12, 12), pady=10)
        self.avatar_btn.bind("<Button-1>", lambda _e=None: AvatarDialog(self.app))
        self.name_var = tk.StringVar(value=app.player_name)
        name_box, entry = rounded_entry(field, self.name_var, font=ui_font(15), outer=PANEL, radius=12, width=20)
        name_box.pack(side="left", fill="x", expand=True, padx=(0, 14), pady=12)
        entry.bind("<Return>", lambda _e=None: self.app.host_room())
        self.name_var.trace_add("write", lambda *_a: (self.app.remember_name(self.name_var.get()),
                                                       self.update_avatar()))
        self.update_avatar()

        btns = tk.Frame(wrap, bg=BG)
        btns.pack(fill="x", pady=(2, 0))
        for text, hint, cmd, bgc, fgc in (
                (i18n.t("menu_host", lang), i18n.t("menu_host_hint", lang), self.app.host_room, ACCENT2, "#02160c"),
                (i18n.t("menu_join", lang), i18n.t("menu_join_hint", lang), self.app.open_browser, BLUE, "#04121f")):
            col = tk.Frame(btns, bg=BG)
            col.pack(side="left", expand=True, fill="x", padx=5)
            FlatButton(col, text, cmd, bg=bgc, fg=fgc, width=200, pady=14, radius=16,
                       font=ui_font(16, True)).pack()
            tk.Label(col, text=hint, bg=BG, fg=MUTED, font=ui_font(10), wraplength=200,
                     justify="center").pack(pady=(6, 0))

        foot = tk.Frame(wrap, bg=BG)
        foot.pack(pady=(26, 0))
        for text, cmd in ((i18n.t("menu_lang", lang), self.app.toggle_lang),
                          (i18n.t("menu_help", lang), self.app.show_help),
                          (i18n.t("menu_quit", lang), self.app.close)):
            FlatButton(foot, text, cmd, bg=PANEL, fg=MUTED, padx=16, pady=8, radius=12,
                       font=ui_font(12, True)).pack(side="left", padx=5)

    def refresh(self) -> None:
        pass

    def update_avatar(self) -> None:
        cv = self.avatar_btn
        cv.delete("all")
        grid = self.app.my_avatar
        if grid is not None:
            cv.create_image(30, 30, image=grid.circle(self.app, 52))
            cv.create_oval(4, 4, 56, 56, outline=ACCENT2, width=2)
        else:
            cv.create_oval(4, 4, 56, 56, fill=ACCENT2, outline="")
            cv.create_text(30, 30, text=(self.name_var.get().strip()[:1] or "?").upper(),
                           fill="#10202a", font=ui_font(22, True))
        cv.create_oval(38, 38, 58, 58, fill=ACCENT, outline=PANEL, width=2)
        cv.create_text(48, 48, text="✎", fill="#231a03", font=ui_font(10, True))


class LobbyFrame(tk.Frame):
    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app, bg=BG)
        self.app = app
        lang = app.lang
        self._rooms: list[dict[str, Any]] = []

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=22, pady=(16, 6))
        FlatButton(head, "←", self.app.back_to_menu, bg=PANEL, fg=INK, width=46, pady=6, radius=12,
                   font=ui_font(15, True)).pack(side="left", padx=(0, 14))
        self.title = tk.Label(head, text=i18n.t("lobby_title", lang), bg=BG, fg=INK,
                              font=ui_font(22, True))
        self.title.pack(side="left")
        self.status = tk.Label(head, text="", bg=BG, fg=MUTED, font=ui_font(12))
        self.status.pack(side="right")
        FlatButton(head, i18n.t("menu_lang", lang), self.app.toggle_lang, bg=PANEL,
                   fg=MUTED, padx=14, pady=7, radius=12, font=ui_font(11, True)).pack(side="right", padx=8)

        # a scrollable column, centred like the web lobby
        scroller = tk.Canvas(self, bg=BG, highlightthickness=0)
        scroller.pack(fill="both", expand=True)
        self._scroller = scroller
        col = tk.Frame(scroller, bg=BG)
        self._col = col
        col_win = scroller.create_window(0, 0, window=col, anchor="n")
        tk.Frame(col, width=620, height=1, bg=BG).pack()

        def _resize(_e: Any = None) -> None:
            scroller.coords(col_win, scroller.winfo_width() / 2, 8)
            scroller.configure(scrollregion=(0, 0, scroller.winfo_width(), max(col.winfo_reqheight() + 24,
                                                                                scroller.winfo_height())))

        scroller.bind("<Configure>", _resize)
        col.bind("<Configure>", _resize)
        scroller.bind_all("<MouseWheel>", lambda e: scroller.yview_scroll(-1 if e.delta > 0 else 1, "units")
                          if self.winfo_ismapped() and col.winfo_reqheight() > scroller.winfo_height() else None, add="+")

        # ---------------------------------------------------------- browser
        self.browser_holder, left = rounded_panel(col, bg=PANEL, radius=18, pad=10,
                                                  stretch=False, fill="x", pady=6)
        tk.Label(left, text=i18n.t("lobby_rooms", lang), bg=PANEL, fg=INK,
                 font=ui_font(15, True), anchor="w").pack(fill="x", padx=14, pady=(14, 6))
        self.scan_btn = FlatButton(left, i18n.t("lobby_search", lang), self.app.scan_rooms,
                                   bg=PANEL2, fg=INK, padx=12, pady=8, radius=12, font=ui_font(12, True))
        self.scan_btn.pack(fill="x", padx=14, pady=(0, 8))
        self.rooms_box = tk.Frame(left, bg=PANEL)
        self.rooms_box.pack(fill="x", padx=10, pady=4)
        self.empty_hint = tk.Label(left, text=i18n.t("lobby_no_rooms", lang), bg=PANEL,
                                   fg=MUTED, font=ui_font(12))
        self.empty_hint.pack(pady=6)
        iprow = tk.Frame(left, bg=PANEL)
        iprow.pack(fill="x", padx=14, pady=(8, 16))
        tk.Label(iprow, text=i18n.t("lobby_join_ip", lang), bg=PANEL, fg=MUTED,
                 font=ui_font(11)).grid(row=0, column=0, sticky="w")
        self.ip_var = tk.StringVar(value="")
        ip_box, _ip_entry = rounded_entry(iprow, self.ip_var, font=mono_font(13), outer=PANEL, width=16)
        ip_box.grid(row=1, column=0, sticky="we", pady=(2, 0))
        FlatButton(iprow, i18n.t("lobby_join_btn", lang),
                   lambda: self.app.join_room(self.ip_var.get().strip() or "127.0.0.1"),
                   bg=BLUE, fg="#04121f", padx=16, pady=8, radius=12,
                   font=ui_font(12, True)).grid(row=1, column=1, padx=(8, 0), pady=(2, 0))
        iprow.columnconfigure(0, weight=1)

        # ------------------------------------------------------------ seats
        self.room_holder, mid = rounded_panel(col, bg=PANEL, radius=18, pad=10,
                                              stretch=False, fill="x", pady=6)
        self.room_title = tk.Label(mid, text="", bg=PANEL, fg=INK, anchor="w",
                                   font=ui_font(17, True))
        self.room_title.pack(fill="x", padx=16, pady=(14, 2))
        self.room_sub = tk.Label(mid, text="", bg=PANEL, fg=MUTED, anchor="w",
                                 justify="left", wraplength=560, font=ui_font(11))
        self.room_sub.pack(fill="x", padx=16, pady=(0, 10))
        self.seats_box = tk.Frame(mid, bg=PANEL)
        self.seats_box.pack(fill="x", padx=16, pady=4)
        self.spectator_label = tk.Label(mid, text="", bg=PANEL, fg=MUTED, anchor="w",
                                        font=ui_font(11))
        self.spectator_label.pack(fill="x", padx=16, pady=(2, 4))
        self.host_box = tk.Frame(mid, bg=PANEL)
        self.host_box.pack(fill="x", padx=16, pady=(6, 16))

        # ------------------------------------------------------------- chat
        self.chat_holder, right = rounded_panel(col, bg=PANEL, radius=18, pad=10,
                                                stretch=False, fill="x", pady=6)
        tk.Label(right, text=i18n.t("chat_title", lang), bg=PANEL, fg=INK, anchor="w",
                 font=ui_font(15, True)).pack(fill="x", padx=14, pady=(14, 6))
        self.chat = _ChatBox(right, app)
        self.chat.text.configure(height=7)
        self.chat.widget.pack(fill="x", padx=10, pady=(0, 12))
        self._show_panels(False)

    def _show_panels(self, connected: bool) -> None:
        """Browsing for a room shows the room list; being in a room shows seats and chat."""
        for holder, on in ((self.browser_holder, not connected), (self.room_holder, connected),
                           (self.chat_holder, connected)):
            if on:
                holder.pack(fill="x", pady=6)
            else:
                holder.pack_forget()

    # ------------------------------------------------------------- rendering
    def set_rooms(self, rooms: list[dict[str, Any]]) -> None:
        self._rooms = rooms
        for child in self.rooms_box.winfo_children():
            child.destroy()
        lang = self.app.lang
        if not rooms:
            self.empty_hint.configure(text=i18n.t("lobby_no_rooms", lang))
            self.empty_hint.pack(pady=6)
            return
        self.empty_hint.pack_forget()
        for room in rooms:
            _holder, card = rounded_panel(self.rooms_box, bg=PANEL2, radius=14,
                                          pad=6, fill="x", pady=4)
            card.columnconfigure(0, weight=1)
            state = room.get("state", "lobby")
            state_text = i18n.t({"lobby": "lobby_status_lobby",
                                 "playing": "lobby_status_playing",
                                 "round_over": "lobby_status_round",
                                 "match_over": "lobby_status_match"}.get(state, "lobby_status_lobby"), lang)
            host, port = room.get("host", ""), room.get("port", 45872)
            tk.Label(card, text=("🔒 " if room.get("locked") else "") + (room.get("name") or i18n.t("unnamed_room", lang)),
                     bg=PANEL2, fg=INK, font=ui_font(12, True), anchor="w",
                     justify="left").grid(row=0, column=0, sticky="w",
                                          padx=(10, 6), pady=(8, 0))
            btns = tk.Frame(card, bg=PANEL2)
            btns.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=(8, 0))
            FlatButton(btns, i18n.t("lobby_spectate_btn", lang),
                       lambda h=host, p=port: self.app.join_room(h, int(p), spectate=True),
                       bg=PANEL, fg=MUTED, padx=7, pady=4,
                       font=ui_font(10)).pack(side="left", padx=(0, 4))
            FlatButton(btns, i18n.t("lobby_join_btn", lang),
                       lambda h=host, p=port: self.app.join_room(h, int(p)),
                       bg=ACCENT2, fg="#02160c", padx=9, pady=4,
                       font=ui_font(11, True)).pack(side="left")
            tk.Label(card, text=f"{host}:{port} · {i18n.t('players_n', lang, n=room.get('players', 0))}"
                                f" · {state_text}",
                     bg=PANEL2, fg=MUTED, font=ui_font(10), anchor="w",
                     justify="left").grid(row=1, column=0, columnspan=2, sticky="w",
                                          padx=(10, 8), pady=(2, 8))

    def refresh(self) -> None:
        app = self.app
        lang = app.lang
        room = app.room or {}
        seats = room.get("seats", [])
        connected = app.session is not None and app.session.welcome is not None
        self._show_panels(connected)
        self.scan_btn.set_enabled(not connected)
        self.scan_btn.set_text(i18n.t("lobby_room_open" if connected else "lobby_search", lang))
        if not connected:
            self.room_title.configure(text=i18n.t("menu_join", lang))
            self.room_sub.configure(text=i18n.t("lobby_join_hint", lang))
            for child in self.seats_box.winfo_children():
                child.destroy()
            for child in self.host_box.winfo_children():
                child.destroy()
            self.spectator_label.configure(text="")
            return
        self.empty_hint.pack_forget()
        host_pid = room.get("host_pid", 0)
        state = room.get("state", "lobby")
        self.room_title.configure(text=room.get("name") or i18n.t("lobby_room", lang))
        tags = ([sx(app, "locked")] if room.get("locked") else []) + (["✋"] if room.get("approval") else [])
        self.room_sub.configure(
            text=" · ".join([i18n.t('lobby_seats', lang, n=len(seats))] + tags
                            + [i18n.rules_text(room.get('rules', {}), lang)]))
        self.status.configure(text=i18n.t("lobby_connected", lang))

        for child in self.seats_box.winfo_children():
            child.destroy()
        for seat in seats:
            self._seat_row(seat, host_pid)
        free = int(room.get("capacity", 10)) - len(seats)
        if state == "lobby" and free > 0:
            tk.Label(self.seats_box, text=i18n.t("lobby_empty_seats", lang, n=free),
                     bg=PANEL, fg="#5f7a77", font=ui_font(11),
                     anchor="w").pack(fill="x", padx=10, pady=(4, 0))
        if state == "lobby" and app.my_pid == host_pid:
            tk.Label(self.seats_box,
                     text=i18n.t("lobby_share_hint", lang,
                                 port=room.get("port", 45872)),
                     bg=PANEL, fg="#54706d", font=ui_font(10), anchor="w",
                     justify="left", wraplength=300).pack(fill="x", padx=10, pady=(12, 0))
        specs = room.get("spectators", [])
        self.spectator_label.configure(
            text=(i18n.t("lobby_spectators", lang) + "：" +
                  ", ".join(s.get("name", "?") for s in specs)) if specs else "")

        for child in self.host_box.winfo_children():
            child.destroy()
        is_host = app.my_pid == host_pid and app.my_pid is not None
        if is_host and state == "lobby":
            self._host_controls()
        elif state == "lobby":
            tk.Label(self.host_box, text=i18n.t("lobby_wait_host", lang), bg=PANEL,
                     fg=MUTED, font=ui_font(12)).pack(anchor="w")

    def _seat_row(self, seat: dict[str, Any], host_pid: int) -> None:
        lang = self.app.lang
        app = self.app
        _holder, row = rounded_panel(self.seats_box, bg=PANEL2, radius=12,
                                     pad=5, fill="x", pady=3)
        color = SEAT_COLORS[seat.get("pid", 0) % len(SEAT_COLORS)]
        tk.Frame(row, bg=color, width=5).pack(side="left", fill="y")
        tk.Label(row, text=str(seat.get("pid", 0) + 1), bg=PANEL2, fg=MUTED, width=3,
                 font=mono_font(12)).pack(side="left")
        make_avatar(row, app, app.avatar_for(seat.get("pid")), seat.get("name", "?"),
                    color, 30, PANEL2).pack(side="left", padx=(2, 4), pady=4)
        name = seat.get("name", "?")
        badges = []
        if seat.get("host"):
            badges.append(i18n.t("lobby_host_badge", lang))
        if app.my_pid == seat.get("pid"):
            badges.append(i18n.t("lobby_you_badge", lang))
        tk.Label(row, text=name + (f"  ({', '.join(badges)})" if badges else ""),
                 bg=PANEL2, fg=INK, font=ui_font(13, True), anchor="w").pack(
                     side="left", fill="x", expand=True, padx=8, pady=8)
        if not seat.get("connected", True):
            tk.Label(row, text=i18n.t("lobby_offline_badge", lang), bg=PANEL2,
                     fg=DANGER, font=ui_font(10)).pack(side="right", padx=8)
        if app.my_pid == seat.get("pid"):
            FlatButton(row, "✎", lambda: RenameDialog(app), bg=PANEL, fg=MUTED, width=34, pady=4,
                       radius=10, font=ui_font(12, True)).pack(side="right", padx=6, pady=6)
        if app.my_pid == host_pid and seat.get("pid") != app.my_pid:
            FlatButton(row, i18n.t("lobby_kick", lang),
                       lambda pid=seat.get("pid"): app.send({"t": "kick", "pid": pid}),
                       bg=PANEL, fg=MUTED, padx=8, pady=4,
                       font=ui_font(10)).pack(side="right", padx=6, pady=6)

    def _host_controls(self) -> None:
        lang = self.app.lang
        app = self.app
        rules = (app.room or {}).get("rules", {})
        if app.web_server is not None:
            FlatButton(self.host_box, "📱 " + wt(app, "button"), app.show_web_info, bg=PANEL2,
                       fg=INK, padx=12, pady=6, font=ui_font(11, True)).pack(anchor="w", pady=(0, 10))
        tk.Label(self.host_box, text=sx(app, "title"), bg=PANEL, fg=INK,
                 font=ui_font(13, True), anchor="w").pack(fill="x", pady=(0, 4))
        sec = tk.Frame(self.host_box, bg=PANEL)
        sec.pack(fill="x", pady=(0, 12))
        sec.columnconfigure(1, weight=1)
        tk.Label(sec, text="🔒 " + sx(app, "password"), bg=PANEL, fg=MUTED, font=ui_font(11),
                 anchor="w").grid(row=0, column=0, sticky="w")
        pw_var = tk.StringVar(value=app.sec_password)
        pw_box, pw_entry = rounded_entry(sec, pw_var, font=mono_font(13), outer=PANEL, width=14)
        pw_box.grid(row=0, column=1, sticky="we", padx=(12, 0))
        pw_job: dict[str, Any] = {"id": None}

        def _commit_pw(_e: Any = None) -> None:
            if pw_job["id"] is not None:
                try:
                    sec.after_cancel(pw_job["id"])
                except Exception:
                    pass
                pw_job["id"] = None
            app.set_security(password=pw_var.get())

        pw_var.trace_add("write", lambda *_a: (pw_job.update(id=sec.after(900, _commit_pw))
                                                if pw_job["id"] is None else None))
        pw_entry.bind("<Return>", _commit_pw)
        pw_entry.bind("<FocusOut>", _commit_pw, add="+")
        tk.Label(sec, text=sx(app, "password_hint"), bg=PANEL, fg="#5f7a77", font=ui_font(9),
                 anchor="w").grid(row=1, column=1, sticky="w", padx=(14, 0))
        tk.Label(sec, text="✋ " + sx(app, "approval"), bg=PANEL, fg=MUTED, font=ui_font(11),
                 anchor="w").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        Switch(sec, bool(app.sec_approval), lambda value: app.set_security(approval=value),
               width=46, height=24).grid(row=2, column=1, sticky="e", pady=(8, 0))
        tk.Label(self.host_box, text=i18n.t("lobby_rules", lang), bg=PANEL, fg=INK,
                 font=ui_font(13, True), anchor="w").pack(fill="x", pady=(0, 4))
        toggles = [("stack_draw2", "rule_stack_draw2"), ("stack_wild4", "rule_stack_wild4"),
                   ("jump_in", "rule_jump_in"), ("seven_swap", "rule_seven_swap"),
                   ("zero_rotate", "rule_zero_rotate"), ("combo_same", "rule_combo_same"),
                   ("draw_playable", "rule_draw_playable"),
                   ("wild4_needs_no_color", "rule_wild4_needs_no_color")]
        grid = tk.Frame(self.host_box, bg=PANEL)
        grid.pack(fill="x")
        for i, (key, label) in enumerate(toggles):
            on = bool(rules.get(key))
            cell = tk.Frame(grid, bg=PANEL)
            cell.grid(row=i // 2, column=i % 2, sticky="we", pady=3,
                      columnspan=2 if i == len(toggles) - 1 and i % 2 == 0 else 1)
            cell.columnconfigure(0, weight=1)
            lbl = tk.Label(cell, text=i18n.t(label, lang), bg=PANEL, fg=MUTED,
                           font=ui_font(11), anchor="w", justify="left",
                           wraplength=150)
            lbl.grid(row=0, column=0, sticky="we", padx=(0, 8))
            Switch(cell, on,
                   lambda value, k=key: app.send({"t": "rules", "rules": {k: value}}),
                   width=46, height=24).grid(row=0, column=1, sticky="e",
                                             padx=(10, 0))

            def _wrap(_e: Any = None, l=lbl, c=cell) -> None:
                width = c.winfo_width()
                if width > 1:
                    l.configure(wraplength=max(64, width - 64))

            cell.bind("<Configure>", _wrap)
        grid.columnconfigure(0, weight=1, uniform="rules")
        grid.columnconfigure(1, weight=1, uniform="rules")

        row = tk.Frame(self.host_box, bg=PANEL)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="⏱ " + i18n.t("rule_turn_time", lang), bg=PANEL, fg=MUTED,
                 font=ui_font(11), anchor="w").pack(side="left")
        current = int(rules.get("turn_time") or 0)
        for secs in (60, 30, 15, 0):
            picked = current == secs
            FlatButton(row, i18n.t("turn_time_off", lang) if secs == 0 else f"{secs}s",
                       lambda v=secs: app.send({"t": "rules", "rules": {"turn_time": float(v)}}),
                       bg=ACCENT2 if picked else PANEL2, fg="#02160c" if picked else INK,
                       padx=9, pady=4, radius=9, font=ui_font(10, True)).pack(side="right", padx=(4, 0))

        seats = (app.room or {}).get("seats", [])
        self.start_btn = FlatButton(self.host_box, i18n.t("lobby_start", lang),
                                    lambda: app.send({"t": "start"}),
                                    bg=ACCENT2, fg="#02160c", padx=20, pady=10,
                                    font=ui_font(14, True))
        self.start_btn.pack(fill="x", pady=(12, 0))
        if len(seats) < 2:
            tk.Label(self.host_box,
                     text=i18n.t("lobby_need_players", lang, n=2 - len(seats)),
                     bg=PANEL, fg=MUTED, font=ui_font(10)).pack(pady=(5, 0))
        self.start_btn.set_enabled(len(seats) >= 2)

    def append_chat(self, msg: dict[str, Any]) -> None:
        self.chat.append(msg, self.app.lang)


class _ChatBox:
    def __init__(self, parent: tk.Misc, app: "UnoApp") -> None:
        self.app = app
        wrap = tk.Frame(parent, bg=PANEL)
        self.widget = wrap
        self.text = tk.Text(wrap, height=4, width=1, bg=PANEL, fg=INK, relief="flat", bd=0, highlightthickness=0,
                            font=ui_font(12), wrap="word", state="disabled",
                            padx=10, pady=8, insertbackground=INK)
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("name", foreground=ACCENT, font=ui_font(12, True))
        self.text.tag_configure("me", foreground=ACCENT2, font=ui_font(12, True))
        self.text.tag_configure("row", spacing1=6, lmargin2=44)
        row = tk.Frame(wrap, bg=PANEL)
        row.pack(fill="x", pady=(6, 0))
        entry_box, self.entry = rounded_entry(row, font=ui_font(12), outer=PANEL)
        entry_box.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e=None: self._send())
        FlatButton(row, i18n.t("chat_send", app.lang), self._send, bg=ACCENT2,
                   fg="#02160c", padx=14, pady=6, radius=12, font=ui_font(12, True)).pack(
                       side="left", padx=(8, 0))
        self._rendered = 0
        self._placeholder = False
        self.text.tag_configure("muted", foreground="#5f7a77", font=ui_font(12))

    def _clear_placeholder(self) -> None:
        if self._placeholder:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
            self._placeholder = False

    def _show_placeholder(self) -> None:
        if self._placeholder or self.text.get("1.0", "end-1c").strip():
            return
        self.text.configure(state="normal")
        self.text.insert("end", i18n.t("chat_empty", self.app.lang), "muted")
        self.text.configure(state="disabled")
        self._placeholder = True

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.app.send({"t": "chat", "text": text[:300]})
        self.entry.delete(0, "end")
        self.app.focus_set()                  # hand the keyboard back to the shortcuts

    def append(self, msg: dict[str, Any], lang: str) -> None:
        name = msg.get("name", "?")
        pid = msg.get("pid")
        is_me = pid == self.app.my_pid and pid is not None
        self._clear_placeholder()
        self.text.configure(state="normal")
        start = self.text.index("end-1c")
        color = SEAT_COLORS[pid % len(SEAT_COLORS)] if isinstance(pid, int) else MUTED
        grid = self.app.avatar_for(pid) if isinstance(pid, int) else None
        badge = make_avatar(self.text, self.app, grid, name, color, 26, PANEL)
        self.text.window_create("end", window=badge, padx=6, align="center")
        self.text.insert("end", name + "：", "me" if is_me else "name")
        self.text.insert("end", str(msg.get("text", "")) + "\n")
        self.text.tag_add("row", start, "end-1c")
        self.text.see("end")
        self.text.configure(state="disabled")

    def rebuild(self, history: list[dict[str, Any]], lang: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        for msg in history:
            self.append(msg, lang)
        if not history:
            self._show_placeholder()


class GameFrame(tk.Frame):
    def __init__(self, app: "UnoApp") -> None:
        super().__init__(app, bg=BG)
        self.app = app
        lang = app.lang
        self._card_w = 120
        self._hover_card: str | None = None
        self._log_rendered = 0
        self._seat_pos: dict[int, tuple[float, float]] = {}
        self._fly: dict[str, Any] | None = None
        self._deal_t0 = 0.0
        self._hand_prev: list[str] = []
        self._enter: dict[str, Any] = {}
        self._mini_cache: dict[str, Any] = {}
        self._hover_idx: int | None = None
        self._hand_hit: list[tuple[int, str, float, float, float, float]] = []   # resting card boxes
        self._uno_geo: tuple[float, float, float, float] | None = None
        self._uno_fill: int | None = None
        self._uno_chip: dict[str, Any] | None = None
        self._pulse_t = 0.0
        self._dir_glow = 0.0
        self._disc_ring: int | None = None
        self._disc_t0 = 0.0
        self._disc_center: tuple[float, float] | None = None
        self._chip_rect: int | None = None
        self._chip_color = ""
        self._chip_glow = 0.0
        self._pending_shape: int | None = None
        self._pending_glow = 0.0
        self._turn_rings: list[tuple[int, bool, str]] = []
        self._fx: dict[str, Any] | None = None          # big "SKIP" / "+4" banner
        self._confetti: dict[str, Any] | None = None    # round-won confetti
        self._conf_key: Any = None
        self._overlay_gen = 0
        self._pile_center: tuple[float, float] | None = None
        self._sb: dict[int, dict[str, Any]] = {}
        self._reacts: list[dict[str, Any]] = []         # floating emoji reactions
        self._react_pop: tk.Frame | None = None
        self._bump: dict[int, float] = {}
        self._prev: dict[str, Any] = {}
        self._drag: dict[str, Any] | None = None
        self._hand_size = 120

        body = self.panes = panes.ThreePane(
            self, key="game", prefs=app.prefs,
            defaults={"left": 260, "right": 360},
            mins={"left": 200, "center": 520, "right": 300},
            titles={"left": i18n.t("lobby_players", lang), "right": i18n.t("log_title", lang)},
            bg=BG, strip_bg=PANEL2, strip_hover="#25505f", fg=MUTED, accent=ACCENT2, font=ui_font)
        body.pack(fill="both", expand=True, padx=8)
        if "layout.game.left_collapsed" not in app.prefs:
            body.collapse("left")             # first run looks like the web; the strip opens it

        # ---- left cell: scores (✕ folds it, the edge strip brings it back)
        self.left_holder, left = rounded_panel(body.left, bg=PANEL, radius=18, pad=10, stretch=True,
                                               fill="both", expand=True)
        head_row = tk.Frame(left, bg=PANEL)
        head_row.pack(fill="x", padx=4, pady=(2, 6))
        tk.Label(head_row, text=i18n.t("lobby_players", lang), bg=PANEL, fg=INK, anchor="w",
                 font=ui_font(13, True)).pack(side="left")
        tk.Label(head_row, text=f"{i18n.t('game_cards_short', lang)}   {i18n.t('game_score_short', lang)}",
                 bg=PANEL, fg=MUTED, font=ui_font(9)).pack(side="right", padx=(0, 24))
        self.score_box = tk.Frame(left, bg=PANEL)
        self.score_box.pack(fill="both", expand=True)

        # ---- right cells: log over chat, split by a draggable divider
        self.right_holder, right = rounded_panel(body.right, bg=PANEL, radius=18, pad=10, stretch=True,
                                                 fill="both", expand=True)
        self.right_split = panes.VSplit(right, key="game_right", prefs=app.prefs, default_top=190,
                                        min_top=80, min_bottom=170, bg=PANEL, accent=ACCENT2)
        self.right_split.pack(fill="both", expand=True)
        log_cell = self.right_split.top
        tk.Label(log_cell, text=i18n.t("log_title", lang), bg=PANEL, fg=INK, anchor="w",
                 font=ui_font(13, True)).pack(fill="x", padx=2, pady=(2, 4))
        self.log = tk.Text(log_cell, height=4, width=1, bg=PANEL, fg="#b9cdd6", relief="flat", bd=0,
                           highlightthickness=0, font=ui_font(11), wrap="word", state="disabled",
                           padx=10, pady=6)
        self.log.pack(fill="both", expand=True, padx=2)
        self.log.tag_configure("hi", foreground=ACCENT)
        chat_cell = self.right_split.bottom
        tk.Label(chat_cell, text=i18n.t("chat_title", lang), bg=PANEL, fg=INK, anchor="w",
                 font=ui_font(13, True)).pack(fill="x", padx=2, pady=(4, 2))
        self.chat = _ChatBox(chat_cell, app)
        self.chat.widget.pack(fill="both", expand=True, padx=2, pady=(0, 2))

        # ---- centre: top bar, then table over controls with a draggable divider
        main = body.center
        head = tk.Frame(main, bg=BG)
        head.pack(side="top", fill="x", padx=2, pady=(10, 6))
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        self.round_label = tk.Label(titles, text="", bg=BG, fg=INK, font=ui_font(15, True))
        self.round_label.pack(anchor="w")
        self.target_label = tk.Label(titles, text="", bg=BG, fg=MUTED, font=ui_font(11))
        self.target_label.pack(anchor="w")
        FlatButton(head, "⋯", self.show_menu, bg=PANEL2, fg=INK, width=44, pady=5, radius=12,
                   font=ui_font(16, True)).pack(side="right")
        if not getattr(app, "no_emoji", False):
            self.btn_react = FlatButton(head, "😊", self.toggle_react_palette, bg=PANEL2, fg=INK,
                                        width=44, pady=5, radius=12, font=emoji_font(16))
            self.btn_react.pack(side="right", padx=(0, 6))
        self.turn_banner = PillLabel(head, text="", bg=PANEL2, fg=MUTED, font=ui_font(13, True))
        self.turn_banner.pack(side="left", fill="x", expand=True, padx=12)

        self.main_split = panes.VSplit(main, key="game_main", prefs=app.prefs, anchor="bottom",
                                       default_bottom=346, min_top=240, min_bottom=292,
                                       bg=BG, accent=ACCENT2)
        self.main_split.pack(side="top", fill="both", expand=True)
        center = self.main_split.top
        bottom = self.main_split.bottom
        self.table_holder = center
        self.table = tk.Canvas(center, bg=BG, highlightthickness=0)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Configure>", lambda _e=None: self.draw_table())
        self.table.bind("<Button-1>", lambda _e=None: self.table.focus_set(), add="+")
        self.overlay = tk.Frame(center, bg=FELT2, highlightthickness=0, bd=0)
        self.overlay_visible = False
        self._resize_job: Any = None
        self.bind("<Configure>", self._on_resize)

        self._sort = app.prefs.get_bool("sort", False)
        self.hint_label = tk.Label(bottom, text="", bg=BG, fg=MUTED, font=ui_font(12))
        self.hint_label.pack(side="top", pady=(6, 2))
        self._hint_hot = False
        bar = tk.Frame(bottom, bg=BG)
        bar.pack(side="top", pady=(2, 8))
        self.btn_catch = FlatButton(bar, i18n.t("game_btn_catch", lang), self.app.do_catch,
                                    bg=DANGER, fg="#250707", width=128, pady=9, radius=12,
                                    font=ui_font(13, True), keycap="C")
        self.btn_uno = FlatButton(bar, i18n.t("game_btn_uno", lang), self.app.do_uno,
                                  bg=ACCENT, fg="#231a03", width=128, pady=9, radius=12,
                                  font=ui_font(13, True), keycap="U")
        self.btn_pass = FlatButton(bar, i18n.t("game_btn_pass", lang), self.app.do_pass,
                                   bg=PANEL2, fg=INK, width=128, pady=9, radius=12,
                                   font=ui_font(13, True), keycap="P")
        self.btn_draw = FlatButton(bar, i18n.t("game_btn_draw", lang), self.app.do_draw,
                                   bg=ACCENT2, fg="#02160c", width=128, pady=9, radius=12,
                                   font=ui_font(13, True), keycap="D")
        for btn in (self.btn_catch, self.btn_uno, self.btn_pass, self.btn_draw):
            btn.pack(side="left", padx=4)
        self.hand_canvas = tk.Canvas(bottom, height=120, bg=BG, highlightthickness=0)
        self.hand_canvas.pack(side="top", fill="both", expand=True)
        # hover is decided from the cards' RESTING boxes, never from the lifted items: a card that
        # moves away from the pointer would otherwise trigger Leave -> Enter -> Leave ... forever
        self.hand_canvas.bind("<Motion>", self._on_hand_motion)
        self.hand_canvas.bind("<Leave>", lambda _e=None: self._hover(None))
        self.hand_label = tk.Label(self.hand_canvas, text="", bg=PANEL, fg=MUTED, font=ui_font(12))
        self._hand_title_win = self.hand_canvas.create_window(22, 14, anchor="nw",
                                                              window=self.hand_label, tags=("static",))
        self.sort_btn = FlatButton(self.hand_canvas, "⇅ " + gt(app, "sort"), self._toggle_sort,
                                   bg=PANEL, fg=MUTED, padx=10, pady=3, font=ui_font(11, True))
        tk.Canvas.configure(self.sort_btn, bg=PANEL)
        self._sort_win = self.hand_canvas.create_window(600, 8, anchor="ne", window=self.sort_btn,
                                                        tags=("static",))
        # "Play x N": appears next to Sort when the selected number card can be played with its twins
        self._combo_cards: list[str] = []
        self.combo_btn = FlatButton(self.hand_canvas, "▶", self._play_combo, bg=ACCENT2, fg="#02160c",
                                    padx=10, pady=3, width=132, font=ui_font(11, True))
        tk.Canvas.configure(self.combo_btn, bg=PANEL)
        self._combo_win = self.hand_canvas.create_window(400, 8, anchor="ne", window=self.combo_btn,
                                                         state="hidden", tags=("static",))
        self.hand_canvas.bind("<Configure>", lambda _e=None: (self._layout_hand_panel(), self.draw_hand()))
        self.hand_canvas.bind("<B1-Motion>", self._drag_motion)
        self.hand_canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.hand_canvas.bind("<ButtonPress-1>", lambda _e=None: self.hand_canvas.focus_set(), add="+")
        self.status = tk.Label(self, text="", bg=BG, fg=MUTED)   # spectator text (not shown)
        self._pile_dy = 0.0
        self._felt_key: tuple | None = None
        self._table_sig_last: Any = None
        self._hand_sig_last: Any = None
        self._hint_text = ""
        self._dir_t = 0.0
        self._sb_sig: Any = None
        self._dir_pos: tuple[float, float, bool] | None = None
        self._pile_items: list[int] = []

    # ---------------------------------------------------------------- side tabs / menu / sort
    def show_menu(self) -> None:
        GameMenuDialog(self.app)

    # ---------------------------------------------------------- emoji reactions
    def _seated(self) -> bool:
        me = self._me()
        return me is not None and any(p.get("pid") == me for p in self._players())

    def toggle_react_palette(self) -> None:
        if self._react_pop is not None:
            self.close_react_palette()
            return
        if not self._seated():
            self.app.toast.show(i18n.t("react_spectator", self.app.lang))
            return
        pop = tk.Frame(self.table_holder, bg=PANEL2, highlightthickness=1,
                       highlightbackground="#2f5a69")
        leader = self._is_leader()
        for names, special in ((REACTIONS, False), (SPECIAL_REACTIONS, True)):
            row = tk.Frame(pop, bg=PANEL2)
            row.pack(side="top", padx=4, pady=(4, 0 if not special else 4))
            for emoji in names:
                locked = special and not leader
                FlatButton(row, emoji, lambda e=emoji: self.send_react(e),
                           bg="#16313c" if locked else PANEL2, fg=MUTED if locked else INK,
                           padx=6, pady=4, radius=10, font=emoji_font(20)).pack(side="left", padx=2, pady=2)
            if special:
                tk.Label(row, text=i18n.t("react_leader_only", self.app.lang) if not leader
                         else i18n.t("react_leader_you", self.app.lang),
                         bg=PANEL2, fg=ACCENT if leader else MUTED, font=ui_font(10, True)
                         ).pack(side="left", padx=(8, 4))
        pop.place(relx=1.0, x=-10, y=8, anchor="ne")
        pop.lift()
        self._react_pop = pop

    def close_react_palette(self) -> None:
        pop, self._react_pop = self._react_pop, None
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass

    def _is_leader(self) -> bool:
        """Same rule as the host: highest match score, and above zero."""
        scores = {p.get("pid"): int(p.get("score") or 0) for p in self._players()}
        top = max(scores.values(), default=0)
        return top > 0 and scores.get(self._me()) == top

    def send_react(self, emoji: str) -> None:
        self.close_react_palette()
        if not self._seated():
            return
        if emoji in SPECIAL_REACTIONS and not self._is_leader():
            self.app.toast.show(i18n.t("react_locked", self.app.lang), danger=True)
            return
        if emoji in REACTIONS or emoji in SPECIAL_REACTIONS:
            self.app.send({"t": "react", "emoji": emoji})

    def add_react(self, msg: dict[str, Any]) -> None:
        emoji = msg.get("emoji")
        if getattr(self.app, "no_emoji", False):
            return
        if emoji not in REACTIONS and emoji not in SPECIAL_REACTIONS:
            return
        self._reacts.append({"pid": msg.get("pid"), "emoji": emoji,
                             "name": str(msg.get("name") or ""), "t0": time.monotonic(),
                             "dx": random.uniform(-14, 14)})
        self._reacts = self._reacts[-24:]
        self._draw_reacts()

    def _draw_reacts(self, now: float | None = None) -> None:
        c = self.table
        c.delete("react")
        if not self._reacts:
            return
        now = time.monotonic() if now is None else now
        w = max(420, c.winfo_width())
        h = max(300, c.winfo_height())
        me = self._me()
        keep: list[dict[str, Any]] = []
        for r in self._reacts:
            t = now - r["t0"]
            if t >= REACT_SECONDS:
                continue
            keep.append(r)
            pid = r["pid"]
            if pid == me:
                bx, by = w / 2, h - 40.0
            elif pid in self._seat_pos:
                bx, by = self._seat_pos[pid]
                bx += 40                                         # beside the avatar, not over it
            else:
                continue
            big = 8 if r["emoji"] in SPECIAL_REACTIONS else 0
            u = min(1.0, t / 0.28)
            size = int(12 + (20 + big) * (1 - (1 - u) ** 3))     # pops in
            rise = 34 * ease_out(min(1.0, t / 1.7))
            if t > REACT_SECONDS - 0.4:                          # shrinks away at the end
                size = max(8, int(size * (REACT_SECONDS - t) / 0.4))
            x = min(w - 30, bx + r["dx"] * min(1.0, t / 0.6) * 0.5)
            y = max(size * 0.9 + 6, by - rise)                   # never clipped by the canvas top
            c.create_text(x, y, text=r["emoji"], font=emoji_font(size), tags=("react",))
        self._reacts = keep

    def _toggle_sort(self) -> None:
        self._sort = not self._sort
        self.app.prefs.set("sort", self._sort)
        self.app.prefs.save()
        self.app.selected_index = None
        self._hand_prev = []
        self.draw_hand()

    def _layout_hand_panel(self) -> None:
        c = self.hand_canvas
        w = max(360, c.winfo_width())
        h = max(120, c.winfo_height())
        c.delete("panelbg")
        rounded(c, 2, 6, w - 2, h + 40, r=20, fill=PANEL, outline="", tags=("panelbg",))
        c.tag_lower("panelbg")
        c.coords(self._hand_title_win, 22, 14)
        c.coords(self._sort_win, w - 20, 10)
        c.coords(self._combo_win, w - 20 - self.sort_btn.winfo_reqwidth() - 10, 10)

    # ---------------------------------------------------------------- helpers
    @property
    def snap(self) -> dict[str, Any]:
        return self.app.snap or {}

    def _players(self) -> list[dict[str, Any]]:
        return self.snap.get("players", [])

    def _player(self, pid: int | None) -> dict[str, Any]:
        for p in self._players():
            if p.get("pid") == pid:
                return p
        # events can arrive one message before the state that names the seats
        for seat in (self.app.room or {}).get("seats", []):
            if seat.get("pid") == pid:
                return {"pid": pid, "name": seat.get("name") or f"P{pid}",
                        "count": 0, "score": 0, "wins": 0}
        return {"pid": pid, "name": "?", "count": 0, "score": 0, "wins": 0}

    def _pname(self, pid: int | None) -> str:
        p = self._player(pid)
        return p.get("name") or f"P{pid}"

    def _me(self) -> int | None:
        return self.app.my_pid

    def _is_my_turn(self) -> bool:
        return self.snap.get("turn") == self._me() and self.snap.get("phase") == "playing"

    def _card_w_for(self, n: int) -> int:
        return 120 if n <= 11 else 80

    # ------------------------------------------------------------- rendering
    def refresh(self) -> None:
        lang = self.app.lang
        snap = self.snap
        if not snap:
            return
        self.round_label.configure(text=i18n.t("game_round_no", lang, n=snap.get("round_no", 1)))
        target = (snap.get("rules") or {}).get("target_score")
        self.target_label.configure(
            text=i18n.t("game_target", lang, n=target) if target else i18n.t("game_spectating", lang))
        self._detect_changes()
        self._update_turn_banner()
        try:
            sig = self._table_sig()
            if sig != self._table_sig_last or self._fly:
                self._table_sig_last = sig
                self.draw_table()
            if not self.panes.is_collapsed("left"):
                ssig = (tuple((p.get("pid"), p.get("name"), p.get("count"), p.get("score"), p.get("wins"),
                               p.get("said_uno"), p.get("connected"), p.get("host"))
                              for p in self._players()), self.snap.get("turn"), self.app.lang,
                        tuple((k, v[0]) for k, v in sorted(self.app.avatars.items())), self.app.my_avatar_ver)
                if ssig != self._sb_sig:
                    self._sb_sig = ssig
                    self._draw_scoreboard()
            hsig = self._hand_sig()
            if hsig != self._hand_sig_last or self._enter or self._deal_t0:
                self._hand_sig_last = hsig
                self.draw_hand()
        except Exception as exc:  # never kill the table over one bad sprite
            self.app.toast.show(f"UI error: {exc}", danger=True, ms=3200)
        self._update_buttons()
        self._update_overlay()
        self._render_log()

    def _table_sig(self) -> tuple:
        s = self.snap
        win = s.get("uno_window") or {}
        app = self.app
        return (self.table.winfo_width(), self.table.winfo_height(), self.overlay_visible, app.lang,
                tuple((p.get("pid"), p.get("name"), p.get("count"), p.get("said_uno"), p.get("connected"))
                      for p in self._players()),
                s.get("turn"), s.get("phase"), s.get("top"), tuple(s.get("top_cards") or ()),
                s.get("current_color"), s.get("direction"), s.get("pending"), s.get("draw_count"),
                win.get("pid"), win.get("said"), repr(s.get("revealed") or ""),
                tuple((k, v[0]) for k, v in sorted(app.avatars.items())), app.my_avatar_ver,
                bool(s.get("drawn")), bool(s.get("chooser")))

    def _hand_sig(self) -> tuple:
        s = self.snap
        return (tuple(ordered_hand(s.get("hand") or [], self._sort)), tuple(s.get("legal") or ()),
                tuple(s.get("jumpable") or ()), s.get("turn"), s.get("phase"), s.get("drawn"),
                self.app.selected_index, self._sort, self.hand_canvas.winfo_width(),
                self.hand_canvas.winfo_height(), self.app.lang)

    def _set_hint(self, text: str) -> None:
        if text != self._hint_text:
            self._hint_text = text
            self.hint_label.configure(text=text)

    def _detect_changes(self) -> None:
        snap = self.snap
        now = time.monotonic()
        round_no = snap.get("round_no")
        prev = self._prev
        if prev.get("round_no") != round_no:
            self._prev = prev = {"round_no": round_no}
        if prev.get("top") != snap.get("top") and snap.get("top"):
            self._disc_t0 = now
        if prev.get("color") != snap.get("current_color"):
            self._chip_glow = 1.0
        if prev.get("pending") != snap.get("pending"):
            self._pending_glow = 1.0
        prev_counts = prev.get("counts") or {}
        prev_scores = prev.get("scores") or {}
        first = not prev_counts
        for p in snap.get("players") or []:
            pid = p.get("pid")
            changed = (prev_counts.get(pid, p.get("count")) != p.get("count")
                       or prev_scores.get(pid, p.get("score")) != p.get("score"))
            if changed and not first:
                self._bump[pid] = now + 0.8
        prev["top"] = snap.get("top")
        prev["color"] = snap.get("current_color")
        prev["pending"] = snap.get("pending")
        prev["counts"] = {p.get("pid"): p.get("count") for p in snap.get("players") or []}
        prev["scores"] = {p.get("pid"): p.get("score") for p in snap.get("players") or []}

    def refresh_light(self) -> None:
        """Cheap periodic update for countdowns."""
        self.draw_table()
        self._update_buttons()

    def _update_turn_banner(self) -> None:
        lang = self.app.lang
        snap = self.snap
        if snap.get("phase") != "playing":
            if snap.get("phase") == "match_over":
                text = i18n.t("lobby_status_match", lang)
            elif snap.get("phase") == "round_over":
                text = i18n.t("lobby_status_round", lang)
            else:
                text = ""
            self.turn_banner.configure(text=text, bg=PANEL2, fg=MUTED)
            return
        me = self._me()
        chooser = snap.get("chooser")
        if chooser and chooser.get("pid") != me:
            text = i18n.t("game_choosing", lang, name=self._pname(chooser.get("pid")))
            bg, fg = PURPLE, "#1b1030"
        elif snap.get("turn") == me:
            text, bg, fg = i18n.t("game_turn_you", lang), ACCENT, "#231a03"
        else:
            text = i18n.t("game_turn_wait", lang, name=self._pname(snap.get("turn")))
            bg, fg = PANEL2, MUTED
        self.turn_banner.configure(text=text, bg=bg, fg=fg)

    def _draw_scoreboard(self) -> None:
        lang = self.app.lang
        for child in self.score_box.winfo_children():
            child.destroy()
        me = self._me()
        turn = self.snap.get("turn")
        self._sb = {}
        for p in self._players():
            row = tk.Frame(self.score_box, bg=PANEL2 if p.get("pid") == turn else PANEL)
            row.pack(fill="x", pady=2)
            color = SEAT_COLORS[p.get("pid", 0) % len(SEAT_COLORS)]
            tk.Frame(row, bg=color, width=4).pack(side="left", fill="y")
            make_avatar(row, self.app, self.app.avatar_for(p.get("pid")), p.get("name", "?"),
                        color, 26, row["bg"]).pack(side="left", padx=(6, 0), pady=3)
            chips = []
            if p.get("host"):
                chips.append("★")
            if p.get("said_uno"):
                chips.append("UNO")
            if not p.get("connected", True):
                chips.append("⚡")
            if p.get("wins"):
                tk.Label(row, text=f"★{p['wins']}", bg=row["bg"], fg=GOLD,
                         font=ui_font(9)).pack(side="right", padx=(0, 4))
            score_lbl = tk.Label(row, text=f"{p.get('score', 0)}", bg=row["bg"],
                                 fg=ACCENT2, width=4, anchor="e", font=mono_font(11))
            score_lbl.pack(side="right", padx=(0, 2))
            count_lbl = tk.Label(row, text=f"{p.get('count', 0)}", bg=row["bg"],
                                 fg=MUTED, width=3, anchor="e", font=mono_font(11))
            count_lbl.pack(side="right", padx=(0, 4))
            self._sb[p.get("pid")] = {"count": count_lbl, "score": score_lbl, "row": row}
            label = p.get("name", "?") + (i18n.t("you_suffix", lang) if p.get("pid") == me else "")
            tk.Label(row, text=label, bg=row["bg"], fg=INK if p.get("pid") != turn else ACCENT,
                     font=ui_font(12, True), anchor="w").pack(side="left", fill="x",
                                                            expand=True, padx=6, pady=4)
            if chips:
                tk.Label(row, text=" ".join(chips), bg=row["bg"], fg=MUTED,
                         font=ui_font(8)).pack(side="left", padx=2)

    # ---------------------------------------------------------------- table
    def _on_resize(self, _event: Any = None) -> None:
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(120, self._after_resize)

    def _after_resize(self) -> None:
        self._resize_job = None
        if not self.winfo_exists():
            return
        self.draw_table()
        self.draw_hand()

    def draw_table(self) -> None:
        self._draw_table_base()
        self._draw_fx()
        self._draw_confetti()
        self._draw_reacts()

    def _draw_table_base(self) -> None:
        c = self.table
        for item in c.find_all():                       # keep the (expensive) felt, redraw the rest
            if "felt" not in c.gettags(item):
                c.delete(item)
        w = max(420, c.winfo_width())
        h = max(300, c.winfo_height())
        lang = self.app.lang
        snap = self.snap
        dim = 0.42 if self.overlay_visible else 0.0
        black = "#04120d"
        key = (w, h, dim)
        if self._felt_key != key or not c.find_withtag("felt"):
            c.delete("felt")
            self._felt_key = key
            # felt: a soft radial light with an inset rim, like the web table
            rounded(c, 2, 2, w - 2, h - 2, r=22, fill=_mix(FELT2, black, dim), outline="", tags=("felt",))
            steps = 18
            for i in range(steps):
                t = i / (steps - 1)
                k = 1 - t * 0.9
                rx, ry = (w / 2 - 14) * k, (h / 2 - 14) * k
                c.create_oval(w / 2 - rx, h / 2 - ry, w / 2 + rx, h / 2 + ry,
                              fill=_mix(_mix(FELT2, FELT, t), black, dim), outline="", tags=("felt",))
            rounded(c, 2, 2, w - 2, h - 2, r=22, fill="", outline=_mix("#0c5a44", black, dim),
                    width=3, tags=("felt",))
            c.tag_lower("felt")
        if self.overlay_visible:
            # a dialog covers the table: skip the cards so nothing peeks out
            return
        cx = w / 2

        me = self._me()
        players = self._players()
        opp = [p for p in players if p.get("pid") != me]
        self._seat_pos = {}
        self._uno_chip = None
        self._turn_rings = []
        self._pile_dy = 0.0
        top = self._draw_seats(c, w, h, opp, snap, lang)

        # centre: direction | draw pile | discard pile (sprite size follows the free height)
        avail = h - 12 - top
        size, cw_, ch_ = (120, 120, 180) if avail >= 262 else ((80, 80, 120) if avail >= 190 else (60, 60, 90))
        gap = 26
        group_w = 96 + 34 + cw_ * 2 + gap
        gx = cx - group_w / 2
        draw_x = gx + 96 + 34
        disc_x = draw_x + cw_ + gap
        pile_y = top + max(4.0, (avail - (ch_ + 50)) / 2)
        pile_y = min(pile_y, h - 12 - ch_ - 46)
        self._pile_center = (draw_x + cw_ / 2, pile_y + ch_ / 2)
        self._disc_center = (disc_x + cw_ / 2, pile_y + ch_ / 2)
        self._pile_items = [c.create_image(draw_x, pile_y, anchor="nw", image=self.app.assets.back(size))]
        self._draw_count = c.create_text(
            draw_x + cw_ / 2, pile_y + ch_ + 16, text=i18n.t("game_pile", lang, n=snap.get("draw_count", 0)),
            fill="#a8c8bb", font=ui_font(10))
        top_card = snap.get("top")
        if top_card:
            cards = snap.get("top_cards") or [top_card]
            for i, card in enumerate(reversed(cards[1:4])):
                c.create_image(disc_x + 8 - i * 4, pile_y + 3 - i * 3, anchor="nw",
                               image=self.app.assets.image(card, size))
            c.create_image(disc_x, pile_y, anchor="nw", image=self.app.assets.image(top_card, size))
        # invisible except for a brief soft pulse when a card lands (see tick_anim)
        self._disc_ring = rounded(c, disc_x - 5, pile_y - 5, disc_x + cw_ + 5, pile_y + ch_ + 5, r=16,
                                  outline="", width=2)

        # colour chip under the discard pile
        color = snap.get("current_color") or "R"
        chip_w, chip_h = 92, 28
        chip_y = pile_y + ch_ + 8
        chip_x = disc_x + cw_ / 2 - chip_w / 2
        self._chip_rect = rounded(c, chip_x, chip_y, chip_x + chip_w, chip_y + chip_h,
                                  r=14, fill=COLOR_HEX.get(color, "#666"), outline="")
        self._chip_color = color
        c.create_text(disc_x + cw_ / 2, chip_y + chip_h / 2, text=i18n.color_text(color, lang),
                      fill="#101c22", font=ui_font(12, True))

        # direction: a slowly turning arrow (tick_anim keeps it moving)
        d = snap.get("direction", 1)
        ax, ay = gx + 48, pile_y + ch_ / 2 - 14
        self._dir_pos = (ax, ay, d != 1)
        self._draw_dir(c, time.monotonic())
        c.create_text(ax, ay + 38, text=i18n.t("game_direction_cw", lang) if d == 1
                      else i18n.t("game_direction_ccw", lang), fill="#8fd0b5", font=ui_font(10))

        # pending penalty pill (top right of the table)
        pending = snap.get("pending") or 0
        if pending:
            pw_, ph_ = 64, 32
            px, py = w - 20 - pw_, 14.0
            self._pending_shape = rounded(c, px, py, px + pw_, py + ph_, r=12, fill=DANGER, outline="")
            c.create_text(px + pw_ / 2, py + ph_ / 2, text=f"+{pending}", fill="#ffffff",
                          font=ui_font(15, True))

        # flying card for the latest play
        fly = self._fly
        if fly:
            t = min(1.0, max(0.0, (time.monotonic() - fly["t0"]) / fly["dur"]))
            e = t * t * (3 - 2 * t)
            sx, sy = fly["src"]
            tx, ty = fly.get("dst") or self._disc_center
            x = sx + (tx - sx) * e
            y = sy + (ty - sy) * e - 46 * math.sin(math.pi * e)
            fsize = 120 if e < 0.5 else (80 if e < 0.82 else 60)
            iw, ih = {120: (120, 180), 80: (80, 120), 60: (60, 90)}[fsize]
            sprite = (self.app.assets.back(fsize) if fly.get("back")
                      else self.app.assets.image(fly["card"], fsize))
            c.create_image(x - iw / 2, y - ih / 2, anchor="nw", image=sprite)

    def _draw_dir(self, c: tk.Canvas, now: float) -> None:
        """The turning direction arrow."""
        c.delete("dir")
        if not self._dir_pos:
            return
        ax, ay, ccw = self._dir_pos
        glow = max(0.0, min(1.0, self._dir_glow))
        col = _mix("#7fe0b8", ACCENT, glow)
        start = (now * (360 / 5.0)) % 360 * (-1 if ccw else 1)
        r = 15 + 3 * glow
        pts: list[float] = []
        for k in range(0, 261, 12):
            a = math.radians(start + (-k if ccw else k))
            pts += [ax + r * math.cos(a), ay + r * math.sin(a)]
        c.create_line(*pts, fill=col, width=3, arrow="last", arrowshape=(9, 11, 5),
                      capstyle="round", smooth=True, tags=("dir",))

    def _draw_seats(self, c: tk.Canvas, w: float, h: float,
                    opp: list[dict[str, Any]], snap: dict[str, Any], lang: str) -> float:
        """Opponents in a centred, wrapping row (like the web); returns the y below them."""
        m = len(opp)
        if not m:
            return 12.0
        revealed = bool(snap.get("revealed"))
        slot = 152.0 if revealed else 98.0
        row_h = 200.0 if revealed else 142.0
        per_row = max(1, int((w - 28) // slot))
        rows = (m + per_row - 1) // per_row
        per_row = max(1, -(-m // rows))
        y = 10.0
        for r in range(rows):
            items = opp[r * per_row:(r + 1) * per_row]
            x0 = (w - len(items) * slot) / 2 + slot / 2
            for j, p in enumerate(items):
                x = x0 + j * slot
                self._draw_seat(c, x, y, p, snap, lang)
                self._seat_pos[p.get("pid")] = (x, y + 44)
            y += row_h
        return y

    def _mini_card(self, card: str) -> Any:
        img = self._mini_cache.get(card)
        if img is None:
            try:
                img = self.app.assets.image(card, 80).subsample(3, 3)
            except (FileNotFoundError, tk.TclError):
                img = self.app.assets.back(44)
            self._mini_cache[card] = img
        return img

    def _draw_revealed(self, c: tk.Canvas, x: float, top: float, hand: list[str]) -> float:
        """Face-up hand as a small grid; returns the height used."""
        shown = hand[:15]
        cols = min(5, len(shown))
        first = self._mini_card(shown[0])
        mw, mh = first.width(), first.height()
        gap = 2
        x0 = x - (cols * mw + (cols - 1) * gap) / 2
        for i, card in enumerate(shown):
            r, col = divmod(i, 5)
            c.create_image(x0 + col * (mw + gap), top + r * (mh + gap), anchor="nw",
                           image=self._mini_card(card))
        rows = (len(shown) + 4) // 5
        used = rows * (mh + gap)
        if len(hand) > len(shown):
            c.create_text(x, top + used + 6, text=f"+{len(hand) - len(shown)}", fill=MUTED,
                          font=ui_font(9, True))
            used += 12
        return float(used)

    def _draw_seat(self, c: tk.Canvas, x: float, y: float,
                   p: dict[str, Any], snap: dict[str, Any], lang: str,
                   max_chars: int = 12) -> None:
        """Opponent seat: coloured avatar, name, count badge and a little fan of card backs."""
        pid = p.get("pid")
        is_turn = snap.get("turn") == pid and snap.get("phase") == "playing"
        count = p.get("count", 0)
        online = p.get("connected", True)
        seat_col = SEAT_COLORS[(pid or 0) % len(SEAT_COLORS)]
        if not online:
            seat_col = _mix(seat_col, FELT2, 0.62)
        r = 26
        ay = y + 44

        # avatar (+ breathing glow on this seat's turn)
        if is_turn:
            for extra, t in ((13, 0.88), (9, 0.74), (5, 0.52)):
                c.create_oval(x - r - extra, ay - r - extra, x + r + extra, ay + r + extra,
                              outline=_mix(seat_col, FELT2, t), width=4)
            rid = c.create_oval(x - r - 3, ay - r - 3, x + r + 3, ay + r + 3,
                                outline=seat_col, width=2)
            self._turn_rings.append((rid, False, seat_col))
        grid = self.app.avatar_for(pid)
        if grid is not None:
            c.create_image(x, ay, image=grid.circle(self.app, 2 * r))
            c.create_oval(x - r, ay - r, x + r, ay + r, outline=seat_col, width=2)
        else:
            c.create_oval(x - r, ay - r, x + r, ay + r, fill=seat_col, outline="")
            initial = (str(p.get("name", "?")).strip()[:1] or "?").upper()
            c.create_text(x, ay, text=initial, fill="#101c22", font=ui_font(22, True))

        # name
        name = str(p.get("name", "?"))
        if len(name) > max_chars:
            name = name[:max_chars - 1] + "…"
        c.create_text(x, ay + r + 12, text=name,
                      fill=seat_col if is_turn else (INK if online else MUTED),
                      font=ui_font(11, True))

        # card-count badge on the avatar's lower right
        if count:
            br = 10
            bx, by = x + r * 0.78, ay + r * 0.78
            c.create_oval(bx - br, by - br, bx + br, by + br,
                          fill=DANGER if count <= 2 else "#16333d", outline=FELT2, width=2)
            c.create_text(bx, by, text=str(count), fill="#ffffff" if count <= 2 else INK,
                          font=ui_font(10, True))

        if p.get("said_uno"):
            rounded(c, x - 20, ay - r - 14, x + 20, ay - r + 2, r=8, fill=ACCENT, outline="")
            c.create_text(x, ay - r - 6, text="UNO!", fill="#231a03", font=ui_font(9, True))

        # little fan of card backs (or the revealed hand)
        fan_top = ay + r + 22
        revealed = snap.get("revealed") or {}
        rhand = revealed.get(pid) or revealed.get(str(pid)) or []
        fan_h = 36.0
        if rhand:
            fan_h = self._draw_revealed(c, x, fan_top, rhand)
        elif count:
            n = min(count, 4)
            width = 22 + 9 * (n - 1)
            for i in range(n):
                mx = x - width / 2 + i * 9
                c.create_rectangle(mx, fan_top, mx + 22, fan_top + 34, fill="#14141c",
                                   outline="#ffffff", width=2)
                c.create_oval(mx + 5, fan_top + 9, mx + 17, fan_top + 25, fill="#7a1a22", outline="")
        else:
            rounded(c, x - 12, fan_top, x + 12, fan_top + 34, r=5, outline="#2b5c4b",
                    dash=(4, 4), width=2)

        window = snap.get("uno_window") or {}
        if window and window.get("pid") == pid:
            remain = self._remain(window)
            cy = fan_top + fan_h + 6
            rounded(c, x - 33, cy, x + 33, cy + 20, r=10, fill="#5a1116", outline=DANGER, width=1)
            self._uno_chip = {
                "pid": pid,
                "text": c.create_text(x, cy + 10, text=f"UNO {remain:.1f}",
                                      fill="#ffdede", font=ui_font(9, True)),
            }
        if not online:
            c.create_text(x + r * 0.8, ay - r * 0.8, text="⚡", fill=DANGER, font=ui_font(12))

    def draw_hand(self) -> None:
        c = self.hand_canvas
        for item in c.find_all():
            tags = c.gettags(item)
            if "static" not in tags and "panelbg" not in tags:
                c.delete(item)
        snap = self.snap
        hand = ordered_hand(snap.get("hand") or [], self._sort)
        legal = set(snap.get("legal") or [])
        jumpable = set(snap.get("jumpable") or [])
        n = len(hand)
        if self._me() is None:
            self.hand_label.configure(text=i18n.t("game_spectating", self.app.lang))
            self._set_hint("")
            self._hand_hit = []
            self._update_combo_btn()
            return
        w = max(360, c.winfo_width())
        h = max(120, c.winfo_height())
        size, cw, chh, step = 60, 60, 90, 14.0
        for cand, ww, hh in ((120, 120, 180), (80, 80, 120), (60, 60, 90)):
            if cand != 60 and h < hh + 66:
                continue                       # the panel is too short for this size
            st = min(ww * 0.58, (w - 72 - ww) / (n - 1)) if n > 1 else 0
            if n <= 1 or st >= 16:
                size, cw, chh, step = cand, ww, hh, st
                break
        step = max(step, 14.0)
        self._hand_size = size
        drag_idx = self._drag["idx"] if self._drag else None
        total_w = cw + step * max(0, n - 1)
        x0 = max(6.0, (w - total_w) / 2)
        base_y = h - chh - 4
        now = time.monotonic()
        my_turn_play = snap.get("phase") == "playing" and snap.get("turn") == self._me()
        if hand != self._hand_prev:
            # cards that were not in the previous hand slide in (the deal has its own animation)
            seen: dict[str, int] = {}
            for old in self._hand_prev:
                seen[old] = seen.get(old, 0) + 1
            fresh: list[int] = []
            for i, card in enumerate(hand):
                if seen.get(card, 0) > 0:
                    seen[card] -= 1
                else:
                    fresh.append(i)
            dealing = bool(self._deal_t0) and now - self._deal_t0 < 1.0
            if fresh and self._hand_prev and not dealing:
                self._enter = {"t0": now, "idx": fresh}
                self.app.animate(1.0, lambda _t: self.draw_hand())
            self._hand_prev = list(hand)
        enter_idx: list[int] = self._enter.get("idx", []) if self._enter else []
        self._hand_hit = []
        for i, card in enumerate(hand):
            if i == drag_idx:
                continue
            x = x0 + i * step
            y = base_y
            if self._deal_t0:
                t = (now - self._deal_t0 - i * 0.03) / 0.30
                if t < 1:
                    y += (1 - max(0.0, t)) ** 2 * 64
            if i in enter_idx:
                k = enter_idx.index(i)
                t = (now - self._enter["t0"] - 0.30 - k * 0.08) / 0.34
                if t < 0:
                    continue                      # still travelling from the draw pile
                if t < 1:
                    y += (1 - t) ** 2 * 70
            if my_turn_play and card in legal:
                y -= 12                           # playable cards stand up
            y_rest = y - (10 if i == self.app.selected_index else 0)      # where the card sits without hover
            self._hand_hit.append((i, card, x, y_rest - 18, x + (step if i < n - 1 else cw), y_rest + chh))
            if i == self._hover_idx:
                y -= 4
            if i == self.app.selected_index:
                y -= 10
            tag = f"hand{i}"
            c.create_image(x, y, anchor="nw",
                           image=self.app.assets.image(card, size), tags=(tag,))
            if my_turn_play and card not in legal:
                c.create_image(x, y, anchor="nw", image=make_shade(self.app, cw, chh),
                               tags=(tag,))       # dim what cannot be played now
            if card in legal or card in jumpable:
                vis_w = step if i < n - 1 else cw
                dx = x + min(vis_w, cw) / 2
                dy = y + chh - 13
                col = ACCENT2 if card in legal else BLUE
                c.create_oval(dx - 7, dy - 7, dx + 7, dy + 7, fill="#0d1a21",
                              outline="", tags=(tag,))
                c.create_oval(dx - 5, dy - 5, dx + 5, dy + 5, fill=col,
                              outline="", tags=(tag,))
            c.tag_bind(tag, "<Button-1>",
                       lambda e, idx=i: self._drag_start(e, idx))
        self.hand_label.configure(text=i18n.t("game_hand", self.app.lang, n=n))
        try:
            c.configure(cursor="hand2" if snap.get("turn") == self._me() else "")
        except tk.TclError:
            pass
        self._update_combo_btn()

    # ----------------------------------------------------------- combo play
    def _update_combo_btn(self) -> None:
        snap = self.snap
        combos = snap.get("combo") or {}
        order: list[str] = []
        idx = self.app.selected_index
        if combos and idx is not None and self._me() is not None and snap.get("turn") == self._me():
            hand = ordered_hand(snap.get("hand") or [], self._sort)
            if 0 <= idx < len(hand):
                order = list(combos.get(hand[idx]) or [])
        self._combo_cards = order
        if order:
            text = "▶ " + i18n.t("combo_btn", self.app.lang, n=len(order))
            if text != self.combo_btn._text:
                self.combo_btn._text = text
                self.combo_btn._draw()
            self.hand_canvas.itemconfigure(self._combo_win, state="normal")
        else:
            self.hand_canvas.itemconfigure(self._combo_win, state="hidden")

    def _play_combo(self) -> None:
        cards = list(self._combo_cards)
        if len(cards) < 2:
            return
        self.app.selected_index = None
        self.app.send({"t": "combo", "cards": cards})
        self.draw_hand()

    # ------------------------------------------------------------ drag play
    def _drag_start(self, event: tk.Event, idx: int) -> None:
        snap = self.snap
        if not snap or snap.get("phase") != "playing" or self._me() is None:
            return
        hand = ordered_hand(snap.get("hand") or [], self._sort)
        if idx < 0 or idx >= len(hand):
            return
        card = hand[idx]
        legal = set(snap.get("legal") or [])
        jump = set(snap.get("jumpable") or [])
        my_turn = snap.get("turn") == self._me()
        self.app.selected_index = idx
        self._drag = {"idx": idx, "card": card, "legal": card in legal and my_turn,
                      "jump": card in jump and not (card in legal and my_turn),
                      "x": event.x_root, "y": event.y_root,
                      "ghost": None, "moved": False, "over": False}
        size = self._hand_size
        img = self.app.assets.image(card, size)
        ghost = tk.Label(self, image=img, bd=0, bg=PANEL)
        ghost.image = img  # type: ignore[attr-defined]
        lx = event.x_root - self.winfo_rootx()
        ly = event.y_root - self.winfo_rooty()
        ghost.place(x=lx - size // 2, y=ly - size * 3 // 4)
        ghost.lift()
        self._drag["ghost"] = ghost
        self.draw_hand()
        if self._drag["legal"]:
            self._set_hint(i18n.t("game_drag_release", self.app.lang))
        elif self._drag["jump"]:
            self._set_hint(i18n.t("game_drag_jump", self.app.lang))
        else:
            self._set_hint(i18n.t("game_drag_illegal", self.app.lang))

    def _drag_motion(self, event: tk.Event) -> None:
        d = self._drag
        if not d or not d.get("ghost"):
            return
        d["lx"] = event.x_root - self.winfo_rootx()
        d["ly"] = event.y_root - self.winfo_rooty()
        if not d.get("pending"):
            d["pending"] = True
            self.after_idle(self._drag_flush)
        if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) > 8:
            d["moved"] = True
        over = self._over_table(event.x_root, event.y_root)
        if over != d.get("over"):
            d["over"] = over
            self._highlight_drop(over and (d["legal"] or d["jump"]))

    def _drag_flush(self) -> None:
        d = self._drag
        if not d or not d.get("ghost"):
            return
        d["pending"] = False
        size = self._hand_size
        try:
            d["ghost"].place(x=d["lx"] - size // 2, y=d["ly"] - size * 3 // 4)
        except tk.TclError:
            return

    def _over_table(self, x_root: int, y_root: int) -> bool:
        t = self.table
        x = x_root - t.winfo_rootx()
        y = y_root - t.winfo_rooty()
        return 0 <= x <= t.winfo_width() and 0 <= y <= t.winfo_height()

    def _highlight_drop(self, on: bool) -> None:
        """Subtle cue instead of a drawn circle: brighten the discard border."""
        if self._disc_ring is None:
            return
        try:
            if on:
                self.table.itemconfigure(self._disc_ring, outline=ACCENT2, width=5)
            else:
                mine = self._is_my_turn()
                self.table.itemconfigure(
                    self._disc_ring,
                    outline=ACCENT if mine else "#2b5c4b",
                    width=3 if mine else 2)
        except tk.TclError:
            self._disc_ring = None

    def _drag_end(self, event: tk.Event) -> None:
        d = self._drag
        if not d:
            return
        self._drag = None
        ghost = d.get("ghost")
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        self._highlight_drop(False)
        dropped = d["moved"] and self._over_table(event.x_root, event.y_root)
        self.app.selected_index = None if dropped else d["idx"]
        if dropped:
            if d["legal"]:
                self.app.play_card(d["card"])
            elif d["jump"]:
                self.app.send({"t": "play", "card": d["card"], "jump": True})
            else:
                self.app.toast.show(i18n.error_text("illegal", self.app.lang),
                                    danger=True)
        try:
            self.draw_hand()
        except Exception:
            pass

    def _on_hand_motion(self, event: tk.Event) -> None:
        if self._drag is not None:
            return
        for i, card, x0, y0, x1, y1 in reversed(self._hand_hit):
            if x0 <= event.x < x1 and y0 <= event.y <= y1:
                if i != self._hover_idx:
                    self._hover(card, i)
                return
        if self._hover_idx is not None:
            self._hover(None)

    def _hover(self, card: str | None, idx: int | None = None) -> None:
        if self._drag is not None:
            return
        lang = self.app.lang
        new = idx if card is not None else None
        if new == self._hover_idx:
            return
        if card is None:
            self._set_hint(self._default_hint())
        else:
            self._set_hint(i18n.card_text(card, lang))
        c = self.hand_canvas
        if self._hover_idx is not None:
            try:
                c.move(f"hand{self._hover_idx}", 0, 10)
            except tk.TclError:
                pass
        self._hover_idx = new
        if new is not None:
            try:
                c.move(f"hand{new}", 0, -10)
            except tk.TclError:
                pass

    # ------------------------------------------------------------ animations
    def flash_play(self, card: str, pid: int | None) -> None:
        w = max(420, self.table.winfo_width())
        h = max(300, self.table.winfo_height())
        if pid is None or pid == self._me():
            src = (w / 2, h - 20)
        else:
            src = self._seat_pos.get(pid, (w / 2, 110))
        dst = self._disc_center or (w / 2, 110)
        self._fly = {"card": card, "src": src, "dst": dst,
                     "t0": time.monotonic(), "dur": 0.32}
        self.app.animate(0.32, lambda _t: self.draw_table(), done=self._fly_done)

    # ------------------------------------------------------- big-text effects
    FX_WORDS = {
        "skip": ("SKIP", "跳過"),
        "reverse": ("REVERSE", "迴轉"),
        "draw2": ("+2", "+2"),
        "wild4": ("+4", "+4"),
        "wild": ("WILD", "變色"),
        "uno": ("UNO!", "UNO!"),
    }

    def flash_fx(self, ev: dict[str, Any]) -> None:
        """Pop a big coloured word in the middle of the table for action cards."""
        if ev.get("t") == "uno_call":
            kind, color = "uno", ACCENT
        else:
            card = ev.get("card") or ""
            if card == "wild4":
                kind = "wild4"
            elif card == "wild":
                kind = "wild"
            else:
                kind = card[1:]
            if kind not in self.FX_WORDS:
                return
            if kind in ("wild", "wild4"):
                color = COLOR_HEX.get(ev.get("color") or "", "#ffffff")
            else:
                color = COLOR_HEX.get(card[:1], "#ffffff")
        zh = self.app.lang == "zh"
        pid = ev.get("pid")
        self._fx = {"text": self.FX_WORDS[kind][1 if zh else 0], "color": color,
                    "sub": self._pname(pid) if pid is not None else "",
                    "t0": time.monotonic(), "dur": 1.45}
        self._draw_fx()

    def _draw_fx(self, now: float | None = None) -> None:
        c = self.table
        c.delete("fx")
        fx = self._fx
        if not fx:
            return
        now = time.monotonic() if now is None else now
        t = now - fx["t0"]
        if t >= fx["dur"]:
            self._fx = None
            return
        w = max(420, c.winfo_width())
        h = max(300, c.winfo_height())
        cx = w / 2
        cy = max(160.0, min(h * 0.55, h - 264)) + 66
        u = min(1.0, t / 0.22)
        size = 30 + 46 * (1 - (1 - u) ** 3)
        if t > 0.22:
            size -= 10 * min(1.0, (t - 0.22) / 0.25)
        size = int(size)
        fade = max(0.0, (t - (fx["dur"] - 0.4)) / 0.4)
        main = _mix(fx["color"], FELT2, fade)
        edge = _mix("#ffffff", FELT2, fade)
        shade = _mix("#04120d", FELT2, fade)
        font = ("Arial Black", size)
        text = fx["text"]
        c.create_text(cx + 3, cy + 5, text=text, fill=shade, font=font, tags=("fx",))
        for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, -2), (-2, 2), (2, 2)):
            c.create_text(cx + dx, cy + dy, text=text, fill=edge, font=font, tags=("fx",))
        c.create_text(cx, cy, text=text, fill=main, font=font, tags=("fx",))
        if fx.get("sub"):
            sy = cy + size * 0.78 + 10
            half = 12 + 4.2 * len(str(fx["sub"]))
            rounded(c, cx - half, sy - 12, cx + half, sy + 12, r=12,
                    fill=_mix("#06241b", FELT2, fade), outline="", tags=("fx",))
            c.create_text(cx, sy, text=fx["sub"], fill=_mix(INK, FELT2, fade),
                          font=ui_font(12, True), tags=("fx",))

    # ------------------------------------------------------------- confetti
    def start_confetti(self, big: bool = False) -> None:
        import random

        w = max(420, self.table.winfo_width())
        palette = list(COLOR_HEX.values()) + ["#ffffff", ACCENT]
        n = 150 if big else 90
        parts = []
        for _ in range(n):
            parts.append({
                "x0": random.uniform(0, w), "y0": random.uniform(-(300 if big else 220), -10),
                "vx": random.uniform(-50, 50), "vy": random.uniform(150, 320),
                "sw": random.uniform(8, 28), "ph": random.uniform(0, 6.28),
                "sp": random.uniform(2.5, 7.0), "sz": random.uniform(4.5, 8.5),
                "col": random.choice(palette),
            })
        self._confetti = {"t0": time.monotonic(), "dur": 8.0 if big else 5.6, "parts": parts}

    def _draw_confetti(self, now: float | None = None) -> None:
        c = self.table
        c.delete("confetti")
        conf = self._confetti
        if not conf:
            return
        now = time.monotonic() if now is None else now
        t = now - conf["t0"]
        if t >= conf["dur"]:
            self._confetti = None
            return
        h = max(300, c.winfo_height())
        for p in conf["parts"]:
            y = p["y0"] + p["vy"] * t
            if y < -12 or y > h + 12:
                continue
            x = p["x0"] + p["vx"] * t + p["sw"] * math.sin(p["ph"] + t * 2.2)
            rot = p["ph"] + t * p["sp"] * 0.6
            flutter = 0.25 + 0.75 * abs(math.cos(p["ph"] + t * p["sp"]))
            ca, sa = math.cos(rot), math.sin(rot)
            pts = []
            for px, py in ((-1.0, -0.55), (1.0, -0.55), (1.0, 0.55), (-1.0, 0.55)):
                px *= p["sz"]
                py *= p["sz"] * flutter
                pts += [x + px * ca - py * sa, y + px * sa + py * ca]
            c.create_polygon(*pts, fill=p["col"], outline="", tags=("confetti",))

    def _remain(self, window: dict[str, Any]) -> float:
        """Seconds left on a timed window, counted on *this* machine's clock."""
        if "remaining" in window:
            age = time.monotonic() - getattr(self.app, "_snap_rx", time.monotonic())
            return max(0.0, float(window["remaining"]) - age)
        return max(0.0, window.get("deadline", 0) - time.monotonic())

    def flash_draw(self, pid: int | None) -> None:
        w = max(420, self.table.winfo_width())
        h = max(300, self.table.winfo_height())
        src = self._pile_center or (w / 2 - 60, h / 2)
        if pid is None or pid == self._me():
            dst = (w / 2, h - 30)
        else:
            dst = self._seat_pos.get(pid, (w / 2, 110))
        self._fly = {"back": True, "card": None, "src": src, "dst": dst,
                     "t0": time.monotonic(), "dur": 0.28}
        self.app.animate(0.28, lambda _t: self.draw_table(), done=self._fly_done)

    def _fly_done(self) -> None:
        self._fly = None
        try:
            self.draw_table()
        except Exception:
            pass

    def flash_direction(self) -> None:
        def step(t: float) -> None:
            self._dir_glow = 1.0 - t
            try:
                self.draw_table()
            except Exception:
                pass

        def done() -> None:
            self._dir_glow = 0.0
            try:
                self.draw_table()
            except Exception:
                pass

        self.app.animate(0.55, step, done=done)

    def start_deal(self) -> None:
        self._deal_t0 = time.monotonic()
        self.app.animate(0.62, lambda _t: self.draw_hand())

    def tick_anim(self, now: float) -> None:
        snap = self.snap
        if not snap:
            return
        window = snap.get("uno_window")
        chip = self._uno_chip
        if window and chip and chip.get("pid") == window.get("pid"):
            remain = self._remain(window)
            try:
                self.table.itemconfigure(chip["text"], text=f"UNO {remain:.1f}")
            except tk.TclError:
                self._uno_chip = None
        if window and self._me() is not None:
            remain = self._remain(window)
            if window.get("pid") == self._me():
                self._set_hint(i18n.t("game_uno_press", self.app.lang, s=f"{remain:.1f}"))
            else:
                self._set_hint(i18n.t("game_uno_catch", self.app.lang, s=f"{remain:.1f}"))
        elif snap.get("phase") == "playing":
            self._set_hint(self._default_hint())
        timer = snap.get("turn_timer")
        if timer and snap.get("phase") == "playing":
            left = self._remain(timer)
            total = max(1.0, float(timer.get("total") or 1.0))
            color = DANGER if left <= 5 else (ACCENT if left / total < 0.4 else ACCENT2)
            self.turn_banner.set_timer(str(int(math.ceil(left))), left / total, color)
        else:
            self.turn_banner.set_timer(None)
        k = 0.5 + 0.5 * math.sin(now * 4.4)
        if snap.get("phase") == "playing" and snap.get("turn") == self._me():
            try:
                self.turn_banner.configure(bg=_mix(ACCENT, "#ffeaa8", k * 0.55))
            except tk.TclError:
                pass
        if self._fx:
            self._draw_fx(now)
        if self._confetti:
            self._draw_confetti(now)
        if self._reacts:
            self._draw_reacts(now)
        # the direction arrow turns slowly
        if self._dir_pos and not self.overlay_visible and now - self._dir_t >= 0.07:
            self._dir_t = now
            try:
                self._draw_dir(self.table, now)
            except tk.TclError:
                pass
        # the draw pile bobs when it is your turn to draw
        want = 0.0
        if (snap.get("phase") == "playing" and snap.get("turn") == self._me()
                and not snap.get("drawn") and not snap.get("chooser") and not self.overlay_visible):
            want = -5.0 * (0.5 + 0.5 * math.sin(now * 4.5))
        if self._pile_items and abs(want - self._pile_dy) > 0.2:
            try:
                for item in self._pile_items:
                    self.table.move(item, 0, want - self._pile_dy)
                self._pile_dy = want
            except tk.TclError:
                self._pile_items = []
        # gold hint line while something is expected of you
        window_now = snap.get("uno_window")
        hot = bool(snap.get("phase") == "playing" and self._me() is not None
                   and (snap.get("turn") == self._me() or (window_now and not window_now.get("said"))))
        if hot != self._hint_hot:
            self._hint_hot = hot
            self.hint_label.configure(fg=ACCENT if hot else MUTED, font=ui_font(12, hot))
        # turn rings breathe
        for rid, small, col in self._turn_rings:
            try:
                self.table.itemconfigure(
                    rid, outline=_mix(col, "#ffffff", 0.08 + 0.38 * k),
                    width=2 + (0.8 if small else 1.2) * k)
            except tk.TclError:
                pass
        # discard pile pops when a card lands, glows softly on your turn
        if self._disc_ring is not None:
            try:
                if self._disc_t0 and now - self._disc_t0 < 0.55:
                    t = (now - self._disc_t0) / 0.55
                    self.table.itemconfigure(self._disc_ring,
                                             outline=_mix(ACCENT2, "#ffffff", 0.75 * t),
                                             width=int(2 + 7 * (1 - t)))
                else:
                    self.table.itemconfigure(self._disc_ring, outline="")
            except tk.TclError:
                self._disc_ring = None
        # colour chip flash after a wild
        if self._chip_rect is not None and self._chip_color and self._chip_glow > 0:
            self._chip_glow = max(0.0, self._chip_glow - 0.05)
            try:
                self.table.itemconfigure(
                    self._chip_rect,
                    fill=_mix(COLOR_HEX.get(self._chip_color, "#666"), "#ffffff",
                              0.5 * self._chip_glow))
            except tk.TclError:
                self._chip_rect = None
        # pending penalty bubble pulses
        if self._pending_shape is not None and self._pending_glow > 0:
            self._pending_glow = max(0.0, self._pending_glow - 0.02)
            try:
                self.table.itemconfigure(self._pending_shape,
                                         fill=_mix(DANGER, "#ffa3a3", 0.30 * k))
            except tk.TclError:
                self._pending_shape = None
        # score / hand-count bumps in the scoreboard
        expired = []
        for pid, until in list(self._bump.items()):
            item = self._sb.get(pid)
            if item is None:
                continue
            if now < until:
                glow = max(0.0, (until - now) / 0.8)
                item["count"].configure(fg=_mix(MUTED, "#ffffff", 0.6 * glow))
                item["score"].configure(fg=_mix(ACCENT2, "#ffffff", 0.7 * glow))
            else:
                item["count"].configure(fg=MUTED)
                item["score"].configure(fg=ACCENT2)
                expired.append(pid)
        for pid in expired:
            self._bump.pop(pid, None)

    def _default_hint(self) -> str:
        lang = self.app.lang
        snap = self.snap
        if snap.get("phase") != "playing":
            return ""
        if snap.get("drawn"):
            return i18n.t("game_drawn_hint", lang, card=i18n.card_text(snap.get("drawn"), lang))
        if snap.get("turn") == self._me():
            return i18n.t("game_click_to_play", lang)
        return i18n.t("game_not_your_turn", lang)

    # ------------------------------------------------------------- buttons
    def _update_buttons(self) -> None:
        lang = self.app.lang
        snap = self.snap
        me = self._me()
        playing = snap.get("phase") == "playing"
        my_turn = playing and snap.get("turn") == me
        drawn = snap.get("drawn")
        window = snap.get("uno_window")
        pending = snap.get("pending") or 0
        if me is None:
            for btn in (self.btn_draw, self.btn_pass, self.btn_uno, self.btn_catch):
                btn.set_enabled(False)
            self.hand_label.configure(text=i18n.t("game_spectating", lang))
            self._set_hint("")
            self.status.configure(text=i18n.t("game_spectating", lang))
            return
        self.btn_draw.set_enabled(bool(my_turn and not drawn and not snap.get("chooser")))
        self.btn_pass.set_enabled(bool(my_turn and drawn))
        self.btn_uno.set_enabled(bool(window and window.get("pid") == me
                                      and not window.get("said")))
        self.btn_catch.set_enabled(bool(window and window.get("pid") != me
                                        and not window.get("said")))
        self._set_hint(self._default_hint())
        self.status.configure(text="")

    # --------------------------------------------------------------- overlay
    def _clear_overlay(self) -> None:
        self._overlay_gen += 1          # cancels a slide-in that is still running
        for child in self.overlay.winfo_children():
            child.destroy()
        self.overlay.place_forget()
        was = self.overlay_visible
        self.overlay_visible = False
        if was:
            self.draw_table()

    def _overlay_begin(self) -> tk.Frame:
        for child in self.overlay.winfo_children():
            child.destroy()
        _holder, body = rounded_panel(self.overlay, bg=OV_BG, radius=20, pad=16)
        return body

    def _overlay_show(self) -> None:
        was_visible = self.overlay_visible
        self._overlay_gen += 1
        gen = self._overlay_gen
        self.overlay.place(relx=0.5, rely=0.5, anchor="center")
        self.overlay.lift()
        self.overlay_visible = True
        self.draw_table()
        if was_visible:
            return                      # already on screen: no second slide-in

        def step(t: float) -> None:
            if gen != self._overlay_gen or not self.overlay_visible:
                return
            try:
                self.overlay.place_configure(relx=0.5, rely=0.5,
                                             y=int((1 - ease_out(t)) * 18),
                                             anchor="center")
            except tk.TclError:
                pass

        self.app.animate(0.18, step)

    def _update_overlay(self) -> None:
        snap = self.snap
        phase = snap.get("phase")
        if phase in ("round_over", "match_over"):
            key = (phase, snap.get("round_no"))
            if key != self._conf_key:
                self._conf_key = key
                self.start_confetti(big=phase == "match_over")
            self._show_end_overlay()
        elif (snap.get("chooser") or {}).get("pid") == self._me():
            self._show_swap_overlay()
        elif self.app.picker:
            self._show_picker_overlay()
        else:
            self._conf_key = None
            self._clear_overlay()

    def _show_end_overlay(self) -> None:
        lang = self.app.lang
        snap = self.snap
        body = self._overlay_begin()
        title = (i18n.t("game_match_over", lang) if snap.get("phase") == "match_over"
                 else i18n.t("game_round_over", lang, n=snap.get("round_no", 1)))
        tk.Label(body, text=title, bg=OV_BG, fg=ACCENT,
                 font=ui_font(24, True)).pack(pady=(0, 4))
        winner = (snap.get("match_winner") if snap.get("phase") == "match_over"
                  else snap.get("round_winner"))
        if winner is not None:
            text = (i18n.t("game_match_winner", lang, name=self._pname(winner))
                    if snap.get("phase") == "match_over"
                    else i18n.t("game_round_winner", lang, name=self._pname(winner)))
            tk.Label(body, text=text, bg=OV_BG, fg=INK, font=ui_font(13)).pack(pady=(0, 8))
        rows = tk.Frame(body, bg=OV_BG)
        rows.pack(fill="x")
        round_points = snap.get("round_points") or {}

        def _points(pid: int) -> int:
            # JSON object keys arrive as strings over the wire
            return int(round_points.get(pid, round_points.get(str(pid), 0)) or 0)

        players = sorted(self._players(), key=lambda q: -q.get("score", 0))
        compact = len(players) > 7
        tk.Label(rows, text=f"{i18n.t('game_score_short', lang)}   {i18n.t('game_round_points', lang)}",
                 bg=OV_BG, fg=MUTED, font=ui_font(9), anchor="e").pack(fill="x", pady=(0, 2))
        for p in players:
            row = tk.Frame(rows, bg=OV_BG)
            row.pack(fill="x", pady=0 if compact else 1)
            pid = p.get("pid")
            if pid == winner:
                delta, dcolor = f"+{p.get('round_score', 0)}", ACCENT2
            else:
                delta = f"-{_points(p.get('pid'))}" if _points(p.get("pid")) else "0"
                dcolor = "#ff8b8b"
            seat_color = SEAT_COLORS[(pid or 0) % len(SEAT_COLORS)]
            make_avatar(row, self.app, self.app.avatar_for(pid), p.get("name", "?"), seat_color,
                        20 if compact else 26, OV_BG).pack(side="left", padx=(0, 8))
            tk.Label(row, text=p.get("name", "?"), bg=OV_BG, fg=INK, width=11 if compact else 13,
                     anchor="w", font=ui_font(11 if compact else 12, True)).pack(side="left")
            if p.get("wins"):
                tk.Label(row, text=f"★{p['wins']}", bg=OV_BG, fg=GOLD,
                         font=ui_font(9)).pack(side="left", padx=(0, 6))
            tk.Label(row, text=f"{p.get('score', 0)}", bg=OV_BG, fg=ACCENT2,
                     width=5 if compact else 6, anchor="e",
                     font=mono_font(11 if compact else 12)).pack(side="left")
            tk.Label(row, text=delta, bg=OV_BG, fg=dcolor,
                     width=5 if compact else 6, anchor="e",
                     font=mono_font(10 if compact else 11)).pack(side="left")
        btns = tk.Frame(body, bg=OV_BG)
        btns.pack(fill="x", pady=(12, 0))
        if self.app.is_host():
            if snap.get("phase") == "match_over":
                FlatButton(btns, i18n.t("game_btn_rematch", lang),
                           lambda: self.app.send({"t": "rematch"}),
                           bg=ACCENT, fg="#231a03", padx=18, pady=8,
                           font=ui_font(13, True)).pack(fill="x")
            else:
                FlatButton(btns, i18n.t("game_btn_next_round", lang),
                           lambda: self.app.send({"t": "next_round"}),
                           bg=ACCENT2, fg="#02160c", padx=18, pady=8,
                           font=ui_font(13, True)).pack(fill="x")
        else:
            tk.Label(btns,
                     text=i18n.t("game_wait_rematch", lang) if snap.get("phase") == "match_over"
                     else i18n.t("game_wait_next_round", lang),
                     bg=OV_BG, fg=MUTED, font=ui_font(12)).pack()
        self._overlay_show()

    def _show_swap_overlay(self) -> None:
        lang = self.app.lang
        me = self._me()
        body = self._overlay_begin()
        tk.Label(body, text=i18n.t("game_swap_prompt", lang), bg=OV_BG, fg=INK,
                 font=ui_font(15, True)).pack(pady=(2, 10))
        for p in [q for q in self._players() if q.get("pid") != me]:
            FlatButton(body,
                       f"{p.get('name', '?')}  ({i18n.t('game_cards_n', lang, n=p.get('count', 0))})",
                       lambda pid=p.get("pid"): self.app.send({"t": "swap", "target": pid}),
                       bg=PANEL2, fg=INK, padx=16, pady=10, radius=14,
                       font=ui_font(13, True)).pack(fill="x", pady=3)
        timeout = (self.snap.get("rules") or {}).get("choice_timeout", 12)
        tk.Label(body, text=i18n.t("game_swap_cancel", lang, n=timeout), bg=OV_BG,
                 fg=MUTED, font=ui_font(10)).pack(pady=(8, 0))
        self._overlay_show()

    def _show_picker_overlay(self) -> None:
        lang = self.app.lang
        body = self._overlay_begin()
        tk.Label(body, text=i18n.t("game_color_prompt", lang), bg=OV_BG, fg=INK,
                 font=ui_font(15, True)).pack(pady=(2, 12))
        grid = tk.Frame(body, bg=OV_BG)
        grid.pack()
        for i, color in enumerate(("R", "Y", "G", "B")):
            FlatButton(grid, i18n.color_text(color, lang),
                       lambda c=color: self.app.choose_color(c),
                       bg=COLOR_HEX[color], fg="#101c22", width=150, padx=18, pady=22,
                       radius=18, font=ui_font(15, True)).grid(row=i // 2, column=i % 2, padx=5, pady=5)
        FlatButton(body, i18n.t("cancel", lang), self.app.cancel_picker, bg=PANEL2,
                   fg=MUTED, padx=12, pady=6, font=ui_font(11)).pack(pady=(14, 0))
        self._overlay_show()

    # ---------------------------------------------------------------- log
    def event_lines(self, event: dict[str, Any]) -> str | None:
        lang = self.app.lang
        t = event.get("t")
        pid = event.get("pid")
        name = self._pname(pid) if pid is not None else "?"
        if t == "round_start":
            return i18n.t("log_round_start", lang,
                          n=event.get("round_no") or self.snap.get("round_no", 1),
                          name=self._pname(event.get("first")))
        if t == "play":
            card = event.get("card")
            if card and is_wild(card):
                return i18n.t("log_play_pick", lang, name=name,
                              card=i18n.card_text(card, lang),
                              color=i18n.color_text(event.get("color"), lang))
            return i18n.t("log_play", lang, name=name, card=i18n.card_text(card, lang))
        if t == "draw":
            n = event.get("n", 1)
            if event.get("reason") == "start":
                return i18n.t("log_draw", lang, name=name, n=n)
            return i18n.t("log_draw", lang, name=name, n=n) if n > 1 else i18n.t("log_draw_one", lang, name=name)
        if t == "skip":
            return i18n.t("log_skip", lang, name=name)
        if t == "reverse":
            return i18n.t("log_reverse", lang)
        if t == "pending":
            return i18n.t("log_pending", lang, n=event.get("n", 0))
        if t == "uno_call":
            return i18n.t("log_uno", lang, name=name)
        if t == "uno_safe":
            return i18n.t("log_uno_safe", lang, name=name)
        if t == "catch":
            return i18n.t("log_catch", lang, name=self._pname(event.get("by")),
                          target=self._pname(event.get("pid")), n=event.get("n", 2))
        if t == "swap":
            return i18n.t("log_swap", lang, name=name, target=self._pname(event.get("target")))
        if t == "rotate":
            return i18n.t("log_rotate", lang)
        if t == "pass":
            return i18n.t("log_pass", lang, name=name)
        if t == "turn_timeout":
            return i18n.t("log_timeout", lang, name=name)
        if t == "round_end":
            return i18n.t("log_round_end", lang, name=self._pname(event.get("winner")),
                          n=event.get("points", 0))
        if t == "match_end":
            return i18n.t("log_match_end", lang, name=self._pname(event.get("winner")))
        if t == "may_play":
            return None
        return None

    def _render_log(self) -> None:
        events = self.app.log_events
        if not events:
            if not getattr(self, "_log_placeholder", False):
                self.log.configure(state="normal")
                self.log.insert("end", i18n.t("log_empty", self.app.lang))
                self.log.configure(state="disabled")
                self._log_placeholder = True
            return
        if getattr(self, "_log_placeholder", False):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self._log_placeholder = False
        if len(events) <= self._log_rendered:
            return
        self.log.configure(state="normal")
        for ev in events[self._log_rendered:]:
            line = self.event_lines(ev)
            if line:
                self.log.insert("end", line + "\n")
        self._log_rendered = len(events)
        self.log.see("end")
        self.log.configure(state="disabled")

    def append_chat(self, msg: dict[str, Any]) -> None:
        self.chat.append(msg, self.app.lang)


class UnoApp(tk.Tk):
    def __init__(self, name: str = "", lang: str | None = None, host_port: int = 45872) -> None:
        super().__init__()
        # Layout sizes, collapsed columns and the language live on disk so the
        # window looks the same after a restart.
        self.prefs = prefs.Prefs()
        self.lang = lang or str(self.prefs.get("lang") or "") or i18n.detect_lang()
        if self.lang not in ("zh", "en"):
            self.lang = i18n.detect_lang()
        saved_name = str(self.prefs.get("name") or "").strip()
        self.player_name = (name or saved_name or self._default_name()).strip()[:16]
        try:
            lanip.lan_addresses(refresh=True)      # the one place we ask the OS (main thread)
        except Exception:
            pass
        try:
            log_path = Path(self.prefs.path).parent / "hang.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.exists() and log_path.stat().st_size > 200_000:
                log_path.unlink()
            self._hang_log = open(log_path, "a", encoding="utf-8")
            faulthandler.enable(file=self._hang_log, all_threads=True)
            self._hang_log.write(f"\n=== session start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                                 f"pid={os.getpid()} python={sys.version.split()[0]} "
                                 f"tk={tk.TkVersion} ===\n")
            self._hang_log.flush()
        except Exception:
            self._hang_log = None
        self.no_emoji = os.environ.get("UNO_NO_EMOJI") == "1"   # --no-emoji: no emoji-font reactions on this table
        self._last_pump = time.monotonic()
        self._modal_depth = 0                    # >0 while a native dialog (file picker) is open
        self._start_watchdog()
        self.sec_password = ""
        self.sec_approval = False
        self._join_cards: dict[str, tk.Frame] = {}
        self._name_save_job: Any = None
        self._rename_old: str | None = None
        self._quiet_name = False
        self.host_port = host_port
        self.discovery = True
        self.title(i18n.t("app_title", self.lang))
        self.geometry("1280x840+60+30")
        self.minsize(1040, 780)
        self.configure(bg=BG)
        self.assets = card_assets.CardAssets(self)
        # avatars: mine (from disk) and the other seats' (fetched over the LAN)
        self.my_avatar: avatars.AvatarGrid | None = None
        self.my_avatar_b64 = ""
        self.my_avatar_ver = ""
        self.avatars: dict[int, tuple[str, avatars.AvatarGrid]] = {}
        self._av_requested: dict[int, str] = {}
        self._av_pushed: set[tuple[Any, str]] = set()
        self._load_saved_avatar()

        self.session: Any = None
        self.host_session: Any = None
        self.web_enabled = True               # --no-web switches the browser version off
        self.web_port = webserver.DEFAULT_WEB_PORT
        self.web_server: Any = None
        self.web_urls: list[str] = []
        self.my_pid: int | None = None
        self.room: dict[str, Any] | None = None
        self.snap: dict[str, Any] | None = None
        self.chat_history: list[dict[str, Any]] = []
        self.log_events: list[dict[str, Any]] = []
        self.selected_index: int | None = None
        self.picker: dict[str, Any] | None = None
        self._light_t = 0.0
        self.toast = Toast(self)
        self._busy = False
        self._ui_queue: "queue.Queue[tuple[Any, tuple[Any, ...]]]" = queue.Queue()
        self._anims: list[list[Any]] = []

        self.menu: MenuFrame | None = None
        self.lobby: LobbyFrame | None = None
        self.game: GameFrame | None = None
        self._build_screens()
        self.show("menu")
        self.bind_all("<KeyPress>", self._on_key)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, self._pump)

    # ------------------------------------------------------------ screen mgmt
    def _build_screens(self) -> None:
        for frame in (self.menu, self.lobby, self.game):
            if frame is not None:
                frame.destroy()
        self.menu = MenuFrame(self)
        self.menu.place(x=0, y=0, relwidth=1, relheight=1)
        self.lobby = LobbyFrame(self)
        self.lobby.place(x=0, y=0, relwidth=1, relheight=1)
        self.game = GameFrame(self)
        self.game.place(x=0, y=0, relwidth=1, relheight=1)
        self._screen = "menu"

    def _on_key(self, event: "tk.Event") -> None:
        focus = self.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text)):
            return
        if getattr(self, "_screen", "") != "game" or self.game is None:
            return
        key = (event.keysym or "").lower()
        if key not in ("d", "p", "u", "c", "escape"):
            # input methods / odd layouts: fall back to the typed character, then the physical key
            char = (getattr(event, "char", "") or "").lower()
            code = (event.keycode or 0) & 0xFF
            key = char if char in ("d", "p", "u", "c") else {2: "d", 35: "p", 32: "u", 8: "c"}.get(code, key)
        if key == "d":
            self.do_draw()
        elif key == "p":
            self.do_pass()
        elif key == "u":
            self.do_uno()
        elif key == "c":
            self.do_catch()
        elif key == "escape" and self.picker:
            self.picker = None
            self.game._update_overlay()

    def show(self, name: str) -> None:
        self._screen = name
        frame = {"menu": self.menu, "lobby": self.lobby, "game": self.game}[name]
        assert frame is not None
        frame.tkraise()
        if name == "lobby":
            assert self.lobby is not None
            self.lobby.refresh()
            self.lobby.chat.rebuild(self.chat_history, self.lang)
        elif name == "game":
            assert self.game is not None
            self.focus_set()                  # so the D / P / U / C keys work right away
            self.game.refresh()
            # lobby chat lines typed before the game started belong here too
            self.game.chat.rebuild(self.chat_history, self.lang)

    def refresh(self) -> None:
        if self._screen == "lobby" and self.lobby:
            self.lobby.refresh()
        elif self._screen == "game" and self.game:
            self.game.refresh()

    # --------------------------------------------------------------- avatars
    def _avatar_path(self) -> Path:
        return Path(self.prefs.path).parent / "avatar.png"

    def _load_saved_avatar(self) -> None:
        if not self.prefs.get("avatar"):
            return
        try:
            grid = avatars.grid_from_file(self, str(self._avatar_path()))
            self._install_my_avatar(grid)
        except Exception:
            self.my_avatar = None

    def _install_my_avatar(self, grid: "avatars.AvatarGrid | None") -> None:
        self.my_avatar = grid
        self.my_avatar_b64 = grid.to_b64(self) if grid is not None else ""
        self.my_avatar_ver = ("%08x" % zlib.crc32(self.my_avatar_b64.encode("ascii"))
                              if self.my_avatar_b64 else "")

    def set_my_avatar(self, path: str) -> None:
        """Read a photo, store it, show it everywhere and push it to the room."""
        grid = avatars.grid_from_file(self, path)
        self._install_my_avatar(grid)
        try:
            target = self._avatar_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(grid.to_png_bytes(self))
            self.prefs.set("avatar", target.name)
            self.prefs.save()
        except OSError:
            pass
        self._after_my_avatar_changed()

    def clear_my_avatar(self) -> None:
        self._install_my_avatar(None)
        try:
            self._avatar_path().unlink()
        except OSError:
            pass
        self.prefs.set("avatar", "")
        self.prefs.save()
        self._after_my_avatar_changed()

    def _after_my_avatar_changed(self) -> None:
        self._av_pushed.discard((self.my_pid, self.my_avatar_ver))
        self._sync_avatars(force_push=True)
        self._avatars_changed()

    def avatar_for(self, pid: Any) -> "avatars.AvatarGrid | None":
        if not isinstance(pid, int):
            return None
        if pid == self.my_pid and self.my_avatar is not None:
            return self.my_avatar
        entry = self.avatars.get(pid)
        return entry[1] if entry else None

    def _sync_avatars(self, force_push: bool = False) -> None:
        """Push my avatar when the room lacks it; fetch the ones I am missing."""
        if self.session is None or not self.room:
            return
        want: list[int] = []
        dropped = False
        for seat in self.room.get("seats") or []:
            pid = seat.get("pid")
            ver = seat.get("av") or ""
            if not isinstance(pid, int):
                continue
            if pid == self.my_pid:
                key = (pid, self.my_avatar_ver)
                if ver != self.my_avatar_ver and key not in self._av_pushed:
                    self._av_pushed.add(key)
                    self.send({"t": "avatar", "data": self.my_avatar_b64})
                continue
            if not ver:
                if self.avatars.pop(pid, None) is not None:
                    dropped = True
                self._av_requested.pop(pid, None)
            elif pid not in self.avatars or self.avatars[pid][0] != ver:
                if self._av_requested.get(pid) != ver:
                    self._av_requested[pid] = ver
                    want.append(pid)
        if want:
            self.send({"t": "avatar_get", "pids": want})
        if dropped:
            self._avatars_changed()

    def _avatars_changed(self) -> None:
        self.refresh()
        for frame in (self.game, self.lobby):
            if frame is not None:
                try:
                    frame.chat.rebuild(self.chat_history, self.lang)
                except Exception:
                    pass
        if self.menu is not None:
            self.menu.update_avatar()

    # --------------------------------------------------------------- actions
    def toggle_lang(self) -> None:
        self.lang = "en" if self.lang == "zh" else "zh"
        self.prefs.set("lang", self.lang)
        self.prefs.save()
        self.title(i18n.t("app_title", self.lang))
        keep = self._screen
        self._build_screens()
        self.show(keep)
        self.toast.show("English" if self.lang == "en" else "中文")

    def close(self) -> None:
        """Persist the UI state and quit (used by the window close button)."""
        self._closing = True
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        try:
            self.prefs.set("lang", self.lang)
            self.prefs.save()
        finally:
            self._stop_web()
            self.destroy()

    def show_help(self) -> None:
        win = tk.Toplevel(self)
        win.title(i18n.t("help_title", self.lang))
        win.configure(bg=BG)
        tk.Label(win, text=i18n.t("help_title", self.lang), bg=BG, fg=INK,
                 font=ui_font(18, True)).pack(padx=24, pady=(20, 8))
        tk.Label(win, text=i18n.t("help_body", self.lang), bg=BG, fg=INK,
                 justify="left", font=ui_font(12), wraplength=520).pack(padx=24, pady=10)
        FlatButton(win, i18n.t("close", self.lang), win.destroy, bg=ACCENT,
                   fg="#231a03", padx=18, pady=8).pack(pady=(4, 20))

    # ------------------------------------------------------------- room security
    def set_security(self, password: str | None = None, approval: bool | None = None) -> None:
        """Host: set the room password / switch join approval (kept for the next rooms too)."""
        msg: dict[str, Any] = {"t": "security"}
        if password is not None:
            self.sec_password = " ".join(str(password).split())[:24]
            msg["password"] = self.sec_password
        if approval is not None:
            self.sec_approval = bool(approval)
            msg["approval"] = self.sec_approval
        if self.session is not None and self.host_session is not None:
            self.send(msg)

    def _show_join_request(self, jid: str, name: str, spectate: bool) -> None:
        card = tk.Frame(self, bg=PANEL, highlightthickness=2, highlightbackground=ACCENT)
        text = sx(self, "wants_watch" if spectate else "wants_join", name=name)
        tk.Label(card, text="✋  " + text, bg=PANEL, fg=INK, font=ui_font(13, True)).pack(padx=16, pady=(12, 8))
        row = tk.Frame(card, bg=PANEL)
        row.pack(padx=14, pady=(0, 12))

        def answer(allow: bool) -> None:
            self.send({"t": "join_answer", "id": jid, "allow": allow})
            self._drop_join_card(jid)

        FlatButton(row, sx(self, "allow"), lambda: answer(True), bg=ACCENT2, fg="#02160c", width=96,
                   pady=7, radius=12, font=ui_font(12, True)).pack(side="left", padx=4)
        FlatButton(row, sx(self, "deny"), lambda: answer(False), bg=DANGER, fg="#250707", width=96,
                   pady=7, radius=12, font=ui_font(12, True)).pack(side="left", padx=4)
        self._join_cards[jid] = card
        self._layout_join_cards()
        try:
            self.bell()
        except tk.TclError:
            pass

    def _drop_join_card(self, jid: str) -> None:
        card = self._join_cards.pop(jid, None)
        if card is not None:
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._layout_join_cards()

    def _layout_join_cards(self) -> None:
        for i, card in enumerate(self._join_cards.values()):
            card.place(relx=1.0, x=-18, y=64 + i * 96, anchor="ne")
            card.lift()

    def ask_password(self, host: str, port: int, spectate: bool, wrong: bool = False) -> None:
        PasswordDialog(self, host, port, spectate, wrong)

    # ---------------------------------------------------------------- player name
    def _default_name(self) -> str:
        """First launch: the computer's user name (Kyle), else 'Player'."""
        try:
            import getpass

            user = getpass.getuser().strip()
        except Exception:
            user = ""
        if user and user.isascii() and user.replace("_", "").replace(".", "").isalnum():
            return (user[:1].upper() + user[1:])[:16]
        return i18n.t("name_default", self.lang)

    def remember_name(self, value: str) -> None:
        """What was typed in a name field: use it now, keep it for next time."""
        if self._quiet_name:
            return
        value = " ".join(str(value).split())[:16]
        if not value:
            return
        self.player_name = value
        self.prefs.set("name", value)
        if self._name_save_job is not None:
            try:
                self.after_cancel(self._name_save_job)
            except Exception:
                pass
        self._name_save_job = self.after(600, self.prefs.save)

    def rename_me(self, value: str) -> None:
        """Change the name at the table (works in the lobby and during a game)."""
        value = " ".join(str(value).split())[:16]
        if not value or value == self.player_name:
            return
        self._rename_old = self.player_name
        self.remember_name(value)
        if self.session is not None:
            self.send({"t": "name", "name": value})
        if self.menu is not None and hasattr(self.menu, "name_var"):
            self._quiet_name = True
            try:
                self.menu.name_var.set(value)
            finally:
                self._quiet_name = False

    def host_room(self) -> None:
        if self.host_session is not None:
            self.toast.show(i18n.t("lobby_host_label", self.lang))
            self.show("lobby")
            return
        self.toast.show(i18n.t("loading", self.lang))

        def run() -> None:
            err: Exception | None = None
            last_port = self.host_port
            for offset in range(0, 12):
                port = self.host_port + offset
                try:
                    from net import HostSession  # type: ignore

                    room_name = f"{self.player_name} 的 UNO 房間" if self.lang == "zh" else f"{self.player_name}'s UNO room"
                    hs = HostSession(self.player_name, port=port, room_name=room_name,
                                     discovery=self.discovery, lang=self.lang)
                    welcome = hs.start()
                    self._ui(lambda h=hs, w=welcome: self._host_ready(h, w))
                    return
                except OSError as exc:
                    err = exc
                    last_port = port
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    err = exc
                    break
            msg = str(err) if err else "port busy"
            self._ui(lambda m=msg: self.toast.show(
                i18n.t("net_host_failed", self.lang, msg=m), danger=True))

        threading.Thread(target=run, daemon=True).start()

    def _host_ready(self, hs: Any, welcome: dict[str, Any]) -> None:
        self.host_session = hs
        self.session = hs.client
        self.my_pid = 0
        self.room = welcome.get("room")
        self._start_web(hs)
        self._reset_views()
        self._handle(welcome)
        if self.sec_password or self.sec_approval:        # keep the settings for this new room
            self.send({"t": "security", "password": self.sec_password, "approval": self.sec_approval})
        if self._screen != "game":
            self.show("lobby")
        self.toast.show(f"{i18n.t('lobby_host_label', self.lang)} · {hs.server.port if hasattr(hs, 'server') else ''}")

    # ------------------------------------------------------------ web version
    def _maybe_waiting(self, client: Any) -> None:
        if getattr(client, "waiting", False) and getattr(client, "welcome", None) is None:
            self.toast.show(sx(self, "waiting"), ms=5000)

    def _start_web(self, hs: Any) -> None:
        """Serve the browser version of this room (phones, no install needed)."""
        self._stop_web()
        if not self.web_enabled:
            return
        try:
            server = hs.server
            ws = webserver.WebServer(server.port, self.web_port,
                                     cards_dir=card_assets.DEFAULT_CARDS_DIR,
                                     info=lambda: server._room_dict())
            ws.start()
        except Exception as exc:
            self.web_server = None
            self.web_urls = []
            self.toast.show(wt(self, "failed", msg=str(exc)), danger=True, ms=4000)
            return
        self.web_server = ws
        self.web_urls = [f"http://{ip}:{ws.port}/" for ip in webserver.local_ips()]

    def _stop_web(self) -> None:
        if self.web_server is not None:
            try:
                self.web_server.close()
            except Exception:
                pass
        self.web_server = None
        self.web_urls = []

    def show_web_info(self) -> None:
        if self.web_server is None or not self.web_urls:
            self.toast.show(wt(self, "off"))
            return
        WebInfoDialog(self)

    def open_browser(self) -> None:
        self.show("lobby")
        self.scan_rooms()

    def scan_rooms(self) -> None:
        if self.session is not None:
            return
        if self._busy:
            return
        self._busy = True
        if self.lobby:
            self.lobby.status.configure(text=i18n.t("lobby_searching", self.lang))

        def run() -> None:
            try:
                import net  # type: ignore

                rooms = net.discover_rooms(timeout=0.9)
            except Exception:
                rooms = []
            self._ui(lambda r=rooms: self._rooms_ready(r))

        threading.Thread(target=run, daemon=True).start()

    def _rooms_ready(self, rooms: list[dict[str, Any]]) -> None:
        self._busy = False
        if self.lobby:
            self.lobby.set_rooms(rooms)
            self.lobby.status.configure(text=f"{len(rooms)} room(s)")

    def join_room(self, host: str, port: int | None = None, spectate: bool = False,
                  password: str = "") -> None:
        if self.session is not None:
            self.toast.show(i18n.t("net_connected", self.lang))
            return
        host = (host or "").replace("http://", "").split(":")[0]
        if not host:
            return
        port = int(port or 45872)
        self.toast.show(i18n.t("net_connecting", self.lang))

        def run() -> None:
            try:
                import net  # type: ignore

                base = (self.player_name or "Player").strip() or "Player"
                names = [base] + [f"{base} {i}" for i in range(2, 9)]
                last: Exception | None = None
                for cand in names:
                    client = net.NetClient(cand, spectate=spectate, password=password)
                    self._ui(lambda c=client: self.after(1500, lambda: self._maybe_waiting(c)))
                    try:
                        welcome = client.connect(host, port=port)
                    except Exception as exc:
                        last = exc
                        try:
                            client.close()
                        except Exception:
                            pass
                        if "name_taken" in str(exc):
                            continue
                        raise
                    self.player_name = cand
                    if cand != base:
                        self._ui(lambda n=cand: self.toast.show(
                            i18n.t("net_renamed", self.lang, name=n)))
                    self._ui(lambda c=client, w=welcome: self._joined(c, w))
                    return
                if last is not None:
                    raise last
            except Exception as exc:  # pragma: no cover - defensive
                msg = str(exc)
                code = msg.rsplit(": ", 1)[-1].strip()
                if code == "bad_password":
                    self._ui(lambda: self.ask_password(host, port, spectate, wrong=bool(password)))
                elif code in ("join_denied", "join_timeout", "not_lobby", "lobby_full"):
                    self._ui(lambda c=code: self.toast.show(i18n.error_text(c, self.lang), danger=True))
                elif isinstance(exc, TimeoutError):
                    self._ui(lambda: self.toast.show(i18n.error_text("join_timeout", self.lang), danger=True))
                else:
                    self._ui(lambda m=msg: self.toast.show(
                        i18n.t("net_error", self.lang, msg=m), danger=True))

        threading.Thread(target=run, daemon=True).start()

    def _joined(self, client: Any, welcome: dict[str, Any]) -> None:
        name = (welcome.get("name") or self.player_name or "").strip()
        if name:
            self.player_name = name
        if self.menu is not None and hasattr(self.menu, "name_var"):
            self._quiet_name = True               # a "Kyle 2" fix-up must not overwrite the saved name
            try:
                self.menu.name_var.set(self.player_name)
            except Exception:
                pass
            finally:
                self._quiet_name = False
        self.session = client
        self.my_pid = welcome.get("you")
        self.room = welcome.get("room")
        self._reset_views()
        self._handle(welcome)

    def _reset_views(self) -> None:
        self.snap = None
        self.selected_index = None
        self.picker = None
        self.log_events = []
        self.chat_history = []
        self.my_pid = self.my_pid
        self.avatars = {}
        self._av_requested = {}
        self._av_pushed = set()

    def leave_game(self) -> None:
        self.disconnect()
        self.show("menu")

    def back_to_menu(self) -> None:
        self.disconnect()
        self.show("menu")

    def disconnect(self) -> None:
        if self.session is not None:
            try:
                if hasattr(self.session, "send"):
                    self.session.send({"t": "leave"})
                self.session.close()
            except Exception:
                pass
        self._stop_web()
        if self.host_session is not None:
            try:
                self.host_session.close()
            except Exception:
                pass
        self.session = None
        self.host_session = None
        self.my_pid = None
        self.room = None
        self.snap = None
        self.picker = None
        self.selected_index = None

    # ------------------------------------------------------------- networking
    def send(self, msg: dict[str, Any]) -> None:
        if self.session is None:
            return
        try:
            self.session.send(msg)
        except Exception as exc:  # pragma: no cover
            self.toast.show(str(exc), danger=True)

    def is_host(self) -> bool:
        room = self.room or {}
        return self.my_pid is not None and self.my_pid == room.get("host_pid", -1)

    # --------------------------------------------------------------- intents
    def click_hand(self, idx: int) -> None:
        """Select a card; playing requires dragging it up onto the table."""
        snap = self.snap
        if not snap or snap.get("phase") != "playing":
            return
        hand = snap.get("hand") or []
        if idx < 0 or idx >= len(hand):
            return
        self.selected_index = idx
        if self.game:
            self.game.draw_hand()

    def play_card(self, card: str) -> None:
        snap = self.snap or {}
        legal = snap.get("legal") or []
        jumpable = snap.get("jumpable") or []
        my_turn = snap.get("turn") == self.my_pid
        if card in jumpable and not my_turn:
            self.send({"t": "play", "card": card, "jump": True})
            self.selected_index = None
            return
        if card in legal and is_wild(card) and not card.startswith("wild4"):
            self.picker = {"kind": "color", "card": card, "jump": False}
            self.selected_index = None
            if self.game:
                self.game._update_overlay()
            return
        if card in legal and is_wild(card):
            self.picker = {"kind": "color", "card": card, "jump": False}
            self.selected_index = None
            if self.game:
                self.game._update_overlay()
            return
        if card in legal:
            self.send({"t": "play", "card": card})
            self.selected_index = None
            return
        if card in jumpable:
            self.toast.show(i18n.t("game_not_your_turn", self.lang))
            return
        self.toast.show(i18n.error_text("illegal", self.lang), danger=True)

    def choose_color(self, color: str) -> None:
        picker = self.picker or {}
        card = picker.get("card")
        self.picker = None
        self.selected_index = None
        if card:
            self.send({"t": "play", "card": card, "color": color})
        if self.game:
            self.game.refresh()

    def cancel_picker(self) -> None:
        self.picker = None
        if self.game:
            self.game.refresh()

    def do_draw(self) -> None:
        self.send({"t": "draw"})

    def do_pass(self) -> None:
        self.send({"t": "pass"})

    def do_uno(self) -> None:
        self.send({"t": "uno"})

    def do_catch(self) -> None:
        self.send({"t": "catch"})

    # ------------------------------------------------------------- pump loop
    def _ui(self, fn: Any, *args: Any) -> None:
        """Call a UI function from a worker thread."""
        self._ui_queue.put((fn, args))

    def animate(self, duration: float, step: Callable[[float], None],
                done: Callable[[], None] | None = None,
                delay: float = 0.0) -> list[Any]:
        """Schedule a small eased animation ticked by the UI pump."""
        item: list[Any] = [time.monotonic() + delay, max(0.001, duration), step, done]
        self._anims.append(item)
        return item

    def _tick_anims(self) -> None:
        if not self._anims:
            return
        now = time.monotonic()
        keep: list[list[Any]] = []
        for item in self._anims:
            t0, dur, step, done = item
            t = (now - t0) / dur
            if t < 0:
                keep.append(item)
                continue
            t = min(1.0, t)
            try:
                step(ease_out(t))
            except tk.TclError:
                continue
            except Exception:
                continue
            if t >= 1.0:
                if done:
                    try:
                        done()
                    except Exception:
                        pass
            else:
                keep.append(item)
        self._anims = keep

    def _start_watchdog(self) -> None:
        """If the UI stops answering for 10 s, log the time and every thread's stack (once per stall)."""
        log = getattr(self, "_hang_log", None)
        if log is None:
            return

        def run() -> None:
            stuck: float | None = None              # the _last_pump value of the stall being reported
            while not getattr(self, "_closing", False):
                time.sleep(1.0)
                try:
                    if self._modal_depth > 0:       # a file picker is open: waiting is expected
                        stuck = None
                        continue
                    last = self._last_pump
                    stale = time.monotonic() - last
                    if stuck is None:
                        if stale > 10.0:
                            stuck = last
                            log.write(f"\n--- {time.strftime('%H:%M:%S')} UI stuck for {stale:.0f}s "
                                      f"(screen={getattr(self, '_screen', '?')}) ---\n")
                            faulthandler.dump_traceback(file=log, all_threads=True)
                            log.flush()
                    elif last != stuck:             # the main loop ran again
                        log.write(f"--- {time.strftime('%H:%M:%S')} UI answering again "
                                  f"after a stall of about {last - stuck:.0f}s ---\n")
                        log.flush()
                        stuck = None
                except Exception:
                    pass

        threading.Thread(target=run, name="uno-watchdog", daemon=True).start()

    def _pump(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._last_pump = time.monotonic()
        try:
            self._tick_anims()
            while True:
                try:
                    fn, args = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                fn(*args)
            session = self.session
            if session is not None:
                if hasattr(session, "tick"):
                    session.tick()
                alive = getattr(session, "alive", lambda: True)()
                for msg in session.poll():
                    self._handle(msg)
                if not alive and self.session is not None:
                    self.session = None
                    self.toast.show(i18n.t("net_disconnected", self.lang), danger=True, ms=3200)
                    self.show("menu")
            if self._screen == "game" and self.game is not None and self.snap:
                now = time.monotonic()
                self.game.tick_anim(now)
        except Exception as exc:  # pragma: no cover - keep the UI alive
            import traceback

            traceback.print_exc()
            try:
                self.toast.show(f"UI error: {exc}", danger=True, ms=4000)
            except tk.TclError:
                pass
        finally:
            if not getattr(self, "_closing", False):
                try:
                    self.after(50, self._pump)
                except tk.TclError:
                    pass

    def _handle(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == "welcome":
            self.my_pid = msg.get("you")
            self.room = msg.get("room") or self.room
            self.chat_history = list(msg.get("chat") or [])
            if msg.get("snap"):
                self.snap = msg["snap"]
                self._snap_rx = time.monotonic()
                self.show("game")
            else:
                self.show("lobby")
            if self.game:
                self.game.chat.rebuild(self.chat_history, self.lang)
            if self.lobby:
                self.lobby.chat.rebuild(self.chat_history, self.lang)
            self.refresh()
            self._sync_avatars()
        elif t == "room":
            self.room = msg.get("room") or self.room
            self.refresh()
            self._sync_avatars()
        elif t == "join_request":
            self._show_join_request(str(msg.get("id")), str(msg.get("name") or "?"), bool(msg.get("spectate")))
        elif t == "join_cancel":
            self._drop_join_card(str(msg.get("id")))
        elif t == "avatar":
            pid, data = msg.get("pid"), msg.get("data")
            grid = avatars.grid_from_b64(self, data) if isinstance(data, str) else None
            if isinstance(pid, int) and grid is not None:
                self.avatars[pid] = (str(msg.get("v") or ""), grid)
                self._avatars_changed()
        elif t == "state":
            snap = msg.get("snap") or self.snap
            seq = msg.get("seq", (snap or {}).get("event_seq"))
            if (self.snap or {}).get("event_seq") == seq and self.snap is not None \
                    and self._screen == "game":
                return
            self.snap = snap
            self._snap_rx = time.monotonic()
            if msg.get("room"):
                self.room = msg["room"]
                self._sync_avatars()
            if self._screen != "game":
                self.show("game")
            else:
                self.refresh()
        elif t == "events":
            items = msg.get("items") or []
            self.log_events.extend(items)
            if len(self.log_events) > 400:
                self.log_events = self.log_events[-300:]
                if self.game:
                    self.game._log_rendered = 0
            if self.game:
                self.game._render_log()
                for ev in items:
                    kind = ev.get("t")
                    if kind == "play" and ev.get("card"):
                        self.game.flash_play(ev["card"], ev.get("pid"))
                        self.game.flash_fx(ev)
                    elif kind == "uno_call":
                        self.game.flash_fx(ev)
                    elif kind == "draw":
                        self.game.flash_draw(ev.get("pid"))
                    elif kind == "round_start":
                        self.game.start_deal()
                    elif kind == "reverse":
                        self.game.flash_direction()
        elif t == "chat":
            item = {"pid": msg.get("pid"), "name": msg.get("name"),
                    "text": msg.get("text"), "ts": msg.get("ts")}
            self.chat_history.append(item)
            self.chat_history = self.chat_history[-50:]
            if self._screen == "game" and self.game:
                self.game.append_chat(item)
            elif self.lobby:
                self.lobby.append_chat(item)
        elif t == "react":
            if self._screen == "game" and self.game:
                self.game.add_react(msg)
        elif t == "error":
            code = msg.get("code", "")
            if code in ("name_taken", "bad_msg") and self._rename_old:
                self.remember_name(self._rename_old)
                self._rename_old = None
            self.toast.show(i18n.error_text(code, self.lang), danger=True)
        elif t == "kicked":
            self.disconnect()
            self.toast.show(i18n.t("game_kicked", self.lang), danger=True)
            self.show("menu")
        elif t == "pong" or t == "ack":
            pass


def run(name: str = "", lang: str | None = None) -> None:
    app = UnoApp(name=name, lang=lang)
    app.mainloop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UNO LAN UI")
    parser.add_argument("--name", default="")
    parser.add_argument("--lang", choices=i18n.LANGS, default=None)
    args = parser.parse_args()
    run(args.name, args.lang)
