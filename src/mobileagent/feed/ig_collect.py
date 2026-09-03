"""Collect what Instagram is actually serving, without changing anything.

Read-only by construction: it opens each reel's More sheet to read Meta's
AI-generated description, then closes it with BACK. It never taps a sheet row -
Interested, Not interested, Save and Report all write to the account.

On reel links: there is no permalink in the accessibility tree, and the one
route to it - Share > Copy link - puts the URL in the clipboard, which Android
will not hand to `adb` (clipboard reads are restricted to the foreground app;
`cmd clipboard get-text` is unimplemented and `service call clipboard` returns
"No items"). Worse, that share sheet carries a Play Store "Send to device"
target that a mistimed tap opens. So each reel is identified by its AUTHOR and
its profile URL, which is stable, honest and free. A true permalink would need
a foreground clipboard reader.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import instagram as ig
from . import read as rd
from .x import JOURNAL, _nodes, _raw_xml


def collect(n: int = 20, out_dir: str = "", serial: str = "",
            queries: tuple = ()) -> dict:
    """Walk n reels and record author, Meta's description, and engagement.

    Ads and interstitials are counted but never opened. Reels whose description
    has not generated are recorded as such rather than dropped - how often that
    happens is itself a finding about how much of the feed is legible to us.
    """
    ig.ensure_reels(serial)
    rows, ads, nodesc = [], 0, 0
    t0 = time.time()

    for i in range(n):
        nodes = _nodes(_raw_xml(serial))
        ad = ig.is_ad(serial, nodes=nodes)
        if ad["is_ad"]:
            ads += 1
            rows.append({"i": i, "kind": "ad", "markers": ad["markers"]})
            ig.next_reel(serial)
            continue

        info = ig.reel_info(serial, nodes=nodes)
        sheet = ig.open_more(serial)
        desc = sheet.get("description")
        if not desc:
            nodesc += 1
        rows.append({
            "i": i, "kind": "reel",
            "author": info.get("author"),
            "profile": ("https://instagram.com/%s" % info["author"])
                       if info.get("author") else None,
            "likes": info.get("likes"), "comments": info.get("comments"),
            "reposts": info.get("reposts"), "saves": info.get("saves"),
            "meta_description": desc,
            "options": sheet.get("options"),
        })
        for _ in range(2):
            if ig.close_sheet(serial).get("closed"):
                break
        ig.next_reel(serial)

    # Optional: score every description against each query in one batch, so a
    # collection doubles as a labelled set for choosing thresholds later.
    described = [r for r in rows if r.get("meta_description")]
    if queries and described and rd.relevance_available():
        texts = [r["meta_description"] for r in described]
        for q in queries:
            scores = rd.relevance(q, texts) or []
            for r, s in zip(described, scores):
                r.setdefault("scores", {})[q] = s

    out = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "instagram", "surface": "reels",
        "requested": n, "reels": len(described) + nodesc, "ads": ads,
        "no_description": nodesc,
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
    }
    d = Path(out_dir) if out_dir else JOURNAL.parent
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("ig-reels-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    out["path"] = str(p)
    return out
