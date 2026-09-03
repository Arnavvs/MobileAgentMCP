---
name: feed-shaper
description: Reshape a real X/Twitter feed on a connected Android phone toward a topic and away from another - search, dwell, like, follow, unfollow, and measure the change with before/after snapshots. Use when asked to change, retrain, clean up or steer a feed ("make my feed football", "get politics out of my timeline", "measure whether the feed changed"), or to read what is on screen in an X feed right now.
---

# Feed shaper

Drives X on a physical Android device through `MobileAgentMCP`, using only the
account's own controls, and measures what changed.

Repo: `C:\Users\HP\OneDrive\Desktop\Dev\MobileAgentMCP`
Run everything from that directory. `sys.path` needs `src` on it, which
`tools/xfeed.py` does for you.

## Before anything

```powershell
adb devices
```

Prefer the USB serial over the wireless one - a screen read is ~0.2 s on USB
against ~0.5 s wireless, and every action is dumps. **This particular cable is
flaky**: it has dropped mid-run repeatedly (`device offline`, exit 137). If a
run dies that way, `adb connect 192.168.1.5:5555` and continue on wireless
rather than restarting the campaign.

PowerShell has no `&&`. Use separate lines.

## The method that works

Measured 2026-09-03 on a live account: For-you went 0% -> 22% football and
35% -> 17% politics in 19 minutes, with 1 of 17 authors surviving. A second
run reproduced it. The route is the account owner's own, not a control-panel
theory:

1. **Unfollow the unwanted topic's accounts. This is the strongest lever, and
   it is not optional.** Follows are the IN-NETWORK candidate source (Thunder
   serves posts from accounts you follow, directly, unranked) - so no amount of
   out-of-network dwelling outweighs a follow graph full of the old topic.
   Measured 2026-09-03: two Bollywood campaigns barely moved a football-heavy
   feed (bollywood 0% -> 17%), and then unfollowing 11 football accounts in
   61 s moved it to **bollywood 36%, football 9%** on its own.
   `following_list` then `unfollow`; 34 follows enumerate in ~30 s.
   Note this cuts BOTH ways: every account a campaign follows becomes tomorrow's
   in-network noise. Unfollow what the last campaign added before steering
   somewhere new.
2. **Pick 3-4 CURRENT keywords.** Web-search first; a stale keyword returns a
   stale timeline and teaches the ranker nothing. Real storylines beat generic
   terms ("Alvarez Atletico Madrid" over "football").
3. **Per keyword**: search it, then OPEN each matching result - read its
   replies, like it, come back (`engage_post`). Do not merely scroll past a
   relevant post: opening it sends a click, real dwell and a favorite where a
   scroll-past sends nothing, and click/dwell/favorite are three separately
   scored actions. Follow one or two matching accounts.
4. **After each keyword**: return to For you and work the feed the same way -
   OPEN anything relevant, scroll fast past the rest. The asymmetry is the
   signal.
5. **Snapshot before and after** and compare.

`feed/campaign.py` does 2-5:

```powershell
python -c "import sys; sys.path.insert(0,'src'); from mobileagent.feed import campaign as cp; print(cp.run(['keyword one','keyword two'], topic='football', avoid='politics', apply=True, serial='USB_SERIAL'))"
```

Leave `apply=False` to see what it would do.

## Reading the screen

`feed/read.py` holds ONE persistent uiautomator2 connection - 0.21 s per read
against 2.47 s for a shell dump, and it returns the full tree where the shell
dump returns a compressed one.

```powershell
python tools/xfeed.py read --scrolls 8
```

`Reader.posts()` returns handle, text, age, ad flag and metrics per visible
post. `read.tag()` adds crude regex topics - a pre-filter, never judgement. It
called Basecamp politics (`mp\b` inside "Basecamp") and "nuclear arsenals"
football.

## Relevance, on the phone

`phone/relevance_server.py` scores posts by MEANING, in Termux on the device:
Model2Vec potion-base-8M static embeddings, 256-dim, no transformer - a
vocabulary lookup and a mean. Loads in ~46 ms, scores a screenful in ~1 ms
(~106 ms including the adb round trip).

Start it on the phone, then bridge it:

```powershell
.\tmx.ps1 -File .\phone\start_relevance.sh
```

```powershell
adb -s HIDMFQ8X894DIVLZ forward tcp:8765 tcp:8765
```

```python
from mobileagent.feed import read as rd
rd.relevance_available()                                  # False -> regex fallback
rd.best_post(posts, "bollywood film celebrity gossip")    # prefer this
rd.score_posts(posts, "bollywood film celebrity gossip")
```

It fixes exactly what the regex could not - measured on live posts:

| text | football | bollywood | politics |
|---|---|---|---|
| Nuclear-armed states ramped up arsenals | 0.038 | -0.092 | **0.150** |
| ... makers of Basecamp and HEY | 0.005 | -0.035 | -0.004 |
| Arsenal beat Chelsea 2-0 | **0.291** | 0.092 | -0.003 |
| Toxic advance booking record for Yash | -0.039 | **0.196** | -0.008 |
| Rahul Gandhi on vote chori in Lok Sabha | -0.047 | 0.092 | **0.469** |

Three things it took measurement to get right:

- **Score the TEXT only.** Gluing the handle on cost "Toxic advance booking
  record for Yash" more than half its score (0.196 -> 0.072): handles tokenise
  into subword noise and mean pooling cannot down-weight it.
- **Normalise hashtags.** "#ShahidKapoor" is one unknown word that shatters into
  meaningless pieces. Splitting camel case took that post from 0.097 to 0.303 -
  the difference between ignored and top-ranked. URLs and @mentions go too.
- **Prefer `best_post` to a threshold.** Ordering was correct in every live
  sample; the cut-off was not. Short or list-like posts score low even when on
  topic ("Directors & their Highest Grossers" scored 0.018), because mean
  pooling rewards longer, keyword-dense text.

## Hard-won rules

- **One dump path only.** uiautomator2 IS a UiAutomation and Android permits
  one. Reading through u2 while acting through shell `uiautomator dump` kills
  one of them (exit 137). `feed/x.py::_raw_xml` is now the single u2-backed
  path; do not add another.
- **Never tap by menu position.** X's post menu is surface-dependent. Match on
  the item's label, and refuse when it is absent.
- **The negative-feedback item follows the ranker.** Present on For you
  ("Not interested in Post") and search/Top ("This post's not helpful"); absent
  on search/Latest and List/Topic tabs, which are reverse-chronological.
- **Never open an ad.** A promoted post's body is not a post link - tapping one
  opened the Play Store install sheet for a crypto wallet. `open_post` skips
  ads and backs out if a tap leaves X at all.
- **Post bodies are not always long.** "2027 UCL winners" is a real post at 16
  characters; identify a body by column WIDTH plus an exclusion list, never by
  text length.
- **Bound taps to the content band** (`y` 470-2150). A post scrolled under the
  sticky header keeps its controls in the tree, so an unguarded "topmost Like"
  tap hits the header - on search that is the query box, which opens People.
- **Clickability sits on anonymous parents.** Match a label and tap its centre;
  filtering on the label node's own `clickable` flag finds nothing.
- **`on_home: false` is ambiguous** - it means the tab strip is absent, which
  covers both a scrolled feed and X not being open. Resolve the foreground
  first. Never use BACK as blind recovery; it walks out of the app.
- **Reset a scrolled list by reopening it**, not by scrolling further.
- **Go to the top before reading the feed.** `scroll_to_top` prefers the blue
  new-posts pill, which refreshes as well as scrolls. Resuming where the last
  pass stopped reads the OLD ranking - and a snapshot that does it reports the
  stale feed, hiding the change it exists to measure.
- **Tie the tap to the judgement.** Classify a post and open THAT post: pass
  `expect=<its text>` to `engage_post`. Two independent scans of the same
  screen can disagree, and a campaign targeting Bollywood opened a football
  post because nothing connected the decision to the tap.

## Cost accounting

Every entry point is instrumented. After a run:

```python
from mobileagent.feed import x as xf
xf.cost_report()      # calls, total, mean and share per action
xf.reset_timings()
```

Use the `share` column to decide what to optimise: a 3 s action that runs once
does not matter; a 0.4 s action that runs 200 times does.

## Scope

Supported controls and ordinary reading only. Likes and follows are ranking
WRITES (`favorite` and `follow_author` are scored actions), so:

- `consume()` dwells and never engages - safe as a treatment.
- `engage()` and `like()` write. Both journal to `artifacts/feed/journal.jsonl`.
- Never run a treatment against a timeline being used as a measurement
  baseline, and never mix levers if attribution matters.
- Unfollowing is effectively irreversible - re-following does not restore the
  ranker's history. Enumerate and show the list before firing unless the owner
  has said otherwise.

No fake accounts, no bot-detection evasion, no engagement the account owner
would not recognise as theirs.
