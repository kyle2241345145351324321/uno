"""Player avatars for the UNO client (standard library + Tkinter only).

A photo is turned into a small square grid of pixels (``AvatarGrid``), which
can be

* saved as ``avatar.png`` next to the preferences file,
* sent over the LAN as a short base64 PNG (kept under ``MAX_B64`` bytes), and
* rendered as a *circular* ``tk.PhotoImage`` of any diameter (pixels outside
  the circle stay transparent, the UI draws a thin ring over the edge).

Reading photos: PNG / GIF work everywhere through Tk itself.  JPEG, HEIC, TIFF
and friends are converted with macOS ``sips`` when available, or with Pillow
when it happens to be installed; neither is required.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from typing import Any

STORE_SIZE = 64             # stored / transmitted avatar edge in pixels
MAX_B64 = 16 * 1024         # hard cap for the wire format (server enforces too)
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


class AvatarError(Exception):
    """The picture could not be read or is unusable."""


class AvatarGrid:
    """``STORE_SIZE`` x ``STORE_SIZE`` RGB pixels, row-major."""

    def __init__(self, rows: list[list[tuple[int, int, int]]]) -> None:
        self.rows = rows
        self.size = len(rows)
        self._circles: dict[int, tk.PhotoImage] = {}

    # ------------------------------------------------------------ rendering
    def circle(self, master: tk.Misc, diameter: int) -> tk.PhotoImage:
        """A circular PhotoImage (transparent outside the circle)."""
        diameter = max(8, int(diameter))
        cached = self._circles.get(diameter)
        if cached is not None:
            return cached
        img = tk.PhotoImage(master=master, width=diameter, height=diameter)
        n = self.size
        r = diameter / 2.0
        for y in range(diameter):
            dy = y + 0.5 - r
            span = r * r - dy * dy
            if span <= 0:
                continue
            half = span ** 0.5
            x0 = max(0, int(r - half + 0.5))
            x1 = min(diameter, int(r + half + 0.5))
            if x1 <= x0:
                continue
            sy = min(n - 1, int((y + 0.5) * n / diameter))
            row = self.rows[sy]
            cells = []
            for x in range(x0, x1):
                red, green, blue = row[min(n - 1, int((x + 0.5) * n / diameter))]
                cells.append(f"#{red:02x}{green:02x}{blue:02x}")
            img.put("{" + " ".join(cells) + "}", to=(x0, y))
        self._circles[diameter] = img
        return img

    # ------------------------------------------------------------ transport
    def to_png_bytes(self, master: tk.Misc) -> bytes:
        img = tk.PhotoImage(master=master, width=self.size, height=self.size)
        for y, row in enumerate(self.rows):
            cells = " ".join(f"#{r:02x}{g:02x}{b:02x}" for r, g, b in row)
            img.put("{" + cells + "}", to=(0, y))
        fd, tmp = tempfile.mkstemp(prefix="uno-avatar-", suffix=".png")
        os.close(fd)
        try:
            img.write(tmp, format="png")
            with open(tmp, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def to_b64(self, master: tk.Misc) -> str:
        data = base64.b64encode(self.to_png_bytes(master)).decode("ascii")
        if len(data) > MAX_B64:
            raise AvatarError("too_big")
        return data


# --------------------------------------------------------------------------- #
# decoding helpers
# --------------------------------------------------------------------------- #
def _pixel(img: tk.PhotoImage, x: int, y: int) -> tuple[int, int, int]:
    value: Any = img.get(x, y)
    if isinstance(value, str):
        value = value.split()
    return int(value[0]), int(value[1]), int(value[2])


def _grid_from_photo(img: tk.PhotoImage) -> AvatarGrid:
    """Centre-crop to a square and box-average down to STORE_SIZE."""
    w, h = img.width(), img.height()
    if w < 2 or h < 2:
        raise AvatarError("empty")
    side = min(w, h)
    ox, oy = (w - side) // 2, (h - side) // 2
    n = STORE_SIZE
    # keep the per-pixel Tcl calls bounded for big photos
    sub = max(1, side // (n * 3))
    if sub > 1:
        img = img.subsample(sub, sub)
        w, h = img.width(), img.height()
        side = min(w, h)
        ox, oy = (w - side) // 2, (h - side) // 2
    rows: list[list[tuple[int, int, int]]] = []
    for gy in range(n):
        y0 = oy + gy * side / n
        y1 = oy + (gy + 1) * side / n
        ys = sorted({min(h - 1, int(y0 + (y1 - y0) * f)) for f in (0.25, 0.75)})
        row: list[tuple[int, int, int]] = []
        for gx in range(n):
            x0 = ox + gx * side / n
            x1 = ox + (gx + 1) * side / n
            xs = sorted({min(w - 1, int(x0 + (x1 - x0) * f)) for f in (0.25, 0.75)})
            pts = [_pixel(img, x, y) for y in ys for x in xs]
            row.append((sum(p[0] for p in pts) // len(pts),
                        sum(p[1] for p in pts) // len(pts),
                        sum(p[2] for p in pts) // len(pts)))
        rows.append(row)
    return AvatarGrid(rows)


def _load_with_tk(master: tk.Misc, path: str) -> tk.PhotoImage | None:
    try:
        return tk.PhotoImage(master=master, file=path)
    except tk.TclError:
        return None


def _convert_to_png(path: str, out: str) -> bool:
    """Convert any common photo format to PNG (sips on macOS, else Pillow)."""
    if sys.platform == "darwin" and shutil.which("sips"):
        try:
            res = subprocess.run(
                ["sips", "-s", "format", "png", "-Z", "512", path, "--out", out],
                capture_output=True, timeout=30)
            if res.returncode == 0 and os.path.exists(out):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((512, 512))
            im.save(out, "PNG")
        return True
    except Exception:
        return False


def grid_from_file(master: tk.Misc, path: str) -> AvatarGrid:
    """Load any picture the machine can decode into an avatar grid."""
    if not os.path.isfile(path):
        raise AvatarError("missing")
    img = _load_with_tk(master, path)
    if img is None:
        fd, tmp = tempfile.mkstemp(prefix="uno-avatar-src-", suffix=".png")
        os.close(fd)
        try:
            if not _convert_to_png(path, tmp):
                raise AvatarError("unsupported")
            img = _load_with_tk(master, tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if img is None:
            raise AvatarError("unsupported")
    return _grid_from_photo(img)


def grid_from_b64(master: tk.Misc, data: str) -> AvatarGrid | None:
    """Decode a wire avatar; returns None for anything malformed."""
    if not isinstance(data, str) or not data or len(data) > MAX_B64 \
            or not _B64_RE.match(data):
        return None
    try:
        img = tk.PhotoImage(master=master, data=data)
        if img.width() < 2 or img.height() < 2 or img.width() > 512 or img.height() > 512:
            return None
        return _grid_from_photo(img)
    except (tk.TclError, ValueError):
        return None


def valid_wire_avatar(data: Any) -> bool:
    """Server-side check: base64 text that decodes to a small PNG."""
    if not isinstance(data, str) or not data or len(data) > MAX_B64:
        return False
    if not _B64_RE.match(data):
        return False
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return False
    return raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) <= MAX_B64
