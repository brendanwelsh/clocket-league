#!/usr/bin/env python3
"""Capture the AWTRIX matrix straight off the clock and build one GIF per screen.

Drives each scoreboard screen on a real clock (over HTTP), grabs the rendered
32x8 buffer from GET /api/screen at a fixed rate, and writes equal-length,
LED-styled GIFs into docs/gifs/. Used to make the README gallery — no hand
recording needed.

Usage:  python tools/capture_gifs.py 192.168.11.35
"""
import importlib.util
import json
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

FPS = 10
DUR = 3.0                 # every GIF is exactly this long
SCALE = 11                # px per LED
GAP = 2                   # dark gap between LEDs
DOT = SCALE - GAP

tx = cl.HttpTransport(CLOCK)
sb = cl.Scoreboard(tx)


def grab():
    buf = json.load(urllib.request.urlopen(f"http://{CLOCK}/api/screen", timeout=4))
    return buf  # 256 ints, row-major 32x8 (0xRRGGBB)


def render(buf):
    """LED-panel look: each pixel a rounded dot on near-black."""
    W, H = 32 * SCALE, 8 * SCALE
    img = Image.new("RGB", (W, H), (6, 6, 8))
    d = ImageDraw.Draw(img)
    for i, c in enumerate(buf):
        x, y = i % 32, i // 32
        r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
        x0, y0 = x * SCALE + GAP // 2, y * SCALE + GAP // 2
        if r or g or b:
            d.ellipse([x0, y0, x0 + DOT, y0 + DOT], fill=(r, g, b))
        else:
            d.ellipse([x0, y0, x0 + DOT, y0 + DOT], fill=(16, 16, 20))
    return img


def capture(name, animate):
    stop = threading.Event()
    th = threading.Thread(target=animate, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.25)                      # let the first publish land
    frames, end = [], time.time() + DUR
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
    path = os.path.join(OUT, name + ".gif")
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"  {name}.gif  ({len(frames)} frames)")


# --- animators: each keeps publishing its screen until `stop` is set ---------
def hold(card):
    def fn(stop):
        while not stop.is_set():
            tx.notify(card)
            time.sleep(0.5)
    return fn


def seq(steps):
    """steps = [(card, secs), ...]; loops the sequence while capturing."""
    def fn(stop):
        while not stop.is_set():
            for card, secs in steps:
                tx.notify(card)
                t = time.time() + secs
                while time.time() < t and not stop.is_set():
                    time.sleep(0.05)
    return fn


def car(color):
    def fn(stop):
        while not stop.is_set():
            cl.play_car(tx, color, time.sleep, 0.045)
    return fn


def boost_self_anim(stop):
    b = 60
    while not stop.is_set():
        b = cl.step_boost(b)
        sb.players = [{"team": 0, "boost": b, "you": True}]
        tx.notify(sb._boost_self())
        time.sleep(0.25)


def boost_team_anim(stop):
    bs = [40, 80, 55]
    while not stop.is_set():
        bs = [cl.step_boost(x) for x in bs]
        sb.players = [{"team": 0, "boost": 0, "you": True}] + \
            [{"team": 0, "boost": bs[i], "you": False} for i in range(3)]
        tx.notify(sb._boost_team())
        time.sleep(0.25)


def clock_anim(stop):
    while not stop.is_set():
        for s in (92, 44, 8):                 # gray -> amber -> red+blink
            sb.ot = False
            sb.secs = s
            tx.notify(sb._time_panel())
            t = time.time() + 1.0
            while time.time() < t and not stop.is_set():
                time.sleep(0.05)


def score_clock_anim(stop):
    sb.t0, sb.t1, sb.ot = 2, 1, False
    s = 224
    while not stop.is_set():
        sb.secs = s
        tx.notify(sb._live_card())
        s = s - 2 if s > 180 else 224
        time.sleep(0.5)


def goal(team):
    def fn(stop):
        sb.t0, sb.t1, sb.flash_team = (2, 1, 0) if team == 0 else (2, 2, 1)
        seq([(sb._goal_banner(), 1.2), (sb._score_only(), 1.8)])(stop)
    return fn


def ot_anim(stop):
    while not stop.is_set():
        tx.notify(sb._held("OVERTIME", center=False, color=cl.GOLD, blink=400))
        t = time.time() + 1.3
        while time.time() < t and not stop.is_set():
            time.sleep(0.05)
        sb.t0, sb.t1, sb.ot = 2, 2, True
        for s in (3, 8, 13):
            sb.secs = s
            tx.notify(sb._time_panel())
            t = time.time() + 0.55
            while time.time() < t and not stop.is_set():
                time.sleep(0.05)
    sb.ot = False


def score_anim(stop):
    sb.t0, sb.t1 = 2, 1
    while not stop.is_set():
        tx.notify(sb._score_panel())
        time.sleep(0.5)


def final_anim(stop):
    sb.t0, sb.t1 = 3, 2
    wc = cl.BLUE if sb.t0 > sb.t1 else cl.ORANGE
    seq([(sb._held("BLUE" if wc == cl.BLUE else "ORANGE", color=wc), 1.1),
         (sb._held("WINS!", color=wc, blink=350), 1.1),
         (sb._final_score(), 1.1)])(stop)


SCENES = [
    ("01-kickoff-car",  car(cl.BLUE)),
    ("02-countdown",    seq([(sb._held("3", color=cl.WHITE), 0.7),
                             (sb._held("2", color=cl.WHITE), 0.7),
                             (sb._held("1", color=cl.WHITE), 0.7),
                             (sb._held("GO!", color=cl.GREEN, blink=350), 0.9)])),
    ("03-score-clock",  score_clock_anim),
    ("04-score",        score_anim),
    ("05-clock",        clock_anim),
    ("06-goal-blue",    goal(0)),
    ("07-goal-orange",  goal(1)),
    ("08-post",         hold(sb._held("POST", color=cl.GOLD, blink=300))),
    ("09-overtime",     ot_anim),
    ("10-boost-you",    boost_self_anim),
    ("11-boost-team",   boost_team_anim),
    ("12-final",        final_anim),
    ("13-winner-car",   car(cl.BLUE)),
]


def main():
    print(f"capturing {len(SCENES)} screens from {CLOCK} -> docs/gifs/  ({DUR}s each)")
    for name, animate in SCENES:
        if animate is None:
            continue
        capture(name, animate)
    print("done")


if __name__ == "__main__":
    main()
