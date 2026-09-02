#!/usr/bin/env python
"""Record a human changing a feed: gestures, the screen they acted on, and the
content that was visible.

Why this exists: the agent's failures on X were never about tapping accurately,
they were about not knowing the ROUTE - which control leads where, what the
screen looks like once a header has collapsed, which back press exits the app.
A human takes the right route by default. Recording one pass turns the route
into data.

Gestures alone are useless, though. A tap at (802, 1929) means nothing without
the screen it landed on. So this interleaves two streams into one ordered log:

    gesture  - tap / swipe / long_press, with the control it HIT, resolved
               against the last screen captured BEFORE the gesture
    screen   - the settled UI after an action: foreground activity, every
               labelled node, and - on X - the assembled tweets with metrics

Screens are captured on SETTLE (no touch for `--settle` seconds), not on a
timer. Mid-scroll dumps are torn and expensive; the interesting state is what
the screen became once the user stopped moving.

Runs until you press Ctrl+C. There is no time limit: you decide when the feed
has changed enough.

    python tools/trace_human.py --label "indian politics"
    python tools/trace_human.py --label "pin a topic" --settle 1.5

Scope note, from the project's own rules: this is for learning the UI route and
choosing pacing that does not hammer the app. It is NOT input-fingerprint
mimicry for defeating bot detection - out of scope here, and replayed
coordinates would break on the next layout change anyway. Use the route; treat
timings as an order of magnitude, not a signature.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobileagent import device as dev  # noqa: E402
from mobileagent import ui as uix  # noqa: E402
from mobileagent.feed import x as xf  # noqa: E402
from mobileagent.tools.apps.twitter import assemble_tweets  # noqa: E402

X_PKG = "com.twitter.android"

_ABS_X = "ABS_MT_POSITION_X"
_ABS_Y = "ABS_MT_POSITION_Y"
_BTN = "BTN_TOUCH"

# `getevent -lt` prints FIVE fields when watching every device:
#     [   12345.678] /dev/input/event2: EV_ABS ABS_MT_POSITION_X 00000abc
# but only FOUR when a device path is given - the device column is dropped:
#     [   12345.678] EV_ABS ABS_MT_POSITION_X 00000abc
# Requiring the device column made every run report zero gestures in silence.
_LINE = re.compile(
    r"^\[\s*([\d.]+)\]\s+(?:(\S+):\s+)?(\S+)\s+(\S+)\s+(\S+)\s*$")


def pick_serial(explicit: str = "") -> str:
    if explicit:
        return explicit
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                         timeout=15).stdout
    devs = [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]
    usb = [d for d in devs if ":" not in d]
    return (usb or devs or [""])[0]


def touch_device(serial: str) -> tuple[str, int, int]:
    """Find the touchscreen and its raw X/Y maxima, for scaling to pixels.

    `getevent -p` prints ABS axes as HEX CODES, not names: 0035 is
    ABS_MT_POSITION_X, 0036 is ABS_MT_POSITION_Y. An earlier version matched on
    the names, found nothing, and silently fell back to 4095 - this panel
    reports 8639 x 19199, so every coordinate would have been wrong by about a
    factor of two while looking entirely plausible. Fail loud instead.
    """
    out = subprocess.run(["adb", "-s", serial, "shell", "getevent", "-p"],
                         capture_output=True, text=True, timeout=20).stdout
    best = None
    dev_path, axes = "", {}
    for line in out.splitlines():
        m = re.match(r"add device \d+: (\S+)", line)
        if m:
            if dev_path and "0035" in axes and "0036" in axes and not best:
                best = (dev_path, axes["0035"], axes["0036"])
            dev_path, axes = m.group(1), {}
            continue
        m = re.match(r"\s*(003[56])\s*:.*max\s+(\d+)", line)
        if m:
            axes[m.group(1)] = int(m.group(2))
    if dev_path and "0035" in axes and "0036" in axes and not best:
        best = (dev_path, axes["0035"], axes["0036"])
    if not best or best[1] <= 0 or best[2] <= 0:
        raise SystemExit(json.dumps({
            "error": "no touchscreen with ABS_MT_POSITION_X/Y (0035/0036)",
            "hint": "run: adb shell getevent -p and check the panel's axes"}))
    return best


def screen_size(serial: str) -> tuple[int, int]:
    out = subprocess.run(["adb", "-s", serial, "shell", "wm", "size"],
                         capture_output=True, text=True, timeout=20).stdout
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def hit_test(nodes: list[dict], x: int, y: int) -> dict | None:
    """Smallest labelled node containing the point.

    Resolved against the screen captured BEFORE the gesture, which is the
    causally correct one: the control existed on that screen, and tapping it is
    what produced the next.
    """
    best, best_area = None, None
    for n in nodes:
        if not n["label"]:
            continue
        x1, y1, x2, y2 = n["bounds"]
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        area = (x2 - x1) * (y2 - y1)
        if best_area is None or area < best_area:
            best, best_area = n, area
    if not best:
        return None
    return {"label": best["label"][:80], "bounds": list(best["bounds"])}


def direction(dx: int, dy: int) -> str:
    if abs(dx) > abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


class Recorder:
    def __init__(self, serial: str, path: Path, settle: float,
                 sw: int, sh: int, rx: int, ry: int, dev_path: str):
        self.serial, self.settle = serial, settle
        self.sw, self.sh, self.rx, self.ry = sw, sh, rx, ry
        self.dev_path = dev_path
        self.fh = path.open("w", encoding="utf-8")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.nodes: list[dict] = []       # last captured screen
        self.last_gesture_at = 0.0
        self.dirty = True                 # a screen capture is owed
        self.n_gestures = 0
        self.n_screens = 0
        self.raw_seen = 0
        self.raw_tail: list[str] = []
        self.last_sig = None

    def write(self, rec: dict) -> None:
        with self.lock:
            self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.fh.flush()     # a Ctrl+C must never lose the session

    # ---------------- screen ----------------

    def capture_screen(self, reason: str) -> None:
        t0 = time.time()
        try:
            xml = xf._raw_xml(self.serial)
        except Exception as e:                     # device asleep, adb blip
            self.write({"kind": "screen_error", "at": round(time.time(), 3),
                        "reason": reason, "error": str(e)[:200]})
            return
        nodes = xf._nodes(xml)
        labels = [n["label"] for n in nodes if n["label"]]
        sig = hash(tuple(labels))
        with self.lock:
            self.nodes = nodes

        fg = dev.foreground(serial=self.serial)
        pkg = fg.get("package") or ""
        rec = {"kind": "screen", "at": round(time.time(), 3), "reason": reason,
               "foreground": pkg, "activity": fg.get("activity"),
               "unchanged": sig == self.last_sig,
               "capture_ms": int((time.time() - t0) * 1000),
               "labels": labels[:120]}

        if pkg == X_PKG:
            # The timeline a sample came from is the single most important
            # label on it - a feed observation with no timeline is unusable.
            try:
                tl = xf.timelines(self.serial, nodes=nodes)
                rec["timeline"] = tl.get("active")
                rec["tabs"] = [t["name"] for t in tl.get("tabs", [])]
            except Exception:
                rec["timeline"] = None
            try:
                tweets = assemble_tweets(uix.parse(xml))
                rec["tweets"] = [{
                    "handle": t.get("handle"), "text": (t.get("text") or "")[:280],
                    "age": t.get("age"), "is_ad": t.get("is_ad"),
                    "metrics": t.get("metrics"),
                } for t in tweets]
            except Exception as e:
                rec["tweets_error"] = str(e)[:150]

        self.last_sig = sig
        self.n_screens += 1
        self.write(rec)

    # ---------------- gestures ----------------

    def on_gesture(self, rec: dict) -> None:
        with self.lock:
            nodes = list(self.nodes)
        if rec["kind"] in ("tap", "long_press"):
            hit = hit_test(nodes, rec["to"][0], rec["to"][1])
            rec["hit"] = hit["label"] if hit else None
            rec["hit_bounds"] = hit["bounds"] if hit else None
        else:
            dx = rec["to"][0] - rec["from"][0]
            dy = rec["to"][1] - rec["from"][1]
            rec["direction"] = direction(dx, dy)
        self.n_gestures += 1
        self.last_gesture_at = time.time()
        self.dirty = True
        self.write(rec)

    def reader(self) -> None:
        proc = subprocess.Popen(
            ["adb", "-s", self.serial, "shell", "getevent", "-lt", self.dev_path],
            stdout=subprocess.PIPE, text=True, bufsize=1)
        cur: dict = {}
        last_up = None
        try:
            while not self.stop.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped:
                    self.raw_seen += 1
                    if len(self.raw_tail) < 8:
                        self.raw_tail.append(stripped)
                m = _LINE.match(stripped)
                if not m:
                    continue
                ts, _dev, _typ, code, val = m.groups()
                ts = float(ts)
                # Latch the START point on the first coordinate AFTER a DOWN:
                # this panel emits BTN_TOUCH DOWN before the first position, so
                # sampling at DOWN yields the previous gesture's leftovers - and
                # since state is cleared at UP, that is always None. Every swipe
                # then measured zero distance and was recorded as a tap.
                if code == _ABS_X:
                    cur["x"] = int(val, 16) * self.sw // self.rx
                    if cur.get("down") and cur.get("x0") is None:
                        cur["x0"] = cur["x"]
                elif code == _ABS_Y:
                    cur["y"] = int(val, 16) * self.sh // self.ry
                    if cur.get("down") and cur.get("y0") is None:
                        cur["y0"] = cur["y"]
                elif code == _BTN:
                    if val.upper() == "DOWN":
                        cur = {"down": ts, "x": None, "y": None,
                               "x0": None, "y0": None}
                    elif val.upper() == "UP" and cur.get("down"):
                        x0, y0 = cur.get("x0"), cur.get("y0")
                        x1, y1 = cur.get("x"), cur.get("y")
                        if None in (x0, y0, x1, y1):
                            cur = {}
                            continue
                        dur = ts - cur["down"]
                        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                        kind = ("swipe" if dist > 40
                                else "long_press" if dur > 0.5 else "tap")
                        self.on_gesture({
                            "kind": kind, "at": round(time.time(), 3),
                            "from": [x0, y0], "to": [x1, y1],
                            "duration_s": round(dur, 3),
                            "distance_px": round(dist),
                            "gap_since_prev_s": (round(cur["down"] - last_up, 3)
                                                 if last_up else None),
                        })
                        last_up = ts
                        cur = {}
        except Exception as e:
            # The reader runs in a daemon thread. If it dies quietly the
            # session keeps capturing screens and records not one gesture -
            # indistinguishable, after the fact, from a human who did nothing.
            # Leave evidence in the log.
            self.write({"kind": "reader_error", "at": round(time.time(), 3),
                        "error": "%s: %s" % (type(e).__name__, e)})
        finally:
            proc.terminate()


def main() -> int:
    p = argparse.ArgumentParser(
        description="record a human changing a feed; Ctrl+C to stop")
    p.add_argument("--serial", default="")
    p.add_argument("--label", default="", help="what the human is doing")
    p.add_argument("--out", default="artifacts/feed/traces")
    p.add_argument("--settle", type=float, default=1.2,
                   help="seconds of stillness before capturing the screen")
    p.add_argument("--max-gap", type=float, default=6.0, dest="max_gap",
                   help="capture even mid-scroll if this long since the last")
    a = p.parse_args()

    s = pick_serial(a.serial)
    if not s:
        print(json.dumps({"error": "no adb device attached"}))
        return 2
    dev_path, rx, ry = touch_device(s)
    sw, sh = screen_size(s)

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / ("trace-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))

    rec = Recorder(s, path, a.settle, sw, sh, rx, ry, dev_path)
    rec.write({"kind": "meta", "label": a.label, "serial": s,
               "screen": [sw, sh], "touch_max": [rx, ry],
               "input_device": dev_path, "settle_s": a.settle,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

    print("recording to %s" % path, file=sys.stderr)
    print("drive the phone by hand - press Ctrl+C when the feed has changed",
          file=sys.stderr)

    t = threading.Thread(target=rec.reader, daemon=True)
    t.start()
    rec.capture_screen("start")

    try:
        last_cap = time.time()
        while True:
            time.sleep(0.25)
            now = time.time()
            if not rec.last_gesture_at:
                continue
            still = now - rec.last_gesture_at
            # Two triggers, because either alone loses data. SETTLE captures the
            # state an action produced. But a long uninterrupted scroll never
            # settles - and that is exactly when content streams past - so
            # MAX-GAP forces a capture mid-scroll as well. Those dumps can be
            # torn, so they are marked `reason: interval` and analysis can
            # discount them rather than trusting them equally.
            if rec.dirty and still >= a.settle:
                rec.dirty = False
                rec.capture_screen("settled")
                last_cap = time.time()
            elif still < a.settle and (now - last_cap) >= a.max_gap:
                rec.capture_screen("interval")
                last_cap = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        rec.stop.set()
        rec.capture_screen("end")
        rec.fh.close()

    out = {"trace": str(path), "gestures": rec.n_gestures,
           "screens": rec.n_screens, "raw_lines_seen": rec.raw_seen,
           "label": a.label}
    if not rec.n_gestures:
        out["diagnosis"] = ("no events reached us - was the screen touched, "
                            "and is the phone unlocked?" if not rec.raw_seen
                            else "events arrived but did not parse - paste "
                                 "raw_sample")
        out["raw_sample"] = rec.raw_tail
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
