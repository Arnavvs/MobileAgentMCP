"""Ingest scrape artifacts into SQLite, with full timing instrumentation.

  python pipeline/ingest.py                    # ingest everything in artifacts/
  python pipeline/ingest.py --stats            # show what's in the DB
  python pipeline/ingest.py --timings          # per-step timing report

Idempotent: re-running the same artifacts does not duplicate rows. Observations
are keyed by (item, observed_at) so a re-scrape appends a new observation rather
than overwriting - the change over time is the point.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "pipeline", "data.db")
ARTIFACTS = os.path.join(ROOT, "artifacts")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "pipeline", "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    return con


def _num(v):
    """Accept both bare ints and the {raw, value} shape the scrapers emit."""
    if isinstance(v, dict):
        return v.get("value")
    return v


def upsert_account(con, platform, handle) -> str:
    aid = f"{platform}:{handle}"
    con.execute(
        "INSERT INTO accounts(account_id,platform,handle,first_seen,last_seen) "
        "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET last_seen=excluded.last_seen",
        (aid, platform, handle, now(), now()))
    return aid


def upsert_item(con, platform, kind, account_id, key, shortcode=None,
                permalink=None) -> str:
    iid = f"{platform}:{sid(kind, account_id, key)}"
    con.execute(
        "INSERT INTO items(item_id,platform,kind,account_id,shortcode,permalink,"
        "first_seen,last_seen,times_seen) VALUES(?,?,?,?,?,?,?,?,1) "
        "ON CONFLICT(item_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "times_seen=items.times_seen+1",
        (iid, platform, kind, account_id, shortcode, permalink, now(), now()))
    return iid


def record_timing(con, run_id, step, target, seconds, items=None, bytes_=None):
    con.execute(
        "INSERT OR REPLACE INTO timings(run_id,step,target,seconds,items,bytes,"
        "started_at) VALUES(?,?,?,?,?,?,?)",
        (run_id, step, target or "", seconds, items, bytes_, now()))


# ---------------------------------------------------------------- instagram

def ingest_profile(con, path) -> dict:
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    handle = rec.get("handle")
    if not handle:
        return {"file": os.path.basename(path), "skipped": "no handle"}

    run_id = sid("run", handle, rec.get("scraped_at") or path)
    con.execute(
        "INSERT OR IGNORE INTO runs(run_id,started_at,app,app_version,transport)"
        " VALUES(?,?,?,?,?)",
        (run_id, rec.get("scraped_at") or now(), "instagram",
         (rec.get("profile") or {}).get("app_version"), "wireless"))

    for step, secs in (rec.get("timings") or {}).items():
        record_timing(con, run_id, step, handle, secs)

    p = rec.get("profile") or {}
    aid = upsert_account(con, "instagram", handle)
    con.execute(
        "INSERT OR REPLACE INTO profile_observations(obs_id,account_id,run_id,"
        "observed_at,display_name,bio,external_link,followers,following,"
        "post_count,verified,is_private,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid("prof", handle, rec.get("scraped_at")), aid, run_id,
         rec.get("scraped_at") or now(), p.get("display_name"), p.get("bio"),
         p.get("external_link"), _num(p.get("followers")), _num(p.get("following")),
         _num(p.get("posts")), int(bool(p.get("verified"))),
         int(bool(rec.get("private") or p.get("private"))), json.dumps(p)))

    n_items = 0
    for post in (rec.get("posts") or {}).get("posts", []) or []:
        key = f"{post.get('caption','')[:60]}|{post.get('likes')}|{post.get('media_count')}"
        iid = upsert_item(con, "instagram", "post", aid, key)
        con.execute(
            "INSERT OR REPLACE INTO item_observations(obs_id,item_id,run_id,"
            "observed_at,caption,audio,location,posted_age,likes,comments,"
            "reposts,media_kind,media_count,source,raw_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid("io", iid, rec.get("scraped_at")), iid, run_id,
             rec.get("scraped_at") or now(), post.get("caption"),
             post.get("audio"), post.get("location"), post.get("date"),
             post.get("likes"), post.get("comments"), post.get("reposts"),
             post.get("type"), post.get("media_count"), "profile_grid",
             json.dumps(post)))
        n_items += 1

    for reel in (rec.get("reels_grid") or {}).get("reels", []) or []:
        key = f"reel|{reel.get('grid_index')}|{reel.get('views')}"
        iid = upsert_item(con, "instagram", "reel", aid, key)
        con.execute(
            "INSERT OR REPLACE INTO item_observations(obs_id,item_id,run_id,"
            "observed_at,views,source,raw_json) VALUES(?,?,?,?,?,?,?)",
            (sid("io", iid, rec.get("scraped_at")), iid, run_id,
             rec.get("scraped_at") or now(), reel.get("views"),
             "reels_grid", json.dumps(reel)))
        n_items += 1

    for det in (rec.get("reel_details") or {}).get("reels", []) or []:
        f = det.get("fields", det)
        key = f"reeldet|{f.get('username')}|{(f.get('caption') or '')[:50]}"
        iid = upsert_item(con, "instagram", "reel", aid, key)
        con.execute(
            "INSERT OR REPLACE INTO item_observations(obs_id,item_id,run_id,"
            "observed_at,caption,audio,likes,comments,saves,reposts,source,raw_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid("io", iid, rec.get("scraped_at"), det.get("grid_index")), iid,
             run_id, rec.get("scraped_at") or now(), f.get("caption"),
             f.get("audio"), _num(f.get("like_count")), _num(f.get("comment_count")),
             _num(f.get("save_count")), _num(f.get("repost_count")),
             "reel_detail", json.dumps(det)))
        n_items += 1

    record_timing(con, run_id, "ingest", handle, round(time.time() - t0, 3), n_items)
    return {"file": os.path.basename(path), "handle": handle, "items": n_items}


def ingest_media(con) -> dict:
    """Register reel MP4s, measuring nothing we can't verify from the file."""
    n = 0
    for path in glob.glob(os.path.join(ARTIFACTS, "reels_*", "*.mp4")):
        b = os.path.getsize(path)
        mid = sid("media", os.path.basename(path), b)
        con.execute(
            "INSERT OR REPLACE INTO media(media_id,kind,local_path,bytes,"
            "acquired,created_at) VALUES(?,?,?,?,?,?)",
            (mid, "reel_video", path, b, "screen_capture", now()))
        n += 1
    return {"media_registered": n}


def stats(con):
    q = lambda s: con.execute(s).fetchone()[0]
    print("=== DATABASE ===")
    for t in ("runs", "accounts", "profile_observations", "items",
              "item_observations", "comments", "media", "timings"):
        print(f"  {t:22} {q(f'SELECT COUNT(*) FROM {t}')}")
    print(f"  db size              {os.path.getsize(DB)/1024:.1f} KB")
    mb = con.execute("SELECT COALESCE(SUM(bytes),0) FROM media").fetchone()[0]
    print(f"  media on disk        {mb/1048576:.1f} MB")
    print("\n=== ACCOUNTS ===")
    for r in con.execute(
        "SELECT a.handle, p.followers, p.post_count, p.is_private "
        "FROM accounts a LEFT JOIN profile_observations p "
        "ON p.account_id=a.account_id GROUP BY a.account_id"):
        print(f"  @{r['handle']:<18} followers={r['followers'] or '-':<8} "
              f"posts={r['post_count'] or '-':<5} "
              f"{'PRIVATE' if r['is_private'] else ''}")


def timings_report(con):
    print("=== TIMINGS BY STEP (seconds) ===")
    # 'TOTAL' is a per-run summary the scraper records, not a step. Summing it
    # alongside the real steps double-counts every run.
    rows = con.execute(
        "SELECT step, COUNT(*) n, ROUND(AVG(seconds),2) avg_s, "
        "ROUND(MIN(seconds),2) min_s, ROUND(MAX(seconds),2) max_s, "
        "ROUND(SUM(seconds),1) total_s FROM timings "
        "WHERE step NOT IN ('TOTAL','ingest') GROUP BY step "
        "ORDER BY total_s DESC").fetchall()
    runs_total = con.execute(
        "SELECT ROUND(AVG(seconds),1), ROUND(SUM(seconds),1) FROM timings "
        "WHERE step='TOTAL'").fetchone()
    print(f"  {'step':<20}{'n':>4}{'avg':>9}{'min':>8}{'max':>9}{'total':>10}")
    grand = 0.0
    for r in rows:
        grand += r["total_s"] or 0
        print(f"  {r['step']:<20}{r['n']:>4}{r['avg_s']:>9}{r['min_s']:>8}"
              f"{r['max_s']:>9}{r['total_s']:>10}")
    print(f"  {'sum of steps':<20}{'':>4}{'':>9}{'':>8}{'':>9}{round(grand,1):>10}")
    if runs_total and runs_total[0]:
        print(f"  {'per-run wall clock':<20}{'':>4}{runs_total[0]:>9}"
              f"{'':>8}{'':>9}{runs_total[1]:>10}")
    if grand:
        print("\n  share of total:")
        for r in rows[:6]:
            pct = 100.0 * (r["total_s"] or 0) / grand
            bar = "#" * int(pct / 2)
            print(f"    {r['step']:<20} {pct:5.1f}%  {bar}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--timings", action="store_true")
    args = ap.parse_args()

    con = connect()
    if args.stats:
        stats(con); return
    if args.timings:
        timings_report(con); return

    t0 = time.time()
    out = []
    for path in sorted(glob.glob(os.path.join(ARTIFACTS, "profiles", "*.json"))):
        if any(k in os.path.basename(path) for k in ("partial", "media")):
            continue
        out.append(ingest_profile(con, path))
    out.append(ingest_media(con))
    con.commit()
    for o in out:
        print(" ", json.dumps(o, ensure_ascii=False))
    print(f"\ningested in {time.time()-t0:.2f}s -> {DB}")
    stats(con)


if __name__ == "__main__":
    main()
