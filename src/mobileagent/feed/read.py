"""Real-time feed reading: what is on screen right now, as structured posts.

Built because every feed-shaping decision needs to be made against what is
actually visible - "is this post football or politics" cannot be answered from a
tap coordinate. The agent needs to read before it acts, at a rate that keeps up
with scrolling.

The enabling measurement (RMX3395, USB, 2026-09-03):

    shell `uiautomator dump` + `cat`   2.47 s per read
    uiautomator2 `dump_hierarchy()`    0.21 s per read      <- 12x faster

So this module holds ONE persistent `uiautomator2` connection and reuses it,
rather than shelling out per read. 0.21 s is roughly five reads a second, which
is fast enough to read a feed while scrolling it. `feed/x.py` still uses the
shell path for control actions, where a 2 s cost is irrelevant next to the
settle time after a tap.

A second reason to prefer u2 here: the shell dump returns a COMPRESSED tree
(~8 KB for a screen where u2 returns ~25 KB), so it drops nodes. More tree is
strictly better for reading.

Nothing in this module writes to the device. It is the instrument, not the
treatment.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from .. import device as dev
from .. import ui as uix

X_PKG = "com.twitter.android"

# Deliberately broad and deliberately editable. These are a starting point for
# sorting a feed, not a claim about what any post is really about - a human
# should be able to read the list and disagree with it.
TOPICS: dict[str, re.Pattern] = {
    "football": re.compile(
        r"football|soccer|barcelona|barca|real madrid|messi|ronaldo|yamal|"
        r"mbappe|haaland|premier league|la ?liga|uefa|fifa|champions league|"
        r"arsenal|chelsea|liverpool|man utd|manchester|tottenham|psg|bayern|"
        r"juventus|inter milan|striker|midfielder|goalkeeper|transfer window|"
        r"matchday|\bgoal\b|\bgoals\b|penalty|offside|\bfc\b|\bcf\b", re.I),
    "politics": re.compile(
        r"modi|bjp|congress|rahul gandhi|aap\b|kejriwal|jaishankar|"
        r"\bpolitic|election|parliament|lok sabha|rajya sabha|minister|"
        r"government|govt\b|vote|voter|democracy|protest|policy|"
        r"trump|biden|senate|republican|democrat|"
        r"[ऀ-ॿ]", re.I),
    "bollywood": re.compile(
        # Current storylines first - a live film title matches far more posts
        # than the word "bollywood" ever does. Refresh these per campaign.
        r"toxic|dhurandhar|bhooth bangla|border 2|"
        r"bollywood|tollywood|box office|teaser|trailer|"
        r"salman|shah ?rukh|srk\b|aamir|ranbir|deepika|alia|katrina|"
        r"kareena|priyanka|vicky|kartik|ranveer|yash\b|prabhas|"
        r"karan johar|bhansali|film ?fare|kapoor|khan\b|"
        r"first day|opening day|crore|advance booking", re.I),
    "ad": None,          # filled from the post's own is_ad flag
}


class Reader:
    """A persistent, low-latency view of what X is showing.

    Hold one of these for the length of a session: the u2 connection is the
    expensive part, and re-creating it per read throws away the speed this
    module exists for.
    """

    def __init__(self, serial: str = ""):
        self.serial = serial
        self._d = None
        self.reads = 0
        self.total_s = 0.0

    @property
    def d(self):
        if self._d is None:
            self._d = dev.u2(self.serial)
        return self._d

    # ---------------- core read ----------------

    def raw(self) -> str:
        t0 = time.time()
        xml = self.d.dump_hierarchy()
        self.reads += 1
        self.total_s += time.time() - t0
        return xml

    def posts(self, classify: bool = True) -> list[dict]:
        """Every post currently on screen, in document order.

        Reuses `assemble_tweets` so there is one tweet-boundary rule in the
        project rather than two that drift apart.
        """
        from ..tools.apps.twitter import assemble_tweets

        xml = self.raw()
        out = []
        for t in assemble_tweets(uix.parse(xml)):
            p = {
                "handle": t.get("handle"),
                "name": t.get("name"),
                "text": (t.get("text") or "").strip(),
                "age": t.get("age"),
                "is_ad": bool(t.get("is_ad")),
                "metrics": t.get("metrics") or {},
            }
            if classify:
                p["topics"] = tag(p)
            out.append(p)
        return out

    def screen(self) -> dict:
        """Posts plus the context needed to decide what to do with them."""
        from . import x as xf

        xml = self.raw()
        nodes = xf._nodes(xml)
        fg = dev.foreground(serial=self.serial)
        return {
            "package": fg.get("package"),
            "in_x": fg.get("package") == X_PKG,
            "surface": xf.surface(self.serial, nodes=nodes),
            "labels": [n["label"] for n in nodes if n["label"]][:60],
            "nodes": nodes,
        }

    def stats(self) -> dict:
        return {"reads": self.reads,
                "avg_s": round(self.total_s / self.reads, 3) if self.reads else None}


def tag(post: dict) -> list[str]:
    """Topic labels for one post. Cheap, transparent, and overridable."""
    hay = "%s %s" % (post.get("text") or "", post.get("handle") or "")
    out = [name for name, pat in TOPICS.items() if pat and pat.search(hay)]
    if post.get("is_ad"):
        out.append("ad")
    return out


def summarise(posts: list[dict]) -> dict:
    """Composition of what is on screen - the number that decides an action."""
    if not posts:
        return {"n": 0}
    counts: dict[str, int] = {}
    for p in posts:
        for t in p.get("topics") or ["untagged"]:
            counts[t] = counts.get(t, 0) + 1
    return {"n": len(posts), "counts": counts,
            "football_share": round(counts.get("football", 0) / len(posts), 2),
            "politics_share": round(counts.get("politics", 0) / len(posts), 2)}
