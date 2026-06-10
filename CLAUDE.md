# CLAUDE.md — clocket-league

Project rules and specs for this repo. **Read first** before changing anything.

## What this is

A standalone, shareable tool that turns an **AWTRIX 3 pixel clock** (Ulanzi TC001,
32×8 RGB matrix) into a **live Rocket League scoreboard**. It reads Rocket League's
own local **Stats API** and drives the clock over the LAN. Public, MIT,
single-file: `clocket_league.py`. Repo: github.com/brendanwelsh/clocket-league.

**It is fully self-contained. Do NOT add a dependency on ballshark (or any other
tracker) — that was deliberately removed so the project is clean to share.**

## Hard rules

1. **Every new feature gets a GIF.** When you add a screen/callout, add a scene to
   `tools/capture_gifs.py`, regenerate (`python tools/capture_gifs.py <clock-ip>`),
   and add a row to the README "Every screen" gallery. The gallery must always
   match the feature set.
2. **Colors are exactly RL's** — blue `#1C7DF7`, orange `#FF8000`, used for
   *everything* blue/orange (cars, scores, banners). No brightened/dimmed variants.
3. **Test before claiming done.** A clean edit ≠ working on the matrix. Verify with
   the synthetic checks (drive a `Scoreboard` with a fake transport) and/or capture
   a frame off `/api/screen`.
4. **Standalone & friendly README.** Standard README depth — link out for generic
   setup (Python, flashing AWTRIX); only spell out the project-specific bits (the
   Stats-API `.ini`, the run command). Don't over-explain.

## Architecture (clocket_league.py)

```
source ──normalized events──▶ Scoreboard ──notify payloads──▶ transport ──▶ clock
(rl / demo / showcase)         (state machine)                 (http / mqtt)
```

- **Sources** (generators yielding normalized events; reconnect forever, never raise):
  - `rl` (the only live one): reads RL's Stats API TCP socket `127.0.0.1:49123`,
    brace-frames `{"Event","Data"}` envelopes. Maps `UpdateState`
    (score/`TimeSeconds`/`bOvertime`/players[boost,score,goals]), `GoalScored`,
    `CrossbarHit`, `StatfeedEvent` (Save/Demolish), `MatchCreated`/`MatchEnded`.
  - `demo`: a scripted highlight match (no RL). `showcase`: on-clock tour.
- **Normalized events:** `start` · `tick{t0,t1,t0name,t1name,secs,ot,players}` ·
  `goal` · `crossbar` · `save` · `demo` · `end` · `_connected`/`_disconnected`/`None`.
- **Scoreboard:** one held AWTRIX notification at a time (full-screen takeover).
  `_render(now)` is a pure function of state + timed windows, checked in priority
  order. `_publish` skips an identical payload (re-pushing restarts a scroll — that
  cuts long text off; never re-push the same card).
- **Transports:** `http` (POST `/api/notify` + `/api/notify/dismiss`, zero deps,
  default) or `mqtt` (`<prefix>/notify`).

## Screens / callouts (current set)

Launch greeting (rainbow `THIS IS ROCKET LEAGUE!`) → persistent gray
`WAITING FOR MATCH` while idle. Match: kickoff car + `3·2·1·GO!` → score+clock
(gray→amber→red urgency) → `GOAL!` (scorer color) → scorer name → score → `POST`
→ `WHAT A SAVE!` / `DEMO` / hat trick (a bomb → top-hat icon + `TRICK`) →
`OVERTIME`/`+m:ss` → your/team boost → `FINAL` (BLUE/ORANGE WINS → score → `MVP`)
→ winner's car drives the ball away → back to WAITING.

Config: `--screens`, `--swap-secs`, `--disable greeting,countdown,goal,post,overtime,urgency`,
`--team-names`, `--player-name`, `--source`, `--transport`. See README.

## GIFs

`tools/capture_gifs.py` drives each screen on a real clock, grabs `/api/screen`,
and renders the **AWTRIX web-UI look** — 29px square LEDs at 33px pitch inside the
TC001 bezel (`tools/bezel.png`) — one GIF per screen into `docs/gifs/`, quantized
to a shared 128-color palette. Long/scrolling banners need a publish-once animator
and enough duration to scroll fully.

## Files

`clocket_league.py` (everything) · `tools/capture_gifs.py` + `tools/bezel.png`
(gallery) · `docs/gifs/*` · `docs/TECHNICAL.md` (deep dive) · `README.md` ·
`.env.example` · `run.bat` (Windows) · `clocket-league.service` (Linux).

## Deployment

Runs on the **gaming PC** (where RL's localhost socket is) with
`--source rl --transport http --clock-host <ip>`. Enable RL's Stats API once
(`DefaultStatsAPI.ini`, `Port=49123`/`PacketSendRate=30`, restart RL).
