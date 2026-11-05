"""Browser gateway for the UNO LAN table (standard library only).

While a room is hosted, this small web server lets people without the program
play from a phone or any browser on the same Wi-Fi:

    http://<host-ip>:8080/

Every browser player is bridged to the room as an ordinary TCP client
(``net.NetClient`` on 127.0.0.1), so the host's game engine stays the only
authority and web players sit at the same table as desktop players: same
rules, seats, reconnect tokens, chat and avatars.

Transport to the browser: JSON over HTTP.  Server -> browser uses
Server-Sent Events (``/api/events``), browser -> server uses ``POST /api/send``.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import i18n

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WEB_PORT = 8080
MAX_BODY = 96 * 1024
MAX_SESSIONS = 24
SESSION_GRACE = 45.0          # seconds a browser may stay away before its seat is released
MAX_NAME = 24
# message types a browser may forward to the room (host-only actions are not offered)
ALLOWED = {"chat", "react", "play", "combo", "draw", "pass", "swap", "uno", "catch",
           "avatar", "avatar_get", "name", "leave"}
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
# home-screen (PWA) assets: the web-app manifest and the icon files in ./icons
_ICON_FILES = {
    "/icons/icon-192.png": "icon-192.png",
    "/icons/icon-512.png": "icon-512.png",
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",
    "/favicon.ico": "favicon-32.png",
    "/icons/favicon-32.png": "favicon-32.png",
}
_ICON_SIZE = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180, "favicon-32.png": 32}
_icon_cache: dict[str, bytes] = {}


def _clean_png(data: bytes) -> bytes | None:
    """Keep only the critical chunks (drops metadata such as C2PA 'caBX'); None if it is not a PNG."""
    import struct
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    out, i = [data[:8]], 8
    while i + 12 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        if kind in (b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"):
            out.append(data[i:i + 12 + length])
        i += 12 + length
    return b"".join(out)


def _fallback_icon(size: int) -> bytes:
    """A plain UNO-card icon drawn in pure Python, used when the icon files are missing."""
    import math
    import struct
    import zlib
    cos, sin = math.cos(math.radians(-12)), math.sin(math.radians(-12))
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            u, v = (x / size - .5), (y / size - .5)
            a, b = u * cos + v * sin, -u * sin + v * cos            # rotate into the card's frame
            rgb = (15, 64, 51)
            if abs(a) < .23 and abs(b) < .33:
                rgb = (255, 255, 255)
                if abs(a) < .205 and abs(b) < .305:
                    rgb = (238, 75, 87)
                    e1, e2 = a * cos - b * sin, a * sin + b * cos     # the tilted white oval
                    if (e1 / .19) ** 2 + (e2 / .27) ** 2 < 1:
                        rgb = (255, 255, 255)
            row += bytes(rgb)
        rows.append(bytes(row))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


MANIFEST = {
    "name": "UNO LAN Party",
    "short_name": "UNO",
    "description": "UNO at the table on your local network",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#0c1a22",
    "theme_color": "#0c1a22",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
_CARD_RE = re.compile(r"^/cards/(?:(\d{2,3})/)?([A-Za-z0-9]{2,12})\.png$")


def _clean_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())[:MAX_NAME].strip()
    return name or "Player"


class _QuietServer(ThreadingHTTPServer):
    """Phones close connections all the time; that is not worth a traceback."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class Session:
    """One browser player, bridged to the room by its own NetClient."""

    def __init__(self, sid: str, client: Any, name: str) -> None:
        self.sid = sid
        self.client = client
        self.name = name
        self.out: queue.Queue[dict] = queue.Queue(maxsize=3000)
        self.last_seen = time.monotonic()
        self.streams = 0
        self.closed = False

    def push(self, msg: dict) -> None:
        try:
            self.out.put_nowait(msg)
        except queue.Full:                       # keep the newest, drop the oldest
            try:
                self.out.get_nowait()
                self.out.put_nowait(msg)
            except (queue.Empty, queue.Full):
                pass


class WebServer:
    """HTTP front-end; start() returns the TCP port actually bound."""

    def __init__(self, room_port: int, port: int = DEFAULT_WEB_PORT, *,
                 bind: str = "0.0.0.0", room_host: str = "127.0.0.1",
                 cards_dir: str | None = None, html_path: str | None = None,
                 info: Callable[[], dict] | None = None) -> None:
        self.room_port = room_port
        self.room_host = room_host
        self.port = port
        self.bind = bind
        self.cards_dir = cards_dir or os.path.join(HERE, "cards")
        self.html_path = html_path or os.path.join(HERE, "web_client.html")
        self.icons_dir = os.path.join(HERE, "icons")
        self.info = info
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self.error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> int:
        handler = self._make_handler()
        last_exc: Exception | None = None
        for port in range(self.port, self.port + 11):
            try:
                self._httpd = _QuietServer((self.bind, port), handler)
                self.port = port
                break
            except OSError as exc:
                last_exc = exc
        if self._httpd is None:
            self.error = str(last_exc)
            raise OSError(f"web server could not bind: {last_exc}")
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="uno-web",
                         daemon=True).start()
        threading.Thread(target=self._janitor, name="uno-web-janitor", daemon=True).start()
        return self.port

    def close(self) -> None:
        self._stop.set()
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for sess in sessions:
            self._shutdown_session(sess, say_goodbye=False)
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except OSError:
                pass
            self._httpd = None

    def _janitor(self) -> None:
        while not self._stop.wait(5.0):
            now = time.monotonic()
            with self.lock:
                stale = [s for s in self.sessions.values()
                         if s.streams == 0 and now - s.last_seen > SESSION_GRACE]
            for sess in stale:
                self.drop(sess.sid)

    # ------------------------------------------------------------- sessions
    def join(self, body: dict) -> tuple[int, dict]:
        import net

        name = _clean_name(body.get("name"))
        spectate = bool(body.get("spectate"))
        password = str(body.get("password") or "")[:24]
        token = body.get("token")
        token = token if isinstance(token, str) and _TOKEN_RE.match(token) else None
        with self.lock:
            live = len(self.sessions)
        if live >= MAX_SESSIONS:
            return 503, {"error": "full"}
        if token:                                  # page reload: replace the old bridge
            with self.lock:
                old = [s.sid for s in self.sessions.values()
                       if getattr(s.client, "token", None) == token]
            for sid in old:
                self.drop(sid)
        candidates = [name] + [f"{name} {i}" for i in range(2, 10)]
        client = None
        welcome: dict | None = None
        for cand in candidates:
            client = net.NetClient(cand, spectate=spectate, password=password)
            if token:
                client.token = token
            try:
                welcome = client.connect(self.room_host, self.room_port, timeout=5.0)
                name = cand
                break
            except ConnectionError as exc:
                text = str(exc)
                client.close()
                client = None
                if "name_taken" in text:
                    continue
                code = text.rsplit(": ", 1)[-1].strip() or "rejected"
                return 400, {"error": code}
            except TimeoutError:
                client.close()
                return 504, {"error": "join_timeout"}
            except OSError:
                client.close()
                return 502, {"error": "unreachable"}
        if client is None or welcome is None:
            return 400, {"error": "name_taken"}
        sid = secrets.token_urlsafe(18)
        sess = Session(sid, client, name)
        with self.lock:
            self.sessions[sid] = sess
        threading.Thread(target=self._pump, args=(sess,), name=f"uno-web-{sid[:4]}",
                         daemon=True).start()
        return 200, {"sid": sid, "welcome": welcome, "name": name,
                     "token": getattr(client, "token", token)}

    def _pump(self, sess: Session) -> None:
        """Copy room messages for this player into the browser's event queue."""
        while not sess.closed and not self._stop.is_set():
            try:
                msgs = sess.client.poll()
            except Exception:
                msgs = []
            for msg in msgs:
                sess.push(msg)
            try:
                alive = sess.client.alive()
            except Exception:
                alive = False
            if not alive:
                sess.push({"t": "closed"})
                sess.closed = True
                break
            time.sleep(0.03)

    def _shutdown_session(self, sess: Session, say_goodbye: bool = True) -> None:
        sess.closed = True
        try:
            if say_goodbye:
                sess.client.send({"t": "leave"})
        except Exception:
            pass
        try:
            sess.client.close()
        except Exception:
            pass

    def drop(self, sid: str) -> None:
        with self.lock:
            sess = self.sessions.pop(sid, None)
        if sess is not None:
            self._shutdown_session(sess)

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self.lock:
            sess = self.sessions.get(sid)
        if sess is not None:
            sess.last_seen = time.monotonic()
        return sess

    # ----------------------------------------------------------------- http
    def icon_bytes(self, name: str) -> bytes:
        """Icon PNG: from ./icons or next to this file; a generated icon if neither is usable."""
        cached = _icon_cache.get(name)
        if cached is not None:
            return cached
        data: bytes | None = None
        for folder in (self.icons_dir, HERE):
            try:
                with open(os.path.join(folder, name), "rb") as fh:
                    data = _clean_png(fh.read())
            except OSError:
                continue
            if data:
                break
        if not data:
            data = _fallback_icon(_ICON_SIZE.get(name, 192))
        _icon_cache[name] = data
        return data

    def i18n_payload(self) -> dict:
        return {"STR": i18n.STR, "ERRORS": i18n.ERRORS,
                "VALUE_NAMES": getattr(i18n, "VALUE_NAMES", {}),
                "COLOR_NAMES": getattr(i18n, "COLOR_NAMES", {})}

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "UnoWeb/1"

            def log_message(self, fmt: str, *args: Any) -> None:  # silence
                pass

            # ---- helpers
            def _send(self, code: int, body: bytes, ctype: str,
                      cache: str = "no-store") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _read_json(self) -> dict | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if length <= 0 or length > MAX_BODY:
                    return None
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None
                return data if isinstance(data, dict) else None

            # ---- GET
            def do_HEAD(self) -> None:  # noqa: N802
                self.do_GET()

            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                path = url.path
                try:
                    if path in ("/", "/index.html"):
                        with open(web.html_path, "rb") as fh:
                            return self._send(200, fh.read(), "text/html; charset=utf-8")
                    if path == "/manifest.webmanifest":
                        return self._send(200, json.dumps(MANIFEST).encode("utf-8"),
                                          "application/manifest+json", "public, max-age=3600")
                    if path in _ICON_FILES:
                        return self._send(200, web.icon_bytes(_ICON_FILES[path]), "image/png",
                                          "public, max-age=3600")
                    if path == "/api/i18n":
                        return self._json(200, web.i18n_payload())
                    if path == "/api/info":
                        info = web.info() if web.info else {}
                        with web.lock:
                            info = dict(info)
                            info["web_players"] = len(web.sessions)
                        return self._json(200, info)
                    if path == "/api/events":
                        return self._events(parse_qs(url.query).get("sid", [None])[0])
                    match = _CARD_RE.match(path)
                    if match:
                        size, name = match.groups()
                        if size and size not in ("44", "60", "80", "120", "160"):
                            return self._send(404, b"", "text/plain")
                        full = os.path.join(web.cards_dir, size or "", f"{name}.png")
                        if os.path.isfile(full):
                            with open(full, "rb") as fh:
                                return self._send(200, fh.read(), "image/png",
                                                  "public, max-age=86400")
                    self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except OSError:
                    try:
                        self._send(500, b"error", "text/plain")
                    except OSError:
                        self.close_connection = True

            # ---- SSE
            def _events(self, sid: str | None) -> None:
                sess = web.get(sid)
                if sess is None:
                    return self._json(404, {"error": "no_session"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.close_connection = True
                sess.streams += 1
                try:
                    self.wfile.write(b"retry: 1500\n\n")
                    self.wfile.flush()
                    while not web._stop.is_set():
                        try:
                            msg = sess.out.get(timeout=8.0)
                        except queue.Empty:
                            sess.last_seen = time.monotonic()
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            if sess.closed:
                                break
                            continue
                        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(b"data: " + line.encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                        sess.last_seen = time.monotonic()
                        if msg.get("t") == "closed":
                            break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    sess.streams = max(0, sess.streams - 1)
                    sess.last_seen = time.monotonic()

            # ---- POST
            def do_POST(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                body = self._read_json()
                if body is None:
                    return self._json(400, {"error": "bad_request"})
                try:
                    if url.path == "/api/join":
                        code, payload = web.join(body)
                        return self._json(code, payload)
                    sid = parse_qs(url.query).get("sid", [None])[0]
                    sess = web.get(sid)
                    if sess is None:
                        return self._json(404, {"error": "no_session"})
                    if url.path == "/api/send":
                        kind = body.get("t")
                        if kind not in ALLOWED:
                            return self._json(400, {"error": "not_allowed"})
                        if kind == "leave":
                            web.drop(sess.sid)
                            return self._json(200, {"ok": True})
                        try:
                            sess.client.send(body)
                        except Exception:
                            return self._json(410, {"error": "closed"})
                        return self._json(200, {"ok": True})
                    if url.path == "/api/leave":
                        web.drop(sess.sid)
                        return self._json(200, {"ok": True})
                    self._json(404, {"error": "not_found"})
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        return Handler


def local_ips() -> list[str]:
    """This machine's LAN IPv4 addresses, best first - VPN / virtual adapters are skipped."""
    import lanip

    return lanip.lan_ips() or ["127.0.0.1"]
