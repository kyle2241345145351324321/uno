#!/usr/bin/env python3
"""Build UNO card sprites from the Inkscape deck sheet.

Source: UNO_cards_deck.svg (14 x 8 grid of 60x90 cards).
We render the sheet once with headless Chrome, slice the grid, keep one PNG per
unique face, and draw a matching card back.  Everything is deterministic and
checked: duplicate cells must be pixel-identical.

Usage:
    python3 tools/build_cards.py [--svg PATH] [--sheet PATH] [--cards DIR]

Runtime game code only needs cards/*.png + cards/manifest.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SVG = "/Users/kyle/Downloads/UNO_cards_deck.svg"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
]

# 14 columns x 8 rows, cell = 480x720 px at 2x device scale.
COLS, ROWS = 14, 8
CW, CH = 480, 720

COLORS = ["R", "Y", "G", "B"]
VALUE_COLS = {
    0: "0", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
    8: "8", 9: "9", 10: "skip", 11: "reverse", 12: "draw2",
}
BACK_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"


def render_sheet(svg: str, out_png: str) -> None:
    exe = next((c for c in CHROME_CANDIDATES if shutil.which(c) or os.path.exists(c)), None)
    if exe is None:
        sys.exit("No Chrome/Chromium found; pass --sheet with an existing render.")
    with tempfile.TemporaryDirectory() as td:
        cmd = [
            exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=2",
            "--window-size=3362,2882", "--default-background-color=00000000",
            f"--screenshot={out_png}", f"file://{os.path.abspath(svg)}",
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=td)


def grid_face(c: int, r: int) -> str | None:
    """Semantic card code for a grid cell, or None for empty cells."""
    color = COLORS[r % 4]
    if r < 4:
        if c == 13:
            return "wild"
        if c in VALUE_COLS:
            return color + VALUE_COLS[c]
        return None
    if c == 0:
        return None
    if c == 13:
        return "wild4"
    return color + VALUE_COLS[c]


def extract(sheet_path: str, cards_dir: str) -> dict:
    sheet = Image.open(sheet_path).convert("RGBA")
    if sheet.size[0] < CW * COLS or sheet.size[1] < CH * ROWS:
        sys.exit(f"Unexpected sheet size {sheet.size}, expected >= {(CW*COLS, CH*ROWS)}")
    os.makedirs(cards_dir, exist_ok=True)
    manifest = {"cell": [CW, CH], "source": "UNO_cards_deck.svg",
                "faces": {}, "grid": [[grid_face(c, r) for c in range(COLS)] for r in range(ROWS)]}
    seen: dict[str, Image.Image] = {}
    for r in range(ROWS):
        for c in range(COLS):
            code = grid_face(c, r)
            if code is None:
                continue
            cell = sheet.crop((c * CW, r * CH, (c + 1) * CW, (r + 1) * CH))
            cell = finish_card(cell)
            if code in seen:
                a, b = seen[code].tobytes(), cell.tobytes()
                if a != b:
                    diff = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
                    print(f"  note: duplicate {code} differs (mean {diff:.2f})")
                continue
            seen[code] = cell
            fname = f"{code}.png"
            cell.save(os.path.join(cards_dir, fname))
            manifest["faces"][code] = {"file": fname, "cell": [c, r]}
    return manifest


def finish_card(cell: Image.Image) -> Image.Image:
    """Cell crops share strokes with neighbours: redraw a complete rounded border."""
    w, h = cell.size
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([1, 1, w - 2, h - 2], radius=80, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(cell, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([1, 1, w - 2, h - 2], radius=80, outline=(0, 0, 0, 255), width=5)
    return out


def card_back(size=(CW, CH)) -> Image.Image:
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=80, fill=(18, 18, 26, 255),
                        outline=(245, 245, 245, 255), width=6)
    d.rounded_rectangle([22, 22, w - 23, h - 23], radius=66, outline=(233, 48, 48, 255), width=7)
    # big tilted oval ring
    oval = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(oval)
    od.ellipse([70, 120, w - 70, h - 90], outline=(226, 42, 42, 255), width=16)
    od.ellipse([92, 142, w - 92, h - 112], outline=(255, 214, 0, 255), width=5)
    img.alpha_composite(oval.rotate(-16, resample=Image.BICUBIC, center=(w // 2, h // 2)))
    # UNO word mark
    txt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    font = ImageFont.truetype(BACK_FONT, 168)
    word = "UNO"
    box = td.textbbox((0, 0), word, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x, y = (w - tw) // 2 - box[0], (h - th) // 2 - box[1] - 12
    for dx, dy, col in ((10, 12, (120, 0, 0, 255)), (0, 0, (255, 214, 0, 255))):
        td.text((x + dx, y + dy), word, font=font, fill=col)
    td.text((x, y), word, font=font, fill=(255, 255, 255, 255))
    txt = txt.rotate(-16, resample=Image.BICUBIC, center=(w // 2, h // 2))
    img.alpha_composite(txt)
    # four-colour ellipse badge, bottom right
    badge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    cx, cy, rx, ry = w - 96, h - 92, 44, 62
    pie = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pie)
    for i, col in enumerate([(255, 85, 85, 255), (85, 170, 85, 255), (85, 85, 255, 255), (255, 170, 0, 255)]):
        pd.pieslice([cx - rx, cy - ry, cx + rx, cy + ry], 90 * i, 90 * (i + 1), fill=col)
    bmask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(bmask).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    badge.paste(pie, (0, 0), bmask)
    bd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(255, 255, 255, 255), width=6)
    img.alpha_composite(badge)
    return img


THUMB_SIZES = (("160", (160, 240)), ("120", (120, 180)), ("80", (80, 120)),
               ("60", (60, 90)), ("44", (44, 66)))


def write_scaled(cards_dir: str, manifest: dict) -> dict:
    """Pre-resample with Lanczos at build time so the game needs no Pillow."""
    names = [meta["file"] for meta in manifest["faces"].values()] + [manifest["back"]["file"]]
    sizes = {}
    for folder, (w, h) in THUMB_SIZES:
        out_dir = os.path.join(cards_dir, folder)
        os.makedirs(out_dir, exist_ok=True)
        for name in names:
            src = os.path.join(cards_dir, name)
            img = Image.open(src).convert("RGBA").resize((w, h), Image.LANCZOS)
            img.save(os.path.join(out_dir, name))
        sizes[folder] = [w, h]
    return sizes


def contact_sheet(cards_dir: str, out: str) -> None:
    codes = sorted(json.load(open(os.path.join(cards_dir, "manifest.json")))["faces"])
    cols = 9
    tw, th = 160, 240
    rows = (len(codes) + 2 + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * tw, rows * (th + 26)), (24, 24, 30, 255))
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    items = [(c, os.path.join(cards_dir, c + ".png")) for c in codes]
    items.append(("back", os.path.join(cards_dir, "back.png")))
    items.append(("back@2x", os.path.join(cards_dir, "back.png")))
    for i, (code, path) in enumerate(items):
        r, c = divmod(i, cols)
        im = Image.open(path).resize((tw - 8, th - 8), Image.LANCZOS)
        sheet.alpha_composite(im, (c * tw + 4, r * (th + 26) + 4))
        d.text((c * tw + 8, r * (th + 26) + th), code[:16], font=font, fill=(230, 230, 230, 255))
    sheet.convert("RGB").save(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--svg", default=DEFAULT_SVG)
    ap.add_argument("--sheet", default=None, help="pre-rendered 6724x5764 PNG")
    ap.add_argument("--cards", default=os.path.join(ROOT, "cards"))
    ap.add_argument("--contact", default="/tmp/uno_contact.png")
    args = ap.parse_args()

    sheet = args.sheet
    if not sheet:
        sheet = "/tmp/uno_sheet@2x.png"
        print(f"rendering {args.svg} -> {sheet}")
        render_sheet(args.svg, sheet)
    manifest = extract(sheet, args.cards)
    card_back().save(os.path.join(args.cards, "back.png"))
    manifest["back"] = {"file": "back.png"}
    manifest["sizes"] = write_scaled(args.cards, manifest)
    with open(os.path.join(args.cards, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    contact_sheet(args.cards, args.contact)
    print(f"faces: {len(manifest['faces'])}, cards dir: {args.cards}, contact: {args.contact}")


if __name__ == "__main__":
    main()
