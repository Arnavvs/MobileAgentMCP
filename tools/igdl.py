#!/usr/bin/env python
"""Download reels by link, or every reel a collection linked.

    python tools/igdl.py one https://www.instagram.com/reel/DblkACQpwij/
    python tools/igdl.py one DblkACQpwij --backends instaloader
    python tools/igdl.py collection --limit 5
    python tools/igdl.py collection --path artifacts/feed/ig-reels-....json

Backends are tried in order and the winner is reported, along with what every
other one said. Files land in artifacts/reels_dl/ and an already-downloaded
shortcode is reported as `cached` rather than fetched again.

Cobalt is only useful against an instance you run - set COBALT_URL (and
COBALT_KEY if it needs one). The public instance answers 403.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobileagent.feed import ig_download as dl  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Instagram reel downloader")
    p.add_argument("--backends", default=",".join(dl.BACKENDS),
                   help="comma-separated, in order: ytdlp,instaloader,cobalt")
    p.add_argument("--out", default="", dest="out_dir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("one", help="download a single link or shortcode")
    sp.add_argument("link")
    sp.add_argument("--overwrite", action="store_true")

    sp = sub.add_parser("collection", help="download everything a pass linked")
    sp.add_argument("--path", default="", help="defaults to the newest collection")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--pace", type=float, default=1.5, dest="pace_s",
                    help="seconds between downloads")

    a = p.parse_args()
    backends = tuple(b.strip() for b in a.backends.split(",") if b.strip())

    if a.cmd == "one":
        r = dl.download(a.link, backends=backends, out_dir=a.out_dir,
                        overwrite=a.overwrite)
    else:
        r = dl.download_collection(a.path, limit=a.limit, backends=backends,
                                   out_dir=a.out_dir, pace_s=a.pace_s)
        for x in r.get("results", []):
            print("  %-12s %-11s %10s B  %s"
                  % (x.get("shortcode"), x.get("backend") or "FAILED",
                     x.get("bytes"), x.get("author") or ""))
        r = {k: v for k, v in r.items() if k != "results"}

    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0 if not r.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
