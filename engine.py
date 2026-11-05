"""UNO rules engine (pure, deterministic, no I/O).

The engine owns one table of 2-10 players, deals cards, validates moves and
applies effects.  Networking (net.py) and the UI both talk to it through
:meth:`UnoGame.snapshot` and the small action methods; the host is the only
process that owns an ``UnoGame``, clients send intents.

Cards are plain strings: ``"R5"``, ``"Gskip"``, ``"Bdraw2"``, ``"wild"``,
``"wild4"``.  ``card_color("wild") is None``.

Rules implemented (all toggleable from :class:`Rules`):
  * classic matching (colour / number / symbol), Wild, Wild Draw Four
  * +2 and +4 stacking (same symbol only)
  * Wild Draw Four may only be played when the hand has no current colour
  * draw one card, optionally play it immediately, otherwise pass
  * Skip / Reverse / Draw Two first-card effects
  * UNO call window with opponents able to catch a forgotten call
  * party rules: jump-in on an identical card, 7 = swap hands, 0 = rotate hands
  * round scoring (numbers face value, symbols 20, wilds 50) up to 500
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

COLORS = ("R", "Y", "G", "B")
COLOR_LABELS = {"R": "Red", "Y": "Yellow", "G": "Green", "B": "Blue"}
ACTION_VALUES = ("skip", "reverse", "draw2")
WILD_VALUES = ("wild", "wild4")


def card_color(card: str) -> str | None:
    return card[0] if card and card[0] in COLORS else None


def card_value(card: str) -> str:
    return card[1:] if card and card[0] in COLORS else card


def is_wild(card: str) -> bool:
    return card in WILD_VALUES


def card_points(card: str) -> int:
    if card in WILD_VALUES:
        return 50
    value = card_value(card)
    return int(value) if value.isdigit() else 20


def make_deck() -> list[str]:
    deck: list[str] = []
    for color in COLORS:
        deck.append(color + "0")
        for digit in "123456789":
            deck.extend((color + digit, color + digit))
        for value in ACTION_VALUES:
            deck.extend((color + value, color + value))
    deck.extend(["wild"] * 4)
    deck.extend(["wild4"] * 4)
    assert len(deck) == 108
    return deck


# (min, max) for every numeric house rule; the host cannot go outside these.
RULE_LIMITS: dict[str, tuple[float, float]] = {
    "start_cards": (1, 10),
    "target_score": (0, 10000),
    "uno_window": (1.0, 30.0),
    "uno_penalty": (1, 4),
    "turn_time": (0.0, 300.0),          # 0 = no turn timer
}


@dataclass
class Rules:
    start_cards: int = 7
    target_score: int = 500
    stack_draw2: bool = True
    stack_wild4: bool = True
    jump_in: bool = True
    seven_swap: bool = False
    zero_rotate: bool = True
    uno_window: float = 5.0
    uno_penalty: int = 2
    draw_playable: bool = True
    wild4_needs_no_color: bool = True
    choice_timeout: float = 12.0
    turn_time: float = 0.0              # seconds per turn; on timeout the player draws one card and passes
    combo_same: bool = False            # play several number cards of the same value in one turn

    def sanitize(self) -> "Rules":
        """Clamp numeric rules into RULE_LIMITS (in place) and return self."""
        for key, (low, high) in RULE_LIMITS.items():
            value = getattr(self, key)
            setattr(self, key, type(value)(min(high, max(low, value))))
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_cards": self.start_cards,
            "target_score": self.target_score,
            "stack_draw2": self.stack_draw2,
            "stack_wild4": self.stack_wild4,
            "jump_in": self.jump_in,
            "seven_swap": self.seven_swap,
            "zero_rotate": self.zero_rotate,
            "uno_window": self.uno_window,
            "uno_penalty": self.uno_penalty,
            "draw_playable": self.draw_playable,
            "wild4_needs_no_color": self.wild4_needs_no_color,
            "turn_time": self.turn_time,
            "combo_same": self.combo_same,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Rules":
        rules = cls()
        if not data:
            return rules
        data = dict(data)
        # rooms created before the split sent a single seven_zero flag
        if "seven_zero" in data:
            legacy = bool(data.pop("seven_zero"))
            data.setdefault("seven_swap", legacy)
            data.setdefault("zero_rotate", legacy)
        for key, value in data.items():
            if hasattr(rules, key) and key != "choice_timeout":
                setattr(rules, key, type(getattr(rules, key))(value))
        return rules.sanitize()


@dataclass
class Player:
    pid: int
    name: str
    ai: bool = False
    connected: bool = True
    is_host: bool = False
    hand: list[str] = field(default_factory=list)
    score: int = 0
    round_score: int = 0
    said_uno: bool = False
    wins: int = 0
    total_score: int = 0

    def reset_round(self) -> None:
        self.hand = []
        self.round_score = 0
        self.said_uno = False


class UnoGame:
    """Authoritative game table.  ``pid`` is the seat index (0..n-1)."""

    def __init__(
        self,
        names: list[str],
        *,
        ai_flags: list[bool] | None = None,
        rules: Rules | None = None,
        host_pid: int = 0,
        rng: random.Random | None = None,
    ) -> None:
        if not 2 <= len(names) <= 10:
            raise ValueError("UNO needs 2-10 players")
        self.rules = rules or Rules()
        self.players = [
            Player(pid=i, name=name, ai=bool(ai_flags[i]) if ai_flags else False,
                   is_host=(i == host_pid))
            for i, name in enumerate(names)
        ]
        self.rng = rng or random.Random()
        self.round_no = 0
        self.phase = "playing"  # playing | round_over | match_over
        self.turn = 0
        self.direction = 1
        self.draw_pile: list[str] = []
        self.discard: list[str] = []
        self.current_color: str = "R"
        self.pending = 0
        self.pending_type: str | None = None
        self.drawn: tuple[int, str] | None = None
        self.chooser: dict[str, Any] | None = None
        self.uno_window: dict[str, Any] | None = None
        self.turn_deadline: float | None = None      # host clock; None while the turn timer is off/paused
        self._turn_key: tuple | None = None          # what the running timer belongs to
        self.round_winner: int | None = None
        self.match_winner: int | None = None
        self.round_points: dict[int, int] = {}
        self.event_seq = 0
        self.last_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def player(self, pid: int) -> Player:
        return self.players[pid]

    def n(self) -> int:
        return len(self.players)

    def name_of(self, pid: int) -> str:
        return self.players[pid].name

    def _next(self, pid: int, steps: int = 1) -> int:
        return (pid + self.direction * steps) % self.n()

    def _draw_card(self) -> str | None:
        if not self.draw_pile:
            self._reshuffle()
        return self.draw_pile.pop() if self.draw_pile else None

    def _reshuffle(self) -> None:
        if len(self.discard) <= 1:
            return
        top = self.discard.pop()
        self.draw_pile = self.discard[:]
        self.discard = [top]
        self.rng.shuffle(self.draw_pile)

    def _emit(self, event: dict[str, Any], events: list[dict[str, Any]]) -> None:
        self.event_seq += 1
        event["seq"] = self.event_seq
        events.append(event)
        self.last_events = (self.last_events + [event])[-80:]

    def top(self) -> str:
        return self.discard[-1]

    def _close_uno_window(self, events: list[dict[str, Any]]) -> None:
        """Hands just moved (0 rotate / 7 swap): the old UNO window is meaningless."""
        if self.uno_window is not None:
            self._emit({"t": "uno_safe", "pid": self.uno_window["pid"]}, events)
            self.uno_window = None

    def hand_counts(self) -> dict[int, int]:
        return {p.pid: len(p.hand) for p in self.players}

    # --------------------------------------------------------------- round setup
    def start_round(self, first: int | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        self.round_no += 1
        self.phase = "playing"
        self.direction = 1
        self.pending = 0
        self.pending_type = None
        self.drawn = None
        self.chooser = None
        self.uno_window = None
        self.round_winner = None
        self.round_points = {}
        self.discard = []
        self.draw_pile = make_deck()
        self.rng.shuffle(self.draw_pile)
        # always leave at least one card for the starting discard
        deal = max(1, min(self.rules.start_cards, (len(self.draw_pile) - 1) // self.n()))
        for player in self.players:
            player.reset_round()
            player.hand = [self._draw_card() for _ in range(deal)]  # type: ignore[list-item]
        first = 0 if first is None else first % self.n()
        # Flip a starting card; wild cards go back into the deck (official rule).
        for _ in range(60):
            card = self._draw_card()
            if card is None:  # pragma: no cover - 108 cards make this impossible
                break
            if is_wild(card):
                self.draw_pile.insert(0, card)
                continue
            self.discard.append(card)
            break
        self.current_color = card_color(self.discard[-1]) or "R"  # type: ignore[arg-type]
        self.turn = first
        self._emit({"t": "round_start", "first": first, "top": self.top(),
                    "color": self.current_color, "hands": self.hand_counts(),
                    "round_no": self.round_no}, events)
        value = card_value(self.top())
        if value == "skip":
            self.turn = self._next(first, 1)         # the first player loses the turn, nobody else
            self._emit({"t": "skip", "pid": first}, events)
        elif value == "reverse":
            self.direction = -1
            self.turn = self._next(first, 1)
            self._emit({"t": "reverse", "pid": first}, events)
        elif value == "draw2":
            self._deal_penalty(first, 2)
            self.turn = self._next(first, 1)
            self._emit({"t": "draw", "pid": first, "n": 2, "reason": "start"}, events)
        self._emit({"t": "turn", "pid": self.turn}, events)
        self.last_events = events[-1:] if events else []
        return events

    def _deal_penalty(self, pid: int, count: int) -> None:
        player = self.players[pid]
        for _ in range(count):
            card = self._draw_card()
            if card is None:
                break
            player.hand.append(card)
        player.said_uno = False

    # ------------------------------------------------------------------- legality
    def _playable_by_value(self, pid: int, card: str) -> bool:
        """Rules for playing *card* when there is no pending draw."""
        hand = self.players[pid].hand
        if card_value(card) == "wild":
            return True
        if card_value(card) == "wild4":
            if self.rules.wild4_needs_no_color and any(
                card_color(c) == self.current_color for c in hand
            ):
                return False
            return True
        top = self.top() if self.discard else None
        return (card_color(card) == self.current_color
                or (top is not None and card_value(card) == card_value(top)))

    def _stackable(self, card: str) -> bool:
        if self.pending_type == "draw2":
            return self.rules.stack_draw2 and card_value(card) == "draw2"
        if self.pending_type == "wild4":
            return self.rules.stack_wild4 and card_value(card) == "wild4"
        return False

    def legal_cards(self, pid: int) -> list[str]:
        if self.phase != "playing" or self.chooser is not None:
            return []
        player = self.players[pid]
        if pid == self.turn:
            if self.drawn is not None and self.drawn[0] == pid:
                card = self.drawn[1]
                return [card] if self._playable_by_value(pid, card) else []
            if self.pending:
                return [c for c in player.hand if self._stackable(c)]
            return [c for c in player.hand if self._playable_by_value(pid, c)]
        return self.jump_cards(pid)

    def jump_cards(self, pid: int) -> list[str]:
        if not self.rules.jump_in or self.phase != "playing":
            return []
        if self.pending or self.chooser is not None or self.drawn is not None:
            return []
        top = self.top() if self.discard else None
        if top is None or is_wild(top):
            return []
        return [c for c in self.players[pid].hand if c == top]

    def can_play(self, pid: int, card: str, *, jump: bool = False) -> tuple[bool, str]:
        if self.phase != "playing":
            return False, "not_playing"
        if self.chooser is not None:
            return False, "pick_swap"
        if not 0 <= pid < self.n():
            return False, "bad_player"
        player = self.players[pid]
        if card not in player.hand:
            return False, "not_in_hand"
        if jump:
            if card not in self.jump_cards(pid):
                return False, "bad_jump"
            if self.is_wild_card(card):
                return False, "bad_jump"
            return True, ""
        if pid != self.turn:
            return False, "not_your_turn"
        if self.drawn is not None and self.drawn[0] == pid:
            if card != self.drawn[1]:
                return False, "drawn_only"
            return (True, "") if self._playable_by_value(pid, card) else (False, "illegal")
        if self.pending:
            return (True, "") if self._stackable(card) else (False, "must_stack_pending")
        if not self._playable_by_value(pid, card):
            return (False, "wild4_has_color" if card_value(card) == "wild4" else "illegal")
        return True, ""

    @staticmethod
    def is_wild_card(card: str) -> bool:
        return is_wild(card)

    # ---------------------------------------------------------------------- moves
    def play(
        self,
        pid: int,
        card: str,
        color: str | None = None,
        *,
        jump: bool = False,
        now: float = 0.0,
    ) -> list[dict[str, Any]]:
        ok, code = self.can_play(pid, card, jump=jump)
        events: list[dict[str, Any]] = []
        if not ok:
            return [{"t": "error", "code": code}]
        value = card_value(card)
        if is_wild(card):
            if color not in COLORS:
                return [{"t": "error", "code": "need_color"}]
        if jump:
            # a jump-in seizes the turn: effects below apply as if it were our turn
            self.turn = pid
        if self.uno_window is not None and self.uno_window["pid"] != pid:
            self._emit({"t": "uno_safe", "pid": self.uno_window["pid"]}, events)
        self.uno_window = None
        self.drawn = None
        player = self.players[pid]
        player.hand.remove(card)
        self.discard.append(card)
        if color in COLORS:
            self.current_color = color
        else:
            self.current_color = card_color(card) or self.current_color
        player.said_uno = False
        self._emit({"t": "play", "pid": pid, "card": card, "color": self.current_color,
                    "jump": jump, "pending": self.pending}, events)
        if not player.hand:
            events += self._end_round(pid)
            return events
        if len(player.hand) == 1:
            self.uno_window = {"pid": pid, "deadline": now + self.rules.uno_window,
                               "said": False}
            self._emit({"t": "uno_window", "pid": pid}, events)
        if value == "7" and self.rules.seven_swap:
            self.chooser = {"pid": pid, "kind": "swap",
                            "deadline": now + self.rules.choice_timeout}
            self._emit({"t": "choose_swap", "pid": pid}, events)
            return events
        if value == "0" and self.rules.zero_rotate:
            self._rotate_hands()
            self._close_uno_window(events)
            self._emit({"t": "rotate", "pid": pid, "hand_counts": self.hand_counts()}, events)
            self.turn = self._next(pid)
        elif value == "skip":
            self.turn = self._next(pid, 2)
            self._emit({"t": "skip", "pid": pid}, events)
        elif value == "reverse":
            self.direction *= -1
            self.turn = self._next(pid, 2 if self.n() == 2 else 1)
            self._emit({"t": "reverse", "pid": pid}, events)
        elif value == "draw2":
            self.pending += 2
            self.pending_type = "draw2"
            self.current_color = card_color(card) or self.current_color
            self.turn = self._next(pid)
            self._emit({"t": "pending", "n": self.pending, "type": "draw2",
                        "pid": self.turn}, events)
        elif value == "wild4":
            self.pending += 4
            self.pending_type = "wild4"
            self.turn = self._next(pid)
            self._emit({"t": "pending", "n": self.pending, "type": "wild4",
                        "pid": self.turn}, events)
        else:
            self.turn = self._next(pid)
        self._emit({"t": "turn", "pid": self.turn}, events)
        return events

    def _rotate_hands(self) -> None:
        hands = [p.hand[:] for p in self.players]
        for i, player in enumerate(self.players):
            player.hand = hands[(i - self.direction) % self.n()]
            player.said_uno = len(player.hand) == 1 and player.said_uno

    def draw(self, pid: int, now: float = 0.0) -> list[dict[str, Any]]:
        if self.phase != "playing":
            return [{"t": "error", "code": "not_playing"}]
        if self.chooser is not None:
            return [{"t": "error", "code": "pick_swap"}]
        if pid != self.turn:
            return [{"t": "error", "code": "not_your_turn"}]
        if self.drawn is not None and self.drawn[0] == pid:
            return [{"t": "error", "code": "must_play_or_pass"}]
        events: list[dict[str, Any]] = []
        player = self.players[pid]
        if self.uno_window is not None:
            self._emit({"t": "uno_safe", "pid": self.uno_window["pid"]}, events)
            self.uno_window = None
        if self.pending:
            count = self.pending
            self.pending = 0
            self.pending_type = None
            self._deal_penalty(pid, count)
            self._emit({"t": "draw", "pid": pid, "n": count, "reason": "pending",
                        "count": len(player.hand)}, events)
            self.turn = self._next(pid)
            self._emit({"t": "turn", "pid": self.turn}, events)
            return events
        card = self._draw_card()
        if card is None:
            # nothing left to draw anywhere: pass instead of freezing the table
            self.turn = self._next(pid)
            self._emit({"t": "pass", "pid": pid, "reason": "deck_empty"}, events)
            self._emit({"t": "turn", "pid": self.turn}, events)
            return events
        player.hand.append(card)
        player.said_uno = False
        # the drawn card stays private: snapshots carry it only for its owner
        self._emit({"t": "draw", "pid": pid, "n": 1, "reason": "normal",
                    "count": len(player.hand)}, events)
        if self.rules.draw_playable and self._playable_by_value(pid, card):
            self.drawn = (pid, card)
            self._emit({"t": "may_play", "pid": pid}, events)
        else:
            self.turn = self._next(pid)
            self._emit({"t": "turn", "pid": self.turn}, events)
        return events

    # ------------------------------------------------------------------ combo play
    def _combo_state_ok(self, pid: int) -> bool:
        return (self.rules.combo_same and self.phase == "playing" and self.chooser is None
                and pid == self.turn and self.drawn is None and not self.pending)

    def _combo_value_ok(self, card: str) -> bool:
        """Plain number cards only; 7 / 0 are excluded while their special rule is on."""
        value = card_value(card)
        if not value.isdigit():
            return False
        if value == "7" and self.rules.seven_swap:
            return False
        if value == "0" and self.rules.zero_rotate:
            return False
        return True

    def combo_order(self, pid: int, sel: str) -> list[str] | None:
        """The cards played together when *sel* is picked (it goes last when it can), or None."""
        if not self._combo_state_ok(pid):
            return None
        hand = self.players[pid].hand
        if sel not in hand or not self._combo_value_ok(sel):
            return None
        group = [c for c in hand if card_value(c) == card_value(sel)]
        if len(group) < 2:
            return None
        others = list(group)
        others.remove(sel)
        first = next((c for c in others if self._playable_by_value(pid, c)), None)
        if first is not None:                      # `sel` can end the combo, so its colour wins
            others.remove(first)
            return [first] + others + [sel]
        if self._playable_by_value(pid, sel):      # only `sel` itself is legal: it has to start
            return [sel] + others
        return None

    def combo_orders(self, pid: int) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if not self._combo_state_ok(pid):
            return out
        for card in dict.fromkeys(self.players[pid].hand):
            order = self.combo_order(pid, card)
            if order:
                out[card] = order
        return out

    def play_multi(self, pid: int, cards: list[str], now: float = 0.0) -> list[dict[str, Any]]:
        """Play 2+ number cards of the same value in one turn; the first must be legal."""
        if not self.rules.combo_same:
            return [{"t": "error", "code": "combo_off"}]
        if self.phase != "playing":
            return [{"t": "error", "code": "not_playing"}]
        if self.chooser is not None:
            return [{"t": "error", "code": "pick_swap"}]
        if not 0 <= pid < self.n() or pid != self.turn:
            return [{"t": "error", "code": "not_your_turn"}]
        if self.drawn is not None or self.pending:
            return [{"t": "error", "code": "bad_combo"}]
        if (not isinstance(cards, list) or not 2 <= len(cards) <= 10
                or not all(isinstance(c, str) for c in cards)):
            return [{"t": "error", "code": "bad_msg"}]
        player = self.players[pid]
        spare = list(player.hand)
        for card in cards:
            if card not in spare:
                return [{"t": "error", "code": "not_in_hand"}]
            spare.remove(card)
        if (not all(self._combo_value_ok(c) for c in cards)
                or len({card_value(c) for c in cards}) != 1):
            return [{"t": "error", "code": "bad_combo"}]
        if not self._playable_by_value(pid, cards[0]):
            return [{"t": "error", "code": "illegal"}]
        events: list[dict[str, Any]] = []
        if self.uno_window is not None and self.uno_window["pid"] != pid:
            self._emit({"t": "uno_safe", "pid": self.uno_window["pid"]}, events)
        self.uno_window = None
        for card in cards[:-1]:                    # all but the last just land on the pile
            player.hand.remove(card)
            self.discard.append(card)
            self.current_color = card_color(card) or self.current_color
            self._emit({"t": "play", "pid": pid, "card": card, "color": self.current_color,
                        "jump": False, "pending": 0, "combo": True}, events)
        player.said_uno = False
        events += self.play(pid, cards[-1], now=now)   # the last one does the normal turn handling
        return events

    def pass_turn(self, pid: int) -> list[dict[str, Any]]:
        if self.phase != "playing" or self.drawn is None or self.drawn[0] != pid:
            return [{"t": "error", "code": "cannot_pass"}]
        self.drawn = None
        self.turn = self._next(pid)
        events: list[dict[str, Any]] = []
        self._emit({"t": "pass", "pid": pid}, events)
        self._emit({"t": "turn", "pid": self.turn}, events)
        return events

    def choose_swap(self, pid: int, target_pid: int, now: float = 0.0) -> list[dict[str, Any]]:
        if self.chooser is None or self.chooser["pid"] != pid or self.chooser["kind"] != "swap":
            return [{"t": "error", "code": "not_choosing"}]
        if target_pid == pid or not 0 <= target_pid < self.n():
            return [{"t": "error", "code": "bad_target"}]
        mine, theirs = self.players[pid], self.players[target_pid]
        mine.hand, theirs.hand = theirs.hand, mine.hand
        mine.said_uno = len(mine.hand) == 1
        theirs.said_uno = len(theirs.hand) == 1
        self.chooser = None
        self.turn = self._next(pid)
        events: list[dict[str, Any]] = []
        self._close_uno_window(events)
        self._emit({"t": "swap", "pid": pid, "target": target_pid,
                    "hand_counts": self.hand_counts()}, events)
        self._emit({"t": "turn", "pid": self.turn}, events)
        return events

    # ---------------------------------------------------------------- UNO window
    def call_uno(self, pid: int, now: float = 0.0) -> list[dict[str, Any]]:
        if self.uno_window is None or self.uno_window["pid"] != pid:
            return [{"t": "error", "code": "no_uno_window"}]
        if now > self.uno_window["deadline"]:
            return [{"t": "error", "code": "too_late"}]
        self.uno_window["said"] = True
        self.players[pid].said_uno = True
        events: list[dict[str, Any]] = []
        self._emit({"t": "uno_call", "pid": pid}, events)
        return events

    def catch_uno(self, accuser: int, now: float = 0.0) -> list[dict[str, Any]]:
        window = self.uno_window
        if window is None:
            return [{"t": "error", "code": "no_uno_window"}]
        if accuser == window["pid"]:
            return [{"t": "error", "code": "cannot_catch_self"}]
        if self.players[accuser].ai is False and not self.players[accuser].connected:
            return [{"t": "error", "code": "not_connected"}]
        if window["said"]:
            return [{"t": "error", "code": "already_called"}]
        offender = window["pid"]
        self.uno_window = None
        self._deal_penalty(offender, self.rules.uno_penalty)
        events: list[dict[str, Any]] = []
        self._emit({"t": "catch", "by": accuser, "pid": offender,
                    "n": self.rules.uno_penalty, "count": len(self.players[offender].hand)},
                   events)
        return events

    # -------------------------------------------------------------------- ticking
    def _tick_turn_timer(self, now: float, events: list[dict[str, Any]]) -> None:
        """Turn timer: a fresh countdown for every new turn; on timeout draw one card and pass."""
        limit = self.rules.turn_time
        if limit <= 0 or self.phase != "playing" or self.chooser is not None:
            self.turn_deadline = None            # off, between rounds, or waiting for a swap choice
            self._turn_key = None
            return
        # a new turn, a new round, or a card just drawn (decide whether to play it) restarts the clock
        key = (self.round_no, self.turn, self.drawn)
        if key != self._turn_key or self.turn_deadline is None:
            self._turn_key = key
            self.turn_deadline = now + limit
            return
        if now <= self.turn_deadline:
            return
        pid = self.turn
        self._emit({"t": "turn_timeout", "pid": pid}, events)
        if not (self.drawn is not None and self.drawn[0] == pid):
            events += self.draw(pid, now)        # takes the stack penalty if there is one
        if self.drawn is not None and self.drawn[0] == pid:
            events += self.pass_turn(pid)        # the drawn card is not played automatically
        self.turn_deadline = None
        self._turn_key = None

    def tick(self, now: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        self._tick_turn_timer(now, events)
        if self.uno_window is not None:
            if now > self.uno_window["deadline"]:
                pid = self.uno_window["pid"]
                said = self.uno_window["said"]
                self.uno_window = None
                if not said:
                    self._emit({"t": "uno_safe", "pid": pid}, events)
        if self.chooser is not None and now > self.chooser["deadline"]:
            pid = self.chooser["pid"]
            candidates = [p.pid for p in self.players if p.pid != pid]
            target = min(candidates, key=lambda q: (len(self.players[q].hand), q))
            events += self.choose_swap(pid, target, now)
        return events

    # ------------------------------------------------------------------ round end
    def _end_round(self, winner: int) -> list[dict[str, Any]]:
        points = 0
        for player in self.players:
            if player.pid != winner:
                points += sum(card_points(c) for c in player.hand)
        self.players[winner].round_score = points
        self.players[winner].score += points
        self.players[winner].total_score += points
        self.round_winner = winner
        self.round_points = {p.pid: sum(card_points(c) for c in p.hand) for p in self.players}
        self.phase = "round_over"
        self.drawn = None
        self.chooser = None
        self.uno_window = None
        events: list[dict[str, Any]] = []
        self._emit({"t": "round_end", "winner": winner, "points": points,
                    "hands": {p.pid: p.hand[:] for p in self.players},
                    "scores": {p.pid: p.score for p in self.players},
                    "round_scores": {p.pid: p.round_score for p in self.players}}, events)
        if self.rules.target_score and self.players[winner].score >= self.rules.target_score:
            self.match_winner = winner
            self.players[winner].wins += 1
            self.phase = "match_over"
            self._emit({"t": "match_end", "winner": winner,
                        "scores": {p.pid: p.score for p in self.players}}, events)
        return events

    def reset_match(self) -> None:
        """Start a fresh match.  ``wins`` / ``total_score`` are career totals kept for rematches."""
        for player in self.players:
            player.score = 0
            player.round_score = 0
        self.round_no = 0
        self.match_winner = None
        self.round_winner = None
        self.round_points = {}
        self.phase = "playing"

    def next_round(self, first: int | None = None) -> list[dict[str, Any]]:
        """Deal the next round after a round finished (winner leads next)."""
        if self.phase != "round_over":
            return [{"t": "error", "code": "not_round_over"}]
        if first is None and self.round_winner is not None:
            first = self.round_winner
        return self.start_round(first=first)

    # ------------------------------------------------------------------- snapshot
    def snapshot(self, viewer: int | None) -> dict[str, Any]:
        playing = self.phase == "playing"
        data: dict[str, Any] = {
            "round_no": self.round_no,
            "phase": self.phase,
            "rules": self.rules.as_dict(),
            "turn": self.turn,
            "direction": self.direction,
            "current_color": self.current_color,
            "pending": self.pending,
            "pending_type": self.pending_type,
            "draw_count": len(self.draw_pile),
            "top": self.top() if self.discard else None,
            "top_cards": self.discard[-6:],
            "players": [
                {
                    "pid": p.pid,
                    "name": p.name,
                    "ai": p.ai,
                    "host": p.is_host,
                    "connected": p.connected,
                    "count": len(p.hand),
                    "score": p.score,
                    "round_score": p.round_score,
                    "said_uno": p.said_uno,
                    "wins": p.wins,
                    "total_score": p.total_score,
                }
                for p in self.players
            ],
            "chooser": dict(self.chooser) if self.chooser else None,
            "uno_window": dict(self.uno_window) if self.uno_window else None,
            "turn_timer": ({"pid": self.turn, "total": self.rules.turn_time,
                            "deadline": self.turn_deadline}
                           if playing and self.turn_deadline is not None else None),
            "combo": (self.combo_orders(viewer)
                      if viewer is not None and 0 <= viewer < self.n() else {}),
            "drawn": self.drawn[1] if self.drawn and viewer == self.drawn[0] else None,
            "round_winner": self.round_winner,
            "match_winner": self.match_winner,
            "round_points": dict(self.round_points),
            "event_seq": self.event_seq,
            "hand": [],
            "legal": [],
            "jumpable": [],
            "revealed": {},
        }
        if viewer is not None and 0 <= viewer < self.n():
            data["hand"] = self.players[viewer].hand[:]
            if playing:
                data["legal"] = self.legal_cards(viewer)
                data["jumpable"] = self.jump_cards(viewer)
        if viewer is None or self.phase in ("round_over", "match_over"):
            # spectators see every hand; players only get the showdown at round end
            data["revealed"] = {p.pid: p.hand[:] for p in self.players}
        return data


