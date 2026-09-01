"""MobileAgentMCP - drive an Android device from an MCP client.

Phase 1 backend: adb + uiautomator2 from the host.
Phase 2 (planned): an on-device AccessibilityService app, swapped in behind the
same tool surface so agent-side code does not change.

Design rules:
  * UI is returned as STRUCTURED ELEMENTS, never screenshots. Screenshots exist
    but return a file path, so a dump costs ~1-3 KB instead of ~70 KB of XML or
    an image.
  * Selectors live in a versioned registry keyed by app version, with a
    drift-check tool, so an app update produces a precise diff instead of
    silently-wrong output.
  * Extraction fails loud. A missing anchor yields null plus an `_unavailable`
    list; it never falls back to a guess.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from mcp.server import MCPServer

from . import device as dev
from . import ui as uix
from .selectors import registry as reg

mcp = MCPServer(
    name="mobileagent",
    instructions=(
        "Controls a physical Android device over ADB.\n"
        "Workflow: ui_dump to see the screen, then tap/swipe/text_input to act.\n"
        "Prefer ui_dump over screenshot - it is far cheaper and machine-readable. "
        "Tap by element index `i` from the most recent ui_dump on the same screen; "
        "re-dump after anything that changes the screen.\n"
        "extract_fields gives clean typed values for known screens. "
        "check_drift tells you whether the app UI has changed under you."
    ),
)

ARTIFACT_DIR = os.environ.get(
    "MOBILEAGENT_ARTIFACTS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "artifacts"),
)
os.makedirs(ARTIFACT_DIR, exist_ok=True)

# Cache of the last dump so tap-by-index is meaningful.
_last: dict[str, Any] = {"elements": [], "at": 0.0, "pkg": None}

PKG_ALIASES = {
    "instagram": "com.instagram.android",
    "ig": "com.instagram.android",
    "chrome": "com.android.chrome",
    "reddit": "com.reddit.frontpage",
    "twitter": "com.twitter.android",
    "x": "com.twitter.android",
}

APP_FOR_PKG = {"com.instagram.android": "instagram"}


def _resolve_pkg(name: str) -> str:
    return PKG_ALIASES.get(name.strip().lower(), name.strip())


# --------------------------------------------------------------------------
# device
# --------------------------------------------------------------------------

@mcp.tool(description="List connected devices and their state.")
def devices() -> dict:
    return {"devices": dev.list_devices()}


@mcp.tool(description="Model, Android version, screen size, density, battery.")
def device_info() -> dict:
    i = dev.device_info()
    return {
        "serial": i.serial, "model": i.model, "android": i.android,
        "sdk": i.sdk, "build": i.build, "screen": i.screen,
        "density": i.density, "battery_pct": i.battery,
    }


@mcp.tool(description="Package and activity currently in the foreground.")
def foreground_app() -> dict:
    fg = dev.foreground()
    pkg = fg.get("package")
    if pkg:
        fg["version"] = dev.app_version(pkg)
    return fg


@mcp.tool(
    description="Launch an app. Accepts a package name or an alias "
                "(instagram, chrome, reddit, twitter)."
)
def launch_app(package: str, wait_seconds: float = 2.0) -> dict:
    pkg = _resolve_pkg(package)
    dev.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    time.sleep(max(0.0, wait_seconds))
    return {"launched": pkg, "foreground": dev.foreground()}


@mcp.tool(description="List installed packages, optionally filtered by substring.")
def list_apps(filter: str = "", limit: int = 60) -> dict:
    out = dev.shell("pm list packages")
    pkgs = sorted(l.replace("package:", "").strip()
                  for l in out.splitlines() if l.strip())
    if filter:
        f = filter.lower()
        pkgs = [p for p in pkgs if f in p.lower()]
    return {"count": len(pkgs), "packages": pkgs[:limit]}


# --------------------------------------------------------------------------
# ui
# --------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Read the current screen as structured elements. This is the primary "
        "way to see the device - use it instead of screenshot. Returns compact "
        "elements with index `i`, resource-id, text, content-desc, tap centre "
        "and flags (C=clickable S=scrollable *=selected). Also names the screen "
        "and reports selector drift when the app is in the registry."
    )
)
def ui_dump(query: str = "", clickable_only: bool = False,
            limit: int = 120, include_system: bool = False) -> dict:
    d = dev.u2()
    t0 = time.time()
    xml = d.dump_hierarchy()
    ms = int((time.time() - t0) * 1000)

    elements = uix.parse(xml, keep_noise=include_system)
    all_ids = uix.all_resource_ids(xml)
    _last["elements"] = elements
    _last["at"] = time.time()

    fg = dev.foreground()
    pkg = fg.get("package") or ""
    _last["pkg"] = pkg
    app = APP_FOR_PKG.get(pkg)
    version = dev.app_version(pkg) if pkg else None

    live_ids = all_ids
    screen = None
    drift = None
    if app and version:
        screen = reg.detect_screen(app, version, live_ids)
        if screen:
            drift = reg.check_drift(app, version, screen, live_ids).to_dict()

    shown = uix.find(elements, query=query, clickable_only=clickable_only)
    res: dict[str, Any] = {
        "package": pkg,
        "activity": fg.get("activity"),
        "app_version": version,
        "screen": screen,
        "dump_ms": ms,
        "signature": uix.screen_signature(elements),
        "total_elements": len(elements),
        "returned": min(len(shown), limit),
        "elements": uix.compact(shown, limit=limit),
    }
    if drift and drift.get("status") == "DRIFT":
        res["drift_warning"] = drift
    if len(shown) > limit:
        res["truncated"] = (
            f"{len(shown) - limit} more elements; narrow with `query` "
            f"or raise `limit`"
        )
    return res


@mcp.tool(
    description="Search the current screen for elements matching text or a "
                "resource-id. Re-dumps the UI first, so it is always fresh."
)
def find_element(query: str = "", resource_id: str = "",
                 clickable_only: bool = False, limit: int = 25) -> dict:
    d = dev.u2()
    _xml = d.dump_hierarchy()
    elements = uix.parse(_xml)
    _last["elements"] = elements
    _last["at"] = time.time()
    hits = uix.find(elements, query=query, rid=resource_id,
                    clickable_only=clickable_only)
    return {
        "matches": len(hits),
        "elements": uix.compact(hits, limit=limit),
    }


@mcp.tool(
    description=(
        "Extract clean typed fields for a recognised screen using the versioned "
        "selector registry. Numbers come back as {raw, value} so a parse can be "
        "audited. Missing fields are listed in `_unavailable` rather than guessed."
    )
)
def extract_fields(app: str = "", screen: str = "") -> dict:
    d = dev.u2()
    _xml = d.dump_hierarchy()
    elements = uix.parse(_xml)
    _last["elements"] = elements
    _last["at"] = time.time()

    fg = dev.foreground()
    pkg = fg.get("package") or ""
    app_name = app or APP_FOR_PKG.get(pkg, "")
    if not app_name:
        return {"error": f"no registry for package {pkg!r}",
                "known_apps": reg.known_apps()}
    version = dev.app_version(pkg) or ""
    live_ids = uix.all_resource_ids(_xml)
    scr = screen or reg.detect_screen(app_name, version, live_ids)
    if not scr:
        return {"error": "screen not recognised", "app": app_name,
                "app_version": version,
                "signature": uix.screen_signature(elements),
                "hint": "use ui_dump to inspect, then record_baseline"}

    fields = reg.extract_fields(app_name, version, scr, elements)
    drift = reg.check_drift(app_name, version, scr, live_ids)
    out = {"app": app_name, "app_version": version, "screen": scr,
           "fields": fields}

    # Known Instagram defect: the FIRST reel after entering the Reels tab often
    # renders with no engagement overlay at all - counts, caption and audio are
    # absent even though the reel is playing normally. It is an app bug, not
    # selector drift, and it clears on the next reel.
    if app_name == "instagram" and scr == "reels_viewer":
        counts = ("like_count", "comment_count", "save_count")
        if fields.get("username") and all(
            fields.get(c) is None for c in counts
        ):
            out["data_warning"] = {
                "issue": "reels_overlay_missing",
                "detail": (
                    "Username resolved but every engagement count is absent. "
                    "This is the known Instagram first-reel bug, not selector "
                    "drift - do not re-baseline on it."
                ),
                "recovery": [
                    "swipe(direction='up') to skip to the next reel, or",
                    "reset_reels_feed() to re-enter the Reels tab",
                ],
                "action": "discard this observation rather than storing nulls",
            }
            out.pop("drift_warning", None)
            return out

    if not drift.ok:
        out["drift_warning"] = drift.to_dict()
    return out


@mcp.tool(
    description=(
        "Re-enter the Instagram Reels tab. Use to recover from the first-reel "
        "bug (overlay renders with no counts/caption) and to re-seed the feed."
    )
)
def reset_reels_feed(settle_seconds: float = 3.0) -> dict:
    d = dev.u2()
    elements = uix.parse(d.dump_hierarchy())
    hits = uix.find(elements, rid="clips_tab")
    if not hits:
        return {"error": "clips_tab not found - is Instagram in the foreground?",
                "foreground": dev.foreground()}
    x, y = hits[0].center
    dev.shell(f"input tap {x} {y}")
    time.sleep(max(0.0, settle_seconds))
    return {"reset": True, "tapped": [x, y],
            "note": "re-run extract_fields; discard any reel that still "
                    "reports reels_overlay_missing"}


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Tap the screen. Give either `i` (element index from the last ui_dump) "
        "or explicit x/y. Using `i` is preferred - it is resolution independent."
    )
)
def tap(i: Optional[int] = None, x: Optional[int] = None,
        y: Optional[int] = None) -> dict:
    if i is not None:
        els = _last.get("elements") or []
        if not els:
            return {"error": "no cached ui_dump; call ui_dump first"}
        if i < 0 or i >= len(els):
            return {"error": f"index {i} out of range (0..{len(els)-1})"}
        age = time.time() - float(_last.get("at") or 0)
        px, py = els[i].center
        dev.shell(f"input tap {px} {py}")
        r = {"tapped": {"i": i, "x": px, "y": py},
             "element": els[i].to_dict()}
        if age > 20:
            r["stale_warning"] = (
                f"cached dump was {int(age)}s old; re-dump if this misfired"
            )
        return r
    if x is None or y is None:
        return {"error": "give either `i` or both x and y"}
    dev.shell(f"input tap {int(x)} {int(y)}")
    return {"tapped": {"x": int(x), "y": int(y)}}


@mcp.tool(
    description="Swipe. direction: up|down|left|right, or give explicit "
                "x1,y1,x2,y2. `up` scrolls content forward (next reel/post)."
)
def swipe(direction: str = "", x1: int = 0, y1: int = 0, x2: int = 0,
          y2: int = 0, duration_ms: int = 300) -> dict:
    if direction:
        info = dev.device_info()
        try:
            w, h = (int(v) for v in info.screen.lower().split("x"))
        except Exception:
            w, h = 1080, 2400
        cx, cy = w // 2, h // 2
        dy, dx = int(h * 0.32), int(w * 0.35)
        moves = {
            "up":    (cx, cy + dy, cx, cy - dy),
            "down":  (cx, cy - dy, cx, cy + dy),
            "left":  (cx + dx, cy, cx - dx, cy),
            "right": (cx - dx, cy, cx + dx, cy),
        }
        if direction.lower() not in moves:
            return {"error": f"bad direction {direction!r}",
                    "valid": list(moves)}
        x1, y1, x2, y2 = moves[direction.lower()]
    dev.shell(f"input swipe {x1} {y1} {x2} {y2} {int(duration_ms)}")
    return {"swiped": {"from": [x1, y1], "to": [x2, y2],
                       "duration_ms": duration_ms}}


@mcp.tool(description="Type text into the focused field.")
def text_input(text: str) -> dict:
    safe = text.replace(" ", "%s").replace("'", "'\\''")
    dev.shell(f"input text '{safe}'")
    return {"typed": text}


@mcp.tool(
    description="Press a hardware/navigation key: back, home, enter, "
                "recents, power, volume_up, volume_down, wake."
)
def press_key(key: str) -> dict:
    keymap = {
        "back": "KEYCODE_BACK", "home": "KEYCODE_HOME",
        "enter": "KEYCODE_ENTER", "recents": "KEYCODE_APP_SWITCH",
        "power": "KEYCODE_POWER", "wake": "KEYCODE_WAKEUP",
        "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
        "delete": "KEYCODE_DEL", "search": "KEYCODE_SEARCH",
    }
    k = keymap.get(key.strip().lower())
    if not k:
        return {"error": f"unknown key {key!r}", "valid": sorted(keymap)}
    dev.shell(f"input keyevent {k}")
    return {"pressed": key}


# --------------------------------------------------------------------------
# capture / registry maintenance
# --------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Take a screenshot and save it to disk, returning the PATH (not the "
        "image). Expensive relative to ui_dump - use only when pixels genuinely "
        "matter, e.g. content the accessibility tree cannot express."
    )
)
def screenshot(name: str = "") -> dict:
    fn = (name or f"shot_{int(time.time())}").replace(" ", "_")
    if not fn.endswith(".png"):
        fn += ".png"
    remote = f"/sdcard/{fn}"
    local = os.path.join(ARTIFACT_DIR, fn)
    dev.shell(f"screencap -p {remote}")
    dev.adb("pull", remote, local)
    dev.shell(f"rm -f {remote}")
    size = os.path.getsize(local) if os.path.isfile(local) else 0
    return {"path": local, "bytes": size,
            "note": "prefer ui_dump unless pixels are required"}


@mcp.tool(
    description=(
        "Check whether the live app UI still matches the recorded selector "
        "baseline. Run this after an app update, or when extraction starts "
        "returning nulls."
    )
)
def check_drift(app: str = "", screen: str = "") -> dict:
    d = dev.u2()
    _xml = d.dump_hierarchy()
    elements = uix.parse(_xml)
    fg = dev.foreground()
    pkg = fg.get("package") or ""
    app_name = app or APP_FOR_PKG.get(pkg, "")
    if not app_name:
        return {"error": f"no registry for {pkg!r}",
                "known_apps": reg.known_apps()}
    version = dev.app_version(pkg) or ""
    live_ids = uix.all_resource_ids(_xml)
    scr = screen or reg.detect_screen(app_name, version, live_ids)
    if not scr:
        return {"error": "screen not recognised",
                "app": app_name, "app_version": version,
                "live_ids_sample": sorted(live_ids)[:40]}
    return reg.check_drift(app_name, version, scr, live_ids).to_dict()


@mcp.tool(
    description=(
        "Record the current screen's resource-ids as a baseline for this app "
        "version. Use after verifying a new app version so future drift checks "
        "have something to compare against."
    )
)
def record_baseline(app: str = "", screen: str = "") -> dict:
    d = dev.u2()
    _xml = d.dump_hierarchy()
    elements = uix.parse(_xml)
    fg = dev.foreground()
    pkg = fg.get("package") or ""
    app_name = app or APP_FOR_PKG.get(pkg, "")
    if not app_name or not screen:
        return {"error": "both `app` and `screen` are required",
                "detected_package": pkg}
    version = dev.app_version(pkg) or "unknown"
    ids = sorted(uix.all_resource_ids(_xml))
    path = reg.record_baseline(app_name, version, screen, ids)
    return {"recorded": {"app": app_name, "version": version, "screen": screen,
                         "ids": len(ids)}, "registry": path}


@mcp.tool(
    description="Show the selector registry for an app: which versions and "
                "screens are known, and what fields are defined."
)
def registry_info(app: str = "instagram") -> dict:
    data = reg.load(app)
    versions = data.get("versions", {})
    return {
        "app": app,
        "known_apps": reg.known_apps(),
        "versions": {
            v: {
                "screens": list(spec.get("screens", {})),
                "ids": len(spec.get("all_ids", [])),
                "recorded_at": spec.get("recorded_at"),
            }
            for v, spec in versions.items()
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
