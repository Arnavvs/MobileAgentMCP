#!/usr/bin/env python
"""Record a human doing a task on the phone, as gestures plus screen context.

Why this exists: the agent's failures on X were never about tapping accurately,
they were about not knowing the ROUTE - which control leads where, what the
screen looks like when a header has collapsed, which back-key press exits the
app. A human doing the same task takes the right route by default. Recording one
pass gives us the route as data instead of as guesswork.

What it captures, per gesture: down/up timestamps, start and end coordinates,
duration, distance, classification (tap | swipe | long-press), the gap since the
previous gesture, and the foreground activity at the moment the gesture started.

    python tools/trace_human.py --seconds 180 --label "pin a topic"
    python tools/trace_human.py --seconds 300 --out artifacts/feed/traces

Scope note, from the project's own rules: this is for learning the UI route and
choosing pacing that does not hammer the app. It is NOT input-fingerprint
mimicry for defeating bot detection - that is out of scope here, and replaying
captured coordinates verbatim would in any case break on the next layout change.
Use the route; treat the timings as an order of magnitude, not a signature.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

_ABS_X = "ABS_MT_POSITION_X"
_ABS_Y = "ABS_MT_POSITION_Y"
_BTN = "BTN_TOUCH"
_LINE = re.compile(r"^\[\s*([\d.]+)\]\s+(\S+):\s+(\S+)\s+(\S+)\s+(\S+)")


def pick_serial(explicit: str = "") -> str:
    if explicit:
        return explicit
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                         timeout=15).stdout
    devs = [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]
    usb = [d for d in devs if ":" not in d]
    return (usb or devs or [""])[0]


def touch_ranges(serial: str) -> tuple[int, int]:
    """Max raw X/Y the touchscreen reports, for scaling to screen pixels."""
    out = subprocess.run(["adb", "-s", serial, "shell", "getevent", "-p"],
                         capture_output=True, text=True, timeout=20).stdout
    mx = my = 0
    for name, attr in ((_ABS_X, "mx"), (_ABS_Y, "my")):
        m = re.search(name + r".*?max\s+(\d+)", out, re.S)
        if m:
            if attr == "mx":
                mx = int(m.group(1))
            else:
                my = int(m.group(1))
    return mx or 4095, my or 4095


def screen_size(serial: str) -> tuple[int, int]:
    out = subprocess.run(["adb", "-s", serial, "shell", "wm", "size"],
                         capture_output=True, text=True, timeout=20).stdout
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def foreground(serial: str) -> str:
    out = subprocess.run(
        ["adb", "-s", serial, "shell", "dumpsys", "window"],
        capture_output=True, text=True, timeout=20).stdout
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ (\S+)\}", out)
    return m.group(1) if m else ""


def main() -> int:
    p = argparse.ArgumentParser(description="record human gestures on device")
    p.add_argument("--serial", default="")
    p.add_argument("--seconds", type=int, default=180)
    p.add_argument("--label", default="", help="what the human is doing")
    p.add_argument("--out", default="artifacts/feed/traces")
    a = p.parse_args()

    s = pick_serial(a.serial)
    if not s:
        print(json.dumps({"error": "no adb device attached"}))
        return 2
    rx, ry = touch_ranges(s)
    sw, sh = screen_size(s)

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / ("trace-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    fh = path.open("w", encoding="utf-8")
    fh.write(json.dumps({"kind": "meta", "label": a.label, "serial": s,
                         "screen": [sw, sh], "touch_max": [rx, ry],
                         "started": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")

    print("recording %ss to %s - drive the phone by hand now" % (a.seconds, path),
          file=sys.stderr)
    proc = subprocess.Popen(["adb", "-s", s, "shell", "getevent", "-lt"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)

    deadline = time.time() + a.seconds
    cur: dict = {}
    last_up = None
    n = 0
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            m = _LINE.match(line.strip())
            if not m:
                continue
            ts, _dev, _typ, code, val = m.groups()
            ts = float(ts)
            if code == _ABS_X:
                cur["x"] = int(val, 16) * sw // rx
            elif code == _ABS_Y:
                cur["y"] = int(val, 16) * sh // ry
            elif code == _BTN:
                if val.upper() == "DOWN":
                    cur = {"down": ts, "fg": foreground(s),
                           "x": cur.get("x"), "y": cur.get("y")}
                    cur["x0"], cur["y0"] = cur.get("x"), cur.get("y")
                elif val.upper() == "UP" and cur.get("down"):
                    dur = ts - cur["down"]
                    x0, y0 = cur.get("x0"), cur.get("y0")
                    x1, y1 = cur.get("x"), cur.get("y")
                    dist = (((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                            if None not in (x0, y0, x1, y1) else 0)
                    kind = ("swipe" if dist > 40
                            else "long_press" if dur > 0.5 else "tap")
                    rec = {"kind": kind, "from": [x0, y0], "to": [x1, y1],
                           "duration_s": round(dur, 3),
                           "distance_px": round(dist),
                           "gap_since_prev_s": (round(cur["down"] - last_up, 3)
                                                if last_up else None),
                           "foreground": cur.get("fg"),
                           "at": round(cur["down"], 3)}
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    n += 1
                    last_up = ts
                    cur = {}
    finally:
        proc.terminate()
        fh.close()

    print(json.dumps({"trace": str(path), "gestures": n, "label": a.label},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
