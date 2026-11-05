"""Tk image cache for the pre-rendered card sprites.

The build step (``tools/build_cards.py``) writes Lanczos-resampled copies at
480 / 160 / 120 / 80 px wide, so the game itself never needs Pillow.
"""
from __future__ import annotations

import json
import os
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CARDS_DIR = os.path.join(HERE, "cards")
SIZES = {"480": "480", "160": "160", "120": "120", "80": "80"}


class CardAssets:
    """Lazily loaded ``tk.PhotoImage`` cache keyed by (size, card)."""

    def __init__(self, root: tk.Misc, cards_dir: str = DEFAULT_CARDS_DIR) -> None:
        self.root = root
        self.dir = cards_dir
        manifest_path = os.path.join(cards_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.faces: list[str] = sorted(self.manifest["faces"])
        self._cache: dict[tuple[int, str], tk.PhotoImage] = {}

    # ------------------------------------------------------------------ helpers
    def _path(self, size: int, card: str) -> str:
        name = "back.png" if card == "back" else f"{card}.png"
        if size == 480:
            return os.path.join(self.dir, name)
        return os.path.join(self.dir, str(size), name)

    def image(self, card: str, size: int = 120) -> tk.PhotoImage:
        key = (size, card)
        img = self._cache.get(key)
        if img is None:
            path = self._path(size, card)
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"missing card sprite {path}; run: python3 tools/build_cards.py")
            img = tk.PhotoImage(master=self.root, file=path)
            self._cache[key] = img
        return img

    def back(self, size: int = 120) -> tk.PhotoImage:
        return self.image("back", size)

    def preload(self, cards: list[str], size: int = 120) -> None:
        for card in cards:
            self.image(card, size)
        self.back(size)

    def all_names(self) -> list[str]:
        return self.faces + ["back"]
