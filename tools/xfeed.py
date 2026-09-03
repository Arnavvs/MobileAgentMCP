#!/usr/bin/env python
"""CLI front-end for `mobileagent.feed.x` - drive X's feed controls from a shell.

Exists because an agent session is not always connected to the MCP server, but
it can always run a command. Same functions, no duplicated logic.

Read-only by default. Every state-changing subcommand prints the exact tap it
would perform and does nothing until `--apply` is passed.

    python tools/xfeed.py timelines
    python tools/xfeed.py switch "For you"
    python tools/xfeed.py menu --nth 0
    python tools/xfeed.py snapshot --max 40
    python tools/xfeed.py not-interested --nth 0            # plan only
    python tools/xfeed.py not-interested --nth 0 --apply    # fires

Device selection: --serial, else MOBILEAGENT_SERIAL, else the first attached
device, preferring USB over wireless (measured 12.5x faster - see research.md).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobileagent.feed import x as xf  # noqa: E402


def pick_serial(explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return ""
    devs = [ln.split("\t")[0] for ln in out.splitlines()[1:]
            if "\tdevice" in ln]
    if not devs:
        return ""
    usb = [d for d in devs if ":" not in d]
    return (usb or devs)[0]


def main() -> int:
    p = argparse.ArgumentParser(description="X feed control")
    p.add_argument("--serial", default="")
    p.add_argument("--apply", action="store_true",
                   help="actually perform the change (default: plan only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("timelines", help="tab strip + which tab is live")

    sp = sub.add_parser("switch", help="switch to a named timeline tab")
    sp.add_argument("name")

    sp = sub.add_parser("menu", help="open a post's overflow and read it")
    sp.add_argument("--nth", type=int, default=0)

    sp = sub.add_parser("not-interested", help="negative signal (For you + search results)")
    sp.add_argument("--nth", type=int, default=0)

    sp = sub.add_parser("lists", help="open Add/remove from Lists for a post")
    sp.add_argument("--nth", type=int, default=0)
    sp.add_argument("--list", default="", dest="list_name")

    sub.add_parser("tabs-screen", help="open the Add tab -> Timelines screen")

    sp = sub.add_parser("topic-search", help="search the Topics/Lists catalogue")
    sp.add_argument("query", metavar="QUERY")

    sp = sub.add_parser("pin", help="pin a Topic/List as a Home tab")
    sp.add_argument("name")
    sp.add_argument("--unpin", action="store_true")

    sub.add_parser("surface", help="which ranked surface is on screen")

    sp = sub.add_parser("read", help="read posts on screen NOW (fast, u2)")
    sp.add_argument("--scrolls", type=int, default=0,
                    help="scroll+read this many times instead of one read")

    sp = sub.add_parser("search", help="Explore search; the route that works")
    sp.add_argument("query")
    sp.add_argument("--tab", default="", help="top|latest|people|media|lists")

    sp = sub.add_parser("result-tab", help="switch search-result tab")
    sp.add_argument("name")

    sp = sub.add_parser("like", help="like a post (ENGAGEMENT WRITE)")
    sp.add_argument("--nth", type=int, default=0)

    sp = sub.add_parser("consume", help="scroll+dwell; the measured lever")
    sp.add_argument("--seconds", type=float, default=120.0, dest="duration_s")
    sp.add_argument("--dwell-min", type=float, default=1.5, dest="dwell_min")
    sp.add_argument("--dwell-max", type=float, default=6.0, dest="dwell_max")

    sp = sub.add_parser("snapshot", help="sample the live timeline to JSON")
    sp.add_argument("--max", type=int, default=40, dest="max_tweets")
    sp.add_argument("--swipes", type=int, default=20, dest="max_swipes")
    sp.add_argument("--out", default="", dest="out_dir")

    a = p.parse_args()
    s = pick_serial(a.serial)
    if not s:
        print(json.dumps({"error": "no adb device attached"}))
        return 2

    if a.cmd == "timelines":
        r = xf.timelines(serial=s)
    elif a.cmd == "switch":
        r = xf.switch_timeline(a.name, serial=s)
    elif a.cmd == "menu":
        r = xf.post_options(a.nth, serial=s)
        xf.close_sheet(s)
    elif a.cmd == "not-interested":
        r = xf.not_interested(a.nth, apply=a.apply, serial=s)
    elif a.cmd == "lists":
        r = xf.add_to_list(a.nth, a.list_name, apply=a.apply, serial=s)
    elif a.cmd == "tabs-screen":
        r = xf.open_timelines_screen(serial=s)
    elif a.cmd == "topic-search":
        r = xf.search_timelines(a.query, serial=s)
    elif a.cmd == "pin":
        r = xf.pin(a.name, unpin=a.unpin, apply=a.apply, serial=s)
    elif a.cmd == "read":
        from mobileagent.feed import read as rd
        from mobileagent import device as _dev
        import time as _t
        rdr = rd.Reader(s)
        seen = {}
        for i in range(max(1, a.scrolls)):
            for p in rdr.posts():
                seen.setdefault((p["handle"], (p["text"] or "")[:40]), p)
            if a.scrolls:
                _dev.shell("input swipe 540 1700 540 900 260", serial=s)
                _t.sleep(0.5)
        posts = list(seen.values())
        r = {"posts": posts, "summary": rd.summarise(posts),
             "reader": rdr.stats()}
    elif a.cmd == "surface":
        r = xf.surface(serial=s)
    elif a.cmd == "search":
        r = xf.search(a.query, tab=a.tab, serial=s)
    elif a.cmd == "result-tab":
        r = xf.switch_result_tab(a.name, serial=s)
    elif a.cmd == "like":
        r = xf.like(a.nth, apply=a.apply, serial=s)
    elif a.cmd == "consume":
        r = xf.consume(a.duration_s, a.dwell_min, a.dwell_max,
                       apply=a.apply, serial=s)
        r.pop("tweets", None)
    elif a.cmd == "snapshot":
        r = xf.snapshot(a.max_tweets, a.max_swipes, out_dir=a.out_dir,
                        serial=s)
        r.pop("tweets", None)  # the file has them; keep stdout small
    else:
        return 2

    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    raise SystemExit(main())
