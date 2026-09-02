"""X / Twitter feed control surface.

Verified against com.twitter.android 12.21.1-prod.05 on RMX3395, 2026-09-02.
See ../../../feed-former.md for the control inventory this implements.

Two facts drive the whole design:

1. **X's post-options menu is surface-dependent.** `Not interested in Post` is
   present on the For-you tab and ABSENT on a List/Topic tab, because Lists and
   Topics are reverse-chronological pipelines with no ranker to signal. A blind
   index tap on the wrong tab lands on `Follow @handle` - the opposite of the
   intended signal. Every mutation here therefore matches on the item's LABEL
   and refuses when the label is missing. It never taps by position.

2. **The active-tab flag is invisible to `ui.parse`.** The node carrying
   `selected="true"` is an anonymous `android.view.View`: no text, no
   resource-id, not clickable, so `ui.parse`'s `meaningful` test drops it. This
   module reads the raw XML for that one signal. Without it there is no way to
   know which timeline is live, and without that, rule 1 cannot be enforced.

Every state-changing call is two-phase: it returns a `plan` describing the exact
tap it would make, and only performs it when `apply=True`. Applied changes are
appended to `artifacts/feed/journal.jsonl` so a session's edits to a real
account are auditable and reversible after the fact.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .. import device as dev
from .. import ui as uix

X_PKG = "com.twitter.android"

# The Home tab strip. Cells span this band; the sheet and the feed never do.
_TAB_Y_MIN, _TAB_Y_MAX = 280, 460
_TAB_SKIP = {"add tab", "scroll to top", "show navigation drawer",
             "find people to follow"}

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

JOURNAL = Path(__file__).resolve().parents[3] / "artifacts" / "feed" / "journal.jsonl"


# --------------------------------------------------------------------------
# raw helpers
# --------------------------------------------------------------------------

def _raw_xml(serial: str = "") -> str:
    dev.shell("uiautomator dump /sdcard/xfeed.xml", serial=serial)
    return dev.adb("exec-out", "cat", "/sdcard/xfeed.xml", serial=serial)


def _nodes(xml: str) -> list[dict]:
    """Every node, unfiltered - including the anonymous ones ui.parse drops."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for n in root.iter("node"):
        a = n.attrib
        m = _BOUNDS.search(a.get("bounds", ""))
        if not m:
            continue
        b = tuple(int(g) for g in m.groups())
        label = (a.get("text") or "").strip() or (a.get("content-desc") or "").strip()
        out.append({
            "label": label,
            "bounds": b,
            "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2),
            "clickable": a.get("clickable") == "true",
            "selected": a.get("selected") == "true",
            "cls": (a.get("class") or "").rsplit(".", 1)[-1],
        })
    return out


def _tap(x: int, y: int, serial: str = "", settle: float = 2.0) -> None:
    dev.shell("input tap %d %d" % (x, y), serial=serial)
    time.sleep(settle)


def _journal(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# timelines
# --------------------------------------------------------------------------

def timelines(serial: str = "") -> dict:
    """Tab strip contents plus which tab is live.

    The active tab is resolved from the anonymous `selected` cell, matched to a
    label by x-overlap. Returns `active: None` rather than guessing when no cell
    reports selected - mid-animation dumps do that, and a wrong answer here
    silently mis-targets every mutation downstream.
    """
    nodes = _nodes(_raw_xml(serial))
    band = [n for n in nodes
            if _TAB_Y_MIN <= n["bounds"][1] <= 300 and n["bounds"][3] >= 420]
    labelled, selected_cells = [], []
    for n in band:
        lab = n["label"]
        if lab and lab.lower() not in _TAB_SKIP and len(lab) <= 28:
            labelled.append(n)
        if n["selected"]:
            selected_cells.append(n)

    active: Optional[str] = None
    for cell in selected_cells:
        cx1, _, cx2, _ = cell["bounds"]
        for lab in labelled:
            lx1, _, lx2, _ = lab["bounds"]
            overlap = min(cx2, lx2) - max(cx1, lx1)
            if overlap > 0.6 * (lx2 - lx1):
                active = lab["label"]
                break
        if active:
            break

    seen, tabs = set(), []
    for n in sorted(labelled, key=lambda n: n["bounds"][0]):
        if n["label"].lower() in seen:
            continue
        seen.add(n["label"].lower())
        tabs.append({"name": n["label"], "center": n["center"],
                     "active": n["label"] == active})
    return {"active": active, "tabs": tabs, "on_home": bool(tabs),
            "note": None if active else
            "no cell reported selected - dump may be mid-animation; re-dump"}


def ensure_home(serial: str = "", settle: float = 2.5) -> dict:
    """Get to the top of Home, where the tab strip exists.

    `timelines()` reporting `on_home: False` is ambiguous on its own - it means
    the strip is not in the tree, which happens both when the feed is scrolled
    (X collapses the header) and when X is not the foreground app at all. A
    back-key loop that does not tell those apart walks straight out of the app,
    so resolve the foreground first and only then deal with scroll position.
    """
    for attempt in range(4):
        fg = dev.foreground(serial=serial).get("package") or ""
        if fg != X_PKG:
            dev.shell("monkey -p %s -c android.intent.category.LAUNCHER 1"
                      % X_PKG, serial=serial)
            time.sleep(settle + 5.0)   # cold start on this device is slow
            continue

        t = timelines(serial)
        if t["on_home"]:
            return {"on_home": True, "active": t["active"]}

        nodes = _nodes(_raw_xml(serial))
        btn = next((n for n in nodes if n["clickable"]
                    and n["label"].lower() in ("scroll to top", "home")), None)
        if btn:
            _tap(*btn["center"], serial=serial, settle=settle)
        else:
            # Scrolled deep enough that X has hidden BOTH the tab strip and the
            # bottom nav, so there is no control to press - scroll back up
            # instead. Deliberately NOT a back-key press: an earlier version
            # pressed BACK here, which on such a screen walks out of X to the
            # launcher, and the next iteration then types into whatever app is
            # in front. A recovery routine must never be able to leave the app.
            dev.shell("input swipe 540 700 540 2000 220", serial=serial)
            time.sleep(1.2)
    t = timelines(serial)
    return {"on_home": t["on_home"], "active": t["active"],
            "error": None if t["on_home"] else "could not reach Home"}


def switch_timeline(name: str, serial: str = "", settle: float = 2.5) -> dict:
    want = name.strip().lower()
    t = timelines(serial)
    for tab in t["tabs"]:
        if tab["name"].lower() == want:
            if tab["active"]:
                return {"switched": False, "active": tab["name"],
                        "note": "already active"}
            _tap(*tab["center"], serial=serial, settle=settle)
            after = timelines(serial)
            return {"switched": True, "active": after["active"],
                    "requested": tab["name"]}
    return {"error": "timeline %r not found" % name,
            "available": [x["name"] for x in t["tabs"]]}


# --------------------------------------------------------------------------
# post options
# --------------------------------------------------------------------------

def post_options(nth: int = 0, serial: str = "") -> dict:
    """Open the nth visible post's overflow and read the menu back.

    Returns the menu as labels, never as indices - callers must match on text.
    """
    nodes = _nodes(_raw_xml(serial))
    btns = [n for n in nodes
            if n["clickable"] and n["label"].lower() == "post options"]
    btns.sort(key=lambda n: n["bounds"][1])
    if nth >= len(btns):
        return {"error": "only %d post-options buttons visible" % len(btns),
                "wanted": nth}
    _tap(*btns[nth]["center"], serial=serial, settle=2.5)

    menu = _nodes(_raw_xml(serial))
    items = [{"label": n["label"], "center": n["center"]}
             for n in menu
             if n["label"] and n["label"].lower() != "close sheet"
             and n["bounds"][1] > 1000]
    return {"opened_nth": nth, "items": items,
            "labels": [i["label"] for i in items]}


def close_sheet(serial: str = "") -> None:
    dev.shell("input keyevent KEYCODE_BACK", serial=serial)
    time.sleep(1.2)


def not_interested(nth: int = 0, apply: bool = False, serial: str = "") -> dict:
    """Mark the nth visible post 'Not interested in Post'.

    Refuses unless the For-you tab is live AND the menu actually offers the
    item. Both checks matter: on a List/Topic tab the item is absent and the row
    at that position is `Follow @handle`.

    Per xai-org/x-algorithm, `not_interested` is one of five named negative
    labels the Phoenix ranker predicts, weighted well above any positive
    engagement - so this is a real ranking input, not a UI courtesy.
    """
    t = timelines(serial)
    if t["active"] is None:
        return {"error": "cannot determine the active timeline", "detail": t}
    if t["active"].lower() != "for you":
        return {"error": "refusing: 'Not interested' exists only on For you",
                "active": t["active"],
                "hint": "switch_timeline('For you') first"}

    menu = post_options(nth, serial=serial)
    if "error" in menu:
        return menu
    hit = next((i for i in menu["items"]
                if i["label"].lower().startswith("not interested")), None)
    if not hit:
        close_sheet(serial)
        return {"error": "'Not interested in Post' not in this menu",
                "labels": menu["labels"],
                "note": "surface-dependent - see feed-former.md 1.3"}

    plan = {"action": "not_interested", "nth": nth, "timeline": t["active"],
            "label": hit["label"], "tap": hit["center"]}
    if not apply:
        close_sheet(serial)
        return {"applied": False, "plan": plan,
                "note": "re-run with apply=True to fire"}
    _tap(*hit["center"], serial=serial, settle=2.0)
    _journal(plan)
    return {"applied": True, "plan": plan}


def add_to_list(nth: int = 0, list_name: str = "", apply: bool = False,
                serial: str = "") -> dict:
    """Open `Add/remove from Lists` for the nth post; optionally toggle a List.

    With no `list_name` this is a read: it reports which Lists exist and whether
    the empty state is showing. That is the honest first call, because on this
    account no Lists exist yet.
    """
    menu = post_options(nth, serial=serial)
    if "error" in menu:
        return menu
    hit = next((i for i in menu["items"]
                if i["label"].lower().startswith("add/remove from lists")), None)
    if not hit:
        close_sheet(serial)
        return {"error": "'Add/remove from Lists' not in this menu",
                "labels": menu["labels"]}
    _tap(*hit["center"], serial=serial, settle=3.0)

    rows = _nodes(_raw_xml(serial))
    labels = [n["label"] for n in rows if n["label"]]
    empty = any("haven" in l.lower() and "lists" in l.lower() for l in labels)
    if empty or not list_name:
        return {"applied": False, "empty_state": empty, "labels": labels,
                "note": "no list_name given" if not list_name
                else "account has no Lists yet - create one first"}

    target = next((n for n in rows
                   if n["label"].strip().lower() == list_name.strip().lower()),
                  None)
    if not target:
        return {"applied": False, "error": "List %r not on screen" % list_name,
                "labels": labels}
    plan = {"action": "add_to_list", "nth": nth, "list": list_name,
            "tap": target["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*target["center"], serial=serial, settle=1.5)
    _journal(plan)
    return {"applied": True, "plan": plan}


# --------------------------------------------------------------------------
# the Timelines (Add tab) screen
# --------------------------------------------------------------------------

def open_timelines_screen(serial: str = "") -> dict:
    nodes = _nodes(_raw_xml(serial))
    btn = next((n for n in nodes
                if n["clickable"] and n["label"].lower() == "add tab"), None)
    if not btn:
        return {"error": "'Add tab' not visible - are you on Home?"}
    _tap(*btn["center"], serial=serial, settle=3.0)
    return {"opened": True}


def search_timelines(query: str, serial: str = "") -> dict:
    """Search the Topics/Lists catalogue on the Timelines screen.

    The catalogue is US-named: 'football' returns nothing, 'soccer' matches.
    Callers doing keyword lookups must not read an empty result as absence.
    """
    fg = dev.foreground(serial=serial).get("package") or ""
    if fg != X_PKG:
        return {"error": "not in X", "foreground": fg,
                "hint": "ensure_home() first"}
    nodes = _nodes(_raw_xml(serial))
    # "Search" is one of the most common labels on Android. Matching it without
    # first proving we are on the Timelines screen once typed a query into the
    # launcher's Google box and read the autocomplete back as if it were X's.
    if not any(n["label"] == "Timelines" for n in nodes):
        return {"error": "not on the Timelines screen",
                "hint": "open_timelines_screen() first",
                "visible": [n["label"] for n in nodes if n["label"]][:12]}
    box = next((n for n in nodes if n["label"].lower() == "search"), None)
    if not box:
        return {"error": "search box not found on the Timelines screen"}
    _tap(*box["center"], serial=serial, settle=1.5)
    dev.shell("input text '%s'" % query.replace(" ", "%s"), serial=serial)
    time.sleep(2.5)
    rows = _nodes(_raw_xml(serial))
    labels = [n["label"] for n in rows if n["label"]]
    return {"query": query,
            "no_results": any("no results" in l.lower() for l in labels),
            "labels": labels}


def pin(name: str, unpin: bool = False, apply: bool = False,
        serial: str = "") -> dict:
    """Pin or unpin a Topic/List as a Home tab, from the Timelines screen."""
    want_btn = "unpin" if unpin else "pin"
    rows = _nodes(_raw_xml(serial))
    row = next((n for n in rows
                if n["label"].strip().lower() == name.strip().lower()), None)
    if not row:
        return {"error": "%r not on screen" % name,
                "hint": "search_timelines() first; the catalogue is US-named"}
    y = row["bounds"][1]
    btn = next((n for n in rows
                if n["label"].lower() == want_btn
                and abs(n["bounds"][1] - y) < 120), None)
    if not btn:
        return {"error": "no %r button beside %r" % (want_btn, name),
                "note": "already in the desired state?"}
    plan = {"action": want_btn, "name": name, "tap": btn["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*btn["center"], serial=serial, settle=2.0)
    _journal(plan)
    return {"applied": True, "plan": plan}


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def snapshot(max_tweets: int = 40, max_swipes: int = 20, settle_s: float = 1.3,
             out_dir: str = "", serial: str = "") -> dict:
    """Sample the live timeline and write it to artifacts/feed/ as JSON.

    Reuses `tools.apps.twitter.assemble_tweets` rather than reimplementing the
    tweet-boundary rule - one parser, so a fix there fixes both paths. Records
    which timeline was active, because a sample is meaningless without it.
    """
    from ..tools.apps.twitter import assemble_tweets

    # X collapses the tab strip once the feed is scrolled, so a snapshot taken
    # where the last one left off cannot see which timeline it is sampling.
    # Return to the top first: it also makes successive samples comparable.
    t = timelines(serial)
    if t["active"] is None:
        top = next((n for n in _nodes(_raw_xml(serial))
                    if n["clickable"] and n["label"].lower() == "scroll to top"),
                   None)
        if top:
            _tap(*top["center"], serial=serial, settle=1.5)
        t = timelines(serial)
    merged: dict[str, dict] = {}
    order: list[str] = []
    barren = 0
    for _ in range(max_swipes + 1):
        els = uix.parse(_raw_xml(serial))
        fresh = 0
        for tw in assemble_tweets(els):
            key = "%s|%s" % (tw.get("handle"), (tw.get("text") or "")[:70])
            if key not in merged:
                merged[key] = tw
                order.append(key)
                fresh += 1
        barren = 0 if fresh else barren + 1
        if len(merged) >= max_tweets or barren >= 2:
            break
        dev.shell("input swipe 540 1700 540 800 300", serial=serial)
        time.sleep(settle_s)

    tweets = [merged[k] for k in order][:max_tweets]
    ads = sum(1 for tw in tweets if tw.get("is_ad"))
    authors: dict[str, int] = {}
    for tw in tweets:
        h = tw.get("handle") or "?"
        authors[h] = authors.get(h, 0) + 1

    rec = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "x",
        "app_version": dev.app_version(X_PKG, serial=serial),
        "timeline": t["active"],
        "collected": len(tweets),
        "composition": {
            "ad_share": round(ads / len(tweets), 3) if tweets else None,
            "distinct_authors": len(authors),
            "repeat_rate": (round(1 - len(authors) / len(tweets), 3)
                            if tweets else None),
            "top_authors": sorted(authors.items(), key=lambda kv: -kv[1])[:10],
        },
        "tweets": tweets,
    }
    out = Path(out_dir) if out_dir else JOURNAL.parent
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = (t["active"] or "unknown").lower().replace(" ", "")
    path = out / ("x-%s-%s.json" % (slug, stamp))
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    rec["path"] = str(path)
    return rec
