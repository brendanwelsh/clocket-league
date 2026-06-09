# clocket-league

**Your live Rocket League match on a pixel clock.** Score in team colors with the
in-game clock, a blinking **GOAL!** in the scorer's color, **POST** when you ring
the crossbar, overtime that counts up as **+0:42**, and a **BLUE WINS 3-2** finish.

No tracker account, no cloud, no browser — it reads Rocket League's *own* local
Stats API and talks straight to your clock over your LAN.

![clocket-league on an Ulanzi TC001](demo.gif)

---

## What it shows

| Moment | On the clock |
|---|---|
| Kickoff | a soccer ball, then **3 · 2 · 1 · GO!** |
| During play | **`2-1  3:24`** — score in blue/orange, clock gray → **amber** under 1:00 → **red** in the last 10s |
| Goal | **`GOAL!`** blinks in the scoring team's color, then the new score |
| Crossbar | **`POST`** blinks gold |
| Overtime | **`OVERTIME`**, then **`2-2 +0:42`** counting up (gold) |
| Full time | **`BLUE WINS 3-2`** (or the real team name), then back to normal |
| Optional | boost meters — your boost, or your teammates' |

---

## Prerequisites

1. **An AWTRIX 3 clock** — an [Ulanzi TC001](https://www.ulanzi.com/products/ulanzi-pixel-smart-clock-2882)
   (~$60) or any 32×8 AWTRIX device, on your WiFi. Flash AWTRIX 3 and note its IP:
   <https://blueforcer.github.io/awtrix3/>
2. **Rocket League on a PC** (Steam or Epic — the Stats API is PC-only).
3. **Python 3.9+** on that PC.

The default setup needs **no extra Python packages**.

## Install

```bash
git clone https://github.com/brendanwelsh/clocket-league
cd clocket-league
```

Optional extras (only if you use them):
```bash
pip install paho-mqtt        # only for --transport mqtt
pip install websocket-client # only for --source ballshark
```

## Turn on Rocket League's Stats API (one time)

Edit (create if missing) this file in your RL install:

```
<RL install>\TAGame\Config\DefaultStatsAPI.ini
```
```ini
[StatsAPI]
Port=49123
PacketSendRate=30
```

Then **restart Rocket League** (it's only read at launch). This is the official
Psyonix API — <https://www.rocketleague.com/en/developer/stats-api>.

> Typical paths:
> `C:\Program Files (x86)\Steam\steamapps\common\rocketleague\TAGame\Config\` (Steam),
> or your Epic library's `rocketleague\TAGame\Config\`.

## Run

```bash
python clocket_league.py --clock-host 192.168.1.50
```

Use your clock's IP. Now play — the match takes over the clock, and it goes back
to normal between matches. Prefer a file? Copy `.env.example` to `.env`.

**See it without Rocket League** (and record the GIF):
```bash
python clocket_league.py --source demo --clock-host 192.168.1.50
# --demo-once to play it through once; --demo-speed 1.5 to go faster
```

**Keep it running:** [`run.bat`](run.bat) (Windows / Task Scheduler) or
[`clocket-league.service`](clocket-league.service) (Linux systemd).

---

## Options

```
--clock-host IP            your AWTRIX clock (for the default --transport http)
--source rl|ballshark|demo where data comes from (default: rl)
--transport http|mqtt      how to reach the clock (default: http)
--team-names normalize|actual   FINAL says BLUE/ORANGE WINS, or the real team name
--disable a,b,c            turn features off: countdown,goal,post,overtime,urgency
--screens ...              what the steady display shows (see below)
--swap-secs 10             seconds per screen when --screens lists more than one
--player-name @You         your in-game name (needed for the boost screens)
```

Everything has an env var too (see [`.env.example`](.env.example)).

**Don't want POST?** `--disable post`. Want a calmer clock? `--disable urgency`.

### Screens (what the steady display shows)

`--screens` is a comma list that the display rotates through, `--swap-secs` each:

| value | shows |
|---|---|
| `score-time` | score **and** clock on one screen (default) |
| `score` | just the score |
| `time` | just the clock |
| `boost` | **your** boost — a bar that fills left → right |
| `boost-team` | your **teammates'** boost — up to 3 vertical bars, top → bottom (4v4) |

Examples:
```bash
--screens score,time                       # swap score / clock every 10s
--screens score-time,boost,boost-team      # score+clock, then your boost, then mates'
--screens score,time,boost --swap-secs 8
```
The boost screens need to know who you are — set `--player-name` (or `--player-id`);
if it can't find you, those screens are skipped. (Goals/POST/overtime still
interrupt whatever screen is up.)

### Sources & transports

- **`--source rl`** (default) reads RL's local Stats API socket — nothing else needed.
- **`--source ballshark`** reads a running [ballshark](https://github.com/brendanwelsh/ballshark)
  tracker instead, so two programs don't fight over RL's socket.
- **`--transport http`** (default) posts to the clock directly. **`--transport mqtt`**
  publishes to a broker the clock listens on.

---

## How it works (the short version)

Rocket League streams little JSON events on a local socket while you play.
clocket-league watches for the score, the clock, goals, crossbar hits, and start/
end, and paints a single "held" notification on your AWTRIX clock — a full-screen
takeover that stays up during the match and clears when it ends. It auto-reconnects
and never leaves a stale score on the matrix.

Want the gory details (the wire protocol, the event map, the rendering state
machine)? See **[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

## FAQ

- **Needs BakkesMod?** No — official Psyonix Stats API, not the SOS plugin.
- **Console?** No — the Stats API is PC-only.
- **`connection refused`?** RL isn't running, or `PacketSendRate=0` / the ini
  didn't take. Re-check the setup step and restart RL.

---

Not affiliated with Psyonix or Epic Games. "Rocket League" is their trademark.
MIT licensed — see [LICENSE](LICENSE).
