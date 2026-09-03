"""Instagram feed control: read a reel's meaning, then tell the ranker.

Verified against com.instagram.android 440.1.0.46.86 on RMX3395, 2026-09-03.

Instagram is the richest of the three surfaces, and the only one where a
controlled experiment is possible - it has BOTH a per-item signal (Interested /
Not interested) AND a global reset (Reset suggested content). X has neither.

The thing that makes it work, though, is a piece of luck: **Meta hands over its
own content understanding, as text.** A reel carries no caption in the
accessibility tree - just counts - so there is nothing for a text model to read.
But the reel's More sheet contains an AI-generated description:

    "A young woman humorously captures India's middle-class budget struggles,
     where even spending 50 rupees feels like a major financial decision..."

Scored against that with the on-device embedder (`feed.read`):

    indian middle class comedy relatable humour   0.334
    personal finance money saving                 0.290
    football match                                0.140
    bollywood film celebrity gossip               0.098

So video needs no video model here. Meta has already watched the reel and
written down what it is about; we read the sentence. A frame-sampling CLIP model
would cost orders of magnitude more to reach a worse description of the same
thing. The description is not always present - older reels often lack it, and it
is sometimes vague - so `about_reel` says which case it hit rather than
returning an empty string that reads like an opinion.

Everything that writes is two-phase (`apply=False` plans) and journalled, as in
`feed.x`.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import device as dev
from . import read as rd
from .x import _journal, _nodes, _raw_xml, _tap

IG_PKG = "com.instagram.android"

# Rows in the reel/post More sheet. Instagram names them, unlike X.
_OPTION_RID = "control_option_text"
_MORE_RID = "clips_ufi_more_button_component"

# Every one of these WRITES to the account. Named so a caller cannot tap one by
# accident while meaning to read the sheet.
_WRITES = {"interested", "not interested", "report", "save"}


def ensure_reels(serial: str = "", settle: float = 5.0) -> dict:
    """Get to the Reels tab, launching Instagram if needed."""
    fg = dev.foreground(serial=serial).get("package") or ""
    if fg != IG_PKG:
        dev.shell("monkey -p %s -c android.intent.category.LAUNCHER 1" % IG_PKG,
                  serial=serial)
        time.sleep(settle + 3.0)
    nodes = _nodes(_raw_xml(serial))
    if any(n["rid"] == _MORE_RID for n in nodes):
        return {"on_reels": True}
    tab = next((n for n in nodes if n["rid"] == "clips_tab"), None)
    if not tab:
        return {"on_reels": False, "error": "Reels tab not found",
                "foreground": dev.foreground(serial=serial)}
    _tap(*tab["center"], serial=serial, settle=settle)
    return {"on_reels": any(n["rid"] == _MORE_RID
                            for n in _nodes(_raw_xml(serial)))}


# Reels a pass must scroll straight past without opening anything. An ad's
# controls are not a post's controls: on X, tapping a promoted post's body
# opened the Play Store install sheet, and there is no reason to trust a
# sponsored reel's sheet to behave like an organic one. "Suggested for you" and
# follow-prompt interstitials are not reels at all - there is nothing to judge.
_NOT_A_REEL = ("sponsored", "paid partnership", "suggested for you",
               "sponsored ·", "promoted", "follow more accounts")

# CAREFUL if this is ever reused for the HOME feed: there, "Suggested for you"
# marks a RECOMMENDED post - precisely the ranked content whose Interested /
# Not interested controls exist to be used, and skipping it would skip the only
# thing worth shaping. In Reels the same string marks an interstitial with
# nothing to judge. Same words, opposite meaning, different surface.


def is_ad(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Whether the current reel is an ad or an interstitial rather than content."""
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    hits = []
    for n in nodes:
        low = (n["label"] or "").strip().lower()
        if not low or len(low) > 60:
            continue
        for mark in _NOT_A_REEL:
            if mark in low:
                hits.append(n["label"][:60])
                break
    return {"is_ad": bool(hits), "markers": hits[:4]}


def reel_info(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Author and engagement for the reel on screen, from its labels."""
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    by_rid = {}
    for n in nodes:
        if n["rid"] and n["label"]:
            by_rid.setdefault(n["rid"], n["label"])

    def num(rid: str) -> Optional[int]:
        s = by_rid.get(rid) or ""
        digits = "".join(c for c in s if c.isdigit())
        return int(digits) if digits else None

    return {
        "author": by_rid.get("clips_author_username"),
        "caption": by_rid.get("clips_caption_component"),
        "likes": num("like_count"),
        "comments": num("comment_count"),
        "reposts": num("repost_count"),
        "saves": num("save_count"),
    }


def open_more(serial: str = "", wait_s: float = 10.0,
              poll_s: float = 1.2) -> dict:
    """Open the reel's More sheet and wait for Meta's description to generate.

    The More control's touch target is unreliable - a single tap often does
    nothing - so this retries rather than reporting a reel as having no
    description when the sheet never opened. Those two failures look identical
    from the outside and must not be confused.
    """
    for attempt in range(3):
        nodes = _nodes(_raw_xml(serial))
        if any(n["rid"] == _OPTION_RID for n in nodes):
            break                       # already open
        btn = next((n for n in nodes if n["rid"] == _MORE_RID), None)
        if not btn:
            return {"opened": False, "error": "no More button - is a reel open?"}
        dev.shell("input tap %d %d" % btn["center"], serial=serial)
        time.sleep(1.6)
    else:
        return {"opened": False, "error": "More sheet would not open"}

    deadline = time.time() + wait_s
    desc, options = None, []
    while time.time() < deadline:
        nodes = _nodes(_raw_xml(serial))
        options = [n["label"] for n in nodes if n["rid"] == _OPTION_RID]
        if not options:
            time.sleep(poll_s)
            continue
        # The description is the long, UNNAMED text node in the sheet - it
        # carries no resource-id, so it is identified by length and by not
        # being one of the labelled controls or the media node.
        cands = [n["label"] for n in nodes
                 if n["label"] and len(n["label"]) > 45
                 and not n["rid"]
                 and "double-tap" not in n["label"].lower()]
        if cands:
            desc = max(cands, key=len)
            break
        time.sleep(poll_s)

    return {"opened": True, "options": options, "description": desc,
            "generated": bool(desc),
            "note": None if desc else
            "sheet open but no description - older reels often have none, and "
            "it can also still be generating; raise wait_s to tell them apart"}


def close_sheet(serial: str = "", settle: float = 1.2) -> dict:
    dev.shell("input keyevent KEYCODE_BACK", serial=serial)
    time.sleep(settle)
    return {"closed": not any(n["rid"] == _OPTION_RID
                              for n in _nodes(_raw_xml(serial)))}


def signal(kind: str = "not interested", apply: bool = False,
           serial: str = "") -> dict:
    """Tap Interested or Not interested in an OPEN sheet.

    Matches the row by label, never by position - the sheet's contents differ
    between posts and reels, and between recommended and followed content.
    """
    want = kind.strip().lower()
    if want not in ("interested", "not interested"):
        return {"error": "kind must be 'interested' or 'not interested'"}
    nodes = _nodes(_raw_xml(serial))
    row = next((n for n in nodes if n["rid"] == _OPTION_RID
                and n["label"].strip().lower() == want), None)
    if not row:
        return {"error": "%r not in this sheet" % kind,
                "options": [n["label"] for n in nodes
                            if n["rid"] == _OPTION_RID]}
    plan = {"action": "ig_signal", "kind": want, "tap": row["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*row["center"], serial=serial, settle=1.5)
    _journal(plan)
    return {"applied": True, "plan": plan}


def next_reel(serial: str = "", settle: float = 1.4) -> None:
    dev.shell("input swipe 540 1700 540 500 220", serial=serial)
    time.sleep(settle)


def reels_pass(query: str, reels: int = 8, apply: bool = False,
               like_threshold: float = 0.12, drop_threshold: float = 0.03,
               serial: str = "") -> dict:
    """Walk the Reels feed, judging each reel by Meta's own description.

    For each reel: open the sheet, read the description, score it against
    `query`, and tell the ranker - Interested when it is clearly on topic, Not
    interested when it is clearly off. Reels whose description never generated
    are LEFT ALONE: acting on a reel we could not read would be guessing, and a
    wrong Not-interested is as much a write as a right one.

    The thresholds leave a deliberate dead zone, and the first live pass sent NO
    signals because everything landed inside it (0.119, 0.049, 0.137 against a
    narrow query). That is the safe failure, but it is still a failure - a pass
    that writes nothing has shaped nothing. Widen the QUERY before widening the
    thresholds: "indian middle class comedy relatable humour" is far narrower
    than a feed's real variety, and a query naming the whole territory scores
    its members higher.
    """
    ensure_reels(serial)
    out = {"query": query, "applied": apply, "reels": [],
           "scorer": "embedding" if rd.relevance_available() else "unavailable"}
    interested = not_interested = skipped = 0

    ads = 0
    for i in range(reels):
        nodes = _nodes(_raw_xml(serial))
        ad = is_ad(serial, nodes=nodes)
        if ad["is_ad"]:
            # Scroll past without opening anything. Never open an ad's sheet.
            out["reels"].append({"action": "skipped (ad)",
                                 "markers": ad["markers"]})
            ads += 1
            next_reel(serial)
            continue

        info = reel_info(serial, nodes=nodes)
        sheet = open_more(serial)
        rec = {"author": info.get("author"), "likes": info.get("likes"),
               "description": (sheet.get("description") or "")[:200],
               "generated": sheet.get("generated", False)}

        if sheet.get("generated") and out["scorer"] == "embedding":
            s = rd.relevance(query, [sheet["description"]])
            rec["score"] = s[0] if s else None
        else:
            rec["score"] = None

        if rec["score"] is None:
            rec["action"] = "skipped (no description)"
            skipped += 1
        elif rec["score"] >= like_threshold:
            rec["action"] = "interested"
            r = signal("interested", apply=apply, serial=serial)
            rec["result"] = r.get("applied", False)
            interested += 1
        elif rec["score"] <= drop_threshold:
            rec["action"] = "not interested"
            r = signal("not interested", apply=apply, serial=serial)
            rec["result"] = r.get("applied", False)
            not_interested += 1
        else:
            rec["action"] = "left alone (middling)"
            skipped += 1

        out["reels"].append(rec)
        # ALWAYS confirm the sheet is shut before moving on. Tapping a row
        # sometimes dismisses it and sometimes does not, and a sheet left open
        # swallows the next reel entirely - a live pass returned two reels with
        # no author and no description for exactly this reason, which reads like
        # "those reels had no description" when they were never seen at all.
        for _ in range(2):
            if close_sheet(serial).get("closed"):
                break
        next_reel(serial)

    out["interested"] = interested
    out["not_interested"] = not_interested
    out["skipped"] = skipped
    out["ads_skipped"] = ads
    if apply:
        _journal({"action": "ig_reels_pass", "query": query, "n": reels,
                  "interested": interested, "not_interested": not_interested})
    return out
