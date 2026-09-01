"""Full Instagram profile scrape with per-step timings.

  python run_profile_scrape.py deven__singh dangnavya paviikya

Writes artifacts/profiles/<handle>.json and prints a timing table so the
optimisation pass has real numbers rather than guesses.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
os.environ.setdefault("MOBILEAGENT_SERIAL", "192.168.1.5:5555")

from mobileagent.server import mcp  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "artifacts", "profiles")
os.makedirs(OUT, exist_ok=True)


def safe(s) -> str:
    return "".join(ch if ord(ch) < 128 else "." for ch in str(s))


async def call(name, args=None):
    r = await mcp.call_tool(name, args or {})
    try:
        return json.loads(r.content[0].text)
    except Exception:
        return {"_raw": r.content[0].text[:400]}


async def scrape(handle: str, max_posts: int = 8, reel_sample: int = 20,
                 with_reel_details: bool = True) -> dict:
    T: dict[str, float] = {}
    t_all = time.time()

    t = time.time()
    opened = await call("ig_open_profile", {"handle": handle, "settle_s": 5})
    T["open_profile"] = round(time.time() - t, 2)
    if not opened.get("opened"):
        return {"handle": handle, "error": "could not open profile",
                "detail": opened, "timings": T}

    t = time.time()
    stats = await call("ig_profile_stats")
    T["profile_stats"] = round(time.time() - t, 2)

    if stats.get("private"):
        T["TOTAL"] = round(time.time() - t_all, 2)
        rec = {"handle": handle, "private": True, "timings": T,
               "profile": stats,
               "note": "private account - no posts or reels are visible"}
        with open(os.path.join(OUT, f"{handle}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        return rec

    t = time.time()
    grid = await call("ig_scan_grid", {"max_items": 30, "max_swipes": 6})
    T["scan_grid"] = round(time.time() - t, 2)

    t = time.time()
    posts = await call("ig_collect_posts",
                       {"max_posts": max_posts, "max_swipes": 14})
    T["collect_posts"] = round(time.time() - t, 2)

    # Re-open the profile by deep link rather than pressing BACK.
    # BACK is not safe here: if the post feed never opened (empty/private grid)
    # it navigates out of THIS profile to whatever is beneath in the back stack,
    # and every later step then scrapes the previous account while labelling it
    # as this one. That silently produced deven__singh's reels under paviikya.
    t = time.time()
    await call("ig_open_profile", {"handle": handle, "settle_s": 4})
    T["reopen_profile"] = round(time.time() - t, 2)

    t = time.time()
    reels = await call("ig_scan_reels_grid",
                       {"max_reels": reel_sample, "max_swipes": 8,
                        "expect_handle": handle})
    T["scan_reels_grid"] = round(time.time() - t, 2)
    if reels.get("error"):
        return {"handle": handle, "error": reels["error"], "detail": reels,
                "timings": T, "profile": stats, "grid": grid, "posts": posts}

    plan = await call("ig_plan_reel_sample",
                      {"reels": reels.get("reels", [])})

    details = {}
    if with_reel_details and plan.get("open_order"):
        t = time.time()
        details = await call("ig_collect_reel_details",
                             {"indices": plan["open_order"],
                              "expect_handle": handle})
        T["reel_details"] = round(time.time() - t, 2)

    T["TOTAL"] = round(time.time() - t_all, 2)
    rec = {"handle": handle, "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "timings": T, "profile": stats, "grid": grid, "posts": posts,
           "reels_grid": reels, "reel_plan": plan, "reel_details": details}
    with open(os.path.join(OUT, f"{handle}.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rec


def report(rec: dict) -> None:
    h = rec["handle"]
    if rec.get("error"):
        print(f"\n### @{h}: {rec['error']}")
        return
    p = rec.get("profile", {})
    if rec.get("private") or not rec.get("grid"):
        print(f"\n{'='*66}\n@{h}  -  {safe(p.get('display_name'))}  [PRIVATE]")
        print(f"  followers {p.get('followers',{}).get('value')}  "
              f"following {p.get('following',{}).get('value')}  "
              f"posts {p.get('posts',{}).get('value')}")
        print(f"  no content visible; posts/reels skipped "
              f"({rec.get('timings',{}).get('TOTAL')}s)")
        return
    print(f"\n{'='*66}\n@{h}  -  {safe(p.get('display_name'))}"
          f"{'  [verified]' if p.get('verified') else ''}")
    print(f"  followers {p['followers']['value']:<10} "
          f"following {p['following']['value']:<8} posts {p['posts']['value']}")
    print(f"  bio: {safe(p.get('bio'))[:60]}")
    if p.get("external_link"):
        print(f"  link: {safe(p['external_link'])[:60]}")
    g = rec["grid"]
    print(f"  grid: {g.get('post_count')} posts / {g.get('reel_count')} reels")
    print(f"  collected: {rec['posts'].get('collected')} posts, "
          f"{rec['reels_grid'].get('collected')} reels scanned, "
          f"{rec.get('reel_details',{}).get('collected',0)} reel details")
    tiers = rec["reel_plan"].get("tiers", {})
    print(f"  sample -> latest{tiers.get('latest')} top{tiers.get('top')} "
          f"mid{tiers.get('mid')} low{tiers.get('low')}  "
          f"= {rec['reel_plan'].get('unique_to_open')} unique")
    print("  timings:", "  ".join(f"{k}={v}s" for k, v in rec["timings"].items()))


async def main():
    handles = sys.argv[1:] or ["deven__singh"]
    recs = []
    for h in handles:
        print(f"\n>>> scraping @{h} ...")
        rec = await scrape(h)
        recs.append(rec)
        report(rec)
    print(f"\n{'='*66}\nTIMING SUMMARY")
    keys = ["open_profile", "profile_stats", "scan_grid", "collect_posts",
            "reopen_profile", "scan_reels_grid", "reel_details", "TOTAL"]
    print(f"{'handle':<16}" + "".join(f"{k[:12]:>14}" for k in keys))
    for r in recs:
        if r.get("error"):
            continue
        print(f"{r['handle'][:15]:<16}" +
              "".join(f"{r['timings'].get(k,'-'):>14}" for k in keys))


if __name__ == "__main__":
    asyncio.run(main())
