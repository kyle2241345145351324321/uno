# NET_NOTES — LAN layer (`net.py`)

Implementation notes for the v1 LAN protocol. `docs/PROTOCOL.md` is the source of
truth for the wire protocol; this file records the pieces that are easy to miss
when wiring the UI, plus the additions agreed with the Lead.

## Public API (exactly as implemented)

```python
DISCOVERY_PORT   # 45871
DEFAULT_PORT     # 45872
PROTOCOL_VERSION # 1

discover_rooms(timeout=0.8, discovery_port=DISCOVERY_PORT,
               extra_hosts=()) -> list[dict]

class NetClient:
    pid: int | None
    welcome: dict | None
    token: str                       # client-generated reconnect token
    __init__(name="player", spectate=False)
    connect(host, port=DEFAULT_PORT, timeout=5.0) -> dict   # welcome
    send(msg: dict) -> None
    poll() -> list[dict]             # server messages since last poll
    alive() -> bool
    close() -> None

class HostSession:
    engine   # engine.UnoGame | None (property; None until a game starts)
    server   # net.RoomServer (engine/port also useful for embedding)
    client   # NetClient loopback connection of the host (seat 0)
    port     # actual advertised TCP port (ephemeral when port=0)
    __init__(name, port=DEFAULT_PORT, bind="0.0.0.0", rules=None,
             discovery=True, room_name="", lang="zh")
    start(timeout=5.0) -> dict       # host's welcome
    close() -> None
```

`run python3 -m unittest discover -s tests -v` (15 tests) and
`python3 selftest.py` (12/12) are the acceptance commands.

## Wire additions beyond PROTOCOL.md v1 (Lead-approved)

1. `{"t":"events","items":[<engine event>, ...]}` is broadcast to **every**
   client after any action or tick produced engine events. Items are the raw
   engine events (including the internal `"turn"` events). Consumers should
   ignore unknown event types.
2. `{"t":"room","room":<room object>}` is broadcast whenever lobby membership,
   rules, names or the room state change (start/next_round/rematch too), and
   every `state` message carries the same object under `"room"`.
3. `welcome.chat` is the last up-to-50 chat entries as
   `{"pid", "name", "text", "ts"}` (newest last), matching live `chat` messages.

## Notes for `ui.py`

- Snapshot dicts are passed through unchanged from `engine.snapshot(viewer)`.
  Over the wire, JSON object keys become strings: `snap["revealed"]` is keyed
  by `"0"`, `"1"`, ... — convert with `int()` when indexing by pid.
- Players receive `hand`/`legal`/`jumpable` only for their own pid and only
  while `phase == "playing"`; spectators (`viewer=None`) receive `revealed`
  with all hands, and all viewers get `revealed` at round_over/match_over.
- `state.seq` is `snap["event_seq"]` (monotonic across rounds; use it to drop
  stale/out-of-order messages). `state.room` mirrors the latest `room` message.
- The host's own connection is `HostSession.client` (seat 0) — the UI sends
  actions through it exactly like any remote client.
- Host-only messages: `start`, `rules`, `kick`, `next_round`, `rematch`;
  non-hosts get `{"t":"error","code":"not_host"}`.
- `next_round` is only valid in `phase == "round_over"`; the round winner leads
  (the server passes the winner explicitly). `rematch` is only valid in
  `phase == "match_over"`; it calls `engine.reset_match()` and deals a fresh
  round, keeping career `wins`/`total_score` and zeroing scores.

## Behaviour details

- Host-authoritative: one 10 Hz tick thread (single engine mutator) drains the
  inbound action queue, calls `engine.tick(time.monotonic())`, advances AI
  seats (one decision per seat every 0.6–1.4 s), then broadcasts pending
  `events` and a `state` (after changes, and at 1 Hz when idle).
- Reconnect: `NetClient.token` is sent in `hello`; a disconnected human seat is
  kept (driven by an `AI("normal")` until reconnection returns the same pid,
  hand and name). A token from another live socket takes the seat over; the old
  socket receives `error: seat_taken`.
- Discovery: clients broadcast `discover` to 255.255.255.255 and 127.0.0.1 on
  45871 (plus `extra_hosts`) and dedupe replies by `(host, port)`; hosts answer
  by unicast and announce the room every 2 s. `discovery=False` keeps the room
  fully playable; if UDP 45871 is unavailable the TCP server still starts
  (`server.discovery_bound == False`).
- Robustness: UTF-8 JSON lines, 64 KiB cap (violations get `bad_msg` then the
  socket closes), per-connection send lock + 1 MiB outbound cap, rate limiting,
  daemon threads, and `close()` flushes final errors/acks briefly so clients
  see them before EOF.

## Known engine-side inconsistency (no net.py workaround needed by UI)

`engine.next_round(first=None)` documents "winner leads next" but defaults to
`(round_winner + 1) % n`. `net.py` always passes the winner explicitly, so the
server behaves per the docstring. Other callers of `engine.next_round()` should
pass `first=game.round_winner` if they want the documented behaviour.
