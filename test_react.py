"""Loopback tests for emoji reactions (python3 -m unittest discover -s tests -v)."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net  # noqa: E402


def _drain(*clients, wait: float = 0.4):
    time.sleep(wait)
    return [c.poll() for c in clients]


class ReactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = net.HostSession("Kyle", port=45890, discovery=False)
        self.host.start()
        port = self.host.port
        self.bob = net.NetClient("Bob")
        self.bob.connect("127.0.0.1", port, timeout=5)
        self.spec = net.NetClient("Watcher", spectate=True)
        self.spec.connect("127.0.0.1", port, timeout=5)
        _drain(self.host.client, self.bob, self.spec)

    def tearDown(self) -> None:
        self.bob.close()
        self.spec.close()
        self.host.close()

    @staticmethod
    def _reacts(msgs):
        return [m for m in msgs if m.get("t") == "react"]

    def test_broadcast_to_everyone(self):
        self.bob.send({"t": "react", "emoji": "😂", "id": 1})
        for msgs in _drain(self.host.client, self.bob, self.spec):
            got = self._reacts(msgs)
            self.assertEqual(len(got), 1)
            self.assertEqual((got[0]["emoji"], got[0]["name"], got[0]["pid"]), ("😂", "Bob", 1))

    def test_cooldown_is_quiet(self):
        self.bob.send({"t": "react", "emoji": "😂", "id": 1})
        _drain(self.host.client, self.bob)
        self.bob.send({"t": "react", "emoji": "👍", "id": 2})
        host_msgs, bob_msgs = _drain(self.host.client, self.bob, wait=0.3)
        self.assertFalse(self._reacts(host_msgs))
        self.assertFalse([m for m in bob_msgs if m.get("t") == "error"])

    def test_rejects_unknown_emoji_and_spectators(self):
        self.bob.send({"t": "react", "emoji": "💣", "id": 1})
        self.bob.send({"t": "react", "emoji": 5, "id": 2})
        self.spec.send({"t": "react", "emoji": "👍", "id": 3})
        host_msgs, bob_msgs, spec_msgs = _drain(self.host.client, self.bob, self.spec)
        self.assertFalse(self._reacts(host_msgs))
        self.assertEqual(len([m for m in bob_msgs if m.get("t") == "error"]), 2)
        self.assertEqual(len([m for m in spec_msgs if m.get("t") == "error"]), 1)

    def test_special_reactions_only_for_the_leader(self):
        self.host.client.send({"t": "start"})
        _drain(self.host.client, self.bob, self.spec, wait=0.8)
        engine = self.host.engine
        self.assertIsNotNone(engine)
        # nobody ahead yet (all scores 0): locked for everyone
        self.bob.send({"t": "react", "emoji": "👑", "id": 1})
        host_msgs, bob_msgs = _drain(self.host.client, self.bob)
        self.assertFalse(self._reacts(host_msgs))
        self.assertTrue([m for m in bob_msgs if m.get("t") == "error" and m.get("code") == "react_locked"])
        # Bob takes the lead
        engine.players[1].score = 120
        engine.players[0].score = 40
        time.sleep(net.REACT_COOLDOWN)
        self.bob.send({"t": "react", "emoji": "👑", "id": 2})
        host_msgs, = _drain(self.host.client)
        self.assertEqual([m["emoji"] for m in self._reacts(host_msgs)], ["👑"])
        # the host (behind) is locked out, but may still send a normal one
        self.host.client.send({"t": "react", "emoji": "🔥", "id": 3})
        host_msgs, = _drain(self.host.client)
        self.assertTrue([m for m in host_msgs if m.get("code") == "react_locked"])
        self.host.client.send({"t": "react", "emoji": "👍", "id": 4})
        host_msgs, = _drain(self.host.client)
        self.assertEqual([m["emoji"] for m in self._reacts(host_msgs)], ["👍"])
        # a tie shares the lead
        engine.players[0].score = 120
        time.sleep(net.REACT_COOLDOWN)
        self.host.client.send({"t": "react", "emoji": "😎", "id": 5})
        host_msgs, = _drain(self.host.client)
        self.assertEqual([m["emoji"] for m in self._reacts(host_msgs)], ["😎"])

    def test_palette_matches_ui(self):
        import re
        with open(os.path.join(os.path.dirname(net.__file__), "ui.py"), encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(r"^REACTIONS = \((.*?)\)$", text, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', match.group(1))), net.REACTIONS)
        special = re.search(r"^SPECIAL_REACTIONS = \((.*?)\)", text, re.M)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', special.group(1))), net.SPECIAL_REACTIONS)


if __name__ == "__main__":
    unittest.main()
