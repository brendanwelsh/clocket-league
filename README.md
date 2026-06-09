# clocket-league

**Your live Rocket League match on a pixel clock.** The in-game clock ticks down,
the score flashes on every goal, the display lights up `POST` when you ding the
crossbar, you get an `OVERTIME` banner, and a `FINAL` card when it's done.

No tracker account. No cloud. No browser. It reads Rocket League's *own* local
Stats API and talks straight to your clock over your LAN.

```
        ┌────────────────────────────┐
        │  3:24      ← clock ticking  │
        └────────────────────────────┘
   goal →  2-1      (flashes, blue vs orange)
crossbar →  POST     (blinks)
overtime →  OVERTIME → OT 1:12
   end   →  FINAL 3-2
```

---

## What you need

- An **Ulanzi TC001** (~$60) or any 32×8 **AWTRIX 3** device, on your WiFi.
  Flash AWTRIX 3 and note its IP: <https://blueforcer.github.io/awtrix3/>
- **Rocket League** on a PC, with the Stats API turned on (one-time, below).
- **Python 3.9+** on the same PC.

That's it. The default mode needs **zero extra Python packages**.

## 1. Turn on Rocket League's Stats API (once)

Rocket League ships with a local Stats API; you just enable it. Edit (create if
missing) this file in your RL install:

```
<RL install>\TAGame\Config\DefaultStatsAPI.ini
```

```ini
[StatsAPI]
Port=49123
PacketSendRate=30
```

Then **restart Rocket League** (the file is only read at launch). This is the
official, documented API — <https://www.rocketleague.com/en/developer/stats-api>.

> Common RL install paths:
> `C:\Program Files (x86)\Steam\steamapps\common\rocketleague\TAGame\Config\` (Steam)
> or your Epic library's `rocketleague\TAGame\Config\`.

## 2. Run it

```bash
python clocket_league.py --clock-host 192.168.1.50
```

Replace `192.168.1.50` with your clock's IP. Now play. The match takes over the
clock; between matches the clock goes back to normal.

Prefer not to pass flags? Copy `.env.example` to `.env` and set `CLOCK_HOST=...`.

---

## Options

```
--source rl|ballshark      where match data comes from (default: rl)
--transport http|mqtt      how to reach the clock (default: http)

# rl source
--rl-host / --rl-port      RL Stats API socket (default 127.0.0.1:49123)

# ballshark source
--ballshark-ws             ws URL of a running ballshark tracker

# http transport
--clock-host               your AWTRIX clock's IP/host

# mqtt transport
--mqtt-host/-port/-user/-pass
--awtrix-prefix            the clock's MQTT prefix (its uid, e.g. awtrix_11d5f8)
```

Any flag can be an env var (see `.env.example`): `CLOCK_HOST`, `CL_SOURCE`,
`CL_TRANSPORT`, `RL_HOST`, `RL_PORT`, `BALLSHARK_WS`, `MQTT_HOST`, etc.

### Sources

- **`rl`** (default) — reads Rocket League's local Stats API socket directly.
  Self-contained; this is the one for everybody.
- **`ballshark`** — reads a running [ballshark](https://github.com/brendanwelsh/ballshark)
  tracker's WebSocket instead. Use this if you already run ballshark and don't
  want two programs fighting over RL's socket.

### Transports

- **`http`** (default) — POSTs straight to the clock's HTTP API
  (`http://<clock>/api/notify`). Needs nothing but the clock's IP, and no extra
  Python packages.
- **`mqtt`** — publishes to a broker your clock is subscribed to (AWTRIX
  "HomeAssistant discovery" / custom MQTT). Needs `pip install paho-mqtt`.

`--source ballshark` needs `pip install websocket-client`.

---

## Run it forever

**Windows (Task Scheduler / shortcut):** see [`run.bat`](run.bat) — edit the
clock IP, then run it (or add it to Task Scheduler "At log on").

**Linux (systemd):** see [`clocket-league.service`](clocket-league.service).

---

## How it works

Rocket League streams length-prefixed JSON envelopes (`{"Event":...,"Data":...}`)
on a local TCP socket. clocket-league frames them, watches for `UpdateState`
(score + clock + overtime), `GoalScored`, `CrossbarHit`, and match start/end, and
renders a held AWTRIX notification — a full-screen takeover that stays pinned
while a match is live and is dismissed when it ends. It auto-reconnects and never
leaves a stale score on the matrix (it releases on match end, on a silent match,
on disconnect, and on exit).

## FAQ

**Does this need BakkesMod?** No — it uses the official Psyonix Stats API, not the
SOS plugin.

**Console (PS5/Xbox/Switch)?** No — the Stats API is PC-only. Use `--source
ballshark` only if you have ballshark fed some other way.

**Connection refused?** RL isn't running, or `PacketSendRate` is `0` / the ini
wasn't picked up. Re-check step 1 and restart RL.

---

Not affiliated with Psyonix or Epic Games. "Rocket League" is their trademark.
MIT licensed — see [LICENSE](LICENSE).
