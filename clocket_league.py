#!/usr/bin/env python3
"""clocket-league — Rocket League live scoreboard on an AWTRIX pixel clock.

Put your match on a 32x8 LED matrix: the in-game clock ticks down, the score
flashes on every goal, the post lights up when you ding the crossbar, and you
get an OVERTIME banner and a FINAL card when it's done. No tracker account, no
cloud — it reads Rocket League's own local Stats API and talks straight to your
clock.

Hardware: an Ulanzi TC001 (or any 32x8 AWTRIX 3 device). Flash AWTRIX 3 and note
its IP. https://blueforcer.github.io/awtrix3/

INPUT (where the match data comes from) — pick with --source:
  rl         (default) read Rocket League's local Stats API TCP socket directly.
             Enable it once: edit  <RL install>\\TAGame\\Config\\DefaultStatsAPI.ini
                 [StatsAPI]
                 Port=49123
                 PacketSendRate=30
             then restart Rocket League. (Official, documented by Psyonix:
             https://www.rocketleague.com/en/developer/stats-api )
  ballshark  read a running ballshark tracker's WebSocket instead (handy if you
             already run https://github.com/brendanwelsh/ballshark and don't want
             two things fighting over RL's socket).

OUTPUT (how it reaches the clock) — pick with --transport:
  http   (default) POST straight to the clock's HTTP API. Just needs --clock-host.
         Zero extra Python packages.
  mqtt   publish to an MQTT broker the clock is subscribed to (AWTRIX "HomeAssistant
         discovery" / custom MQTT). Needs paho-mqtt and --mqtt-host / --awtrix-prefix.

Quick start (most people):
    python clocket_league.py --clock-host 192.168.1.50
    # play Rocket League. that's it.

Everything can also be set via env vars or a .env file (see .env.example).
MIT licensed. Not affiliated with Psyonix/Epic. "Rocket League" is their trademark.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Look / colors (hex, AWTRIX style). RL's own team colors are blue/orange.
# ---------------------------------------------------------------------------
BLUE = "#1C7DF7"      # team 0
ORANGE = "#F17820"    # team 1
BLUE_HI = "#8CC6FF"   # team 0, brightened (just scored)
ORANGE_HI = "#FFC08A"  # team 1, brightened (just scored)
WHITE = "#FFFFFF"
CLOCK = "#AAAAAA"     # in-game clock, plenty of time
AMBER = "#FFB300"     # clock under a minute
RED = "#FF2A2A"       # clock in the final seconds / loss
GREEN = "#22DD66"     # GO! / win
DIM = "#555555"
POST = "#FFD000"      # crossbar / post flash
OT = "#FFD000"        # overtime accent

# Timings (seconds)
CLOCK_REFRESH = 1.0   # how often to re-push the ticking clock
FLASH_SECS = 4.0      # how long the score flashes after a goal
POST_SECS = 1.6       # how long the POST banner blinks after a crossbar hit
OT_NOTICE_SECS = 3.0  # how long the OVERTIME banner shows when OT begins
START_SECS = 3.6      # kickoff "3 · 2 · 1 · GO!" countdown
FINAL_SECS = 12.0     # how long the FINAL card holds before releasing the clock
IDLE_RELEASE_SECS = 30.0  # if a live match goes silent this long, release the clock


def log(msg: str) -> None:
    print(f"[clocket-league] {msg}", flush=True)


def fmt_clock(secs) -> str:
    s = max(0, int(secs or 0))
    return f"{s // 60}:{s % 60:02d}"


# ===========================================================================
# Transports — turn a notify payload dict into something the clock receives.
# Both expose .notify(dict) and .dismiss(). A "held" (hold:true) notify is a
# full-screen takeover that stays until dismissed — that's our scoreboard.
# ===========================================================================
class HttpTransport:
    """POST to the AWTRIX HTTP API. No broker, no extra packages."""

    def __init__(self, host: str) -> None:
        self.base = f"http://{host}".rstrip("/")

    def _post(self, path: str, body: bytes) -> None:
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=4).read()
        except Exception as e:  # noqa: BLE001 — never let the clock kill the loop
            log(f"http {path} failed: {type(e).__name__}: {e}")

    def notify(self, payload: dict) -> None:
        self._post("/api/notify", json.dumps(payload).encode())

    def dismiss(self) -> None:
        self._post("/api/notify/dismiss", b"")


class MqttTransport:
    """Publish to <prefix>/notify on an MQTT broker the clock listens to."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 prefix: str) -> None:
        import paho.mqtt.client as mqtt  # lazy — only needed for --transport mqtt
        self.notify_topic = f"{prefix}/notify"
        self.dismiss_topic = f"{prefix}/notify/dismiss"
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                            client_id="clocket-league")
        except (AttributeError, TypeError):
            c = mqtt.Client(client_id="clocket-league")
        if user:
            c.username_pw_set(user, password)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.connect_async(host, port, keepalive=30)
        c.loop_start()
        self.c = c

    def notify(self, payload: dict) -> None:
        self.c.publish(self.notify_topic, json.dumps(payload), qos=0)

    def dismiss(self) -> None:
        self.c.publish(self.dismiss_topic, "", qos=0)


# ===========================================================================
# Sources — yield NORMALIZED events the renderer understands:
#   {"type":"start"}                                  match is beginning
#   {"type":"tick","t0":int,"t1":int,"secs":int,"ot":bool}
#   {"type":"goal"}                                   someone scored
#   {"type":"crossbar"}                               ball hit the post/crossbar
#   {"type":"end"}                                    match over (use last tick)
#   {"type":"_connected"} / {"type":"_disconnected"}  link state
#   None                                              idle tick (no data this poll)
# A source is a generator that reconnects forever and never raises.
# ===========================================================================
def _find_complete_json(buf: str):
    """Return (start, end_inclusive) of the next complete JSON object in buf, or
    None. Brace-depth scanner that respects strings + escapes. RL concatenates
    envelopes with no delimiters, so we frame them ourselves."""
    i, n = 0, len(buf)
    while i < n and buf[i] in " \r\n\t":
        i += 1
    if i >= n or buf[i] != "{":
        return None
    depth = 0
    in_str = esc = False
    for j in range(i, n):
        c = buf[j]
        if in_str:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i, j
    return None


def _map_rl_envelope(obj: dict):
    """RL Stats API envelope {"Event","Data"} -> normalized event (or None)."""
    event = obj.get("Event")
    data = obj.get("Data")
    raw = data
    if isinstance(data, str):
        try:
            raw = json.loads(data)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if event == "MatchCreated":
        return {"type": "start"}
    if event == "UpdateState":
        game = raw.get("Game") or {}
        teams = game.get("Teams") or []
        t0 = next((t for t in teams if t.get("TeamNum") == 0), {}) or {}
        t1 = next((t for t in teams if t.get("TeamNum") == 1), {}) or {}
        return {
            "type": "tick",
            "t0": int(t0.get("Score", 0) or 0),
            "t1": int(t1.get("Score", 0) or 0),
            "secs": int(game.get("TimeSeconds", 0) or 0),
            "ot": bool(game.get("bOvertime", False)),
        }
    if event == "GoalScored":
        return {"type": "goal"}
    if event == "CrossbarHit":
        return {"type": "crossbar"}
    if event in ("MatchEnded", "MatchDestroyed"):
        return {"type": "end"}
    return None


def rl_source(host: str, port: int, poll: float = 1.0):
    """Read Rocket League's local Stats API TCP socket directly."""
    while True:
        try:
            log(f"connecting to Rocket League Stats API at {host}:{port} ...")
            sock = socket.create_connection((host, port), timeout=6)
            sock.settimeout(poll)
            yield {"type": "_connected"}
            buf = ""
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    yield None
                    continue
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                while True:
                    hit = _find_complete_json(buf)
                    if hit is None:
                        break
                    s, e = hit
                    piece, buf = buf[s:e + 1], buf[e + 1:]
                    try:
                        obj = json.loads(piece)
                    except ValueError:
                        continue
                    ev = _map_rl_envelope(obj)
                    if ev:
                        yield ev
        except OSError as e:
            log(f"Rocket League socket not available ({type(e).__name__}); "
                f"is RL running with PacketSendRate>0? retrying...")
        finally:
            try:
                sock.close()  # type: ignore[has-type]
            except Exception:
                pass
        yield {"type": "_disconnected"}
        # back off ~2s while still emitting idle ticks so the loop stays alive
        for _ in range(max(1, int(2 / poll))):
            time.sleep(poll)
            yield None


def ballshark_source(ws_url: str, poll: float = 1.0):
    """Read a running ballshark tracker's WebSocket."""
    import websocket  # lazy — only needed for --source ballshark (websocket-client)
    while True:
        ws = None
        try:
            log(f"connecting to ballshark at {ws_url} ...")
            ws = websocket.create_connection(ws_url, timeout=6)
            ws.settimeout(poll)
            yield {"type": "_connected"}
            connected_at = time.monotonic()
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    yield None
                    continue
                if not raw:
                    break
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                t = m.get("type")
                d = m.get("data") or {}
                if t == "match_start":
                    yield {"type": "start"}
                elif t == "tick":
                    yield {
                        "type": "tick",
                        "t0": int(d.get("team0_score", 0) or 0),
                        "t1": int(d.get("team1_score", 0) or 0),
                        "secs": int(d.get("time_seconds", 0) or 0),
                        "ot": bool(d.get("is_overtime", False)),
                    }
                elif t == "goal":
                    yield {"type": "goal"}
                elif t == "crossbar":
                    yield {"type": "crossbar"}
                elif t == "match_end":
                    # ballshark replays the last match_end on connect — ignore it.
                    if time.monotonic() - connected_at > 3.0:
                        yield {"type": "end"}
        except Exception as e:  # noqa: BLE001
            log(f"ballshark link down ({type(e).__name__}); retrying...")
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        yield {"type": "_disconnected"}
        for _ in range(max(1, int(2 / poll))):
            time.sleep(poll)
            yield None


def demo_source(speed: float = 1.0):
    """A scripted, looping match — no Rocket League required. Great for recording
    a GIF: kickoff countdown, a comeback, a crossbar, overtime, a golden goal,
    FINAL — then it loops. --demo-speed scales the pace."""
    def nap(sec):
        time.sleep(max(0.05, sec / speed))

    while True:
        yield {"type": "_connected"}
        yield {"type": "start"}
        nap(START_SECS + 0.4)             # let the 3 · 2 · 1 · GO! play
        t0 = t1 = 0
        # game-second -> action: ("goal", team) | ("crossbar", None)
        script = {
            265: ("goal", 0),             # blue strikes first
            205: ("crossbar", None),      # rang the post
            165: ("goal", 1),             # orange answers
            95:  ("goal", 1),             # orange takes the lead
            20:  ("goal", 0),             # blue equalizes late! (on the 5s grid)
        }
        sec = 300
        while sec >= 0:
            act = script.get(sec)
            if act:
                kind, team = act
                if kind == "goal":
                    t0, t1 = (t0 + 1, t1) if team == 0 else (t0, t1 + 1)
                    yield {"type": "tick", "t0": t0, "t1": t1, "secs": sec, "ot": False}
                    nap(FLASH_SECS + 0.6)
                else:
                    yield {"type": "crossbar"}
                    nap(POST_SECS + 0.5)
            yield {"type": "tick", "t0": t0, "t1": t1, "secs": sec, "ot": False}
            nap(0.45)
            sec -= 5
        # 2-2 at the buzzer -> OVERTIME (sudden death, clock counts up)
        yield {"type": "tick", "t0": t0, "t1": t1, "secs": 0, "ot": True}
        nap(OT_NOTICE_SECS + 0.6)
        ot = 0
        while ot < 35:
            yield {"type": "tick", "t0": t0, "t1": t1, "secs": ot, "ot": True}
            nap(0.45)
            ot += 5
        t0 += 1                           # golden goal — blue wins it
        yield {"type": "tick", "t0": t0, "t1": t1, "secs": ot, "ot": True}
        nap(FLASH_SECS + 0.8)
        yield {"type": "end"}
        nap(FINAL_SECS + 2.5)             # hold FINAL, then loop


# ===========================================================================
# Renderer / state machine — decides what's on the matrix at any moment and
# pushes it through the transport. Steady state = the in-game clock; a goal
# shows the score EXCLUSIVELY (flashing) for a few seconds; a crossbar blinks
# POST; OT shows an OVERTIME banner then an "OT m:ss" clock; the end shows FINAL.
# ===========================================================================
class Scoreboard:
    def __init__(self, transport, my_team=None) -> None:
        self.tx = transport
        self.my_team = my_team   # 0, 1, or None — drives WIN/LOSS on the FINAL card
        self.active = False
        self.t0 = self.t1 = 0
        self.secs = 0
        self.ot = False
        self.flash_team = None   # which team most recently scored (0/1) -> color
        self.last_payload = None
        self.last_pub = 0.0
        self.last_data = 0.0
        # window deadlines (monotonic)
        self.start_until = 0.0
        self.flash_until = 0.0   # goal: score shown exclusively
        self.post_until = 0.0    # crossbar
        self.ot_until = 0.0      # overtime banner
        self.final_until = 0.0   # FINAL card

    # -- payload builders --------------------------------------------------
    @staticmethod
    def _held(text, *, center=True, color=None, blink=0):
        p = {"text": text, "hold": True, "stack": False, "wakeup": True,
             "center": center, "pushIcon": 0}
        if color:
            p["color"] = color
        if blink:
            p["blinkText"] = blink
        return p

    def _clock_card(self):
        # Clock gains urgency as time runs out: gray -> amber under 1:00 ->
        # red+blink in the final 10s. Overtime (sudden death) is always gold.
        if self.ot:
            return self._held([{"t": "OT ", "c": OT},
                               {"t": fmt_clock(self.secs), "c": OT}], blink=600)
        s = max(0, int(self.secs))
        if s <= 10:
            col, blink = RED, 500
        elif s <= 60:
            col, blink = AMBER, 0
        else:
            col, blink = CLOCK, 0
        return self._held([{"t": fmt_clock(s), "c": col}], blink=blink)

    def _score_card(self, blink):
        # The team that just scored is brightened; the other is dimmed, so a
        # glance tells you WHO scored. Score is shown exclusively (no clock).
        c0 = BLUE_HI if self.flash_team == 0 else (DIM if self.flash_team == 1 else BLUE)
        c1 = ORANGE_HI if self.flash_team == 1 else (DIM if self.flash_team == 0 else ORANGE)
        frags = [{"t": str(self.t0), "c": c0}, {"t": "-", "c": WHITE},
                 {"t": str(self.t1), "c": c1}]
        return self._held(frags, blink=blink and 400)

    def _final_card(self):
        c0 = BLUE if self.t0 >= self.t1 else DIM
        c1 = ORANGE if self.t1 >= self.t0 else DIM
        # If you told us your team, say WIN/LOSS; otherwise just FINAL.
        label, lcolor = "FINAL ", WHITE
        if self.my_team in (0, 1) and self.t0 != self.t1:
            won = (self.my_team == 0) == (self.t0 > self.t1)
            label, lcolor = ("WIN ", GREEN) if won else ("LOSS ", RED)
        frags = [{"t": label, "c": lcolor}, {"t": str(self.t0), "c": c0},
                 {"t": "-", "c": WHITE}, {"t": str(self.t1), "c": c1}]
        return self._held(frags, blink=300 if lcolor == GREEN else 0)

    def _countdown_card(self, now):
        """Kickoff '3 · 2 · 1 · GO!' over the start window."""
        rem = self.start_until - now
        if rem <= 0.6:
            return self._held("GO!", color=GREEN, blink=400)
        n = min(3, max(1, int(rem + 0.0)))   # 3.6..0.6 -> 3,2,1
        return self._held(str(n), color=WHITE)

    def _render(self, now):
        """Return the payload that should be on the matrix right now."""
        if now < self.start_until:
            return self._countdown_card(now)
        if now < self.flash_until:
            return self._score_card(blink=True)        # goal: score exclusively
        if now < self.post_until:
            return self._held("POST", color=POST, blink=300)
        if now < self.ot_until:
            return self._held("OVERTIME", center=False, color=OT, blink=400)
        return self._clock_card()

    def _publish(self, payload, now, force=False):
        key = json.dumps(payload, sort_keys=True)
        if not force and key == self.last_payload and (now - self.last_pub) < CLOCK_REFRESH:
            return
        self.tx.notify(payload)
        self.last_payload = key
        self.last_pub = now

    # -- event handlers ----------------------------------------------------
    def on_start(self, now):
        self.active = True
        self.t0 = self.t1 = 0
        self.secs = 0
        self.ot = False
        self.flash_team = None
        self.start_until = now + START_SECS
        self.flash_until = self.post_until = self.ot_until = self.final_until = 0.0
        self.last_data = now
        log("match starting")
        self._publish(self._render(now), now, force=True)

    def on_tick(self, ev, now):
        self.active = True
        self.final_until = 0.0      # a live tick supersedes any FINAL card
        self.last_data = now
        scored0 = ev["t0"] > self.t0
        scored1 = ev["t1"] > self.t1
        goal = scored0 or scored1
        entering_ot = ev["ot"] and not self.ot
        self.t0, self.t1, self.secs, self.ot = ev["t0"], ev["t1"], ev["secs"], ev["ot"]
        if goal:
            self.flash_team = 0 if scored0 else 1
            self.flash_until = now + FLASH_SECS
            log(f"GOAL ({'blue' if scored0 else 'orange'}) -> {self.t0}-{self.t1}"
                f"{' OT' if self.ot else ''}")
        if entering_ot:
            # Show the OVERTIME banner AFTER any goal flash (OT starts right after
            # a tying goal), so both the score and the banner get their moment.
            self.ot_until = max(now, self.flash_until) + OT_NOTICE_SECS
            log("OVERTIME")
        self._publish(self._render(now), now, force=goal or entering_ot)

    def on_goal(self, now):
        # Explicit goal event (RL fires this alongside the score change).
        self.flash_until = now + FLASH_SECS
        self.last_data = now
        self._publish(self._render(now), now, force=True)

    def on_crossbar(self, now):
        self.post_until = now + POST_SECS
        self.last_data = now
        log("POST (crossbar)")
        self._publish(self._render(now), now, force=True)

    def on_end(self, now):
        if not self.active and self.final_until:
            return
        log(f"FINAL {self.t0}-{self.t1}")
        self.active = False
        self.final_until = now + FINAL_SECS
        self.flash_until = self.post_until = self.ot_until = self.start_until = 0.0
        self._publish(self._final_card(), now, force=True)

    def on_idle(self, now):
        """Called on every poll with no new data: refresh clock, expire windows,
        release the FINAL card, and watchdog a match that went silent."""
        if self.final_until:
            if now >= self.final_until:
                self.tx.dismiss()
                self.final_until = 0.0
                self.last_payload = None
                log("released")
            return
        if self.active:
            if now - self.last_data > IDLE_RELEASE_SECS:
                self.release("match went silent")
                return
            self._publish(self._render(now), now)

    def release(self, reason):
        if self.active or self.final_until or self.last_payload:
            log(f"releasing the clock ({reason})")
        self.tx.dismiss()
        self.active = False
        self.t0 = self.t1 = 0
        self.final_until = 0.0
        self.last_payload = None


# ===========================================================================
# Wiring
# ===========================================================================
def load_dotenv(path=".env"):
    """Tiny .env loader (KEY=VALUE per line); does not overwrite real env vars."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def build_args():
    load_dotenv(os.environ.get("CLOCKET_ENV", ".env"))
    e = os.environ.get
    p = argparse.ArgumentParser(
        prog="clocket-league",
        description="Live Rocket League scoreboard on an AWTRIX pixel clock.")
    p.add_argument("--source", choices=["rl", "ballshark", "demo"],
                   default=e("CL_SOURCE", "rl"),
                   help="where match data comes from (rl=RL's socket [default], "
                        "ballshark=tracker WS, demo=scripted match for a GIF)")
    p.add_argument("--transport", choices=["http", "mqtt"],
                   default=e("CL_TRANSPORT", "http"),
                   help="how to reach the clock (default: http)")
    p.add_argument("--my-team", choices=["blue", "orange"],
                   default=e("CL_MY_TEAM", "") or None,
                   help="your team -> the FINAL card shows WIN/LOSS instead of FINAL")
    p.add_argument("--demo-speed", type=float, default=float(e("CL_DEMO_SPEED", "1.0")),
                   help="--source demo pacing multiplier (higher = faster)")
    # rl source
    p.add_argument("--rl-host", default=e("RL_HOST", "127.0.0.1"))
    p.add_argument("--rl-port", type=int, default=int(e("RL_PORT", "49123")))
    # ballshark source
    p.add_argument("--ballshark-ws", default=e("BALLSHARK_WS", "ws://127.0.0.1:5050/ws"))
    # http transport
    p.add_argument("--clock-host", default=e("CLOCK_HOST", ""),
                   help="AWTRIX clock IP/host for --transport http (e.g. 192.168.1.50)")
    # mqtt transport
    p.add_argument("--mqtt-host", default=e("MQTT_HOST", "127.0.0.1"))
    p.add_argument("--mqtt-port", type=int, default=int(e("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-user", default=e("MQTT_USER", ""))
    p.add_argument("--mqtt-pass", default=e("MQTT_PASS", ""))
    p.add_argument("--awtrix-prefix", default=e("AWTRIX_PREFIX", "awtrix"),
                   help="AWTRIX MQTT prefix (its uid, e.g. awtrix_11d5f8)")
    return p.parse_args()


def make_transport(a):
    if a.transport == "http":
        if not a.clock_host:
            sys.exit("error: --transport http needs --clock-host (your clock's IP)")
        log(f"transport: http -> {a.clock_host}")
        return HttpTransport(a.clock_host)
    log(f"transport: mqtt -> {a.mqtt_host}:{a.mqtt_port} prefix={a.awtrix_prefix}")
    return MqttTransport(a.mqtt_host, a.mqtt_port, a.mqtt_user, a.mqtt_pass,
                         a.awtrix_prefix)


def make_source(a):
    if a.source == "demo":
        log(f"source: demo (scripted match, speed={a.demo_speed})")
        return demo_source(a.demo_speed)
    if a.source == "rl":
        log(f"source: rl -> {a.rl_host}:{a.rl_port}")
        return rl_source(a.rl_host, a.rl_port)
    log(f"source: ballshark -> {a.ballshark_ws}")
    return ballshark_source(a.ballshark_ws)


def main():
    a = build_args()
    tx = make_transport(a)
    my_team = {"blue": 0, "orange": 1}.get(a.my_team)
    board = Scoreboard(tx, my_team=my_team)

    stop = {"flag": False}

    def _shutdown(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log("clocket-league running — play Rocket League. Ctrl-C to stop.")
    src = make_source(a)
    try:
        for ev in src:
            if stop["flag"]:
                break
            now = time.monotonic()
            if ev is None:
                board.on_idle(now)
                continue
            t = ev["type"]
            if t == "_connected":
                continue
            if t == "_disconnected":
                board.release("link lost")
            elif t == "start":
                board.on_start(now)
            elif t == "tick":
                board.on_tick(ev, now)
            elif t == "goal":
                board.on_goal(now)
            elif t == "crossbar":
                board.on_crossbar(now)
            elif t == "end":
                board.on_end(now)
    finally:
        board.release("shutdown")
        log("bye")


if __name__ == "__main__":
    main()
