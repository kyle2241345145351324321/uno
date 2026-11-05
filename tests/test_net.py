#!/usr/bin/env python3
"""Network-layer tests for UNO (stdlib unittest, no third-party deps).

Run from the project root::

    python3 -m unittest discover -s tests -v

Every test hosts the room on 127.0.0.1 with ``port=0`` (ephemeral), so the
suite never fights over the default port.  All servers/clients are closed in
cleanups, so the process exits promptly.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

UNO_DIR = Path(__file__).resolve().parent.parent
if str(UNO_DIR) not in sys.path:
    sys.path.insert(0, str(UNO_DIR))

import net  # noqa: E402
from engine import COLORS, card_value  # noqa: E402

ALL_CARDS = (
    [color + value for color in COLORS
     for value in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                   "skip", "reverse", "draw2")]
    + ["wild", "wild4"]
)
DEFAULT_START_CARDS = 7


def clock() -> float:
    return time.monotonic()


def free_card_not_in(hand) -> str:
    hand = set(hand)
    for card in ALL_CARDS:
        if card not in hand:
            return card
    raise AssertionError("hand covers the whole deck")


def play_message(snap: dict, card: str) -> dict:
    """Build a legal play message (wilds need a color that differs from top)."""
    msg = {"t": "play", "card": card}
    if card.startswith("wild"):
        current = snap.get("current_color")
        msg["color"] = next(color for color in COLORS if color != current)
    return msg


class Peer:
    """A NetClient plus a growing mailbox so waits never lose messages."""

    def __init__(self, client: net.NetClient) -> None:
        self.client = client
        self.welcome = client.welcome
        self._messages: list[dict] = []
        self._lock = threading.Lock()
        self._next_id = 0

    # -- raw io ------------------------------------------------------------ #
    def send(self, msg: dict) -> None:
        self.client.send(msg)

    def close(self) -> None:
        self.client.close()

    @property
    def pid(self):
        return self.client.pid

    def pump(self) -> list[dict]:
        msgs = self.client.poll()
        if msgs:
            with self._lock:
                self._messages.extend(msgs)
        return msgs

    def messages(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def latest_state(self):
        with self._lock:
            for msg in reversed(self._messages):
                if msg.get("t") == "state":
                    return msg["snap"]
        self.pump()
        with self._lock:
            for msg in reversed(self._messages):
                if msg.get("t") == "state":
                    return msg["snap"]
        return None

    def wait(self, predicate, timeout: float = 6.0) -> dict:
        """Wait for a buffered-or-new message matching ``predicate``.

        The whole mailbox is rescanned every iteration: a server tick can put
        ``ack`` + ``error`` + ``events`` + ``state`` into the socket in one
        burst, so scrolling only past the previous match would lose messages.
        """
        deadline = clock() + timeout
        while True:
            self.pump()
            with self._lock:
                for msg in self._messages:
                    if predicate(msg):
                        return msg
            if clock() >= deadline:
                with self._lock:
                    tail = self._messages[-8:]
                raise AssertionError(f"timed out waiting for message; tail={tail!r}")
            time.sleep(0.01)

    def wait_state(self, predicate=None, timeout: float = 6.0,
                   min_seq: int | None = None) -> dict:
        """Wait for a state snapshot; ``min_seq`` rejects stale buffered ones."""
        def match(msg):
            if msg.get("t") != "state":
                return False
            snap = msg["snap"]
            if min_seq is not None and snap.get("event_seq", 0) < min_seq:
                return False
            return predicate is None or predicate(snap)

        return self.wait(match, timeout)["snap"]

    def wait_code(self, code: str, kind: str = "error", timeout: float = 5.0) -> dict:
        return self.wait(lambda m: m.get("t") == kind and m.get("code") == code, timeout)

    def wait_events(self, types, timeout: float = 5.0,
                    after_seq: int | None = None) -> dict:
        """Wait for an ``events`` broadcast containing one of ``types``."""
        wanted = (types,) if isinstance(types, str) else tuple(types)

        def match(msg):
            if msg.get("t") != "events":
                return False
            items = msg.get("items") or []
            if after_seq is not None and not any(
                    item.get("seq", 0) > after_seq for item in items):
                return False
            return any(item.get("t") in wanted for item in items)

        return self.wait(match, timeout)

    # -- request/ack -------------------------------------------------------- #
    def action(self, msg: dict, *, expect_ok: bool = True, timeout: float = 5.0) -> dict:
        self._next_id += 1
        payload = dict(msg)
        payload["id"] = self._next_id
        self.send(payload)
        ack = self.wait(
            lambda m: m.get("t") == "ack" and m.get("id") == payload["id"], timeout)
        if expect_ok:
            if not ack.get("ok"):
                raise AssertionError(f"action {msg} failed: {ack!r}")
        else:
            if ack.get("ok"):
                raise AssertionError(f"action {msg} unexpectedly succeeded: {ack!r}")
            if "code" in ack and not ack["code"]:
                raise AssertionError(f"failed ack without code: {ack!r}")
        return ack



def advance(room, peer, pid, predicate, timeout: float = 15.0):
    """Let one seat auto-play until ``predicate(snapshot)`` holds."""
    driver = Driver(peer, pid)
    deadline = clock() + timeout
    while clock() < deadline:
        room.owner.pump()
        snap = room.owner.latest_state()
        if snap is not None and predicate(snap):
            time.sleep(0.04)
            room.owner.pump()
            settled = room.owner.latest_state()
            if settled is not None and predicate(settled):
                return settled
        driver.step()
        room.owner.pump()
        snap = room.owner.latest_state()
        if snap is not None and predicate(snap):
            # settle: make sure no queued action flips the turn right after
            time.sleep(0.06)
            room.owner.pump()
            settled = room.owner.latest_state()
            if settled is not None and predicate(settled):
                return settled
            continue
        time.sleep(0.01)
    raise AssertionError(f"advance timed out; last={room.owner.latest_state()!r}")


class Room:
    """One hosted room (server + host loopback client) with lazy peers."""

    def __init__(self, test: unittest.TestCase, *,
                 discovery: bool = False, host_name: str = "host",
                 lang: str = "zh") -> None:
        self._test = test
        self.host = net.HostSession(host_name, port=0, discovery=discovery, lang=lang)
        self.welcome = self.host.start(timeout=5.0)
        self.owner = Peer(self.host.client)
        self.peers: dict[str, Peer] = {}
        self._closed = False
        test.addCleanup(self.close)

    def join(self, name: str, *, spectate: bool = False) -> Peer:
        client = net.NetClient(name, spectate=spectate)
        client.connect("127.0.0.1", self.host.port, timeout=5.0)
        peer = Peer(client)
        peer.welcome = client.welcome
        self.peers[name] = peer
        self._test.addCleanup(peer.close)
        return peer

    def reconnect(self, name: str, token: str, *, spectate: bool = False) -> Peer:
        client = net.NetClient(name, spectate=spectate)
        client.token = token
        client.connect("127.0.0.1", self.host.port, timeout=5.0)
        peer = Peer(client)
        peer.welcome = client.welcome
        self._test.addCleanup(peer.close)
        return peer

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for peer in list(self.peers.values()):
            try:
                peer.close()
            except Exception:  # pragma: no cover - cleanup best effort
                pass
        try:
            self.owner.close()
        finally:
            self.host.close()


class Driver:
    """Auto-plays one seat from state snapshots (test scaffolding)."""

    def __init__(self, peer: Peer, pid: int = 0) -> None:
        self.peer = peer
        self.pid = pid
        self.snap = None
        self.seq = None
        self._acted_seq = None
        self._next_id = 0

    def pump(self):
        for msg in self.peer.pump():
            if msg.get("t") == "ack" and not msg.get("ok"):
                self._acted_seq = None
        self.snap = self.peer.latest_state()
        self.seq = self.snap.get("event_seq") if self.snap else None
        return self.snap

    def choose(self):
        snap = self.snap
        if not snap or snap.get("phase") != "playing":
            return None
        chooser = snap.get("chooser")
        if chooser:
            if chooser.get("pid") != self.pid:
                return None
            others = [p for p in snap["players"] if p["pid"] != self.pid]
            target = max(others, key=lambda p: (p["count"], p["pid"]))["pid"]
            return {"t": "swap", "target": target}
        if snap.get("turn") != self.pid:
            return None
        if snap.get("drawn"):
            legal = snap.get("legal") or []
            return play_message(snap, legal[0]) if legal else {"t": "pass"}
        legal = snap.get("legal") or []
        if legal:
            return play_message(snap, legal[0])
        return {"t": "draw"}

    def step(self):
        snap = self.pump()
        if not snap:
            return None
        if self._acted_seq is not None and self._acted_seq == self.seq:
            return None
        action = self.choose()
        if action is None:
            return None
        self._acted_seq = self.seq
        self._next_id += 1
        action["id"] = self._next_id
        self.peer.send(action)
        return action

    def drive(self, predicate, timeout: float = 12.0) -> dict:
        deadline = clock() + timeout
        while clock() < deadline:
            snap = self.pump()
            if snap is not None and predicate(snap):
                return snap
            self.step()
            time.sleep(0.01)
        raise AssertionError(f"driver timed out; last snapshot={self.snap!r}")


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01):
    deadline = clock() + timeout
    while clock() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# Raw socket helpers (framing / protocol errors)
# --------------------------------------------------------------------------- #
class RawSocket:
    def __init__(self, host: str, port: int, timeout: float = 4.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def send_line(self, payload) -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.sock.sendall(data + b"\n")

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_message(self, timeout: float = 4.0) -> dict:
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("socket closed before a full line arrived")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line.decode("utf-8"))

    def read_until(self, predicate, timeout: float = 4.0) -> dict:
        """Read lines until one matches (room broadcasts get skipped)."""
        deadline = clock() + timeout
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise AssertionError("no matching message before timeout")
            try:
                msg = self.read_message(timeout=remaining)
            except socket.timeout:
                continue
            if predicate(msg):
                return msg

    def wait_closed(self, timeout: float = 4.0) -> bool:
        """Drain until EOF; True when the server closed the socket."""
        self.sock.settimeout(timeout)
        deadline = clock() + timeout
        while clock() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return False
            if not chunk:
                return True
            self.buf += chunk
        return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class LobbyTests(unittest.TestCase):
    def test_lobby_sync_seats_and_room_broadcasts(self):

        room = Room(self)
        welcome = room.welcome
        self.assertEqual(welcome["t"], "welcome")
        self.assertEqual(welcome["v"], net.PROTOCOL_VERSION)
        self.assertEqual(welcome["you"], 0)
        self.assertEqual(welcome["room"]["state"], "lobby")
        self.assertEqual(welcome["chat"], [])
        seats = welcome["room"]["seats"]
        self.assertEqual(len(seats), 1)
        self.assertEqual(seats[0]["pid"], 0)
        self.assertEqual(seats[0]["name"], "host")
        self.assertTrue(seats[0]["host"])
        self.assertNotIn("ai", seats[0])

        bob = room.join("bob")
        self.assertEqual(bob.welcome["you"], 1)
        cara = room.join("cara")
        self.assertEqual(cara.welcome["you"], 2)
        room.owner.wait(lambda m: m.get("t") == "room"
                        and len(m["room"]["seats"]) == 3)
        self.assertEqual([s["pid"] for s in
                          room.owner.wait(lambda m: m.get("t") == "room"
                                          and len(m["room"]["seats"]) == 3
                                          )["room"]["seats"]], [0, 1, 2])

        # rule edits are validated and broadcast
        room.owner.action({"t": "rules", "rules": {"stack_draw2": False,
                                                   "start_cards": 5}})
        rules_msg = room.owner.wait(
            lambda m: m.get("t") == "room"
            and m["room"]["rules"].get("stack_draw2") is False)
        self.assertEqual(rules_msg["room"]["rules"]["start_cards"], 5)

        # renaming in the lobby
        bob.action({"t": "name", "name": "bobby"})
        named = room.owner.wait(
            lambda m: m.get("t") == "room"
            and any(s["name"] == "bobby" for s in m["room"]["seats"]))
        self.assertIsNotNone(named)

        # non-host cannot mutate the lobby or start the game
        ack = bob.action({"t": "rules", "rules": {"stack_draw2": True}},
                         expect_ok=False)
        self.assertEqual(ack["code"], "not_host")
        ack = bob.action({"t": "start"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")

        # the lobby has no bot commands any more
        ack = room.owner.action({"t": "add_ai", "count": 1}, expect_ok=False)
        self.assertEqual(ack["code"], "unknown_type")

    def test_chat_fanout_welcome_history_and_validation(self):
        room = Room(self)
        bob = room.join("bob")
        spec = room.join("spec", spectate=True)

        bob.action({"t": "chat", "text": "hello table"})
        for peer in (bob, room.owner, spec):
            msg = peer.wait(lambda m: m.get("t") == "chat")
            self.assertEqual(msg["pid"], 1)
            self.assertEqual(msg["name"], "bob")
            self.assertEqual(msg["text"], "hello table")
            self.assertIn("ts", msg)

        # newest is last, and later joiners get the history
        room.owner.action({"t": "chat", "text": "second"})
        room.owner.wait(lambda m: m.get("t") == "chat" and m["text"] == "second")
        carol = room.join("carol")
        chat = carol.welcome["chat"]
        self.assertLessEqual(len(chat), net.MAX_CHAT)
        self.assertEqual(chat[-2]["text"], "hello table")
        self.assertEqual(chat[-1]["text"], "second")
        for entry in chat:
            for key in ("pid", "name", "text", "ts"):
                self.assertIn(key, entry)

        # empty chat is a protocol error
        ack = bob.action({"t": "chat", "text": "   "}, expect_ok=False)
        self.assertEqual(ack["code"], "bad_msg")
        bob.wait_code("bad_msg")

        # over-long chat is truncated, not rejected
        bob.action({"t": "chat", "text": "x" * 400})
        clipped = spec.wait(
            lambda m: m.get("t") == "chat" and m["text"].startswith("xxx"))
        self.assertEqual(len(clipped["text"]), net.MAX_CHAT_CHARS)


class GameStartTests(unittest.TestCase):
    def test_start_requires_host_and_two_seats(self):
        room = Room(self)
        # A host alone cannot start (needs >= 2 seats).
        ack = room.owner.action({"t": "start"}, expect_ok=False)
        self.assertEqual(ack["code"], "need_players")
        self.assertIsNone(room.host.engine)
        # Non-hosts can never start, even when enough seats exist.
        bob = room.join("bob")
        ack = bob.action({"t": "start"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")
        self.assertIsNone(room.host.engine)

    def test_privacy_spectator_reveal_and_state_room_field(self):

        room = Room(self)
        bob = room.join("bob")
        cara = room.join("cara")
        dave = room.join("dave")
        room.owner.action({"t": "start"})
        owner_snap = room.owner.wait_state(lambda s: s["phase"] == "playing")
        bob_snap = bob.wait_state(lambda s: s["phase"] == "playing")
        self.assertIsNotNone(room.host.engine)
        self.assertEqual(len(owner_snap["players"]), 4)

        # players only ever receive their own hand
        self.assertEqual(len(bob_snap["hand"]), DEFAULT_START_CARDS)
        self.assertEqual(bob_snap["revealed"], {})
        for player in bob_snap["players"]:
            self.assertNotIn("hand", player)
        self.assertNotEqual(owner_snap["hand"], bob_snap["hand"])

        # spectator joining mid-game sees the broadcast + every hand
        spec = room.join("spec", spectate=True)
        self.assertIsNone(spec.welcome["you"])
        self.assertIsNotNone(spec.welcome["snap"])
        spec_snap = spec.welcome["snap"]
        self.assertEqual(spec_snap["hand"], [])
        self.assertEqual(set(spec_snap["revealed"]), {str(i) for i in range(4)})
        self.assertTrue(all(len(hand) >= 1 for hand in spec_snap["revealed"].values()))

        # every state message carries the room object as a convenience
        state_msg = bob.wait(lambda m: m.get("t") == "state")
        self.assertIn("room", state_msg)
        self.assertEqual(state_msg["room"]["state"], "playing")
        self.assertEqual(len(state_msg["room"]["seats"]), 4)
        self.assertEqual(state_msg["seq"], state_msg["snap"]["event_seq"])

        # spectators may not act as players or hosts
        ack = spec.action({"t": "play", "card": "R1"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_a_player")
        ack = spec.action({"t": "start"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")

    def test_token_reconnect_restores_seat_and_auto_pass(self):

        room = Room(self)
        bob = room.join("bob")
        room.owner.action({"t": "start"})
        bob.wait_state(lambda s: s["phase"] == "playing")
        self.assertEqual(bob.welcome["you"], 1)
        token = bob.client.token
        self.assertTrue(token)

        bob.close()
        disconnected = room.owner.wait_state(
            lambda s: s["players"][1]["connected"] is False, timeout=6.0)
        self.assertEqual(len(disconnected["players"]), 2)  # seat kept, not freed

        # No bot takes the seat over: a disconnected player's turn is released
        # by the server after AUTO_PASS_GRACE so the table never freezes.
        with room.host.server.lock:
            room.host.server.AUTO_PASS_GRACE = 0.2
            engine = room.host.engine
            engine.turn = 1
            engine.drawn = None
            engine.chooser = None
            engine.uno_window = None
        base_seq = disconnected["event_seq"]
        served = room.owner.wait_state(
            lambda s: s["players"][1]["connected"] is False
            and (s["phase"] != "playing" or s["event_seq"] > base_seq),
            timeout=6.0)
        self.assertIn(served["phase"], ("playing", "round_over", "match_over"))

        # a fresh connection without the token is not allowed mid-game
        stranger = net.NetClient("stranger")
        try:
            with self.assertRaises(ConnectionError) as ctx:
                stranger.connect("127.0.0.1", room.host.port, timeout=3.0)
            self.assertIn("not_lobby", str(ctx.exception))
        finally:
            stranger.close()

        again = room.reconnect("bob", token)
        self.assertEqual(again.welcome["you"], 1)
        self.assertEqual(len(again.welcome["room"]["seats"]), 2)
        restored = again.wait_state(
            lambda s: s["players"][1]["connected"] is True, timeout=6.0)
        self.assertEqual(restored["players"][1]["name"], "bob")
        self.assertEqual(len(restored["players"]), 2)
        again.action({"t": "chat", "text": "back"})
        sent = room.owner.wait(lambda m: m.get("t") == "chat" and m["text"] == "back")
        self.assertEqual(sent["pid"], 1)

    def test_host_engine_is_bound_and_port_is_ephemeral(self):

        room = Room(self)
        self.assertIsNone(room.host.engine)  # created when a game starts
        self.assertGreater(room.host.port, 0)  # port=0 -> ephemeral, advertised
        self.assertEqual(room.host.server.port, room.host.port)
        self.assertEqual(room.host.client.welcome["you"], 0)
        room.join("bob")
        room.owner.action({"t": "start"})
        room.owner.wait_state(lambda s: s["phase"] == "playing")
        self.assertIsNotNone(room.host.engine)
        self.assertEqual(type(room.host.engine).__name__, "UnoGame")


class ActionFlowTests(unittest.TestCase):
    def wait_host_turn(self, room: Room, bob: Peer, timeout: float = 12.0):
        # The host is dealt first (first=0), but the flipped top card may be an
        # action card that skips/reverses; let the other human seat play until
        # it is the host's turn again.
        return advance(room, bob, 1,
                       lambda s: s["phase"] == "playing"
                       and s.get("chooser") is None and s["turn"] == 0,
                       timeout=timeout)

    def test_legal_play_ack_events_and_engine_error_codes(self):

        room = Room(self)
        bob = room.join("bob")
        spec = room.join("spec", spectate=True)
        room.owner.action({"t": "start"})
        snap = self.wait_host_turn(room, bob)
        # The dealt hand may contain no legal card at all: draw until the host
        # actually holds a playable card (turn stays or comes back around).
        while not snap["legal"]:
            base = snap["event_seq"]
            room.owner.action({"t": "draw"})
            room.owner.wait_state(lambda s: s["event_seq"] > base, timeout=3.0)
            snap = self.wait_host_turn(room, bob)
        hand = snap["hand"]

        # illegal card while it is our turn -> engine code not_in_hand
        bogus = free_card_not_in(hand)
        codes = []
        for _attempt in range(8):
            snap = self.wait_host_turn(room, bob)
            self.assertEqual(snap["turn"], 0)
            ack = room.owner.action({"t": "play", "card": bogus}, expect_ok=False)
            codes.append(ack.get("code"))
            if ack.get("code") == "not_in_hand":
                break
            time.sleep(0.05)
        self.assertEqual(ack["code"], "not_in_hand", codes)
        room.owner.wait_code("not_in_hand")

        # a legal card is acked and lands as a play event for everyone.
        # With two players a skip/reverse never hands the turn over, so pick a
        # card whose effect actually passes play to the opponent.
        passing = [c for c in snap["legal"] if card_value(c) not in ("skip", "reverse")]
        card = (passing or snap["legal"])[0]
        room.owner.action(play_message(snap, card))
        events = room.owner.wait(
            lambda m: m.get("t") == "events"
            and any(e.get("t") == "play" and e.get("card") == card
                    for e in m["items"]), timeout=5.0)
        self.assertTrue(events["items"])

        # wait until it is the other player's turn, then check out-of-turn codes
        seat_turn = advance(room, bob, 1,
                            lambda s: s["phase"] == "playing"
                            and s.get("chooser") is None and s["turn"] == 1)
        self.assertNotEqual(seat_turn["turn"], 0)
        room.owner.pump()
        host_hand = room.owner.latest_state()["hand"]

        checks = [
            ({"t": "play", "card": host_hand[0]}, "not_your_turn"),
            ({"t": "draw"}, "not_your_turn"),
            ({"t": "pass"}, "cannot_pass"),
            ({"t": "swap", "target": 0}, "not_choosing"),
            ({"t": "uno"}, "no_uno_window"),
        ]
        for payload, code in checks:
            ack = room.owner.action(payload, expect_ok=False)
            self.assertEqual(ack["code"], code, payload)

        # host-only actions from a non-host are rejected as not_host
        ack = spec.action({"t": "rematch"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")

        ack = room.owner.action({"t": "wibble"}, expect_ok=False)
        self.assertEqual(ack["code"], "unknown_type")

    def test_draw_and_pass_paths(self):

        room = Room(self)
        bob = room.join("bob")
        driver = Driver(bob, 1)
        room.owner.action({"t": "start"})
        saw_draw = False
        saw_pass = False
        deadline = clock() + 20.0
        while clock() < deadline and not (saw_draw and saw_pass):
            driver.step()
            room.owner.pump()
            snap = room.owner.latest_state()
            if snap is None:
                time.sleep(0.02)
                continue
            if snap["phase"] == "round_over":
                base = snap["event_seq"]
                room.owner.action({"t": "next_round"})
                room.owner.wait_state(lambda s: s["event_seq"] > base, timeout=3.0)
                continue
            if (snap["phase"] == "playing" and snap.get("chooser") is None
                    and snap["turn"] == 0):
                base = snap["event_seq"]
                if snap.get("drawn"):
                    # After a playable draw the turn stays with us; a pass
                    # hands it on and therefore must be legal.
                    room.owner.action({"t": "pass"})
                    saw_pass = True
                else:
                    room.owner.action({"t": "draw"})
                    saw_draw = True
                room.owner.wait_state(lambda s: s["event_seq"] > base, timeout=3.0)
            time.sleep(0.01)
        self.assertTrue(saw_draw, "draw action was never acked")
        self.assertTrue(saw_pass, "pass action was never acked")


class RoundFlowTests(unittest.TestCase):
    def test_next_round_and_rematch(self):

        room = Room(self)
        spec = room.join("spec", spectate=True)
        room.join("bob")
        room.join("cara")
        room.owner.action({"t": "start"})
        playing = room.owner.wait_state(lambda s: s["phase"] == "playing")
        self.assertEqual(len(playing["players"]), 3)

        # ---- force a one-card win for the host (engine-level fixture) ---- #
        engine = room.host.engine
        with room.host.server.lock:
            engine.players[0].hand = ["R5"]
            engine.players[0].said_uno = True
            engine.turn = 0
            engine.direction = 1
            engine.discard[-1] = "R7"
            engine.current_color = "R"
            engine.pending = 0
            engine.pending_type = None
            engine.drawn = None
            engine.chooser = None
            engine.uno_window = None
            base = engine.event_seq
        room.owner.action({"t": "play", "card": "R5"})
        snap = room.owner.wait_state(
            lambda s: s["phase"] == "round_over" and s["event_seq"] > base,
            timeout=5.0)
        winner = snap["round_winner"]
        self.assertEqual(winner, 0)
        # showdown hands are public at round end
        self.assertEqual(set(snap["revealed"]), {str(i) for i in range(3)})
        self.assertEqual(len(snap["hand"]), 0)
        previous_round = snap["round_no"]

        # only the host may deal the next round
        ack = spec.action({"t": "next_round"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")

        # the winner of the finished round leads the next one
        base = snap["event_seq"]
        room.owner.action({"t": "next_round"})
        round_start_msg = room.owner.wait_events("round_start", after_seq=base)
        start_event = next(e for e in round_start_msg["items"]
                           if e.get("t") == "round_start")
        self.assertEqual(start_event["first"], winner)
        playing2 = room.owner.wait_state(
            lambda s: s["phase"] == "playing" and s["round_no"] > previous_round,
            timeout=5.0)
        self.assertEqual(playing2["round_no"], previous_round + 1)

        # ---- force the match-over phase (engine-level fixture) ----------- #
        with room.host.server.lock:
            engine = room.host.engine
            engine.players[0].wins = 2
            engine.players[0].total_score = 321
            engine.players[0].score = 100
            engine.players[1].score = 40
            engine.round_winner = 0
            engine.match_winner = 0
            engine.round_points = {0: 40}
            engine.phase = "match_over"
        match_snap = room.owner.wait_state(lambda s: s["phase"] == "match_over",
                                           timeout=5.0)
        self.assertEqual(match_snap["match_winner"], 0)

        ack = spec.action({"t": "rematch"}, expect_ok=False)
        self.assertEqual(ack["code"], "not_host")

        base = match_snap["event_seq"]
        room.owner.action({"t": "rematch"})
        room.owner.wait_events("round_start", after_seq=base)
        fresh = room.owner.wait_state(
            lambda s: s["phase"] == "playing" and s["round_no"] == 1,
            timeout=5.0, min_seq=base)
        self.assertEqual(fresh["phase"], "playing")
        self.assertEqual(fresh["round_no"], 1)
        self.assertTrue(all(p["score"] == 0 for p in fresh["players"]))
        self.assertTrue(all(p["round_score"] == 0 for p in fresh["players"]))
        self.assertEqual(fresh["players"][0]["wins"], 2)       # career kept
        self.assertEqual(fresh["players"][0]["total_score"], 321)
        self.assertIsNone(fresh["match_winner"])
        self.assertIsNone(fresh["round_winner"])
        self.assertGreaterEqual(len(fresh["hand"]), DEFAULT_START_CARDS)

        # rematch is only valid on a finished match
        ack = room.owner.action({"t": "rematch"}, expect_ok=False)
        self.assertEqual(ack["code"], "illegal")


class WireProtocolTests(unittest.TestCase):
    def test_bad_lines_cap_and_ping(self):
        room = Room(self)
        raw = RawSocket("127.0.0.1", room.host.port)
        try:
            raw.send_line({"t": "hello", "v": net.PROTOCOL_VERSION, "name": "raw"})
            welcome = raw.read_until(lambda m: m.get("t") == "welcome")
            self.assertEqual(welcome["you"], 1)

            raw.send_line(b"{this is not json}")
            err = raw.read_until(lambda m: m.get("t") == "error")
            self.assertEqual(err, {"t": "error", "code": "bad_msg"})

            raw.send_line({"t": "ping", "ts": 7})
            pong = raw.read_until(lambda m: m.get("t") == "pong")
            self.assertEqual(pong, {"t": "pong", "ts": 7})

            raw.send_line({"t": "frobnicate"})
            err = raw.read_until(
                lambda m: m.get("t") == "error" and m.get("code") == "unknown_type")
            self.assertEqual(err["t"], "error")
        finally:
            raw.close()

        # an over-long line trips the 64 KiB cap, errors, then closes
        big = RawSocket("127.0.0.1", room.host.port)
        try:
            big.send_line({"t": "hello", "v": net.PROTOCOL_VERSION, "name": "big"})
            big.read_until(lambda m: m.get("t") == "welcome")
            big.send_raw(b'{"t":"chat","text":"' + b"y" * (net.MAX_LINE + 1024))
            err = big.read_until(lambda m: m.get("t") == "error")
            self.assertEqual(err["t"], "error")
            self.assertEqual(err["code"], "bad_msg")
            self.assertTrue(big.wait_closed(), "server did not close after bad_msg")
        finally:
            big.close()

    def test_bad_version_is_rejected(self):
        room = Room(self)
        raw = RawSocket("127.0.0.1", room.host.port)
        try:
            raw.send_line({"t": "hello", "v": 99, "name": "old"})
            err = raw.read_until(lambda m: m.get("t") == "error")
            self.assertEqual(err, {"t": "error", "code": "bad_version"})
        finally:
            raw.close()


class LifecycleTests(unittest.TestCase):
    def test_client_and_host_close_semantics(self):
        hs = net.HostSession("lifecycle", port=0, discovery=False)
        self.assertIsNone(hs.engine)
        welcome = hs.start(timeout=5.0)
        self.assertEqual(welcome["you"], 0)
        self.assertIsNone(hs.engine)  # no game started yet
        self.assertGreater(hs.port, 0)
        with self.assertRaises(RuntimeError):
            hs.start()
        hs.close()
        hs.close()  # idempotent
        self.assertFalse(hs.client.alive())

        client = net.NetClient("solo")
        try:
            with self.assertRaises(OSError):
                client.connect("127.0.0.1", hs.port, timeout=1.0)
        finally:
            client.close()
        client.close()
        self.assertFalse(client.alive())
        self.assertEqual(client.poll(), [])
        client.send({"t": "chat", "text": "gone"})  # must not raise

    def test_host_close_disconnects_clients(self):
        room = Room(self)
        bob = room.join("bob")
        self.assertTrue(bob.client.alive())
        room.host.close()
        self.assertTrue(wait_until(lambda: not bob.client.alive(), timeout=4.0))


class DiscoveryTests(unittest.TestCase):
    def test_discovery_roundtrip_and_dedupe(self):
        hs = net.HostSession("disc", port=0, discovery=True, room_name="Test Room")
        hs.start()
        try:
            if not hs.server.discovery_bound:
                self.skipTest("UDP discovery port 45871 is unavailable")
            rooms = net.discover_rooms(timeout=1.5, extra_hosts=("127.0.0.1",))
            mine = [room for room in rooms if room.get("port") == hs.port]
            if not mine:
                self.skipTest("loopback UDP discovery is blocked on this host")
            room = mine[0]
            self.assertEqual(room["t"], "room")
            self.assertEqual(room["v"], net.PROTOCOL_VERSION)
            self.assertEqual(room["name"], "Test Room")
            self.assertIsInstance(room["host"], str)
            self.assertGreaterEqual(room["players"], 1)
            keys = [(r["host"], r["port"]) for r in rooms]
            self.assertEqual(len(keys), len(set(keys)))  # de-duplicated
        finally:
            hs.close()

    def test_discovery_can_be_disabled(self):
        hs = net.HostSession("nodisc", port=0, discovery=False)
        hs.start()
        try:
            self.assertFalse(hs.server.discovery_bound)
            rooms = net.discover_rooms(timeout=0.6, extra_hosts=("127.0.0.1",))
            self.assertFalse(any(room.get("port") == hs.port for room in rooms))
        finally:
            hs.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
