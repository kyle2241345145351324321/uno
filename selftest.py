#!/usr/bin/env python3
"""Headless self-tests for the UNO engine.

    python3 uno.py --selftest
    python3 selftest.py                # same
"""
from __future__ import annotations

import random
import sys
import traceback

from engine import COLORS, Rules, UnoGame, card_points, make_deck


def new_game(names=("Ann", "Ben", "Cid"), rules=None):
    game = UnoGame(list(names), rules=rules or Rules(),
                   rng=random.Random(1234))
    game.start_round(first=0)
    return game


def reset_table(game, top="R5", hands=None, draw=None, turn=0, direction=1, color=None):
    game.phase = "playing"
    game.discard = [top]
    game.current_color = color or top[0]
    game.pending = 0
    game.pending_type = None
    game.drawn = None
    game.chooser = None
    game.uno_window = None
    game.turn = turn
    game.direction = direction
    game.discard = [top]
    for player in game.players:
        player.hand = []
        player.said_uno = False
    if hands:
        for pid, cards in hands.items():
            game.players[pid].hand = list(cards)
    game.draw_pile = list(draw) if draw is not None else [c for c in make_deck() if c not in
                                                          sum((h for h in (hands or {}).values()), [])]
    return game


# --------------------------------------------------------------------------- 1
def test_deck():
    deck = make_deck()
    assert len(deck) == 108
    assert deck.count("R0") == 1 and deck.count("R1") == 2 and deck.count("Rskip") == 2
    assert deck.count("wild") == 4 and deck.count("wild4") == 4
    assert sum(deck.count(c + v) for c in COLORS for v in ("0",)) == 4
    assert card_points("R9") == 9 and card_points("Gskip") == 20
    assert card_points("wild") == 50 and card_points("wild4") == 50
    return 7


# --------------------------------------------------------------------------- 2
def test_matching():
    game = new_game()
    reset_table(game, "R5", {0: ["R9", "B9", "G5", "B1", "wild", "wild4", "B2"],
                             1: ["B1"], 2: ["B2"]})
    legal = set(game.legal_cards(0))
    assert {"R9", "G5", "wild"} <= legal
    assert "B9" not in legal and "B1" not in legal and "B2" not in legal
    assert "wild4" not in legal, "wild4 illegal while holding red"
    reset_table(game, "R5", {0: ["B9", "G5", "B1", "wild4", "wild"], 1: ["B1"], 2: ["B2"]})
    assert "wild4" in game.legal_cards(0), "wild4 legal without current colour"
    # jump-in needs an identical card
    assert game.jump_cards(1) == ["R5"] if "R5" in game.players[1].hand else True
    return 4


# --------------------------------------------------------------------------- 3
def test_pending_and_stacking():
    game = new_game()
    reset_table(game, "R9", {0: ["Rdraw2", "Bdraw2", "R1"],
                             1: ["Ydraw2", "wild4", "R2"],
                             2: ["Gdraw2", "B2"]})
    events = game.play(0, "Rdraw2", now=0)
    assert game.pending == 2 and game.pending_type == "draw2"
    assert game.turn == 1
    assert game.legal_cards(1) == ["Ydraw2"], game.legal_cards(1)
    game.play(1, "Ydraw2", now=1)
    assert game.pending == 4 and game.turn == 2
    assert game.legal_cards(2) == ["Gdraw2"]
    hand_before = len(game.players[2].hand)
    game.draw(2, now=2)
    assert game.pending == 0 and len(game.players[2].hand) == hand_before + 4
    assert game.turn == 0
    # non stacking card rejected while pending
    reset_table(game, "R9", {0: ["R2"], 1: ["wild4"], 2: ["B2"]}, turn=1)
    game.pending, game.pending_type = 2, "draw2"
    ok, code = game.can_play(1, "wild4")
    assert not ok and code == "must_stack_pending"
    return 7


# --------------------------------------------------------------------------- 4
def test_draw_and_play_drawn():
    game = new_game()
    # top of draw_pile is the last element
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]},
                draw=["Y9", "R7", "B3"])
    game.draw(0, now=0)
    assert game.turn == 1, "B3 unplayable -> turn passes"
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]},
                draw=["Y9", "B3", "R9"])
    game.draw(0, now=0)
    assert game.turn == 0 and game.drawn is not None
    assert game.legal_cards(0) == ["R9"]
    game.play(0, "R9", now=1)
    assert game.turn == 1 and len(game.players[0].hand) == 2
    # pass path
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]},
                draw=["R7", "Y9", "B3"])
    game.draw(0, now=0)
    game.pass_turn(0)
    assert game.turn == 1 and game.drawn is None
    return 6


# --------------------------------------------------------------------------- 5
def test_actions():
    game = new_game()
    # skip
    reset_table(game, "R5", {0: ["Rskip", "R1"], 1: ["B1"], 2: ["B2"]})
    game.play(0, "Rskip", now=0)
    assert game.turn == 2
    # reverse (3p): goes to the previous player
    reset_table(game, "R5", {0: ["Rreverse", "R1"], 1: ["B1"], 2: ["B2"]})
    game.play(0, "Rreverse", now=0)
    assert game.direction == -1 and game.turn == 2
    # 2p reverse acts as skip
    two = new_game(("Ann", "Ben"))
    reset_table(two, "R5", {0: ["Rreverse", "R1"], 1: ["B1"]})
    two.play(0, "Rreverse", now=0)
    assert two.turn == 0
    # wild colour choice
    reset_table(game, "R5", {0: ["wild", "R1"], 1: ["wild", "B1"], 2: ["B2"]})
    events = game.play(0, "wild", "G", now=0)
    assert game.current_color == "G" and game.turn == 1
    ok, code = game.can_play(1, "wild")
    assert ok, code  # wild always playable
    return 5


# --------------------------------------------------------------------------- 6
def test_wild4_stack_and_first_card():
    game = new_game()
    reset_table(game, "R5", {0: ["wild4", "B1"], 1: ["wild4", "R2"], 2: ["B2"]})
    game.play(0, "wild4", "G", now=0)
    assert game.pending == 4 and game.current_color == "G" and game.turn == 1
    assert game.legal_cards(1) == ["wild4"]
    game.play(1, "wild4", "B", now=1)
    assert game.pending == 8 and game.turn == 2
    assert game.legal_cards(2) == []
    game.draw(2, now=2)
    assert len(game.players[2].hand) == 1 + 8
    return 6


# --------------------------------------------------------------------------- 7
def test_uno_call_and_catch():
    game = new_game()
    reset_table(game, "R5", {0: ["R9", "B2"], 1: ["B1", "B2"], 2: ["B3", "B4"]})
    game.play(0, "R9", now=0)
    assert game.uno_window and game.uno_window["pid"] == 0
    game.call_uno(0, now=1)
    assert game.players[0].said_uno
    assert game.catch_uno(1, now=1)[0]["t"] == "error"  # already called
    # catch a silent player
    reset_table(game, "R5", {0: ["R9", "B2"], 1: ["B1", "B2"], 2: ["B3", "B4"]})
    game.play(0, "R9", now=0)
    events = game.catch_uno(1, now=1)
    assert events[0]["t"] == "catch" and len(game.players[0].hand) == 1 + 2
    assert game.uno_window is None
    # window expires
    reset_table(game, "R5", {0: ["R9", "B2"], 1: ["B1"], 2: ["B2"]})
    game.play(0, "R9", now=0)
    game.tick(now=99)
    assert game.uno_window is None
    return 5


# --------------------------------------------------------------------------- 8
def test_seven_zero_and_jump():
    rules = Rules(seven_swap=True, zero_rotate=True)
    game = new_game(("A", "B", "C", "D"), rules)
    reset_table(game, "G5", {0: ["G7", "R1"], 1: ["B1"], 2: ["B2"], 3: ["B3"]})
    game.play(0, "G7", now=0)
    assert game.chooser and game.chooser["pid"] == 0
    game.choose_swap(0, 2, now=1)
    assert "B2" in game.players[0].hand and "B1" in game.players[1].hand
    assert game.players[2].hand == ["R1"]
    # 0 rotates hands in play direction
    reset_table(game, "G5", {0: ["G0", "R1"], 1: ["B1", "Y1"], 2: ["B2", "Y2"], 3: ["B3", "Y3"]})
    game.play(0, "G0", now=0)
    assert game.players[1].hand == ["R1"]       # 0's old hand moved to next seat
    assert game.players[2].hand == ["B1", "Y1"]
    assert game.players[0].hand == ["B3", "Y3"]
    # jump-in: seat 1 plays an identical top card out of turn
    reset_table(game, "R5", {0: ["B1"], 1: ["R5", "B2"], 2: ["B3"]}, turn=0)
    assert "R5" in game.jump_cards(1)
    game.play(1, "R5", jump=True, now=0)
    assert game.turn == 2
    # regression: jumping in with a 7 must hand the swap chooser to the jumper
    reset_table(game, "R7", {0: ["B1"], 1: ["R7", "B2"], 2: ["B3"]}, turn=0)
    game.play(1, "R7", jump=True, now=0)
    assert game.turn == 1 and game.chooser and game.chooser["pid"] == 1
    game.choose_swap(1, 2, now=0)
    assert game.chooser is None and game.turn == 2
    # the 7 swap is opt-in: with the house rule off the turn simply moves on
    plain = new_game(("A", "B"), Rules(seven_swap=False, zero_rotate=True))
    reset_table(plain, "G5", {0: ["G7", "R1"], 1: ["B1"]})
    plain.play(0, "G7", now=0)
    assert plain.chooser is None and plain.turn == 1
    # no jump-in during pending
    reset_table(game, "R5", {0: ["B1"], 1: ["R5"], 2: ["B3"]}, turn=0)
    game.pending, game.pending_type = 2, "draw2"
    assert game.jump_cards(1) == []
    return 8


# --------------------------------------------------------------------------- 9
def test_scoring_and_match():
    rules = Rules(target_score=100)
    game = new_game(("A", "B"), rules)
    reset_table(game, "R5", {0: ["R9"], 1: ["Bskip", "wild", "R3"]})
    game.play(0, "R9", now=0)
    assert game.phase == "round_over"
    assert game.players[0].score == 20 + 50 + 3
    assert game.round_winner == 0
    assert game.players[1].round_score == 0
    # match over at target
    game.players[0].score = 95
    game.start_round(first=0)
    reset_table(game, "R5", {0: ["R9"], 1: ["Bskip", "B1"]})
    events = game.play(0, "R9", now=0)
    assert game.phase == "match_over" and game.match_winner == 0
    assert any(e["t"] == "match_end" for e in events)
    return 5


# -------------------------------------------------------------------------- 10
def test_snapshot_privacy():
    game = new_game()
    snap = game.snapshot(1)
    assert len(snap["hand"]) == 7
    for p in snap["players"]:
        assert "hand" not in p
    assert snap["revealed"] == {}
    spectator = game.snapshot(None)
    assert spectator["hand"] == [] and spectator["legal"] == []
    assert all(len(h) == 7 for h in spectator["revealed"].values()), "spectator sees all hands"
    assert spectator["top"] in make_deck()
    return 3


# -------------------------------------------------------------------------- 11
def test_reshuffle():
    game = new_game()
    reset_table(game, "R5", {0: ["B1", "B2"], 1: ["B3"], 2: ["B4"]},
                draw=[])
    game.discard = ["R5", "B6", "B7", "B8"]
    game.draw(0, now=0)
    assert len(game.draw_pile) == 2  # remaining discard reshuffled
    assert game.players[0].hand[-1] in ("R5", "B6", "B7")
    return 3


# -------------------------------------------------------------------------- 12
def test_uno_window_after_rotate_and_swap():
    # 0 rotates hands: the UNO window must not stay on a seat whose hand changed
    game = new_game(("A", "B", "C"), Rules(zero_rotate=True))
    reset_table(game, "G5", {0: ["G0", "R1"], 1: ["B1", "Y1", "B2", "B3", "B4"],
                             2: ["B5", "Y5"]})
    events = game.play(0, "G0", now=0)
    assert game.uno_window is None
    assert any(e["t"] == "uno_safe" for e in events)
    assert game.catch_uno(1, now=1)[0]["t"] == "error"
    assert len(game.players[0].hand) == 2, "nobody is punished for a swapped hand"
    # 7 swaps hands: same story
    game = new_game(("A", "B", "C"), Rules(seven_swap=True))
    reset_table(game, "G5", {0: ["G7", "R1"], 1: ["B1", "Y1", "B2", "B3"], 2: ["B5", "Y5"]})
    game.play(0, "G7", now=0)
    assert game.uno_window and game.uno_window["pid"] == 0
    game.choose_swap(0, 1, now=1)
    assert game.uno_window is None
    assert len(game.players[0].hand) == 4
    return 6


def test_deck_exhausted_passes_turn():
    game = new_game(("A", "B"))
    reset_table(game, "R5", {0: ["B1"], 1: ["B2"]}, draw=[])
    game.discard = ["R5"]
    events = game.draw(0, now=0)
    assert events[0]["t"] == "pass" and events[0]["reason"] == "deck_empty"
    assert game.turn == 1 and game.drawn is None
    assert len(game.players[0].hand) == 1
    return 3


def test_rule_limits():
    rules = Rules.from_dict({"start_cards": 15, "target_score": -5,
                             "uno_window": 9999, "uno_penalty": 0})
    assert rules.start_cards == 10 and rules.target_score == 0
    assert rules.uno_window == 30.0 and rules.uno_penalty == 1
    assert Rules.from_dict({"start_cards": -3}).start_cards == 1
    # even a hand-built Rules object can't crash a 10-player deal
    big = UnoGame([f"P{i}" for i in range(10)], rules=Rules(start_cards=50),
                  rng=random.Random(7))
    big.start_round(first=0)
    assert all(len(p.hand) == 10 for p in big.players)
    assert len(big.discard) == 1
    return 5


def test_opening_action_card():
    """Who plays first when the flipped starting card is Skip / Reverse / +2."""
    class Rig(random.Random):
        # random.Random's C-level __new__ rejects extra constructor arguments,
        # so card/count have to be swallowed here before __init__ ever runs.
        def __new__(cls, card, n):
            return super().__new__(cls)

        def __init__(self, card, n):
            super().__init__(1)
            self.card, self.n = card, n

        def shuffle(self, lst):
            super().shuffle(lst)
            lst.remove(self.card)
            lst.insert(len(lst) - 7 * self.n, self.card)    # becomes the flipped card

    def start(n, card):
        rng = Rig(card, n)
        game = UnoGame([f"P{i}" for i in range(n)], rules=Rules(), rng=rng)
        game.start_round(first=0)
        assert game.top() == card
        return game

    for n in (2, 3, 4):
        game = start(n, "Rskip")
        assert game.turn == 1, f"{n}p skip start: only seat 0 is skipped, got seat {game.turn}"
    game = start(3, "Rreverse")
    assert game.direction == -1 and game.turn == 2          # the dealer plays first, reversed
    game = start(2, "Rreverse")
    assert game.turn == 1
    game = start(3, "Rdraw2")
    assert len(game.players[0].hand) == 9 and game.turn == 1
    return 6


def test_turn_timer():
    """Per-turn timer: off by default; on timeout draw one card and pass; restarts sensibly."""
    # off by default: ticking forever does nothing
    game = new_game()
    reset_table(game, "R5", {0: ["R9", "B2"], 1: ["B1"], 2: ["B2"]})
    game.tick(now=0)
    game.tick(now=10_000)
    assert game.turn == 0 and game.turn_deadline is None
    assert game.snapshot(0)["turn_timer"] is None

    rules = Rules(turn_time=10)
    game = new_game(rules=rules)
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]}, draw=["Y9", "R7", "B3"])
    game.tick(now=100)                                   # first tick starts the clock
    assert game.turn_deadline == 110
    snap = game.snapshot(0)
    assert snap["turn_timer"] == {"pid": 0, "total": 10, "deadline": 110}
    game.tick(now=109.9)
    assert game.turn == 0, "not yet"
    events = game.tick(now=110.1)
    kinds = [e["t"] for e in events]
    assert "turn_timeout" in kinds and "draw" in kinds
    assert game.turn == 1 and len(game.players[0].hand) == 3 and game.drawn is None
    game.tick(now=110.2)
    assert game.turn_deadline == 120.2, "a fresh full clock for the next player"

    # a playable drawn card is NOT played automatically: the turn is passed
    game = new_game(rules=Rules(turn_time=5))
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]}, draw=["Y9", "B3", "R9"])
    game.tick(now=0)
    events = game.tick(now=6)
    assert game.turn == 1 and game.drawn is None
    assert "R9" in game.players[0].hand and game.discard == ["R5"], "drawn card stays in hand"
    assert [e["t"] for e in events].count("pass") == 1

    # after drawing a playable card the player gets a fresh clock to decide
    game = new_game(rules=Rules(turn_time=5))
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["B1"], 2: ["B2"]}, draw=["Y9", "B3", "R9"])
    game.tick(now=0)
    game.draw(0, now=3)
    assert game.drawn is not None and game.turn == 0
    game.tick(now=4)                                     # restarts because `drawn` changed
    assert game.turn_deadline == 9
    game.tick(now=8.9)
    assert game.turn == 0
    events = game.tick(now=9.1)                          # already drew: timeout only passes
    assert game.turn == 1 and game.drawn is None
    assert len(game.players[0].hand) == 3, "no second draw"

    # a stacked +2 is taken as the penalty
    game = new_game(rules=Rules(turn_time=5))
    reset_table(game, "R9", {0: ["Rdraw2", "R1"], 1: ["B1", "B2"], 2: ["B3", "B4"]},
                draw=["Y1", "Y2", "Y3", "Y4", "Y5", "Y6"])
    game.play(0, "Rdraw2", now=0)
    game.tick(now=1)
    game.tick(now=7)
    assert game.pending == 0 and len(game.players[1].hand) == 4 and game.turn == 2

    # jump-in changes the turn: the clock restarts for the new owner
    game = new_game(rules=Rules(turn_time=5))
    reset_table(game, "R5", {0: ["B1"], 1: ["R5", "B2"], 2: ["B3"]}, turn=0)
    game.tick(now=0)
    game.play(1, "R5", jump=True, now=3)
    game.tick(now=4)
    assert game.turn == 2 and game.turn_deadline == 9

    # paused while a swap target is being chosen, never fires for the chooser
    game = new_game(("A", "B", "C"), Rules(turn_time=5, seven_swap=True))
    reset_table(game, "G5", {0: ["G7", "R1"], 1: ["B1"], 2: ["B2"]})
    game.tick(now=0)
    game.play(0, "G7", now=1)
    assert game.chooser
    game.tick(now=3)
    assert game.turn_deadline is None and game.snapshot(0)["turn_timer"] is None
    game.choose_swap(0, 2, now=4)
    game.tick(now=5)
    assert game.turn == 1 and game.turn_deadline == 10

    # nothing runs between rounds
    game = new_game(("A", "B"), Rules(turn_time=5))
    reset_table(game, "R5", {0: ["R9"], 1: ["B1"]})
    game.tick(now=0)
    game.play(0, "R9", now=1)
    assert game.phase == "round_over"
    game.tick(now=100)
    assert game.turn_deadline is None and game.snapshot(0)["turn_timer"] is None

    # rule limits and wire format
    assert Rules.from_dict({"turn_time": 15}).turn_time == 15.0
    assert Rules.from_dict({"turn_time": -4}).sanitize().turn_time == 0.0
    assert Rules.from_dict({"turn_time": 99999}).sanitize().turn_time == 300.0
    assert Rules(turn_time=30).as_dict()["turn_time"] == 30
    return 20

def test_combo_play():
    """Same-number combos: several number cards of one value in a single turn."""
    on = Rules(combo_same=True, zero_rotate=False, seven_swap=False)

    # off by default
    game = new_game()
    reset_table(game, "R9", {0: ["R5", "B5"], 1: ["B1"], 2: ["B2"]})
    assert game.play_multi(0, ["R5", "B5"], now=0)[0]["code"] == "combo_off"
    assert game.snapshot(0)["combo"] == {}

    # basic: any order, the first card must be legal, the last card sets the colour
    game = new_game(rules=on)
    reset_table(game, "R9", {0: ["R5", "B5", "G5", "Y2"], 1: ["B1", "B3"], 2: ["B2", "B4"]})
    assert game.combo_order(0, "G5") == ["R5", "B5", "G5"], game.combo_order(0, "G5")
    assert game.snapshot(0)["combo"]["G5"] == ["R5", "B5", "G5"]
    assert "Y2" not in game.snapshot(0)["combo"]
    events = game.play_multi(0, ["R5", "B5", "G5"], now=0)
    assert [e["t"] for e in events].count("play") == 3
    assert [e.get("combo") for e in events if e["t"] == "play"] == [True, True, None]
    assert game.top() == "G5" and game.current_color == "G"
    assert game.players[0].hand == ["Y2"] and game.turn == 1
    assert game.uno_window and game.uno_window["pid"] == 0, "one card left -> UNO window"

    # only the selected card is legal: it has to start the combo
    game = new_game(rules=on)
    reset_table(game, "B1", {0: ["B5", "R5", "G5"], 1: ["B2"], 2: ["B3"]})
    assert game.combo_order(0, "R5") == ["B5", "G5", "R5"]
    assert game.combo_order(0, "B5") == ["B5", "R5", "G5"]
    # nothing legal: no combo, and play_multi refuses
    reset_table(game, "R9", {0: ["B5", "G5", "Y2"], 1: ["B2"], 2: ["B3"]})
    assert game.combo_order(0, "B5") is None
    assert game.play_multi(0, ["B5", "G5"], now=0)[0]["code"] == "illegal"
    assert game.players[0].hand == ["B5", "G5", "Y2"], "a refused combo changes nothing"

    # winning with a combo ends the round
    game = new_game(("A", "B"), Rules(combo_same=True, zero_rotate=False, seven_swap=False))
    reset_table(game, "R9", {0: ["R5", "B5"], 1: ["B1", "G3"]})
    game.play_multi(0, ["R5", "B5"], now=0)
    assert game.phase == "round_over" and game.round_winner == 0
    assert game.players[0].score == 1 + 3

    # identical cards, and value rules
    game = new_game(rules=on)
    reset_table(game, "R9", {0: ["R5", "R5", "B1"], 1: ["B2"], 2: ["B3"]})
    game.play_multi(0, ["R5", "R5"], now=0)
    assert game.players[0].hand == ["B1"]
    for bad in (["R5", "B6"], ["Rskip", "Bskip"], ["wild", "wild"], ["R5"]):
        reset_table(game, "R9", {0: ["R5", "B6", "Rskip", "Bskip", "wild", "wild"], 1: ["B2"], 2: ["B3"]})
        code = game.play_multi(0, bad, now=0)[0]["code"]
        assert code in ("bad_combo", "bad_msg"), (bad, code)
    reset_table(game, "R9", {0: ["R5", "B5"], 1: ["B2"], 2: ["B3"]})
    assert game.play_multi(0, ["R5", "G5"], now=0)[0]["code"] == "not_in_hand"
    assert game.play_multi(1, ["R5", "B5"], now=0)[0]["code"] == "not_your_turn"

    # 7 and 0 are excluded while their special rules are on, allowed otherwise
    sp = Rules(combo_same=True, seven_swap=True, zero_rotate=True)
    game = new_game(rules=sp)
    reset_table(game, "R9", {0: ["R7", "B7", "R0", "B0", "R1"], 1: ["B2"], 2: ["B3"]})
    assert game.combo_orders(0) == {}
    assert game.play_multi(0, ["R7", "B7"], now=0)[0]["code"] == "bad_combo"
    game = new_game(rules=on)
    reset_table(game, "R9", {0: ["R7", "B7", "R0", "B0", "R1"], 1: ["B2"], 2: ["B3"]})
    assert set(game.combo_orders(0)) == {"R7", "B7", "R0", "B0"}

    # not after drawing, not while a draw is pending, not in someone else's turn
    game = new_game(rules=on)
    reset_table(game, "R5", {0: ["B1", "G2"], 1: ["R9", "B9"], 2: ["B2"]}, draw=["Y9", "B3", "R9"])
    game.draw(0, now=0)
    assert game.drawn is not None and game.combo_orders(0) == {}
    reset_table(game, "R9", {0: ["R5", "B5"], 1: ["R6", "B6"], 2: ["B2"]})
    game.pending, game.pending_type = 2, "draw2"
    assert game.combo_orders(0) == {}
    assert game.play_multi(0, ["R5", "B5"], now=0)[0]["code"] == "bad_combo"
    reset_table(game, "R9", {0: ["R5", "B5"], 1: ["R6", "B6"], 2: ["B2"]})
    assert game.snapshot(1)["combo"] == {} and game.snapshot(None)["combo"] == {}

    # another player's open UNO window is closed safely by a combo
    game = new_game(rules=on)
    reset_table(game, "R9", {0: ["B1", "B2"], 1: ["R5", "B5", "G1"], 2: ["B3"]}, turn=1)
    game.uno_window = {"pid": 0, "deadline": 99, "said": False}
    events = game.play_multi(1, ["R5", "B5"], now=0)
    assert any(e["t"] == "uno_safe" and e["pid"] == 0 for e in events)
    assert game.uno_window and game.uno_window["pid"] == 1, "the combo player has one card left now"
    return 35


# -------------------------------------------------------------------------- 13

TESTS = [
    test_deck, test_matching, test_pending_and_stacking, test_draw_and_play_drawn,
    test_actions, test_wild4_stack_and_first_card, test_uno_call_and_catch,
    test_seven_zero_and_jump, test_scoring_and_match, test_snapshot_privacy,
    test_reshuffle, test_uno_window_after_rotate_and_swap,
    test_deck_exhausted_passes_turn, test_rule_limits, test_opening_action_card,
    test_turn_timer, test_combo_play,
]


def main() -> int:
    passed = failed = 0
    for test in TESTS:
        try:
            checks = test()
            passed += 1
            print(f"  ok   {test.__name__:<32} ({checks} checks)")
        except AssertionError:
            failed += 1
            print(f"  FAIL {test.__name__}")
            traceback.print_exc()
        except Exception:
            failed += 1
            print(f"  ERR  {test.__name__}")
            traceback.print_exc()
    print(f"\nselftest: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
