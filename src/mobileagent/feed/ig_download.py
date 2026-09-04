"""Fetch the reels a collection linked, by whatever route works.

`feed.ig_links` gives every collected reel a public permalink. This turns those
links into files, so a pass over the feed leaves behind the videos themselves
and not only a description of them.

Three backends, tried in order, because each fails differently:

  ytdlp        our own infra, no login. The default: a public reel came down in
               2.9 s / 3.6 MB on first try. Breaks when Instagram changes its
               page shape, which it does - but yt-dlp ships fixes quickly.
  instaloader  our own infra, authenticated. Slower and rate-limited, but it
               sees what the logged-in account sees, so it reaches reels an
               anonymous fetch cannot, and it carries real metadata.
  cobalt       an online service. The PUBLIC instance is not usable: as of
               2026-09-04 `api.cobalt.tools` answers 403 to an anonymous
               request, so this backend is off unless COBALT_URL points at an
               instance you run. Cobalt is open source and self-hostable, which
               is the only configuration worth relying on.

The router reports which backend produced the file and what every other one
said, so a failure is diagnosable rather than just a missing file. It never
silently falls back to a worse copy: the on-device screen recorder in
`tools/apps/reel_capture.py` re-records the video off the phone's screen and is
deliberately NOT wired in here - it is a different artifact, not a fallback.

Scope: these are public reels from the owner's own feed, fetched for their own
analysis. Automated downloading is against Instagram's terms whatever the
transport, so keep the volume human-plausible and the purpose personal.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIR = ROOT / "artifacts" / "reels_dl"

BACKENDS = ("ytdlp", "instaloader", "cobalt")

_SHORTCODE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")
_BARE = re.compile(r"^[A-Za-z0-9_-]{5,}$")


def normalise(link: str) -> dict:
    """Accept a permalink or a bare shortcode; return both forms."""
    link = (link or "").strip()
    m = _SHORTCODE.search(link)
    if m:
        sc = m.group(1)
    elif _BARE.match(link):
        sc = link
    else:
        return {"error": "not a reel link or shortcode", "given": link[:120]}
    return {"shortcode": sc,
            "url": "https://www.instagram.com/reel/%s/" % sc}


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

def _via_ytdlp(url: str, dest: Path, timeout: int = 120) -> dict:
    try:
        import yt_dlp
    except ImportError:
        return {"ok": False, "error": "yt-dlp not installed (pip install yt-dlp)"}
    opts = {"outtmpl": str(dest.with_suffix(".%(ext)s")),
            "quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": timeout}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        path = Path(y.prepare_filename(info))
        if not path.exists():
            return {"ok": False, "error": "yt-dlp reported success but wrote nothing"}
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size,
                "uploader": info.get("uploader"),
                "title": (info.get("title") or "")[:120]}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def _via_instaloader(shortcode: str, dest: Path, timeout: int = 120) -> dict:
    try:
        import instaloader
    except ImportError:
        return {"ok": False, "error": "instaloader not installed"}
    try:
        L = instaloader.Instaloader(quiet=True, download_comments=False,
                                    save_metadata=False)
        # Reuse the session `tools/apps/instagram_web.py` persists, when there
        # is one - an authenticated fetch reaches reels an anonymous one cannot.
        sess = ROOT / "artifacts" / "ig_session"
        if sess.exists():
            try:
                L.load_session_from_file(os.environ.get("IG_USER", ""), str(sess))
            except Exception:
                pass                      # anonymous is still worth trying
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        if not post.is_video:
            return {"ok": False, "error": "not a video post"}
        out = dest.with_suffix(".mp4")
        req = urllib.request.Request(post.video_url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out.write_bytes(r.read())
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "uploader": post.owner_username,
                "title": (post.caption or "")[:120]}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def _via_cobalt(url: str, dest: Path, timeout: int = 120) -> dict:
    """Cobalt, and only against an instance you run.

    Off by default on purpose: the public instance returns 403 to anonymous
    requests, so pointing at it would produce a backend that always fails and
    an error that looks like a bug in this code.
    """
    base = os.environ.get("COBALT_URL", "").rstrip("/")
    if not base:
        return {"ok": False, "error": "COBALT_URL unset - public api.cobalt.tools "
                                      "answers 403; self-host and set COBALT_URL"}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = os.environ.get("COBALT_KEY")
    if key:
        headers["Authorization"] = "Api-Key %s" % key
    try:
        req = urllib.request.Request(
            base, data=json.dumps({"url": url}).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        link = body.get("url") or (body.get("picker") or [{}])[0].get("url")
        if not link:
            return {"ok": False, "error": "cobalt returned no url",
                    "status": body.get("status"), "body": str(body)[:160]}
        out = dest.with_suffix(".mp4")
        with urllib.request.urlopen(link, timeout=timeout) as r:
            out.write_bytes(r.read())
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "instance": base}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


_RUNNERS = {"ytdlp": lambda n, d, t: _via_ytdlp(n["url"], d, t),
            "instaloader": lambda n, d, t: _via_instaloader(n["shortcode"], d, t),
            "cobalt": lambda n, d, t: _via_cobalt(n["url"], d, t)}


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

def download(link: str, backends: tuple = BACKENDS, out_dir: str = "",
             timeout: int = 120, overwrite: bool = False) -> dict:
    """Fetch one reel, trying each backend until one produces a file."""
    n = normalise(link)
    if "error" in n:
        return n
    d = Path(out_dir) if out_dir else DEFAULT_DIR
    d.mkdir(parents=True, exist_ok=True)
    dest = d / n["shortcode"]

    existing = next((p for p in d.glob(n["shortcode"] + ".*")
                     if p.suffix != ".json"), None)
    if existing and not overwrite:
        return {"shortcode": n["shortcode"], "url": n["url"], "backend": "cached",
                "path": str(existing), "bytes": existing.stat().st_size,
                "seconds": 0.0}

    tried = []
    t0 = time.time()
    for name in backends:
        run = _RUNNERS.get(name)
        if not run:
            tried.append({"backend": name, "error": "unknown backend"})
            continue
        t1 = time.time()
        r = run(n, dest, timeout)
        r["backend"] = name
        r["seconds"] = round(time.time() - t1, 2)
        if r.get("ok"):
            return {"shortcode": n["shortcode"], "url": n["url"],
                    "backend": name, "path": r["path"], "bytes": r["bytes"],
                    "uploader": r.get("uploader"), "title": r.get("title"),
                    "seconds": round(time.time() - t0, 2), "tried": tried}
        tried.append({"backend": name, "error": r.get("error"),
                      "seconds": r["seconds"]})
    return {"shortcode": n["shortcode"], "url": n["url"], "error": "all backends failed",
            "tried": tried, "seconds": round(time.time() - t0, 2)}


def download_collection(path: str = "", limit: int = 0, backends: tuple = BACKENDS,
                        out_dir: str = "", pace_s: float = 1.5) -> dict:
    """Download every reel a collection linked.

    Skips ads and rows with no link. Paces requests: a burst of downloads is the
    part of this that looks least like a person reading their feed, and the
    collection it came from took minutes to gather anyway.
    """
    if not path:
        files = sorted((ROOT / "artifacts" / "feed").glob("ig-reels-*.json"),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            return {"error": "no collection files in artifacts/feed"}
        path = str(files[-1])
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = [r for r in data.get("rows", [])
            if r.get("kind") == "reel" and r.get("url")]
    if limit:
        rows = rows[:limit]

    out, ok, failed, cached = [], 0, 0, 0
    t0 = time.time()
    for i, r in enumerate(rows):
        res = download(r["url"], backends=backends, out_dir=out_dir)
        res["author"] = r.get("author")
        res["meta_description"] = (r.get("meta_description") or "")[:160]
        out.append(res)
        if res.get("backend") == "cached":
            cached += 1
        elif res.get("path"):
            ok += 1
        else:
            failed += 1
        if i + 1 < len(rows):
            time.sleep(pace_s)

    return {"collection": os.path.basename(path), "attempted": len(rows),
            "downloaded": ok, "cached": cached, "failed": failed,
            "seconds": round(time.time() - t0, 1),
            "bytes": sum(r.get("bytes") or 0 for r in out),
            "results": out}
