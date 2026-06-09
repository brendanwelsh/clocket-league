#!/usr/bin/env python3
"""clocket-league — Rocket League live scoreboard on an AWTRIX pixel clock.

Your match on a 32x8 LED matrix: the score sits in team colors with the in-game
clock, a blinking GOAL! pops in the scorer's color on every goal, the post lights
up when you ding the crossbar, overtime counts up as "+m:ss", and it ends on a
"BLUE WINS 3-2" card. Optional boost meters. No tracker account, no cloud — it
reads Rocket League's own local Stats API and talks straight to your clock.

See README.md for setup and docs/TECHNICAL.md for how it works under the hood.
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
# Colors (AWTRIX hex). RL's own team colors are blue / orange.
# ---------------------------------------------------------------------------
BLUE = "#1C7DF7"        # RL blue team
ORANGE = "#FF8000"      # RL orange team (true orange — 50% green reads orange on LEDs)
BLUE_HI = "#8CC6FF"     # brightened (just scored)
ORANGE_HI = "#FFB347"
WHITE = "#FFFFFF"
CLOCK = "#AAAAAA"       # in-game clock, plenty of time
AMBER = "#FFB300"       # clock under a minute
RED = "#FF2A2A"         # clock in the final seconds
GREEN = "#22DD66"       # GO!
GOLD = "#FFD000"        # POST / overtime
TRACK = "#101822"       # empty boost track

# ---------------------------------------------------------------------------
# Timings (seconds)
# ---------------------------------------------------------------------------
CLOCK_REFRESH = 1.0      # how often to re-push the live score+clock
GOAL_BANNER_SECS = 1.8   # blinking "GOAL!" in the scorer's color
FLASH_SECS = 1.8         # then the score alone (both teams lit) before steady
POST_SECS = 1.8          # "POST" banner after a crossbar hit
OT_NOTICE_SECS = 2.6     # "OVERTIME" banner when OT begins
BALL_SECS = 1.3          # soccer ball shown at kickoff, before the countdown
START_SECS = 3.6         # kickoff "3 · 2 · 1 · GO!"
FINAL_NAME_SECS = 4.5    # "BLUE WINS!" shown first
FINAL_SECS = 13.0        # then the score blinks, until this much has elapsed
IDLE_RELEASE_SECS = 30.0  # release if a live match goes silent this long
# Features that can be turned off with --disable a,b,c
ALL_FEATURES = {"countdown", "goal", "post", "overtime", "urgency"}
# Steady "screens" you can rotate through (--screens), shown --swap-secs each
ALL_SCREENS = {"score-time", "score", "time", "boost", "boost-team"}


def log(msg: str) -> None:
    print(f"[clocket-league] {msg}", flush=True)


def fmt_clock(secs) -> str:
    s = max(0, int(secs or 0))
    return f"{s // 60}:{s % 60:02d}"


def boost_pct(v) -> int:
    """RL boost as 0..100 (some builds report 0..255 — scale defensively)."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    if v > 100:
        v = round(v / 255 * 100)
    return max(0, min(100, v))


def _ball_draw(cx, cy=4, r=3):
    """Draw ops for a soccer ball: white circle + one clean black center pentagon."""
    K = "#000000"
    return [
        {"dfc": [cx, cy, r, WHITE]},
        {"dp": [cx, cy - 1, K]},
        {"dp": [cx - 1, cy, K]}, {"dp": [cx, cy, K]}, {"dp": [cx + 1, cy, K]},
        {"dp": [cx, cy + 1, K]},
    ]


def step_boost(b: int) -> int:
    """Evolve a boost value the way it moves in a real match: mostly draining as
    you boost, with sudden jumps up when you grab pads (and the occasional full
    100 off a big pad). Sawtooth, not a smooth wave. (Demo/showcase only.)"""
    import random
    r = random.random()
    if r < 0.08:
        return 100                                  # big pad -> full
    if r < 0.50:
        return min(100, b + random.randint(10, 24))  # small pads / coasting
    return max(0, b - random.randint(10, 22))       # boosting (burns down)


# ===========================================================================
# Transports — .notify(dict) / .dismiss(). A held (hold:true) notify is a
# full-screen takeover that stays until dismissed: that's our scoreboard.
# ===========================================================================
class HttpTransport:
    def __init__(self, host: str) -> None:
        self.base = f"http://{host}".rstrip("/")

    def _post(self, path: str, body: bytes) -> None:
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=4).read()
        except Exception as e:  # noqa: BLE001
            log(f"http {path} failed: {type(e).__name__}: {e}")

    def notify(self, payload: dict) -> None:
        self._post("/api/notify", json.dumps(payload).encode())

    def dismiss(self) -> None:
        self._post("/api/notify/dismiss", b"")


class MqttTransport:
    def __init__(self, host, port, user, password, prefix) -> None:
        import paho.mqtt.client as mqtt  # only for --transport mqtt
        self.notify_topic = f"{prefix}/notify"
        self.dismiss_topic = f"{prefix}/notify/dismiss"
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="clocket-league")
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
# Sources — yield NORMALIZED events:
#   {"type":"start"}
#   {"type":"tick","t0","t1","secs","ot","players":[{team,boost,you}]}
#   {"type":"goal"} {"type":"crossbar"} {"type":"end"}
#   {"type":"_connected"} {"type":"_disconnected"}   None=idle
# A source is a generator that reconnects forever and never raises.
# ===========================================================================
def _find_complete_json(buf: str):
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


def _is_you(name, pid, my_name, my_id) -> bool:
    if my_id and pid and pid == my_id:
        return True
    if my_name and name and name.lstrip("@").lower() == my_name.lstrip("@").lower():
        return True
    return False


def _map_rl_envelope(obj, my_name, my_id):
    event = obj.get("Event")
    raw = obj.get("Data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
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
        players = [
            {"team": p.get("TeamNum", 0), "boost": boost_pct(p.get("Boost")),
             "you": _is_you(p.get("Name"), p.get("PrimaryId"), my_name, my_id)}
            for p in (raw.get("Players") or [])
        ]
        return {"type": "tick", "t0": int(t0.get("Score", 0) or 0),
                "t1": int(t1.get("Score", 0) or 0),
                "t0name": t0.get("Name") or "", "t1name": t1.get("Name") or "",
                "secs": int(game.get("TimeSeconds", 0) or 0),
                "ot": bool(game.get("bOvertime", False)), "players": players}
    if event == "GoalScored":
        return {"type": "goal"}
    if event == "CrossbarHit":
        return {"type": "crossbar"}
    if event in ("MatchEnded", "MatchDestroyed"):
        return {"type": "end"}
    return None


def rl_source(host, port, my_name, my_id, poll=1.0):
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
                    ev = _map_rl_envelope(obj, my_name, my_id)
                    if ev:
                        yield ev
        except OSError as e:
            log(f"RL socket unavailable ({type(e).__name__}); is RL running with "
                f"PacketSendRate>0? retrying...")
        finally:
            try:
                sock.close()  # type: ignore[has-type]
            except Exception:
                pass
        yield {"type": "_disconnected"}
        for _ in range(max(1, int(2 / poll))):
            time.sleep(poll)
            yield None


def ballshark_source(ws_url, my_name, my_id, poll=1.0):
    import websocket  # only for --source ballshark (websocket-client)
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
                t, d = m.get("type"), (m.get("data") or {})
                if t == "match_start":
                    yield {"type": "start"}
                elif t == "tick":
                    players = [
                        {"team": p.get("team_num", 0), "boost": boost_pct(p.get("boost")),
                         "you": _is_you(p.get("name"), p.get("primary_id"), my_name, my_id)}
                        for p in (d.get("players") or [])
                    ]
                    yield {"type": "tick", "t0": int(d.get("team0_score", 0) or 0),
                           "t1": int(d.get("team1_score", 0) or 0),
                           "t0name": d.get("team0_name") or "",
                           "t1name": d.get("team1_name") or "",
                           "secs": int(d.get("time_seconds", 0) or 0),
                           "ot": bool(d.get("is_overtime", False)), "players": players}
                elif t == "goal":
                    yield {"type": "goal"}
                elif t == "crossbar":
                    yield {"type": "crossbar"}
                elif t == "match_end":
                    if time.monotonic() - connected_at > 3.0:  # skip replayed end
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


def demo_source(speed=1.0, once=False):
    """A tight, scripted highlight match — no Rocket League needed. Rips through
    every scene once: kickoff, goals (both teams), a crossbar, a late equalizer,
    overtime, a golden goal, BLUE WINS. Loops unless `once`. For trying it / GIFs.

    Includes fake 4v4 boost so --boost-mode can be demoed too."""
    import random
    boosts = [random.randint(10, 90) for _ in range(8)]  # 'you'+3 mates, +4 opp

    def nap(sec):
        time.sleep(max(0.04, sec / speed))

    def players(sec):
        for i in range(8):
            boosts[i] = step_boost(boosts[i])            # drain/refill like a real game
        ps = [{"team": 0, "boost": boosts[0], "you": True}]
        ps += [{"team": 0, "boost": boosts[i], "you": False} for i in (1, 2, 3)]
        ps += [{"team": 1, "boost": boosts[i], "you": False} for i in (4, 5, 6, 7)]
        return ps

    def tick(t0, t1, secs, ot=False):
        return {"type": "tick", "t0": t0, "t1": t1, "secs": secs, "ot": ot,
                "t0name": "Blue", "t1name": "Orange", "players": players(secs)}

    # (event, dwell) beats. "g0"/"g1"=goal by team, "x"=crossbar, numbers=clock
    while True:
        yield {"type": "_connected"}
        nap(2.0)                              # lead-in so you can hit record
        yield {"type": "start"}
        nap(BALL_SECS + START_SECS + 0.2)     # soccer ball -> 3 · 2 · 1 · GO!
        t0 = t1 = 0
        yield tick(t0, t1, 300); nap(1.6)     # 5:00
        t0 += 1; yield tick(t0, t1, 268); nap(GOAL_BANNER_SECS + FLASH_SECS + 0.5)
        yield tick(t0, t1, 250); nap(0.9)
        yield {"type": "crossbar"}; nap(POST_SECS + 0.4)
        yield tick(t0, t1, 232); nap(0.7)
        t1 += 1; yield tick(t0, t1, 205); nap(GOAL_BANNER_SECS + FLASH_SECS + 0.5)
        t1 += 1; yield tick(t0, t1, 150); nap(GOAL_BANNER_SECS + FLASH_SECS + 0.5)
        yield tick(t0, t1, 52); nap(1.1)      # amber (<1:00)
        yield tick(t0, t1, 8); nap(1.1)       # red (<:10)
        t0 += 1; yield tick(t0, t1, 3); nap(GOAL_BANNER_SECS + FLASH_SECS + 0.6)  # 2-2!
        yield tick(t0, t1, 0, ot=True); nap(OT_NOTICE_SECS + 0.4)   # OVERTIME
        yield tick(t0, t1, 9, ot=True); nap(1.1)
        yield tick(t0, t1, 17, ot=True); nap(1.1)
        t0 += 1; yield tick(t0, t1, 21, ot=True); nap(GOAL_BANNER_SECS + FLASH_SECS + 0.6)
        yield {"type": "end"}
        nap(FINAL_SECS + 2.0)
        if once:
            return


def run_showcase(tx, speed=1.0, loop=True):
    """A guided tour: every screen/scene as its own labeled segment, separated by
    a rolling soccer ball. Record it once and split the clip into one GIF per
    segment. Reuses the real card builders so what you see is what you get."""
    import math
    from time import sleep
    sb = Scoreboard(tx)

    def nap(s):
        sleep(max(0.03, s / speed))

    def push(card, secs):
        tx.notify(card)
        nap(secs)

    def roll():
        """Transition: a little car drives across pushing the soccer ball. No text
        labels — segments are separated by this so you can split the clip cleanly."""
        K = "#000000"
        for x in range(-11, 41, 2):
            bx = x + 11                                 # ball rides ahead, with a gap
            ph = x * 0.7
            draw = [
                {"df": [x, 4, 6, 2, ORANGE]},           # car body
                {"df": [x + 1, 3, 3, 1, ORANGE]},       # cockpit
                {"dp": [x + 1, 6, "#444444"]}, {"dp": [x + 4, 6, "#444444"]},  # wheels
                {"dfc": [bx, 4, 2, WHITE]}, {"dp": [bx, 4, K]},                # ball
                {"dp": [bx + round(1.2 * math.cos(ph)),
                        4 + round(1.2 * math.sin(ph)), K]},                    # spin
            ]
            tx.notify(sb._held(None, draw=draw))
            nap(0.05)

    while True:
        # Kickoff: ball, then 3 · 2 · 1 · GO!
        push(sb._ball_card(), 1.6)
        for n in ("3", "2", "1"):
            push(sb._held(n, color=WHITE), 0.85)
        push(sb._held("GO!", color=GREEN, blink=400), 1.1)
        roll()
        # Score + clock together
        sb.t0, sb.t1 = 2, 1
        for s in (212, 206, 200, 194):
            sb.secs = s
            push(sb._live_card(), 1.0)
        roll()
        # Score only
        push(sb._score_panel(), 3.5)
        roll()
        # Clock only, with urgency (gray -> amber -> red)
        sb.ot = False
        for s in (95, 45, 9):
            sb.secs = s
            push(sb._time_panel(), 2.0)
        roll()
        # Goal (both teams)
        sb.t0, sb.t1, sb.flash_team = 2, 1, 0
        push(sb._goal_banner(), 1.6)
        push(sb._score_only(), 1.6)
        sb.t0, sb.t1, sb.flash_team = 2, 2, 1
        push(sb._goal_banner(), 1.6)
        push(sb._score_only(), 1.6)
        roll()
        # Post
        push(sb._held("POST", color=GOLD, blink=300), 3.0)
        roll()
        # Overtime
        push(sb._held("OVERTIME", center=False, color=GOLD, blink=400), 2.4)
        sb.ot = True
        for s in (4, 9, 14, 19):
            sb.secs = s
            push(sb._time_panel(), 1.0)
        sb.ot = False
        roll()
        # Your boost (fills L->R, realistic flow)
        b = 60
        for _ in range(20):
            b = step_boost(b)
            sb.players = [{"team": 0, "boost": b, "you": True}]
            push(sb._boost_self(), 0.38)
        roll()
        # Teammates' boost (vertical bars, fill bottom->top, realistic)
        bs = [40, 80, 55]
        for _ in range(22):
            bs = [step_boost(x) for x in bs]
            sb.players = ([{"team": 0, "boost": 0, "you": True}] +
                          [{"team": 0, "boost": bs[i], "you": False} for i in range(3)])
            push(sb._boost_team(), 0.38)
        roll()
        # Final: BLUE WINS! then the score blinks
        sb.t0, sb.t1 = 3, 2
        push(sb._final_name(), 3.6)
        push(sb._final_score(), 4.0)
        roll()
        if not loop:
            return


# ===========================================================================
# Renderer / state machine
# ===========================================================================
class Scoreboard:
    def __init__(self, transport, *, disabled=None, screens=None, swap_secs=10.0,
                 team_names="normalize") -> None:
        self.tx = transport
        self.disabled = set(disabled or ())
        self.screens = list(screens) if screens else ["score-time"]
        self.swap_secs = max(2.0, swap_secs)
        self.team_names = team_names   # "normalize" -> BLUE/ORANGE, "actual" -> real
        self.active = False
        self.t0 = self.t1 = 0
        self.t0name = self.t1name = ""
        self.secs = 0
        self.ot = False
        self.players = []
        self.flash_team = None
        self.last_payload = None
        self.last_pub = 0.0
        self.last_data = 0.0
        self.ball_until = 0.0     # soccer ball at kickoff
        self.goal_until = 0.0     # "GOAL!" banner
        self.flash_until = 0.0    # score-only emphasis
        self.post_until = 0.0
        self.ot_until = 0.0
        self.start_until = 0.0    # end of the 3-2-1 countdown
        self.final_name_until = 0.0
        self.final_until = 0.0

    def on(self, feature: str) -> bool:
        return feature not in self.disabled

    # -- payload builders --------------------------------------------------
    @staticmethod
    def _held(text, *, center=True, color=None, blink=0, draw=None):
        p = {"hold": True, "stack": False, "wakeup": True, "center": center,
             "pushIcon": 0}
        if draw is not None:
            p["draw"] = draw
            p["text"] = ""
        else:
            p["text"] = text
        if color:
            p["color"] = color
        if blink:
            p["blinkText"] = blink
        return p

    def _live_card(self):
        """Score (blue-orange, always prominent) + the clock. OT shows '+m:ss'."""
        frags = [{"t": str(self.t0), "c": BLUE}, {"t": "-", "c": WHITE},
                 {"t": str(self.t1), "c": ORANGE}]
        if self.ot:
            frags.append({"t": " +" + fmt_clock(self.secs), "c": GOLD})
            return self._held(frags)
        s = max(0, int(self.secs))
        if self.on("urgency") and s <= 10:
            col, blink = RED, 500
        elif self.on("urgency") and s <= 60:
            col, blink = AMBER, 0
        else:
            col, blink = CLOCK, 0
        frags.append({"t": " " + fmt_clock(s), "c": col})
        return self._held(frags, blink=blink)

    def _goal_banner(self):
        col = BLUE if self.flash_team == 0 else ORANGE
        return self._held("GOAL!", color=col, blink=350)

    def _score_only(self):
        c0 = BLUE_HI if self.flash_team == 0 else BLUE   # both teams stay lit
        c1 = ORANGE_HI if self.flash_team == 1 else ORANGE
        return self._held([{"t": str(self.t0), "c": c0}, {"t": "-", "c": WHITE},
                           {"t": str(self.t1), "c": c1}], blink=400)

    def _team_label(self, team):
        """BLUE/ORANGE, or the actual lobby name if --team-names actual."""
        nm = (self.t0name if team == 0 else self.t1name).strip()
        if self.team_names == "actual" and nm and nm.lower() not in ("blue", "orange"):
            return nm.upper()[:12]
        return "BLUE" if team == 0 else "ORANGE"

    def _final_name(self):
        """First beat: the winner, by name. Plays the full name (scrolls if long),
        no score yet."""
        if self.t0 == self.t1:
            return self._held("FULL TIME", center=False, color=WHITE)
        wteam = 0 if self.t0 > self.t1 else 1
        wcol = BLUE if wteam == 0 else ORANGE
        return self._held(self._team_label(wteam) + " WINS!", center=False, color=wcol)

    def _final_score(self):
        """Second beat: the score on its own, blinking (no scroll)."""
        c0 = BLUE if self.t0 >= self.t1 else WHITE
        c1 = ORANGE if self.t1 >= self.t0 else WHITE
        return self._held([{"t": str(self.t0), "c": c0}, {"t": "-", "c": WHITE},
                           {"t": str(self.t1), "c": c1}], blink=450)

    def _ball_card(self):
        """Soccer ball at kickoff: white ball with one clean black center pentagon."""
        return self._held(None, draw=_ball_draw(15))

    # -- steady "screens" (rotated by --screens / --swap-secs) -------------
    def _score_panel(self):
        return self._held([{"t": str(self.t0), "c": BLUE}, {"t": "-", "c": WHITE},
                           {"t": str(self.t1), "c": ORANGE}])

    def _time_panel(self):
        if self.ot:
            return self._held([{"t": "+" + fmt_clock(self.secs), "c": GOLD}])
        s = max(0, int(self.secs))
        if self.on("urgency") and s <= 10:
            col, blink = RED, 500
        elif self.on("urgency") and s <= 60:
            col, blink = AMBER, 0
        else:
            col, blink = CLOCK, 0
        return self._held([{"t": fmt_clock(s), "c": col}], blink=blink)

    def _boost_self(self):
        me = next((p for p in self.players if p.get("you")), None)
        if not me:
            return None
        pct = me["boost"]
        w = round(pct / 100 * 32)                            # fills left -> right
        col = BLUE if me["team"] == 0 else ORANGE
        draw = [{"df": [0, 0, 32, 8, TRACK]}]
        if w > 0:
            draw.append({"df": [0, 0, w, 8, col]})
        s = str(pct)                                          # the boost number on top
        draw.append({"dt": [max(0, (32 - len(s) * 4) // 2), 1, s, WHITE]})
        return self._held(None, draw=draw)

    def _boost_team(self):
        my = self._your_team()
        mates = [p for p in self.players if p.get("team") == my and not p.get("you")][:3]
        if my is None or not mates:
            return None
        col = BLUE if my == 0 else ORANGE
        draw, xs, w = [], [2, 13, 24], 6        # wide gaps between bars
        for i, p in enumerate(mates):           # vertical, fill bottom -> up
            x = xs[i]
            draw.append({"df": [x, 0, w, 8, TRACK]})
            h = round(p["boost"] / 100 * 8)
            if h > 0:
                draw.append({"df": [x, 8 - h, w, h, col]})
        return self._held(None, draw=draw)

    def _active_screens(self):
        """Drop boost screens we can't draw (no 'you' / no teammates)."""
        out = []
        for s in self.screens:
            if s in ("score-time", "score", "time"):
                out.append(s)
            elif s == "boost" and any(p.get("you") for p in self.players):
                out.append(s)
            elif s == "boost-team" and self._your_team() is not None and any(
                    p.get("team") == self._your_team() and not p.get("you")
                    for p in self.players):
                out.append(s)
        return out or ["score-time"]

    def _screen_card(self, name):
        if name == "score":
            return self._score_panel()
        if name == "time":
            return self._time_panel()
        if name == "boost":
            return self._boost_self() or self._live_card()
        if name == "boost-team":
            return self._boost_team() or self._live_card()
        return self._live_card()   # "score-time"

    def _countdown(self, now):
        rem = self.start_until - now
        if rem <= 0.6:
            return self._held("GO!", color=GREEN, blink=400)
        return self._held(str(min(3, max(1, int(rem)))), color=WHITE)

    def _your_team(self):
        me = next((p for p in self.players if p.get("you")), None)
        return me["team"] if me else None

    def _render(self, now):
        if self.on("countdown") and now < self.ball_until:
            return self._ball_card()
        if self.on("countdown") and now < self.start_until:
            return self._countdown(now)
        if self.on("goal") and now < self.goal_until:
            return self._goal_banner()
        if now < self.flash_until:
            return self._score_only()
        if self.on("post") and now < self.post_until:
            return self._held("POST", color=GOLD, blink=300)
        if self.on("overtime") and now < self.ot_until:
            return self._held("OVERTIME", center=False, color=GOLD, blink=400)
        # steady: rotate through the configured screens, --swap-secs each
        screens = self._active_screens()
        return self._screen_card(screens[int(now / self.swap_secs) % len(screens)])

    def _publish(self, payload, now, force=False):
        key = json.dumps(payload, sort_keys=True)
        if not force and key == self.last_payload and (now - self.last_pub) < CLOCK_REFRESH:
            return
        self.tx.notify(payload)
        self.last_payload, self.last_pub = key, now

    # -- event handlers ----------------------------------------------------
    def _reset_windows(self):
        self.ball_until = self.goal_until = self.flash_until = self.post_until = 0.0
        self.ot_until = self.start_until = 0.0
        self.final_name_until = self.final_until = 0.0

    def on_start(self, now):
        self.active = True
        self.t0 = self.t1 = 0
        self.secs = 0
        self.ot = False
        self.flash_team = None
        self._reset_windows()
        self.ball_until = now + BALL_SECS                 # ball, then countdown
        self.start_until = self.ball_until + START_SECS
        self.last_data = now
        log("match starting")
        self._publish(self._render(now), now, force=True)

    def on_tick(self, ev, now):
        self.active = True
        self.final_until = 0.0
        self.last_data = now
        self.players = ev.get("players") or self.players
        if ev.get("t0name"):
            self.t0name = ev["t0name"]
        if ev.get("t1name"):
            self.t1name = ev["t1name"]
        scored0, scored1 = ev["t0"] > self.t0, ev["t1"] > self.t1
        goal = scored0 or scored1
        entering_ot = ev["ot"] and not self.ot
        self.t0, self.t1, self.secs, self.ot = ev["t0"], ev["t1"], ev["secs"], ev["ot"]
        if goal:
            self.flash_team = 0 if scored0 else 1
            self.goal_until = now + GOAL_BANNER_SECS
            self.flash_until = now + GOAL_BANNER_SECS + FLASH_SECS
            log(f"GOAL ({'blue' if scored0 else 'orange'}) -> {self.t0}-{self.t1}"
                f"{' OT' if self.ot else ''}")
        if entering_ot:
            self.ot_until = max(now, self.flash_until) + OT_NOTICE_SECS
            log("OVERTIME")
        self._publish(self._render(now), now, force=goal or entering_ot)

    def on_goal(self, now):
        self.goal_until = now + GOAL_BANNER_SECS
        self.flash_until = now + GOAL_BANNER_SECS + FLASH_SECS
        self.last_data = now
        self._publish(self._render(now), now, force=True)

    def on_crossbar(self, now):
        if not self.on("post"):
            return
        self.post_until = now + POST_SECS
        self.last_data = now
        log("POST (crossbar)")
        self._publish(self._render(now), now, force=True)

    def on_end(self, now):
        if not self.active and self.final_until:
            return
        log(f"FINAL {self.t0}-{self.t1}")
        self.active = False
        self._reset_windows()
        self.final_name_until = now + FINAL_NAME_SECS   # "BLUE WINS!" first
        self.final_until = now + FINAL_SECS             # then score blinks
        self._publish(self._final_name(), now, force=True)

    def on_idle(self, now):
        if self.final_until:
            if now >= self.final_until:
                self.tx.dismiss()
                self.final_until = 0.0
                self.last_payload = None
                log("released")
            elif now >= self.final_name_until:
                self._publish(self._final_score(), now)   # second beat: score blink
            else:
                self._publish(self._final_name(), now)
            return
        if self.active:
            if now - self.last_data > IDLE_RELEASE_SECS:
                self.release("match went silent")
            else:
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
    p.add_argument("--source", choices=["rl", "ballshark", "demo", "showcase"],
                   default=e("CL_SOURCE", "rl"),
                   help="match data: rl (RL's socket, default), ballshark (tracker WS), "
                        "demo (scripted match), showcase (labeled tour of every screen)")
    p.add_argument("--transport", choices=["http", "mqtt"],
                   default=e("CL_TRANSPORT", "http"), help="how to reach the clock")
    p.add_argument("--disable", default=e("CL_DISABLE", ""),
                   help="comma list of features to turn off: " + ",".join(sorted(ALL_FEATURES)))
    p.add_argument("--team-names", choices=["normalize", "actual"],
                   default=e("CL_TEAM_NAMES", "normalize"),
                   help="FINAL card: normalize=BLUE/ORANGE WINS (default), "
                        "actual=use the real lobby team name")
    p.add_argument("--screens", default=e("CL_SCREENS", "score-time"),
                   help="steady display, a comma list rotated every --swap-secs: "
                        "score-time (combined, default), score, time, boost (yours), "
                        "boost-team. e.g. 'score,time' or 'score-time,boost,boost-team'")
    p.add_argument("--swap-secs", type=float, default=float(e("CL_SWAP_SECS", "10")),
                   help="seconds per screen when --screens has more than one (default 10)")
    p.add_argument("--player-name", default=e("RL_PLAYER_NAME", ""),
                   help="your in-game name, to find 'you' for boost mode")
    p.add_argument("--player-id", default=e("RL_PLAYER_PRIMARY_ID", ""),
                   help="your PrimaryId (Steam|..|0 etc.), alternative to --player-name")
    # rl source
    p.add_argument("--rl-host", default=e("RL_HOST", "127.0.0.1"))
    p.add_argument("--rl-port", type=int, default=int(e("RL_PORT", "49123")))
    # ballshark source
    p.add_argument("--ballshark-ws", default=e("BALLSHARK_WS", "ws://127.0.0.1:5050/ws"))
    # http transport
    p.add_argument("--clock-host", default=e("CLOCK_HOST", ""),
                   help="AWTRIX clock IP/host for --transport http")
    # mqtt transport
    p.add_argument("--mqtt-host", default=e("MQTT_HOST", "127.0.0.1"))
    p.add_argument("--mqtt-port", type=int, default=int(e("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-user", default=e("MQTT_USER", ""))
    p.add_argument("--mqtt-pass", default=e("MQTT_PASS", ""))
    p.add_argument("--awtrix-prefix", default=e("AWTRIX_PREFIX", "awtrix"))
    # demo
    p.add_argument("--demo-speed", type=float, default=float(e("CL_DEMO_SPEED", "1.0")))
    p.add_argument("--demo-once", action="store_true", help="play the demo once and exit")
    return p.parse_args()


def make_transport(a):
    if a.transport == "http":
        if not a.clock_host:
            sys.exit("error: --transport http needs --clock-host (your clock's IP)")
        log(f"transport: http -> {a.clock_host}")
        return HttpTransport(a.clock_host)
    log(f"transport: mqtt -> {a.mqtt_host}:{a.mqtt_port} prefix={a.awtrix_prefix}")
    return MqttTransport(a.mqtt_host, a.mqtt_port, a.mqtt_user, a.mqtt_pass, a.awtrix_prefix)


def make_source(a):
    if a.source == "demo":
        log(f"source: demo (speed={a.demo_speed}{', once' if a.demo_once else ''})")
        return demo_source(a.demo_speed, once=a.demo_once)
    if a.source == "rl":
        log(f"source: rl -> {a.rl_host}:{a.rl_port}")
        return rl_source(a.rl_host, a.rl_port, a.player_name, a.player_id)
    log(f"source: ballshark -> {a.ballshark_ws}")
    return ballshark_source(a.ballshark_ws, a.player_name, a.player_id)


def main():
    a = build_args()
    disabled = {x.strip() for x in a.disable.split(",") if x.strip()}
    bad = disabled - ALL_FEATURES
    if bad:
        sys.exit(f"error: unknown --disable feature(s): {', '.join(bad)}")
    screens = [s.strip() for s in a.screens.split(",") if s.strip()]
    bad_s = set(screens) - ALL_SCREENS
    if bad_s:
        sys.exit(f"error: unknown --screens value(s): {', '.join(bad_s)} "
                 f"(choose from {', '.join(sorted(ALL_SCREENS))})")
    tx = make_transport(a)
    board = Scoreboard(tx, disabled=disabled, screens=screens,
                       swap_secs=a.swap_secs, team_names=a.team_names)
    if disabled:
        log("disabled: " + ", ".join(sorted(disabled)))
    log("screens: " + " · ".join(screens) +
        (f" (swap {a.swap_secs:g}s)" if len(screens) > 1 else ""))

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    if a.source == "showcase":
        log("showcase — a labeled tour of every screen; record + split into GIFs")
        try:
            run_showcase(tx, a.demo_speed, loop=not a.demo_once)
        except KeyboardInterrupt:
            pass
        finally:
            board.release("shutdown")
            log("bye")
        return

    log("running — play Rocket League. Ctrl-C to stop.")
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
