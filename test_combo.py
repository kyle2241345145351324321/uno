"""Loopback tests for same-number combos (python3 -m unittest discover -s tests -v)."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net  # noqa: E402
import webserver  # noqa: E402


def _drain(*clients, wait: float = 0.5):
    time.sleep(wait)
    return [c.poll() for c in clients]


class ComboTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = net.HostSession("Kyle", port=45960, discovery=False)
        self.host.start()
        self.bob = net.NetClient("Bob")
        self.bob.connect("127.0.0.1", self.host.port, timeout=5)
        _drain(self.host.client, self.bob)

    def tearDown(self) -> None:
        self.bob.close()
        self.host.close()

    def _start(self, combo: bool = True):
        self.host.client.send({"t": "rules", "rules": {"combo_same": combo, "zero_rotate": False,
                                                       "seven_swap": False}})
        _drain(self.host.client, self.bob, wait=0.3)
        self.host.client.send({"t": "start"})
        _drain(self.host.client, self.bob, wait=0.8)
        engine = self.host.engine
        with self.host.server.lock:
            engine.turn = 0
            engine.phase = "playing"
            engine.discard = ["R9"]
            engine.current_color = "R"
            engine.pending, engine.drawn, engine.uno_window = 0, None, None
            engine.players[0].hand = ["R5", "B5", "G5", "Y2"]
        return engine

    def test_combo_over_the_wire(self):
        engine = self._start()
        self.host.client.send({"t": "combo", "cards": ["R5", "B5", "G5"], "id": 7})
        host_msgs, bob_msgs = _drain(self.host.client, self.bob)
        self.assertTrue([m for m in host_msgs if m.get("t") == "ack" and m.get("id") == 7 and m.get("ok")])
        for msgs in (host_msgs, bob_msgs):
            plays = [e for m in msgs if m.get("t") == "events" for e in m["items"] if e["t"] == "play"]
            self.assertEqual([e["card"] for e in plays], ["R5", "B5", "G5"])
            self.assertEqual([bool(e.get("combo")) for e in plays], [True, True, False])
        self.assertEqual((engine.top(), engine.current_color, engine.turn), ("G5", "G", 1))
        self.assertEqual(engine.players[0].hand, ["Y2"])

    def test_snapshot_offers_the_combo_only_to_its_owner(self):
        engine = self._start()
        self.assertEqual(engine.snapshot(0)["combo"]["G5"], ["R5", "B5", "G5"])
        self.assertEqual(engine.snapshot(1)["combo"], {})
        self.assertEqual(engine.snapshot(None)["combo"], {})

    def test_bad_combos_are_refused_and_change_nothing(self):
        engine = self._start()
        for cards, code in ((["R5", "Y2"], "bad_combo"), (["R5", "R5"], "not_in_hand"),
                            (["B5", "G5"], "illegal"), (["R5"], "bad_msg"), ("R5", "bad_msg")):
            self.host.client.send({"t": "combo", "cards": cards, "id": 1})
            host_msgs, = _drain(self.host.client, wait=0.3)
            errors = [m for m in host_msgs if m.get("t") == "error"]
            self.assertTrue(errors, cards)
        self.assertEqual(engine.players[0].hand, ["R5", "B5", "G5", "Y2"])
        self.assertEqual(engine.turn, 0)

    def test_off_by_default(self):
        engine = self._start(combo=False)
        self.host.client.send({"t": "combo", "cards": ["R5", "B5"], "id": 2})
        host_msgs, = _drain(self.host.client)
        self.assertTrue([m for m in host_msgs if m.get("t") == "error" and m.get("code") == "combo_off"])
        self.assertEqual(engine.snapshot(0)["combo"], {})

    def test_web_gateway_lets_combo_through(self):
        self.assertIn("combo", webserver.ALLOWED)


if __name__ == "__main__":
    unittest.main()
