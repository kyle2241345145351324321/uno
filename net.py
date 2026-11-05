#!/usr/bin/env python3
"""LAN networking for UNO (protocol v1, standard library only).

Design
------
Host process: :class:`RoomServer` owns the single ``engine.UnoGame``.  One tick
thread at 10 Hz drains client intents, applies them to the engine, ticks the
engine (deadlines), then broadcasts snapshots.  Transport is
TCP JSON-lines; rooms are advertised over UDP.

Client process: :class:`NetClient` is a thin threaded wrapper (one reader
thread feeding a ``queue.Queue``); the Tk UI calls :meth:`NetClient.poll` from
``after(40)``.  The host UI uses exactly the same client path against its own
server via :class:`HostSession`.

Public API (docs/PROTOCOL.md)::

    DISCOVERY_PORT, DEFAULT_PORT, PROTOCOL_VERSION
    discover_rooms(timeout=0.8, discovery_port=DISCOVERY_PORT, extra_hosts=())
    class NetClient:
        pid, welcome, token
        __init__(name="player", spectate=False)
        connect(host, port=DEFAULT_PORT, timeout=5.0) -> dict
        send(msg), poll(), alive(), close()
    class HostSession:
        engine, server, client, port, requested_port
        __init__(name, port=DEFAULT_PORT, bind="0.0.0.0", rules=None,
                 discovery=True, room_name="", lang="zh")
        start(timeout=5.0) -> dict
        close()

Python >= 3.10, no third-party dependencies.
"""
from __future__ import annotations

import base64
import hmac
import json
import queue
import re
import socket
import threading
import time
import uuid
import zlib
from typing import Any

from engine import COLORS, Rules, UnoGame

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_PORT",
    "DISCOVERY_PORT",
    "discover_rooms",
    "NetClient",
    "HostSession",
    "RoomServer",
]

# --------------------------------------------------------------------------- #
# Constants (PROTOCOL.md)
# --------------------------------------------------------------------------- #
PROTOCOL_VERSION = 1
DEFAULT_PORT = 45872
DISCOVERY_PORT = 45871

MAX_SEATS = 10
MAX_SPECTATORS = 8
MAX_LINE = 64 * 1024            # 64 KiB per JSON line
MAX_SEND_BUFFER = 1024 * 1024   # drop a client queued more than 1 MiB
MAX_CHAT = 50
MAX_CHAT_CHARS = 300
MAX_NAME_CHARS = 24
# emoji reactions: a fixed palette (the server rejects anything else) and a per-player cooldown
REACTIONS = ("👍", "😂", "😮", "😡", "😭", "🎉", "🤔", "👏")
REACT_COOLDOWN = 1.2
# special reactions: only the player(s) currently leading the match score (and > 0) may send them
SPECIAL_REACTIONS = ("👑", "🔥", "😎", "🏆")
MAX_AVATAR_B64 = 16 * 1024       # small PNG avatar, base64 text
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")

TICK_INTERVAL = 0.1             # 10 Hz server tick thread
IDLE_BROADCAST = 1.0            # state refresh even when nothing happens
JOIN_TIMEOUT = 10.0             # a socket that never says hello is dropped
JOIN_APPROVAL_TIMEOUT = 60.0    # how long the host has to answer a join request
MAX_PASSWORD = 24
AUTO_PASS_GRACE = 12.0          # disconnected player's turn is auto-played
RATE_BURST = 80.0               # per-connection message bucket
RATE_PER_SEC = 40.0
DISCOVERY_ANNOUNCE = 2.0

DISCOVERY_TARGETS = ("255.255.255.255", "127.0.0.1")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.monotonic()


def _json_line(obj: Any) -> bytes:
    """Encode one protocol frame (UTF-8 JSON + ``\\n``)."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _valid_avatar(data: Any) -> bool:
    """A short base64 string that decodes to a PNG (no Tk needed on the server)."""
    if not isinstance(data, str) or not data or len(data) > MAX_AVATAR_B64:
        return False
    if not _B64_RE.match(data):
        return False
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return False
    return raw.startswith(b"\x89PNG\r\n\x1a\n")


def _avatar_version(data: Any) -> str:
    return "%08x" % zlib.crc32(data.encode("ascii")) if isinstance(data, str) and data else ""


def _safe_name(name: Any, fallback: str = "player") -> str:
    if not isinstance(name, str):
        return fallback
    cleaned = " ".join(name.split())[:MAX_NAME_CHARS]
    return cleaned or fallback


def _safe_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()[:MAX_CHAT_CHARS]


def _valid_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _lan_broadcasts() -> list[str]:
    """Directed broadcast addresses of the real LAN adapters (VPNs skipped)."""
    try:
        import lanip

        return lanip.cached_broadcasts()          # never spawns a process from this thread
    except Exception:
        return []


def _local_ipv4() -> str:
    """The computer's real LAN address - never a VPN / tunnel address."""
    try:
        import lanip

        ip = lanip.best_lan_ip(refresh=True)
        if ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # connect() on UDP sends no packets
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _discovery_targets(extra_hosts: Any = ()) -> list[str]:
    targets = list(DISCOVERY_TARGETS) + _lan_broadcasts()
    try:
        for host in extra_hosts or ():
            if isinstance(host, str) and host and host not in targets:
                targets.append(host)
    except TypeError:
        pass
    return targets


# --------------------------------------------------------------------------- #
# UDP discovery (client side)
# --------------------------------------------------------------------------- #
def discover_rooms(timeout: float = 0.8, discovery_port: int = DISCOVERY_PORT,
                   extra_hosts: Any = ()) -> list[dict]:
    """Broadcast a ``discover`` probe and collect ``room`` replies.

    Replies are de-duplicated by ``(host, port)``; the function also accepts
    unsolicited ``room`` broadcasts sent by hosts every 2 s (those carry no
    nonce).  Returns immediately with an empty list when nothing answers.
    """
    nonce = uuid.uuid4().hex[:8]
    payload = _json_line({"t": "discover", "v": PROTOCOL_VERSION, "nonce": nonce})
    found: dict[tuple[str, int], dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            sock.bind(("0.0.0.0", 0))
        except OSError:
            sock.close()
            return []
        for host in _discovery_targets(extra_hosts):
            try:
                sock.sendto(payload, (host, int(discovery_port)))
            except OSError:
                continue
        deadline = _now() + max(0.0, float(timeout))
        while True:
            remaining = deadline - _now()
            if remaining <= 0:
                break
            try:
                sock.settimeout(min(remaining, 0.2))
                data, addr = sock.recvfrom(MAX_LINE)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                room = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(room, dict) or room.get("t") != "room":
                continue
            if room.get("v") != PROTOCOL_VERSION:
                continue
            reply_nonce = room.get("nonce")
            if reply_nonce is not None and reply_nonce != nonce:
                continue
            host = room.get("host")
            port = room.get("port")
            if not isinstance(host, str) or not host:
                host = addr[0]
            if not isinstance(port, int) or isinstance(port, bool):
                port = addr[1]
            key = (host, int(port))
            if key in found:
                continue
            room = dict(room)
            room["host"], room["port"] = host, int(port)
            found[key] = room
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return list(found.values())


# --------------------------------------------------------------------------- #
# One TCP connection on the server side
# --------------------------------------------------------------------------- #
class _Conn:
    """A framed JSON-lines socket with its own reader/writer threads."""

    def __init__(self, server: "RoomServer", sock: socket.socket, addr: Any) -> None:
        self.server = server
        self.sock = sock
        self.addr = addr
        self.created_at = _now()
        self.last_recv = self.created_at
        self.pid: int | None = None
        self.spectate = False
        self.joined = False
        self.name = ""
        self.token: str | None = None

        self.closed = threading.Event()
        self._send_lock = threading.Lock()      # guards _buf_bytes and _out bookkeeping
        self._wire_lock = threading.Lock()      # serialises socket.sendall
        self._out: queue.Queue = queue.Queue()
        self._buf_bytes = 0
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None

        self._rate_tokens = RATE_BURST
        self._rate_ts = _now()
        self._rate_warned = 0.0
        self.last_react = -1e9

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name=f"uno-writer-{self.addr[0]}:{self.addr[1]}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"uno-reader-{self.addr[0]}:{self.addr[1]}")
        self._writer.start()
        self._reader.start()

    def close(self, flush: float = 0.0) -> None:
        """Close the socket; ``flush`` waits briefly so a final error/ack goes out."""
        if self.closed.is_set():
            return
        if flush > 0:
            # Wait for the writer thread to finish pushing queued bytes: the
            # queue length alone is not enough because the writer pops an item
            # before sendall() returns.
            deadline = _now() + flush
            while self._buf_bytes > 0 and _now() < deadline:
                time.sleep(0.005)
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._out.put(None)

    # -- sending ----------------------------------------------------------- #
    def send(self, obj: Any) -> bool:
        """Queue one message.  Returns False when the connection is closing."""
        if self.closed.is_set():
            return False
        try:
            data = _json_line(obj)
        except (TypeError, ValueError):
            return False
        with self._send_lock:
            if self.closed.is_set():
                return False
            if self._buf_bytes + len(data) > MAX_SEND_BUFFER:
                # Slow/stuck consumer: drop it rather than growing without bound.
                self.close()
                return False
            self._buf_bytes += len(data)
        self._out.put(data)
        return True

    def _write_loop(self) -> None:
        while True:
            data = self._out.get()
            if data is None:
                break
            if self.closed.is_set():
                with self._send_lock:
                    self._buf_bytes = max(0, self._buf_bytes - len(data))
                continue
            try:
                with self._wire_lock:
                    self.sock.sendall(data)
            except OSError:
                with self._send_lock:
                    self._buf_bytes = max(0, self._buf_bytes - len(data))
                self.close()
                break
            else:
                with self._send_lock:
                    self._buf_bytes = max(0, self._buf_bytes - len(data))

    def _protocol_error(self, code: str) -> None:
        self.send({"t": "error", "code": code})

    # -- receiving --------------------------------------------------------- #
    def _allow_message(self) -> bool:
        now = _now()
        elapsed = now - self._rate_ts
        if elapsed > 0:
            self._rate_tokens = min(RATE_BURST, self._rate_tokens + elapsed * RATE_PER_SEC)
            self._rate_ts = now
        if self._rate_tokens >= 1.0:
            self._rate_tokens -= 1.0
            return True
        if now - self._rate_warned >= 1.0:
            self._rate_warned = now
            self._protocol_error("rate_limited")
        return False

    def _read_loop(self) -> None:
        buf = b""
        try:
            while not self.closed.is_set():
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self.last_recv = _now()
                buf += chunk
                if len(buf) > MAX_LINE and b"\n" not in buf:
                    self._protocol_error("bad_msg")
                    # flush the error before the socket goes away
                    self.close(flush=0.3)
                    return
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if len(line) > MAX_LINE:
                        self._protocol_error("bad_msg")
                        self.close(flush=0.3)
                        return
                    msg = self._parse(line)
                    if msg is None:
                        continue
                    if not self._allow_message():
                        continue
                    self.server._enqueue_msg(self, msg)
        finally:
            self.close()

    def _parse(self, line: bytes) -> dict | None:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._protocol_error("bad_msg")
            return None
        if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
            self._protocol_error("bad_msg")
            return None
        return obj


# --------------------------------------------------------------------------- #
# Authoritative room/game server (host process)
# --------------------------------------------------------------------------- #
class RoomServer:
    AUTO_PASS_GRACE = AUTO_PASS_GRACE
    """Owns seats, the ``UnoGame`` and all network threads for one room.

    Not part of the documented UI API (the UI uses :class:`HostSession`), but
    its ``engine`` / ``port`` attributes are useful for embedding.
    """

    def __init__(self, *, bind: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 rules: dict | None = None, discovery: bool = True,
                 room_name: str = "", lang: str = "zh") -> None:
        self.bind = bind
        self.requested_port = int(port)
        self.discovery = bool(discovery)
        self.room_name = room_name
        self.lang = lang
        base = Rules().as_dict()
        if rules:
            try:
                base.update(Rules.from_dict(rules).as_dict())
            except (TypeError, ValueError):
                pass
        self.rules: dict[str, Any] = base
        self.engine: UnoGame | None = None
        self.port: int | None = None
        self.discovery_bound = False

        self.lock = threading.RLock()
        self.seats: list[dict] = []          # contiguous; index == pid
        self.spectators: list[dict] = []
        self.chat: list[dict] = []
        self.password = ""                 # empty = open room
        self.approval = False               # the host must say yes to every new joiner
        self._pending: dict[str, dict] = {}  # join id -> {conn, msg, t0}

        self._conns: set[_Conn] = set()
        self._inbox: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._started = False
        self._listen: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._tick_thread: threading.Thread | None = None
        self._discovery_sock: socket.socket | None = None
        self._discovery_thread: threading.Thread | None = None

        self._stall_key: tuple[int, int] | None = None
        self._stall_since = 0.0
        self._pending_events: list[dict] = []
        self._dirty = False
        self._last_state_broadcast = 0.0
        self._host_ip = _local_ipv4()
        self.last_error: str | None = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> "RoomServer":
        if self._started:
            return self
        self._started = True
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind, self.requested_port))
            sock.listen(16)
            sock.settimeout(0.2)
        except OSError:
            try:
                sock.close()
            finally:
                self._started = False
            raise
        self._listen = sock
        self.port = sock.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                               name="uno-accept")
        self._accept_thread.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                             name="uno-tick")
        self._tick_thread.start()
        if self.discovery:
            self._start_discovery()
        return self

    def close(self) -> None:
        self._stop.set()
        listen = self._listen
        self._listen = None
        if listen is not None:
            try:
                listen.close()
            except OSError:
                pass
        dsock = self._discovery_sock
        self._discovery_sock = None
        if dsock is not None:
            try:
                dsock.close()
            except OSError:
                pass
        for thread in (self._accept_thread, self._tick_thread, self._discovery_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._accept_thread = self._tick_thread = self._discovery_thread = None
        with self.lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            conn.close()
        for conn in conns:
            for thread in (conn._reader, conn._writer):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=0.2)

    # -- accept / inbound -------------------------------------------------- #
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listen = self._listen
            if listen is None:
                break
            try:
                client, addr = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn = _Conn(self, client, addr)
            with self.lock:
                self._conns.add(conn)
            conn.start()

    def _enqueue_msg(self, conn: _Conn, msg: dict) -> None:
        self._inbox.put((conn, msg))

    # -- tick thread -------------------------------------------------------- #
    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(TICK_INTERVAL):
                break
            try:
                self._tick_once()
            except Exception as exc:  # keep the room alive no matter what
                self.last_error = f"{type(exc).__name__}: {exc}"

    def _tick_once(self) -> None:
        now = _now()
        dirty = self._dirty
        self._dirty = False
        dirty = self._reap_conns(now) or dirty
        self._expire_pending(now)

        processed = 0
        while processed < 400:
            try:
                conn, msg = self._inbox.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if conn.closed.is_set():
                continue
            try:
                dirty = self._handle_msg(conn, msg, now) or dirty
            except Exception as exc:
                self.last_error = f"handler: {type(exc).__name__}: {exc}"
                conn.send({"t": "error", "code": "illegal"})

        if self.engine is not None:
            try:
                tick_events = self.engine.tick(now)
            except Exception as exc:
                self.last_error = f"tick: {type(exc).__name__}: {exc}"
                tick_events = []
            if tick_events:
                self._pending_events.extend(tick_events)
                dirty = True
            try:
                dirty = self._release_stalled(now) or dirty
            except Exception as exc:
                self.last_error = f"stalled: {type(exc).__name__}: {exc}"

        if self._pending_events:
            self._broadcast_events()
            dirty = True
        if self.engine is not None and (dirty or now - self._last_state_broadcast >= IDLE_BROADCAST):
            self._broadcast_state()
            self._last_state_broadcast = now

    # -- stalled turns ------------------------------------------------------ #
    def _release_stalled(self, now: float) -> bool:
        """Auto-play a disconnected player's turn so the table never freezes."""
        engine = self.engine
        if engine is None or engine.phase != "playing":
            self._stall_key = None
            return False
        pid = engine.turn
        key = (pid, engine.event_seq)
        if key != self._stall_key:
            self._stall_key = key
            self._stall_since = now
            return False
        if now - self._stall_since < self.AUTO_PASS_GRACE:
            return False
        seat = self.seats[pid] if 0 <= pid < len(self.seats) else None
        if seat is None or seat["connected"]:
            return False
        events: list[dict] = []
        try:
            drawn = engine.drawn
            if drawn and drawn[0] == pid:
                events.extend(engine.pass_turn(pid))
            else:
                events.extend(engine.draw(pid, now))
                if engine.turn == pid and engine.drawn and engine.drawn[0] == pid:
                    events.extend(engine.pass_turn(pid))
        except Exception:
            return False
        self._pending_events.extend(e for e in events if isinstance(e, dict))
        self._stall_key = (engine.turn, engine.event_seq)
        self._stall_since = now
        return True

    # -- connection reaping / seat ownership -------------------------------- #
    def _reap_conns(self, now: float) -> bool:
        changed = False
        with self.lock:
            conns = list(self._conns)
        for conn in conns:
            if conn.closed.is_set():
                with self.lock:
                    self._conns.discard(conn)
                changed = self._detach(conn) or changed
            elif (not conn.joined and now - conn.created_at > JOIN_TIMEOUT
                  and not any(p["conn"] is conn for p in self._pending.values())):
                conn.close()
                with self.lock:
                    self._conns.discard(conn)
                changed = True
        return changed

    def _detach(self, conn: _Conn) -> bool:
        """Forget a dead connection; lobby seats vanish, game seats stay offline."""
        changed = False
        with self.lock:
            if conn.spectate:
                for index, spectator in enumerate(self.spectators):
                    if spectator["conn"] is conn:
                        del self.spectators[index]
                        changed = True
                        break
                conn.spectate = False
            elif conn.pid is not None and 0 <= conn.pid < len(self.seats):
                seat = self.seats[conn.pid]
                if seat["conn"] is conn:
                    if self.engine is None:
                        del self.seats[conn.pid]
                        self._reindex_seats()
                    else:
                        seat["conn"] = None
                        seat["connected"] = False
                        self.engine.players[seat["pid"]].connected = False
                    changed = True
            conn.pid = None
        if changed:
            self._broadcast_room()
            self._dirty = True
        return changed

    def _reindex_seats(self) -> None:
        """Lobby only: keep seat indices contiguous and seat 0 the host."""
        for index, seat in enumerate(self.seats):
            seat["pid"] = index
            seat["host"] = index == 0
            if seat["conn"] is not None:
                seat["conn"].pid = index

    # -- room / snapshot state --------------------------------------------- #
    def _rules_public(self) -> dict[str, Any]:
        if self.engine is not None:
            return self.engine.rules.as_dict()
        return dict(self.rules)

    def _room_dict(self) -> dict[str, Any]:
        with self.lock:
            seats = [
                {"pid": seat["pid"], "name": seat["name"],
                 "host": bool(seat["host"]), "connected": bool(seat["connected"]),
                 "av": _avatar_version(seat.get("avatar"))}
                for seat in self.seats
            ]
            spectators = [{"name": person["name"]} for person in self.spectators]
            state = self.engine.phase if self.engine is not None else "lobby"
            room = {
                "state": state,
                "host_pid": 0 if self.seats else None,
                "lang": self.lang,
                "seats": seats,
                "spectators": spectators,
                "rules": self._rules_public(),
                "players": len(self.seats),
                "capacity": MAX_SEATS,
                "name": self.room_name,
                "locked": bool(self.password),
                "approval": bool(self.approval),
            }
        return room

    def _snapshot(self, viewer: int | None) -> dict:
        """Engine snapshot plus ``remaining`` seconds on timed windows.

        Engine deadlines use the host's monotonic clock, which means nothing on
        another machine, so clients count down from ``remaining`` instead.
        """
        assert self.engine is not None
        snap = self.engine.snapshot(viewer)
        now = _now()
        for key in ("uno_window", "chooser", "turn_timer"):
            window = snap.get(key)
            if isinstance(window, dict) and "deadline" in window:
                window = dict(window)
                window["remaining"] = max(0.0, window["deadline"] - now)
                snap[key] = window
        return snap

    def _viewer_snap(self, viewer: int | None) -> dict:
        return self._snapshot(viewer)

    def _make_welcome(self, conn: _Conn) -> dict:
        viewer = None if conn.spectate else conn.pid
        snap = self._snapshot(viewer) if self.engine is not None else None
        return {
            "t": "welcome",
            "v": PROTOCOL_VERSION,
            "you": conn.pid,
            "room": self._room_dict(),
            "chat": [dict(item) for item in self.chat[-MAX_CHAT:]],
            "snap": snap,
        }

    def _broadcast_room(self) -> None:
        room = self._room_dict()
        with self.lock:
            conns = list(self._conns)
        for conn in conns:
            if conn.joined and not conn.closed.is_set():
                conn.send({"t": "room", "room": room})

    def _broadcast_events(self) -> None:
        items = self._pending_events
        self._pending_events = []
        if not items:
            return
        with self.lock:
            conns = list(self._conns)
        for conn in conns:
            if conn.joined and not conn.closed.is_set():
                conn.send({"t": "events", "items": items})

    def _broadcast_state(self) -> None:
        if self.engine is None:
            return
        room = self._room_dict()
        with self.lock:
            conns = list(self._conns)
        cache: dict[int | None, dict] = {}

        def snap_for(viewer: int | None) -> dict:
            if viewer not in cache:
                cache[viewer] = self._snapshot(viewer)
            return cache[viewer]

        for conn in conns:
            if not conn.joined or conn.closed.is_set():
                continue
            viewer = None if conn.spectate else conn.pid
            snap = snap_for(viewer)
            conn.send({"t": "state", "snap": snap, "seq": snap.get("event_seq", 0),
                       "room": room})

    def _broadcast_chat(self, entry: dict) -> None:
        with self.lock:
            conns = list(self._conns)
        for conn in conns:
            if conn.joined and not conn.closed.is_set():
                conn.send(dict(entry))

    # -- reply helpers ------------------------------------------------------ #
    @staticmethod
    def _ok(conn: _Conn, msg: dict) -> None:
        mid = msg.get("id")
        if _valid_id(mid):
            conn.send({"t": "ack", "id": mid, "ok": True})

    @staticmethod
    def _fail(conn: _Conn, msg: dict, code: str) -> bool:
        mid = msg.get("id")
        if _valid_id(mid):
            conn.send({"t": "ack", "id": mid, "ok": False, "code": code})
        conn.send({"t": "error", "code": code})
        return False

    def _is_host(self, conn: _Conn) -> bool:
        with self.lock:
            return (conn.pid == 0 and not conn.spectate and bool(self.seats)
                    and self.seats[0]["conn"] is conn)

    def _require_host(self, conn: _Conn, msg: dict) -> bool:
        if not self._is_host(conn):
            self._fail(conn, msg, "not_host")
            return False
        return True

    # -- message dispatch --------------------------------------------------- #
    def _handle_msg(self, conn: _Conn, msg: dict, now: float) -> bool:
        kind = msg.get("t")
        if not isinstance(kind, str):
            return self._fail(conn, msg, "bad_msg")
        if kind == "hello":
            return self._handle_hello(conn, msg)
        if kind == "ping":
            conn.send({"t": "pong", "ts": msg.get("ts")})
            return False
        if not conn.joined:
            return self._fail(conn, msg, "bad_msg")
        if kind == "chat":
            return self._handle_chat(conn, msg)
        if kind == "react":
            return self._handle_react(conn, msg, now)
        if kind in ("play", "combo", "draw", "pass", "swap", "uno", "catch"):
            return self._handle_game_action(conn, msg, now)
        if kind == "start":
            return self._handle_start(conn, msg, now)
        if kind == "rules":
            return self._handle_rules(conn, msg)
        if kind == "kick":
            return self._handle_kick(conn, msg)
        if kind == "name":
            return self._handle_name(conn, msg)
        if kind == "security":
            return self._handle_security(conn, msg)
        if kind == "join_answer":
            return self._handle_join_answer(conn, msg)
        if kind == "avatar":
            return self._handle_avatar(conn, msg)
        if kind == "avatar_get":
            return self._handle_avatar_get(conn, msg)
        if kind == "next_round":
            return self._handle_next_round(conn, msg)
        if kind == "rematch":
            return self._handle_rematch(conn, msg)
        if kind == "leave":
            conn.close()
            return True
        return self._fail(conn, msg, "unknown_type")

    # -- hello / lobby join ------------------------------------------------- #
    def _find_token_seat(self, token: str | None) -> dict | None:
        if not token:
            return None
        for seat in self.seats:
            if seat["token"] == token:
                return seat
        return None

    def _name_taken(self, name: str, *, ignore_conn: _Conn | None) -> bool:
        folded = name.casefold()
        for seat in self.seats:
            if seat["conn"] is ignore_conn:
                continue
            if seat["name"].casefold() == folded:
                return True
        for spectator in self.spectators:
            if spectator["conn"] is ignore_conn:
                continue
            if spectator["name"].casefold() == folded:
                return True
        return False

    def _handle_hello(self, conn: _Conn, msg: dict) -> bool:
        if msg.get("v") != PROTOCOL_VERSION:
            conn.send({"t": "error", "code": "bad_version"})
            conn.close(flush=0.4)
            return False
        if conn.joined:
            return False
        token = msg.get("token")
        with self.lock:
            returning = isinstance(token, str) and bool(token) and self._find_token_seat(token) is not None
            first = not self.seats                     # the host's own client opens the room
            password, approval = self.password, self.approval
            already = any(p["conn"] is conn for p in self._pending.values())
        if already:
            return False
        if not returning and not first:
            if password:
                given = str(msg.get("password") or "")[:MAX_PASSWORD]
                if not hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8")):
                    conn.send({"t": "error", "code": "bad_password"})
                    conn.close(flush=0.4)
                    return False
            if approval:
                return self._queue_join(conn, msg)
        return self._finish_hello(conn, msg)

    def _host_conn(self) -> "_Conn | None":
        with self.lock:
            if self.seats and self.seats[0].get("conn") is not None and self.seats[0]["conn"].joined:
                return self.seats[0]["conn"]
        return None

    def _queue_join(self, conn: _Conn, msg: dict) -> bool:
        """Hold a joiner until the host presses Allow or Deny."""
        host_conn = self._host_conn()
        if host_conn is None:
            conn.send({"t": "error", "code": "join_denied"})
            conn.close(flush=0.4)
            return False
        jid = uuid.uuid4().hex[:10]
        with self.lock:
            self._pending[jid] = {"conn": conn, "msg": msg, "t0": _now()}
        conn.send({"t": "waiting", "timeout": JOIN_APPROVAL_TIMEOUT})
        host_conn.send({"t": "join_request", "id": jid,
                        "name": _safe_name(msg.get("name"), "player"),
                        "spectate": bool(msg.get("spectate"))})
        return False

    def _handle_join_answer(self, conn: _Conn, msg: dict) -> bool:
        if conn is not self._host_conn():
            return self._fail(conn, msg, "not_host")
        jid = msg.get("id")
        with self.lock:
            entry = self._pending.pop(jid, None) if isinstance(jid, str) else None
        if entry is None:
            return False
        waiting: _Conn = entry["conn"]
        if waiting.closed.is_set():
            return False
        if bool(msg.get("allow")):
            return self._finish_hello(waiting, entry["msg"])
        waiting.send({"t": "error", "code": "join_denied"})
        waiting.close(flush=0.4)
        return False

    def _handle_security(self, conn: _Conn, msg: dict) -> bool:
        """Host only: set / clear the room password, switch join approval on or off."""
        if conn is not self._host_conn():
            return self._fail(conn, msg, "not_host")
        with self.lock:
            if "password" in msg:
                self.password = " ".join(str(msg.get("password") or "").split())[:MAX_PASSWORD]
            if "approval" in msg:
                self.approval = bool(msg.get("approval"))
        self._ok(conn, msg)
        self._broadcast_room()
        return False

    def _expire_pending(self, now: float) -> None:
        with self.lock:
            stale = [jid for jid, p in self._pending.items()
                     if now - p["t0"] > JOIN_APPROVAL_TIMEOUT or p["conn"].closed.is_set()]
            gone = [(jid, self._pending.pop(jid)) for jid in stale]
        host_conn = self._host_conn()
        for jid, entry in gone:
            waiting: _Conn = entry["conn"]
            if not waiting.closed.is_set():
                waiting.send({"t": "error", "code": "join_timeout"})
                waiting.close(flush=0.4)
            if host_conn is not None:
                host_conn.send({"t": "join_cancel", "id": jid})

    def _finish_hello(self, conn: _Conn, msg: dict) -> bool:
        name = _safe_name(msg.get("name"), "player")
        token = msg.get("token")
        if not isinstance(token, str) or not token:
            token = None
        spectate = bool(msg.get("spectate"))
        welcome: dict | None = None
        dirty = False
        with self.lock:
            seat = self._find_token_seat(token)
            if seat is not None:
                # Reattach to the reserved seat: same pid, hand preserved.
                previous = seat["conn"]
                if previous is not None and previous is not conn:
                    previous.pid = None
                    previous.send({"t": "error", "code": "seat_taken"})
                    previous.close(flush=0.2)
                seat["conn"] = conn
                seat["connected"] = True
                seat["token"] = token
                conn.pid = seat["pid"]
                conn.joined = True
                conn.spectate = False
                conn.name = seat["name"]
                if self.engine is not None:
                    self.engine.players[seat["pid"]].connected = True
                welcome = self._make_welcome(conn)
                dirty = True
            elif spectate or self.engine is not None:
                if self.engine is not None and not spectate:
                    conn.send({"t": "error", "code": "not_lobby"})
                    conn.close(flush=0.4)
                    return False
                if len(self.spectators) >= MAX_SPECTATORS:
                    conn.send({"t": "error", "code": "spectator_full"})
                    conn.close(flush=0.4)
                    return False
                if self._name_taken(name, ignore_conn=conn):
                    conn.send({"t": "error", "code": "name_taken"})
                    conn.close(flush=0.4)
                    return False
                self.spectators.append({"name": name, "token": token, "conn": conn})
                conn.pid = None
                conn.joined = True
                conn.spectate = True
                conn.name = name
                welcome = self._make_welcome(conn)
                dirty = True
            else:
                if len(self.seats) >= MAX_SEATS:
                    conn.send({"t": "error", "code": "lobby_full"})
                    conn.close(flush=0.4)
                    return False
                if self._name_taken(name, ignore_conn=conn):
                    conn.send({"t": "error", "code": "name_taken"})
                    conn.close(flush=0.4)
                    return False
                pid = len(self.seats)
                self.seats.append({
                    "pid": pid,
                    "name": name,
                    "host": pid == 0,
                    "connected": True,
                    "conn": conn,
                    "token": token,
                    "avatar": "",
                })
                conn.pid = pid
                conn.joined = True
                conn.spectate = False
                conn.name = name
                welcome = self._make_welcome(conn)
                dirty = True
        if welcome is not None:
            conn.send(welcome)
            self._broadcast_room()
        return dirty

    # -- chat --------------------------------------------------------------- #
    def _handle_chat(self, conn: _Conn, msg: dict) -> bool:
        text = _safe_text(msg.get("text"))
        if not text:
            return self._fail(conn, msg, "bad_msg")
        entry = {"t": "chat", "pid": conn.pid, "name": conn.name, "text": text,
                 "ts": time.time()}
        with self.lock:
            self.chat = (self.chat + [entry])[-MAX_CHAT:]
        self._ok(conn, msg)
        self._broadcast_chat(entry)
        return False

    # -- emoji reactions ---------------------------------------------------- #
    def _leaders(self) -> set[int]:
        """Seats currently ahead on match score (ties share the lead; nobody leads at 0)."""
        engine = self.engine
        if engine is None:
            return set()
        scores = {i: int(p.score) for i, p in enumerate(engine.players)}
        top = max(scores.values(), default=0)
        return {i for i, v in scores.items() if top > 0 and v == top}

    def _handle_react(self, conn: _Conn, msg: dict, now: float) -> bool:
        """A seated player floats an emoji over their seat; spectators may not."""
        emoji = msg.get("emoji")
        if not isinstance(emoji, str) or conn.spectate or conn.pid is None \
                or emoji not in REACTIONS + SPECIAL_REACTIONS:
            return self._fail(conn, msg, "bad_msg")
        if emoji in SPECIAL_REACTIONS and conn.pid not in self._leaders():
            return self._fail(conn, msg, "react_locked")
        if now - conn.last_react < REACT_COOLDOWN:
            self._ok(conn, msg)                 # too fast: quietly ignored, no error toast
            return False
        conn.last_react = now
        self._ok(conn, msg)
        self._broadcast_chat({"t": "react", "pid": conn.pid, "name": conn.name,
                              "emoji": emoji, "ts": time.time()})
        return False

    # -- engine driving ----------------------------------------------------- #
    def _apply_action(self, pid: int, action: dict, now: float) -> tuple[bool, str, list]:
        """Run one intent on the engine.  Only ever called from the tick thread."""
        engine = self.engine
        if engine is None:
            return False, "not_playing", []
        kind = action.get("type")
        if kind == "play":
            card = action.get("card")
            if not isinstance(card, str):
                return False, "illegal", []
            color = action.get("color")
            if not isinstance(color, str) or color not in COLORS:
                color = None
            events = engine.play(pid, card, color, jump=bool(action.get("jump")), now=now)
        elif kind == "combo":
            cards = action.get("cards")
            if not isinstance(cards, list) or not all(isinstance(c, str) for c in cards):
                return False, "bad_msg", []
            events = engine.play_multi(pid, cards, now=now)
        elif kind == "draw":
            events = engine.draw(pid, now=now)
        elif kind == "pass":
            events = engine.pass_turn(pid)
        elif kind == "swap":
            target = action.get("target")
            if isinstance(target, bool) or not isinstance(target, int):
                return False, "bad_target", []
            events = engine.choose_swap(pid, target, now=now)
        elif kind == "uno":
            events = engine.call_uno(pid, now=now)
        elif kind == "catch":
            events = engine.catch_uno(pid, now=now)
        else:
            return False, "unknown_type", []
        if events and events[0].get("t") == "error":
            return False, events[0].get("code") or "illegal", []
        self._pending_events.extend(events)
        return True, "", events

    def _handle_game_action(self, conn: _Conn, msg: dict, now: float) -> bool:
        if self.engine is None:
            return self._fail(conn, msg, "not_playing")
        if conn.spectate or conn.pid is None:
            return self._fail(conn, msg, "not_a_player")
        pid = conn.pid
        if not 0 <= pid < len(self.seats):
            return self._fail(conn, msg, "bad_player")
        action = {
            "type": msg.get("t"),
            "card": msg.get("card"),
            "color": msg.get("color"),
            "jump": bool(msg.get("jump")),
            "target": msg.get("target"),
            "cards": msg.get("cards"),
        }
        ok, code, _events = self._apply_action(pid, action, now)
        if not ok:
            return self._fail(conn, msg, code)
        self._ok(conn, msg)
        return True

    # -- host / lobby actions ---------------------------------------------- #
    def _handle_start(self, conn: _Conn, msg: dict, now: float) -> bool:
        if not self._require_host(conn, msg):
            return False
        if self.engine is not None:
            return self._fail(conn, msg, "not_lobby")
        if len(self.seats) < 2:
            return self._fail(conn, msg, "need_players")
        names = [seat["name"] for seat in self.seats]
        game = UnoGame(names, ai_flags=[False] * len(names),
                       rules=Rules.from_dict(self.rules), host_pid=0)
        events = game.start_round(first=0)
        for index, seat in enumerate(self.seats):
            game.players[index].connected = bool(seat["connected"])
        self.engine = game
        self._pending_events.extend(events)
        self._ok(conn, msg)
        self._broadcast_room()
        return True

    def _handle_rules(self, conn: _Conn, msg: dict) -> bool:
        if not self._require_host(conn, msg):
            return False
        if self.engine is not None:
            return self._fail(conn, msg, "not_lobby")
        data = msg.get("rules")
        if not isinstance(data, dict):
            return self._fail(conn, msg, "bad_msg")
        allowed = Rules().as_dict()
        merged = dict(self.rules)
        try:
            for key, value in data.items():
                if key not in allowed:
                    continue
                merged[key] = type(allowed[key])(value)
            # clamp numeric rules (start_cards, target_score, ...) into safe ranges
            clean = Rules.from_dict(merged).as_dict()
        except (TypeError, ValueError, OverflowError):
            return self._fail(conn, msg, "bad_msg")
        self.rules = {key: clean[key] for key in allowed}
        self._ok(conn, msg)
        self._broadcast_room()
        return False

    def _handle_kick(self, conn: _Conn, msg: dict) -> bool:
        if not self._require_host(conn, msg):
            return False
        if self.engine is not None:
            return self._fail(conn, msg, "not_lobby")
        pid = msg.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid < len(self.seats):
            return self._fail(conn, msg, "bad_target")
        if pid == 0:
            return self._fail(conn, msg, "bad_target")
        seat = self.seats[pid]
        victim = seat["conn"]
        del self.seats[pid]
        self._reindex_seats()
        if victim is not None:
            victim.pid = None
            victim.joined = False
            victim.send({"t": "kicked", "reason": "kicked"})
            victim.close(flush=0.4)
        self._ok(conn, msg)
        self._broadcast_room()
        return False

    def _handle_name(self, conn: _Conn, msg: dict) -> bool:
        new_name = _safe_name(msg.get("name"), "")
        if not new_name:
            return self._fail(conn, msg, "bad_msg")
        with self.lock:
            if self._name_taken(new_name, ignore_conn=conn):
                return self._fail(conn, msg, "name_taken")
            if conn.spectate:
                for person in self.spectators:
                    if person["conn"] is conn:
                        person["name"] = new_name
                        break
            elif conn.pid is not None and 0 <= conn.pid < len(self.seats):
                self.seats[conn.pid]["name"] = new_name
                if self.engine is not None and conn.pid < len(self.engine.players):
                    self.engine.players[conn.pid].name = new_name     # mid-game rename
            conn.name = new_name
        self._ok(conn, msg)
        self._broadcast_room()
        if self.engine is not None:
            self._broadcast_state()
        return False

    def _handle_avatar(self, conn: _Conn, msg: dict) -> bool:
        """A seated player sets (or clears, with "") their own avatar."""
        data = msg.get("data")
        if conn.spectate or conn.pid is None:
            return self._fail(conn, msg, "not_a_player")
        if data != "" and not _valid_avatar(data):
            return self._fail(conn, msg, "bad_msg")
        with self.lock:
            if not 0 <= conn.pid < len(self.seats) or self.seats[conn.pid]["conn"] is not conn:
                return self._fail(conn, msg, "not_a_player")
            self.seats[conn.pid]["avatar"] = data
        self._ok(conn, msg)
        self._broadcast_room()
        return False

    def _handle_avatar_get(self, conn: _Conn, msg: dict) -> bool:
        """Send the requested seats' avatars to this connection only."""
        pids = msg.get("pids")
        if not isinstance(pids, list):
            return self._fail(conn, msg, "bad_msg")
        out = []
        with self.lock:
            for pid in pids[:MAX_SEATS]:
                if isinstance(pid, bool) or not isinstance(pid, int):
                    continue
                if 0 <= pid < len(self.seats):
                    data = self.seats[pid].get("avatar") or ""
                    if data:
                        out.append({"t": "avatar", "pid": pid,
                                    "v": _avatar_version(data), "data": data})
        for item in out:
            conn.send(item)
        return False

    def _handle_next_round(self, conn: _Conn, msg: dict) -> bool:
        if not self._require_host(conn, msg):
            return False
        if self.engine is None:
            return self._fail(conn, msg, "not_playing")
        if self.engine.phase != "round_over":
            return self._fail(conn, msg, "not_round_over")
        # The winner of the finished round leads the next one (engine
        # docstring: "winner leads next"); pass it explicitly so the behaviour
        # does not depend on the engine's default rotation.
        winner = self.engine.round_winner
        events = self.engine.next_round(first=winner if winner is not None else None)
        if events and events[0].get("t") == "error":
            return self._fail(conn, msg, events[0].get("code") or "illegal")
        self._pending_events.extend(events)
        self._ok(conn, msg)
        self._broadcast_room()
        return True

    def _handle_rematch(self, conn: _Conn, msg: dict) -> bool:
        if not self._require_host(conn, msg):
            return False
        if self.engine is None:
            return self._fail(conn, msg, "not_playing")
        if self.engine.phase != "match_over":
            return self._fail(conn, msg, "illegal")
        self.engine.reset_match()
        events = self.engine.start_round(first=0)
        self._pending_events.extend(events)
        for index, seat in enumerate(self.seats):
            self.engine.players[index].connected = bool(seat["connected"])
        self._ok(conn, msg)
        self._broadcast_room()
        return True

    # -- UDP discovery responder ------------------------------------------- #
    def _start_discovery(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
            sock.settimeout(0.2)
        except OSError:
            try:
                sock.close()
            finally:
                self.discovery_bound = False
            return
        self._discovery_sock = sock
        self.discovery_bound = True
        self._discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True,
                                                 name="uno-discovery")
        self._discovery_thread.start()

    def _discovery_reply(self, data: bytes) -> bytes | None:
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(obj, dict) or obj.get("t") != "discover":
            return None
        if obj.get("v") != PROTOCOL_VERSION:
            return None
        room = self._room_dict()
        room.update({"t": "room", "v": PROTOCOL_VERSION,
                     "nonce": obj.get("nonce"), "host": self._host_ip,
                     "port": self.port})
        return _json_line(room)

    def _discovery_loop(self) -> None:
        sock = self._discovery_sock
        if sock is None:
            return
        last_announce = 0.0
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(MAX_LINE)
            except socket.timeout:
                pass
            except OSError:
                break
            else:
                reply = self._discovery_reply(data)
                if reply is not None:
                    try:
                        sock.sendto(reply, (addr[0], addr[1]))
                    except OSError:
                        pass
            now = _now()
            if now - last_announce >= DISCOVERY_ANNOUNCE:
                last_announce = now
                room = self._room_dict()
                room.update({"t": "room", "v": PROTOCOL_VERSION,
                             "host": self._host_ip, "port": self.port})
                payload = _json_line(room)
                for target in ["255.255.255.255"] + _lan_broadcasts():
                    try:
                        sock.sendto(payload, (target, DISCOVERY_PORT))
                    except OSError:
                        pass


# --------------------------------------------------------------------------- #
# Client side
# --------------------------------------------------------------------------- #
class NetClient:
    """Threaded JSON-lines client used by both joining players and the host UI."""

    def __init__(self, name: str = "player", spectate: bool = False, password: str = "") -> None:
        self.name = name
        self.spectate = bool(spectate)
        self.password = str(password or "")
        # Client-generated reconnect token: the seat is reserved under this
        # token while disconnected, so a reconnect restores pid + hand.
        self.token = uuid.uuid4().hex
        self.pid: int | None = None
        self.welcome: dict | None = None
        self.waiting = False

        self._sock: socket.socket | None = None
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._wire_lock = threading.Lock()
        self._closed = threading.Event()
        self._alive = False
        self._rx: queue.Queue = queue.Queue()
        self._pending: list[dict] = []
        self._reader: threading.Thread | None = None
        self._gen = 0

    # -- connection --------------------------------------------------------- #
    def connect(self, host: str, port: int = DEFAULT_PORT,
                timeout: float = 5.0) -> dict:
        """Open the socket, send ``hello`` and wait for ``welcome``."""
        self.close()
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(None)
        with self._state_lock:
            self._sock = sock
            self._gen += 1
            generation = self._gen
            self._rx = queue.Queue()
            self._pending = []
            self._closed.clear()
            self._alive = True
        self._reader = threading.Thread(target=self._read_loop, args=(sock, generation),
                                        daemon=True, name="uno-net-client")
        self._reader.start()
        try:
            self._send_raw({"t": "hello", "v": PROTOCOL_VERSION, "name": self.name,
                            "spectate": bool(self.spectate), "token": self.token,
                            "password": self.password})
        except OSError:
            self.close()
            raise ConnectionError("could not send hello")
        deadline = _now() + max(0.1, float(timeout))
        while True:
            remaining = deadline - _now()
            if remaining <= 0:
                self.close()
                raise TimeoutError("UNO server did not answer with welcome")
            try:
                msg = self._rx.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if not self._alive:
                    self.close()
                    raise ConnectionError("connection closed before welcome")
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("t") == "welcome":
                self.welcome = msg
                self.pid = msg.get("you")
                return msg
            if msg.get("t") == "waiting":
                # the host is deciding: wait as long as it may take
                self.waiting = True
                deadline = _now() + float(msg.get("timeout") or JOIN_APPROVAL_TIMEOUT) + 5.0
                continue
            if msg.get("t") == "error":
                code = msg.get("code") or "error"
                self.close()
                raise ConnectionError(f"server rejected connection: {code}")
            self._pending.append(msg)

    def _read_loop(self, sock: socket.socket, generation: int) -> None:
        buf = b""
        try:
            while not self._closed.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 4 * MAX_LINE and b"\n" not in buf:
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if len(line) > MAX_LINE:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if isinstance(msg, dict):
                        self._rx.put(msg)
        finally:
            with self._state_lock:
                if generation == self._gen:
                    self._alive = False
                    self._closed.set()
            try:
                sock.close()
            except OSError:
                pass

    # -- io ----------------------------------------------------------------- #
    def _send_raw(self, obj: dict) -> None:
        data = _json_line(obj)
        with self._wire_lock:
            sock = self._sock
            if sock is None or self._closed.is_set():
                raise ConnectionError("not connected")
            sock.sendall(data)

    def send(self, msg: dict) -> None:
        """Thread-safe fire-and-forget send; never raises."""
        try:
            if not isinstance(msg, dict):
                return
            self._send_raw(msg)
        except (OSError, ConnectionError, TypeError, ValueError):
            self._alive = False

    def poll(self) -> list[dict]:
        """Drain and return every message received since the previous call."""
        out = self._pending
        self._pending = []
        while True:
            try:
                out.append(self._rx.get_nowait())
            except queue.Empty:
                break
        return out

    def alive(self) -> bool:
        return bool(self._sock is not None and not self._closed.is_set() and self._alive)

    def close(self) -> None:
        self._closed.set()
        self._alive = False
        with self._state_lock:
            self._gen += 1
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self.pid = None


# --------------------------------------------------------------------------- #
# Host convenience wrapper (what the host UI uses)
# --------------------------------------------------------------------------- #
class HostSession:
    """Own a room: start the server, then attach the host's own loopback client."""

    def __init__(self, name: str, port: int = DEFAULT_PORT, bind: str = "0.0.0.0",
                 rules: dict | None = None, discovery: bool = True,
                 room_name: str = "", lang: str = "zh") -> None:
        self.name = name
        self.requested_port = int(port)
        self.bind = bind
        self.discovery = bool(discovery)
        self.lang = lang
        if not room_name:
            room_name = (f"{name} 的 UNO 房間" if str(lang).startswith("zh")
                         else f"{name}'s UNO Room")
        self.room_name = room_name
        self.server = RoomServer(bind=bind, port=port, rules=rules,
                                 discovery=discovery, room_name=room_name, lang=lang)
        self.client = NetClient(name)
        self._started = False
        self._lock = threading.Lock()

    @property
    def engine(self) -> UnoGame | None:
        return self.server.engine

    @property
    def port(self) -> int:
        return self.server.port if self.server.port is not None else self.requested_port

    def start(self, timeout: float = 5.0) -> dict:
        with self._lock:
            if self._started:
                raise RuntimeError("HostSession already started")
            self._started = True
            self.server.start()
            try:
                return self.client.connect("127.0.0.1", self.server.port, timeout=timeout)
            except Exception:
                self.client.close()
                self.server.close()
                raise

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.server.close()
