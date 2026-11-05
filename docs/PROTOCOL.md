# UNO LAN protocol (v1)

Host-authoritative, JSON-lines over TCP with UDP room discovery. No third-party
dependencies: `socket`, `threading`, `queue`, `json`, `time`, `uuid` only.

- TCP game port: **45872** (`DEFAULT_PORT`)
- UDP discovery port: **45871** (`DISCOVERY_PORT`)
- Encoding: UTF-8, one JSON object per line (`\n` terminated), max line 64 KiB.
- Version field `v: 1` on `hello`/`welcome`/`room`/`discover`.

## Architecture

```
host process                            client process
┌──────────────────────────┐            ┌──────────────┐
│ Tk UI  ── NetClient ──┐  │            │ Tk UI        │
│                      │  │            │   │          │
│ RoomServer(engine,ai)│  │◄──TCP──────►│   NetClient  │
│          ▲           │  │  JSON      └──────────────┘
│  UnoGame (authority) │  │  lines
└──────────────────────┘  │
      UDP announce/reply  │◄──UDP 45871── discover
```

The host UI also connects to its own server over loopback (`NetClient`), so the
UI has exactly one code path for hosting and joining. All rules run on the
server thread; clients only render `state` messages and send intents.

## Discovery (UDP)

Client → broadcast `255.255.255.255:45871` (also `127.0.0.1:45871` for
loopback tests, plus any extra hosts):

```json
{"t": "discover", "v": 1, "nonce": "8f3c"}
```

Host replies **unicast** to the sender:

```json
{"t": "room", "v": 1, "nonce": "8f3c", "name": "Kyle 的 UNO 房間",
 "host": "192.168.1.23", "port": 45872, "players": 3, "capacity": 10,
 "state": "lobby", "lang": "zh"}
```

`state` ∈ `lobby|playing|round_over|match_over`. Clients collect replies for
`timeout` (default 0.8 s) and de-duplicate by `(host, port)`. The host also
sends the same `room` object as a broadcast every 2 s so a passive client list
stays fresh; clients must tolerate duplicates. Discovery is optional:
`--no-discover` disables it, joining by explicit `host:port` still works.

## Lobby model

Server keeps `seats` (max 10, seat index = `pid`) and `spectators` (max 8).

A seat is `{pid, name, ai, host, connected}`. On `start` the server builds
`engine.UnoGame(names, ai_flags, rules)`; before that, `state.state` is
`"lobby"` and no `snap` is sent.

Rules may only be changed while in lobby, by the host. Defaults come from
`Rules()`; accepted keys are exactly `Rules.as_dict()` keys.

## Client → server messages

Common fields: `t` (type). Client-generated actions may carry `id` (int) which
the server echoes in `ack`.

| `t` | fields | who | notes |
| --- | --- | --- | --- |
| `hello` | `v`, `name`, `spectate`?, `token`? | anyone, first message | `token` re-attaches to a reserved seat after a drop |
| `start` | – | host | needs ≥ 2 occupied seats |
| `play` | `card`, `color`?, `jump`? | player | `color` required for `wild`/`wild4` |
| `draw` | – | player | |
| `pass` | – | player | only after drawing |
| `swap` | `target` | player | answer to the 7 chooser |
| `uno` | – | player | must be the player who just went to 1 card |
| `catch` | – | player | catch an opponent who forgot UNO |
| `chat` | `text` (≤ 300 chars) | anyone | broadcast, last 50 kept |
| `name` | `name` | player/spectator | lobby only |
| `rules` | `rules` | host | lobby only, partial dict allowed |
| `kick` | `pid` | host | lobby only |
| `next_round` | – | host | when `phase == "round_over"` |
| `rematch` | – | host | when `phase == "match_over"`, scores reset, wins/`total_score` kept |
| `leave` | – | anyone | closes connection, host may reassign |
| `ping` | `ts` | anyone | answered with `pong` |

## Server → client messages

| `t` | fields |
| --- | --- |
| `welcome` | `v`, `you` (`pid` or `null` for spectator), `room` (see below), `chat` (history: `{pid,name,text,ts}` newest last, max 50), `snap` (when a game is running) |
| `state` | `snap`, `seq` (`snap.event_seq`), `room` (same shape as `room` message) |
| `room` | `room` — broadcast whenever lobby membership / rules / names change (there is no `snap` in the lobby) |
| `events` | `items`: engine event dicts produced by the action/tick that triggered this broadcast. Items are raw engine events and may include internal `turn` events; clients should ignore event types they do not know |
| `chat` | `pid`, `name`, `text`, `ts` |
| `ack` | `id`, `ok`, `code`? |
| `error` | `code`, `msg`? (human text is a fallback only; UI localizes `code`) |
| `kicked` | `reason` |
| `pong` | `ts` |

`room` object:

```json
{"state": "lobby", "host_pid": 0, "lang": "zh",
 "seats": [{"pid":0,"name":"Kyle","host":true,"connected":true}],
 "spectators": [{"name":"TV"}], "rules": {..}, "players": 3, "capacity": 10}
```

The UI keeps its event log from `events` messages and its seats/scores from
`room`/`state.room`.  `state.snap` is exactly `UnoGame.snapshot(viewer)` for that client's seat
(`viewer=None` for spectators, which reveals every hand). Players never
receive another player's hand, only `players[].count`.

Out-of-turn clients still get `legal=[]` and `jumpable=[<identical cards>]` so
the UI can offer jump-in.

## Errors (`error.code` / `ack.code`)

Engine codes pass through unchanged (`not_your_turn`, `not_in_hand`,
`must_stack_pending`, `wild4_has_color`, `need_color`, `drawn_only`,
`bad_jump`, `pick_swap`, `illegal`, `cannot_pass`, `not_choosing`,
`bad_target`, `not_playing`, `bad_player`, `not_round_over`).

Protocol/lobby codes: `bad_msg`, `bad_version`, `lobby_full`,
`spectator_full`, `name_taken`, `not_host`, `not_lobby`, `need_players`,
`seat_taken`, `not_a_player`, `unknown_type`, `rate_limited`.

## Server loop

- One accept thread; one reader thread per socket feeding a `queue.Queue`.
- One tick thread at 10 Hz: applies queued client actions, then
  `engine.tick(time.monotonic())`, then releases a stalled turn, then broadcasts.
- Broadcast `state` after every applied action / engine event and at 1 Hz even
  when idle (so UNO-window countdowns and chooser deadlines are visible).
- There are no bots: every seat is a human connection. If the current player
  is disconnected for more than `AUTO_PASS_GRACE` (12 s), the server draws for
  them (respecting any pending penalty) and passes, so the table never stalls.
  Their seat stays reserved and re-attaches on `hello` with `token`.
- Send lock per connection; drop clients whose send queue exceeds 1 MiB or
  whose socket errors out.
- `engine.tick` and all `engine.*` mutations happen **only** on the tick thread.

## NetClient / HostSession API (what the UI uses)

```python
DISCOVERY_PORT, DEFAULT_PORT, PROTOCOL_VERSION

discover_rooms(timeout=0.8, discovery_port=DISCOVERY_PORT,
               extra_hosts=()) -> list[dict]        # room dicts, de-duplicated

class NetClient:
    pid: int | None        # None while disconnected or spectating
    welcome: dict | None
    def __init__(self, name: str = "player", spectate: bool = False) -> None
    def connect(self, host: str, port: int = DEFAULT_PORT,
                timeout: float = 5.0) -> dict        # returns welcome
    def send(self, msg: dict) -> None                # thread-safe, never raises
    def poll(self) -> list[dict]                     # drain incoming, UI thread
    def alive(self) -> bool
    def close(self) -> None

class HostSession:
    engine: engine.UnoGame                           # read-only for the UI
    server: RoomServer
    client: NetClient
    def __init__(self, name: str, port: int = DEFAULT_PORT, bind: str = "0.0.0.0",
                 rules: dict | None = None, discovery: bool = True,
                 room_name: str = "", lang: str = "zh") -> None
    def start(self, timeout: float = 5.0) -> dict    # starts server + loopback client
    def close(self) -> None
```

UI integration pattern: `client.poll()` from a Tk `after(40 ms)` loop, render
`welcome`/`state`/`chat`/`ack`/`error`/`kicked`.

## Compatibility

`v` must match `PROTOCOL_VERSION`; mismatches are rejected with
`bad_version`. Unknown message types get `error:unknown_type` and are ignored.
