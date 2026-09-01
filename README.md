# MobileAgentMCP

An MCP server that lets an agent drive a real Android phone — read the screen as
**structured UI elements**, tap, swipe, type, and extract clean typed fields from
known app screens.

Built and verified against a **realme narzo 50 Pro 5G (RMX3395, Android 14)**
driving **Instagram 440.1.0.46.86**, unrooted.

---

## Why structured UI, not screenshots

A raw uiautomator hierarchy for one Instagram Reel is **~70 KB of XML**. The
compact element form this server returns is typically **1–3 KB** for the same
screen, and it is machine-readable rather than requiring vision.

Screenshots are still available, but `screenshot` returns a **file path**, not
inline image data, so it can never silently blow up an agent's context.

```jsonc
// ui_dump output, trimmed
{
  "package": "com.instagram.android",
  "screen": "reels_viewer",
  "app_version": "440.1.0.46.86",
  "dump_ms": 358,
  "total_elements": 39,
  "elements": [
    {"i": 12, "id": "clips_author_username", "text": "ally.verma_", "c": [402,1964], "f": "C"},
    {"i": 18, "id": "like_count", "text": "The like number is 9732. View likes.", "c": [990,1041], "f": "C"}
  ]
}
```

`i` is the index used by `tap(i=…)`. `c` is the tap centre. `f` flags:
`C`=clickable, `S`=scrollable, `*`=selected.

---

## Install

```bash
pip install -r requirements.txt
```

Requires `adb` on PATH or at `ADB_PATH`. Enable USB debugging on the phone.

### Register with Claude Code

`.mcp.json` in the repo root already does this. From the repo:

```bash
claude mcp add mobileagent -- python -m mobileagent.server
```

Or rely on the checked-in `.mcp.json` when Claude Code opens this directory.

---

## Tools

**Device** — `devices`, `device_info`, `foreground_app`, `launch_app`, `list_apps`

**Reading the screen** — `ui_dump`, `find_element`, `extract_fields`, `screenshot`

**Acting** — `tap`, `swipe`, `text_input`, `press_key`

**Registry / maintenance** — `check_drift`, `record_baseline`, `registry_info`

**App-specific** — `reset_reels_feed`

---

## Selector registry and drift detection

App UIs change without warning. Selectors live as **data** in
`src/mobileagent/selectors/<app>.json`, pinned to the app version they were
verified against.

```bash
# after an app update, or when extraction starts returning nulls
check_drift()      # -> exactly which resource-ids vanished or appeared
record_baseline()  # -> pin the new version once you have re-verified
```

Three rules the registry enforces, each learned the hard way:

1. **Values often sit on anonymous child nodes**, not on the resource-id that
   names them. Instagram's caption lives under `clips_caption_component` and the
   audio track under `clips_author_info_component`;
   `media_album_art_button` merely carries the literal string `"Audio"`.
   Every element therefore reports its `anchor` = nearest ancestor id.

2. **Intermittent anchors are marked `optional`** so they never read as drift.
   Instagram's `scrubber` appears in only ~29 % of dumps; treating it as
   required makes most dumps look broken and trains you to ignore the warning
   that matters.

3. **Fail loud.** A missing anchor returns `null` and appears in `_unavailable`.
   It never falls back to a guess — a plausible wrong value is worse than a gap.

### Known app issues

Encoded in the registry so they aren't misdiagnosed as drift:

| Issue | Behaviour |
|---|---|
| `reels_overlay_missing` | The first reel after entering the Reels tab often renders with **no engagement overlay** — counts, caption and audio absent while username and video are fine. An Instagram bug. `extract_fields` detects it and returns `data_warning` with recovery steps; do **not** re-baseline on it. Recover with `swipe(direction="up")` or `reset_reels_feed()`. |

---

## Backends

| Phase | Backend | State |
|---|---|---|
| 1 | `adb` + `uiautomator2` from the host | **current** |
| 2 | on-device AccessibilityService app | planned |

Phase 2 will sit behind the **same tool surface**, so agent-side code does not
change.

> **They cannot run at the same time.** `uiautomator2` *is* a `UiAutomation`,
> which is itself a special AccessibilityService, and Android permits only one.
> Phase 2 is a swap, not a merge.

---

## Platform notes

Not every target needs device automation, and two have sanctioned APIs that are
strictly better than driving a phone:

| Target | Recommended route |
|---|---|
| **Reddit** | Official API — covers the authenticated home feed. Don't scrape. |
| **Google** | Custom Search JSON API + Programmable Search Engine. |
| **Instagram** | No sanctioned path; device automation is the route. |
| **X / Twitter** | API is paid and gated; scraping is aggressively detected. |

Automated collection generally breaches these platforms' terms regardless of
transport — wrapping it in MCP changes the convenience, not the permission.
Use throwaway accounts and keep volume human-plausible.

**Out of scope by design:** defeating consent dialogs, CAPTCHA handling,
device-identity spoofing, root exploits, and any anti-bot evasion. Android's
security model is treated as a boundary, not an obstacle. In particular,
MediaProjection consent is **one tap per capture session** and cannot legitimately
be bypassed — not even by a device-owner or privileged app.

---

## Device setup (ColorOS / realme)

ColorOS kills background services aggressively. For anything long-running:

```
Settings > Apps > App management > <app> > Battery usage
  -> Allow background activity
  -> Allow auto launch
```

plus `adb shell cmd deviceidle whitelist +<package>`. Without the **UI** toggle
the ADB whitelist alone is not enough — services die within seconds.
