# How clocket-league works (technical)

A single-file Python tool (`clocket_league.py`, stdlib-only for the default mode)
with three swappable pieces: a **source** (where match data comes from), a
**Scoreboard** state machine (what to show), and a **transport** (how it reaches
the clock).

```
 source ──normalized events──▶ Scoreboard ──notify payloads──▶ transport ──▶ AWTRIX clock
 (rl / demo / showcase)         (state machine)                 (http / mqtt)
```

## The Rocket League Stats API

Rocket League ships a local **Stats API** you enable in
`<RL install>\TAGame\Config\DefaultStatsAPI.ini` (`Port=49123`,
`PacketSendRate=30`). Once on, RL opens a TCP server on `127.0.0.1:49123` and,
while you're in a match, streams **concatenated JSON envelopes with no
delimiters**:

```json
{"Event":"UpdateState","Data":"{...json-encoded string...}"}
```

`Data` is itself a JSON **string** — you parse it a second time. Because the
envelopes aren't delimited, the reader accumulates bytes and walks a
brace-depth scanner (string- and escape-aware) to find each complete object
(`_find_complete_json`).

### Events we use

| RL `Event` | Meaning | What we read from `Data` |
|---|---|---|
| `MatchCreated` | match begins | — (→ kickoff) |
| `UpdateState` | ~30 Hz state | `Game.Teams[].Score`, `Game.TimeSeconds`, `Game.bOvertime`, `Players[].Boost/TeamNum/Name/PrimaryId` |
| `GoalScored` | a goal | — (the score on the next `UpdateState` confirms who) |
| `CrossbarHit` | ball hit the post | — (→ POST) |
| `MatchEnded` / `MatchDestroyed` | match over | — (→ FINAL, using the last score) |

(RL emits plenty more — `BallHit`, `StatfeedEvent`, `ClockUpdatedSeconds`, etc. —
that we ignore.)

## Sources → normalized events

Every source is a generator that reconnects forever, never raises, and yields a
small **normalized** vocabulary so the Scoreboard doesn't care where data came
from:

```
{"type":"start"}
{"type":"tick","t0","t1","t0name","t1name","secs","ot","players":[{team,boost,you}]}
{"type":"goal"}  {"type":"crossbar"}  {"type":"end"}
{"type":"_connected"} | {"type":"_disconnected"} | None   # None = idle poll tick
```

- **`rl`** — the socket reader above (the only live source).
- **`demo`** — a scripted highlight match (no RL needed). Loops, or runs once with
  `--demo-once`. Includes fake 4v4 boost so boost mode is demoable.

The `None` idle tick is important: it lets the state machine refresh the clock,
expire timed banners, run its watchdog, and release the FINAL card even when no
new data is arriving.

## Scoreboard state machine

One held AWTRIX notification (`hold:true`) is on screen at a time — a full-screen
takeover. `_render(now)` is a pure function of state + a set of **time windows**,
checked in priority order:

```
kickoff ball  →  3·2·1·GO!  →  GOAL! banner  →  score-only flash
              →  POST  →  OVERTIME banner  →  steady screens
```

Each event sets a deadline (`goal_until`, `post_until`, `ot_until`, …); `_render`
shows the highest-priority window that hasn't expired, else the **steady
screen**. The steady display is a rotation you configure with `--screens`
(`score-time`, `score`, `time`, `boost`, `boost-team`), each shown for
`--swap-secs`; `int(now / swap_secs) % len(screens)` picks the current one. Publishing is deduped (same payload within the refresh interval is
skipped) so we don't spam the matrix, but the ticking clock naturally repaints
~1×/second.

Robustness: the FINAL card auto-dismisses after a hold; a watchdog releases the
takeover if a live match goes silent (>30 s); and disconnect, shutdown, and end
all dismiss — so a stale score never sticks on the clock.

### Rendering on a 32×8 matrix

- **Text** uses AWTRIX colored fragments: `{"text":[{"t":"2","c":"#1C7DF7"},…]}`.
- **Boost bars** use AWTRIX draw ops in the same notification: filled rects
  (`df`), filled circle (`dfc`), pixels (`dp`). Self boost is one horizontal bar
  (width ∝ %); team boost is up to three vertical bars (height ∝ %, filled top→
  down). The kickoff ball is a white `dfc` with black `dp` pentagon dots.
- Team colors are RL blue `#1C7DF7` / orange `#FF6A00`; the scoring side brightens
  on a goal so you can see who scored.

## Transports

- **`http`** — `POST http://<clock>/api/notify` and `…/api/notify/dismiss`. No
  broker, no dependencies. The default.
- **`mqtt`** — publishes the same payloads to `<prefix>/notify` and
  `…/notify/dismiss` on a broker the clock is subscribed to (`paho-mqtt`).

## Configuration

Flags, environment variables, or a `.env` file (flags win, then env, then
`.env`). Features can be disabled individually with `--disable`
(`countdown,goal,post,overtime,urgency,boost`). See `--help` and `.env.example`.
