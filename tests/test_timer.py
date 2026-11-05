"""Loopback test for the turn timer (python3 -m unittest discover -s tests -v)."""
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


class TurnTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = net.HostSession("Kyle", port=45930, discovery=False)
        self.host.start()
        self.bob = net.NetClient("Bob")
        self.bob.connect("127.0.0.1", self.host.port, timeout=5)
        _drain(self.host.client, self.bob)

    def tearDown(self) -> None:
        self.bob.close()
        self.host.close()

    def _start(self, seconds: float):
        """Set the rule, start the game, return what the two clients received while it began."""
        self.host.client.send({"t": "rules", "rules": {"turn_time": seconds}})
        _drain(self.host.client, self.bob, wait=0.3)
        self.host.client.send({"t": "start"})
        host_msgs, bob_msgs = _drain(self.host.client, self.bob, wait=0.8)
        return host_msgs + bob_msgs

    def test_rule_is_clamped_and_broadcast(self):
        self.host.client.send({"t": "rules", "rules": {"turn_time": 99999}})
        _drain(self.host.client, self.bob)
        self.assertEqual(self.host.server.rules["turn_time"], 300.0)
        self.host.client.send({"t": "rules", "rules": {"turn_time": 30}})
        _drain(self.host.client, self.bob)
        self.assertEqual(self.host.server.rules["turn_time"], 30.0)

    def test_timeout_draws_and_passes_for_everyone(self):
        received = self._start(2)
        engine = self.host.engine
        self.assertIsNotNone(engine)
        first = engine.turn
        hand_before = len(engine.players[first].hand)
        snaps = [m for m in received if m.get("snap")]
        self.assertTrue(snaps, "state broadcasts carry the timer")
        timer = snaps[-1]["snap"]["turn_timer"]
        self.assertEqual(timer["total"], 2.0)
        self.assertLessEqual(timer["remaining"], 2.0)
        host_msgs, bob_msgs = _drain(self.host.client, self.bob, wait=3.2)
        for msgs in (host_msgs, bob_msgs):
            kinds = [e["t"] for m in msgs if m.get("t") == "events" for e in m["items"]]
            self.assertIn("turn_timeout", kinds)
        self.assertNotEqual(engine.turn, first)
        self.assertGreater(len(engine.players[first].hand), hand_before)

    def test_off_by_default(self):
        self.host.client.send({"t": "start"})
        _drain(self.host.client, self.bob, wait=0.6)
        engine = self.host.engine
        first = engine.turn
        _drain(self.host.client, self.bob, wait=2.5)
        self.assertEqual(engine.turn, first)
        self.assertIsNone(engine.snapshot(0)["turn_timer"])


if __name__ == "__main__":
    unittest.main()
