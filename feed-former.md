# Feed Former / Feed Changer — design notes

Grounded in a device walk on 2026-09-02 (realme RMX3395, wireless ADB) using the
MobileAgentMCP tools. Every control listed below was **read off the live
accessibility tree**, not recalled. Where a menu did not contain what I expected,
that is called out explicitly — those absences are the most useful part of this
document.

Scope rule carried over from `projectContext.txt`: feed shaping uses **the
account's own preference controls**. No fake engagement as camouflage, no
synthetic likes/follows to steer a ranker, no bot-detection evasion. What is
below is the supported surface, which turns out to be richer than expected on
Instagram and much thinner than expected on X.

---

## 1. Verified control inventory

### 1.1 Instagram

Deep link `instagram://settings` resolves to
`com.instagram.urlhandlers.settings.SettingsUrlHandlerActivity` — no tap-walk
through the profile tab needed.

`Settings and activity` → **What you see** section:

| Control | Kind | Notes |
|---|---|---|
| Favourites | allow-list | ranks these accounts to the top of feed |
| Muted accounts | deny-list | per-account, posts and/or stories |
| **Content preferences** | submenu | the real lever, below |
| Like and share counts | display | hides counts; changes what *we* can scrape |

`Content preferences` contents (verified):

| Control | Kind | Feed effect |
|---|---|---|
| Sensitive content | 3-state | Standard / Less / More on suggested content |
| Political content | limit/allow | political recommendations |
| **Interested** | history list | posts previously marked interested |
| **Not interested** | history list | posts previously marked not-interested |
| **Specific words and phrases** | keyword mute | hides matching posts/comments |
| **Snooze suggested posts** | 30-day timer | "Hide suggested posts in feed for 30 days" |
| **Reset suggested content** | destructive | clears the recommendation profile |
| Hide all activity bubbles | display | Feed + Reels chrome |
| Hide instants in inbox | display | inbox only |

Per-item, from the reel overflow already wired into `ig_about_reel`:
`Save`, `Playback`, `Why you're seeing this post`, `Interested`,
`Not interested`, `Report`.

> Instagram is the only one of the three that exposes **both** a per-item signal
> (Interested / Not interested) **and** a global reset. That combination is what
> makes a controlled experiment possible — see §2.1C.

### 1.2 Reddit

Two surfaces behave completely differently, and this matters for tooling:

**Home feed (`launcher.default`)** — each post collapses to a *single composite
accessibility node* with no resource-id and no child action buttons:

```
"From IndianStockMarket, Posted 1 day ago, How f***ed I am, Image"
"From CoinDCXTrading, Promoted post, Big earnings can bring big m..."
"From scienceisdope, Posted 3 hours ago, Mercury is n..."
```

Searching the tree for `more options` / `More` / `overflow` / `options` returns
**zero** matches on this screen. There is no per-post feed control reachable
from the home feed via the accessibility tree.

Two consequences:
- Any Reddit feed-shaping action must first **open the post**, act there, then
  come back. Budget the extra navigation.
- `Promoted post` appears inside the composite label, so **ad detection is free**
  on Reddit — wire it straight into `item_observations.is_ad`. Neither Instagram
  nor X hands us that so cheaply.

**Post detail (`FbpActivity`, the full-bleed player)** — overflow verified:

`Copy link`, `Share`, `Save`, `Report`, `Block account`, `Hide`, `Follow post`,
`Award this post`, `Download`.

Feed-relevant subset: **Hide** (per-post negative signal), **Block account**
(hard deny-list), **Follow post** (positive). Note there is **no** "show fewer
posts like this" on this surface.

Reddit's dominant lever is therefore *structural, not per-post*: join/leave
subreddits, mute subreddits, and custom feeds — reachable from the community
menu (`Open community menu`, top-left of the home feed).

### 1.3 X / Twitter 12.21.1-prod.05

`twitter://settings` deep-links to Settings correctly.

**Post options** (labelled `Post options`, *not* "More" — this cost me two failed
lookups). Sampled across three separate recommended posts:

```
Follow @silversprinq | Add/remove from Lists | Mute @silversprinq
Block @silversprinq  | Report post | Request Community Note | View Hidden Replies
```

**There is no "Not interested in this post".** All three sampled posts were from
*unfollowed* accounts (the menu offers `Follow @…`), i.e. algorithmic
recommendations — exactly where a "not interested" control would live if it
existed. It does not, in this build. I had assumed otherwise from memory; the
device says no.

`Privacy and safety` → `Content you see` is nearly empty:

```
Explore Settings | Sensitive media
```

No Topics, no Interests, no per-category controls — those have been removed from
the mobile client. `Privacy and safety` → `Mute and block`:

```
Blocked accounts | Muted accounts | Muted words | Muted notifications
```

`Muted words` → `Add muted words` / `Add muted word`.

So X's entire supported feed-shaping surface is:
**Mute (account) · Block (account) · Muted words (keyword) · Lists (curation)**
— plus the For you / Following tab toggle. That is it.

---

## 2. What "feed former" should actually be

Given the above, one generic cross-app "feed changer" is the wrong abstraction —
the three apps do not share a control vocabulary. What they *do* share is a
smaller set of primitives. Model the tool surface on the primitives:

| Primitive | Instagram | Reddit | X |
|---|---|---|---|
| `feed_mute_keyword` | Specific words and phrases | (none) | Muted words |
| `feed_deny_account` | Muted accounts | Block account | Mute / Block |
| `feed_allow_account` | Favourites | Follow post / join sub | Lists |
| `feed_signal_item(+/-)` | Interested / Not interested | Hide (− only) | **(none)** |
| `feed_reset` | Reset suggested content | (none) | (none) |
| `feed_snapshot` | feed collector | `collect_feed` | `x_collect_timeline` |
| `feed_explain_item` | Why you're seeing this post | (none) | (none) |

Cells marked `(none)` are real gaps, not missing implementation. A tool that
pretends to offer `feed_signal_item` on X would have to fake it — don't build it.

### 2.1 Three concrete builds, in priority order

**A. `feed_snapshot` + drift measurement (build first, zero risk).**
Read-only. Sample each app's feed on a schedule with the existing collectors,
write to `item_observations` with `source='feed'`, and compute composition over
time: share of promoted posts, share by author, topic mix, repeat rate. This
needs no new control surface at all and it is the *measuring instrument* every
other idea depends on. Without it, any feed change is unfalsifiable.

Cheap wins already available:
- Reddit `Promoted post` → `is_ad` (free, from the composite label).
- X `Impressions` is exposed per post (`10.6K`, `809K` seen in the dump) — a
  reach signal Instagram never gives us.
- Instagram `Why you're seeing this post` → per-item attribution, the only
  ground truth any of the three offers about *why* something was ranked in.

**B. `feed_curate` — the deny/allow list manager (build second).**
Declarative: keep a YAML/JSON policy per app (`mute_keywords`, `deny_accounts`,
`allow_accounts`), and a tool that reconciles device state to it. Idempotent,
reversible, and it maps onto controls that exist in all three apps. This is the
highest-leverage real "feed changer" because keyword muting on Instagram and X
is a genuine ranking input, not just a display filter.

Implementation note: the muted-words screens are plain list + add-button, so this
is `find_element` → `tap` → `text_input`, well within the current tool set. Store
the policy in the repo so the change is auditable and undo-able.

**C. `feed_experiment` — controlled A/B on Instagram only (build last).**
Instagram is the only app with both a per-item signal and a global reset, which
makes an actual experiment possible:

```
1. feed_reset                       (Reset suggested content)
2. feed_snapshot  n=200             baseline composition
3. apply treatment: mark k items Not interested in one topic cluster
4. wait 24h / 48h / 7d
5. feed_snapshot  n=200             measure composition delta
6. feed_reset                       return to a clean state
```

That is a legitimate, reproducible study of a recommender's response to its own
documented controls, run on your own account. It is also the most interesting
dataset in this project — almost nobody has an instrumented device that can do
step 2 and step 5 reliably enough to make the delta mean anything.

Add `Snooze suggested posts` as a second treatment arm: it is a clean 30-day
step-function intervention with a known start time.

---

## 3. Things I would not build

- **Cross-app "one feed to rule them all" mirror.** Tempting, but it needs
  continuous background collection on all three, and the value is unclear versus
  the rate-limit budget it burns.
- **Anything that writes engagement to shape ranking** (auto-liking a topic to
  pull the feed toward it). This is fake engagement and out of scope by the
  project's own rules — and it would also corrupt the measurement in §2.1C, since
  you could no longer separate the treatment from the noise you injected.
- **`feed_signal_item` on X.** No supported control exists. Muting the *author*
  is the honest substitute, and it is a much blunter instrument — say so in the
  tool description rather than papering over it.

---

## 4. Open questions for the next pass

1. Instagram `Interested` / `Not interested` are shown as **history lists** in
   Content preferences. If they are readable, they are a free labelled dataset of
   past signals. Not yet dumped — worth one exploration.
2. `Why you're seeing this post` (Instagram) — surfaced in the `ig_about_reel`
   menu but never opened. Highest-value unexplored screen in the project.
3. Reddit custom feeds — reachable from the community menu, not yet enumerated
   (the button scrolls out of the tree, so grab it before the first swipe).
4. Whether Instagram's keyword mute affects *ranking* or only *display*. The
   §2.1C protocol can answer this.
