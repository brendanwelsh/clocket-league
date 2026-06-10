#!/usr/bin/env python3
"""Build one GIF per scoreboard screen, in the AWTRIX web-UI recorder's look.

Two paths:
  * text/blink/scroll screens are CAPTURED off the clock (GET /api/screen) so the
    real device font is used;
  * pure-draw animations (the car) are RENDERED directly from their draw ops, so
    the whole animation plays start-to-finish with nothing clipped.

Both are drawn the way the /screen recorder draws — 29px square LEDs at 33px
pitch inside the TC001 bezel — then quantized and scaled. Output: docs/gifs/.

Usage:  python tools/capture_gifs.py 192.168.11.35
"""
import importlib.util
import json
import math
import os
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("cl", os.path.join(HERE, "clocket_league.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

CLOCK = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.35"
OUT = os.path.join(HERE, "docs", "gifs")
os.makedirs(OUT, exist_ok=True)

FPS = 12
PITCH, LED, PAD, FINAL = 33, 29, 60, 0.45
W, H = 31 * PITCH + LED, 7 * PITCH + LED        # 1052 x 260, like the web UI
_BEZEL = Image.open(os.path.join(HERE, "tools", "bezel.png")).convert("RGBA")

tx = cl.HttpTransport(CLOCK)
sb = cl.Scoreboard(tx)


# --- shared rendering (web-UI look) -----------------------------------------
def render(buf):
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(canvas)
    for i, c in enumerate(buf):
        col = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
        if any(col):
            x0, y0 = (i % 32) * PITCH, (i // 32) * PITCH
            d.rectangle([x0, y0, x0 + LED - 1, y0 + LED - 1], fill=col)
    framed = Image.new("RGBA", (W + 2 * PAD, H + 2 * PAD), (0, 0, 0, 255))
    framed.paste(canvas, (PAD, PAD))
    framed.alpha_composite(_BEZEL.resize(framed.size))
    out = framed.convert("RGB")
    return out.resize((round(out.width * FINAL), round(out.height * FINAL)))


def save_gif(name, frames, fps=FPS):
    fw, fh = frames[0].size
    combo = Image.new("RGB", (fw, fh * len(frames)))          # palette from all frames
    for i, f in enumerate(frames):
        combo.paste(f, (0, i * fh))
    pal = combo.quantize(colors=128, method=Image.FASTOCTREE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    path = os.path.join(OUT, name + ".gif")
    q[0].save(path, save_all=True, append_images=q[1:],
              duration=int(1000 / fps), loop=0, optimize=True)
    print(f"  {name}.gif  ({len(frames)} frames, {os.path.getsize(path) // 1024}KB)")


# --- path A: capture text screens off the clock -----------------------------
def grab():
    return json.load(urllib.request.urlopen(f"http://{CLOCK}/api/screen", timeout=4))


def capture(name, animate, dur):
    stop = threading.Event()
    th = threading.Thread(target=animate, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.2)
    frames, end = [], time.time() + dur
    while time.time() < end:
        t0 = time.time()
        try:
            frames.append(render(grab()))
        except Exception:
            pass
        dt = 1.0 / FPS - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)
    stop.set()
    th.join(timeout=1.5)
    tx.dismiss()
    time.sleep(0.4)
    save_gif(name, frames)


# --- path B: render pure-draw animations directly (nothing clipped) ---------
def _hexint(c):
    return c if isinstance(c, int) else int(str(c).lstrip("#"), 16)


def rasterize(ops):
    """AWTRIX draw ops -> 256-int frame buffer (df / dfc / dp, like the device)."""
    buf = [0] * 256

    def px(x, y, c):
        if 0 <= x < 32 and 0 <= y < 8:
            buf[y * 32 + x] = c
    for op in ops:
        if "df" in op:
            x, y, w, h, col = op["df"]
            c = _hexint(col)
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    px(xx, yy, c)
        elif "dfc" in op:
            cx, cy, r, col = op["dfc"]
            c = _hexint(col)
            for yy in range(cy - r, cy + r + 1):
                for xx in range(cx - r, cx + r + 1):
                    if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                        px(xx, yy, c)
        elif "dp" in op:
            x, y, col = op["dp"]
            px(x, y, _hexint(col))
    return buf


def car_gif(name, color):
    """One full car pass (your/winner color) pushing the ball — rendered directly."""
    frames = []
    for step in range(-13, 45, 1):                 # 1px steps -> smooth, complete
        ops = cl._car(step, color) + cl._soccer(step + 11, 4, 2, spin=step * 0.6)
        frames.append(render(rasterize(ops)))
    save_gif(name, frames, fps=20)


# --- animators for the captured screens -------------------------------------
def seq(steps):
    def fn(stop):
        while not stop.is_set():
            for card, secs in steps:
                tx.notify(card)
                t = time.time() + secs
                while time.time() < t and not stop.is_set():
                    time.sleep(0.05)
    return fn


def hold_anim(make):
    def fn(stop):
        while not stop.is_set():
            tx.notify(make())
            time.sleep(0.4)
    return fn


def clock_at(values, dwell):
    def fn(stop):
        sb.ot = False
        while not stop.is_set():
            for s in values:
                sb.secs = s
                tx.notify(sb._time_panel())
                t = time.time() + dwell
                while time.time() < t and not stop.is_set():
                    time.sleep(0.05)
    return fn


def score_clock_anim(stop):
    sb.t0, sb.t1, sb.ot = 2, 1, False
    s = 228
    while not stop.is_set():
        sb.secs = s
        tx.notify(sb._live_card())
        s = s - 2 if s > 180 else 228
        time.sleep(0.45)


def goal_anim(team):
    def fn(stop):
        sb.t0, sb.t1, sb.flash_team = (2, 1, 0) if team == 0 else (2, 2, 1)
        seq([(sb._goal_banner(), 1.2), (sb._score_only(), 2.0)])(stop)
    return fn


def overtime_anim(stop):
    sb.t0, sb.t1 = 2, 2
    while not stop.is_set():
        sb.ot = False
        tx.notify(sb._held("OVERTIME", center=False, color=cl.GOLD, blink=400))
        t = time.time() + 1.6
        while time.time() < t and not stop.is_set():
            time.sleep(0.05)
        sb.ot = True
        for s in (2, 7, 12, 17, 22):                 # +0:0x counting up
            sb.secs = s
            tx.notify(sb._time_panel())
            t = time.time() + 0.7
            while time.time() < t and not stop.is_set():
                time.sleep(0.05)


def boost_self_anim(stop):
    b = 60
    while not stop.is_set():
        b = cl.step_boost(b)
        sb.players = [{"team": 0, "boost": b, "you": True}]
        tx.notify(sb._boost_self())
        time.sleep(0.28)


def boost_team_anim(stop):
    bs = [40, 80, 55]
    while not stop.is_set():
        bs = [cl.step_boost(x) for x in bs]
        sb.players = [{"team": 0, "boost": 0, "you": True}] + \
            [{"team": 0, "boost": bs[i], "you": False} for i in range(3)]
        tx.notify(sb._boost_team())
        time.sleep(0.28)


def score_panel_anim(stop):
    sb.t0, sb.t1 = 2, 1
    hold_anim(sb._score_panel)(stop)


def final_anim(stop):
    sb.t0, sb.t1 = 3, 2
    wc = cl.BLUE if sb.t0 > sb.t1 else cl.ORANGE
    seq([(sb._held("BLUE" if wc == cl.BLUE else "ORANGE", color=wc), 1.2),
         (sb._held("WINS!", color=wc, blink=350), 1.4),
         (sb._final_score(), 1.6)])(stop)


# name, kind, arg, duration
CAPTURED = [
    ("02-countdown",   seq([(sb._held("3", color=cl.WHITE), 0.8),
                            (sb._held("2", color=cl.WHITE), 0.8),
                            (sb._held("1", color=cl.WHITE), 0.8),
                            (sb._held("GO!", color=cl.GREEN, blink=350), 1.2)]), 4.2),
    ("03-score-clock", score_clock_anim, 4.5),
    ("04-score",       score_panel_anim, 2.8),
    ("05-clock",       clock_at([188, 186, 184, 182], 0.9), 3.6),
    ("06-clock-amber", clock_at([48, 46, 44, 42], 0.9), 3.6),
    ("07-clock-red",   clock_at([9, 8, 7, 6, 5, 4, 3, 2, 1, 0], 0.45), 4.6),
    ("08-overtime",    overtime_anim, 5.6),
    ("09-goal-blue",   goal_anim(0), 4.4),
    ("10-goal-orange", goal_anim(1), 4.4),
    ("11-post",        hold_anim(lambda: sb._held("POST", color=cl.GOLD, blink=300)), 3.6),
    ("12-boost-you",   boost_self_anim, 5.2),
    ("13-boost-team",  boost_team_anim, 5.2),
    ("14-final",       final_anim, 4.6),
]


def main():
    print(f"capturing screens from {CLOCK} -> docs/gifs/")
    car_gif("01-kickoff-car", cl.BLUE)            # your team drives the ball in
    for name, animate, dur in CAPTURED:
        capture(name, animate, dur)
    car_gif("15-winner-car", cl.ORANGE)           # winner's car drives it away
    print("done")


if __name__ == "__main__":
    main()
