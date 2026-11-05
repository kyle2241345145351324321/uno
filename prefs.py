"""Small JSON preference store for the UNO client.

Pane sizes, collapsed panes and the UI language live here so they survive a
restart.  Writes are atomic (temp file + ``os.replace``) and every failure is
swallowed: a read-only or corrupt store must never stop the game from booting.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP = "uno"
FILENAME = "prefs.json"


def default_path() -> Path:
    """Per-user config file (``UNO_PREFS`` overrides, handy for tests)."""
    env = os.environ.get("UNO_PREFS")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP / FILENAME


class Prefs:
    """A tiny flat key/value store backed by one JSON object."""

    def __init__(self, path: str | Path | None = None, *, autosave: bool = False) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.autosave = autosave
        self.error: str | None = None
        self._data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------- io
    def load(self) -> None:
        self._data = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.error = f"read: {exc}"
            return
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            self.error = f"parse: {exc}"
            return
        if isinstance(data, dict):
            self._data = data
        else:
            self.error = "parse: root is not an object"

    def save(self) -> bool:
        """Atomically persist the store; returns False instead of raising."""
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".tmp",
                                       dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1,
                          sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            tmp = None
        except OSError as exc:
            self.error = f"write: {exc}"
            return False
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self.error = None
        return True

    # -------------------------------------------------------------- accessors
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        if self.autosave:
            self.save()

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)
        if self.autosave:
            self.save()

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._data.get(key, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._data.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self._data.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "zh", "en")
        return bool(value)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)
