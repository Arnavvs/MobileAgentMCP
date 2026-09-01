"""Versioned selector registry + drift detection.

App UIs change without warning. This module keeps selectors as DATA (JSON),
pinned to the app version they were verified against, so that when an update
breaks extraction you get a precise diff of which resource-ids appeared or
vanished, instead of silently-wrong output.

Two rules learned during research and enforced here:

* Selectors are anchored to a resource-id, and a value may live on an ANONYMOUS
  CHILD of that anchor. Never assume the value sits on the node whose id names
  it.
* Fail loud. If an expected anchor is missing, report it as missing rather than
  falling back to a guess - a plausible wrong value is worse than a gap.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

_NUM = re.compile(r"(\d[\d,]*)")


def _registry_path(app: str) -> str:
    return os.path.join(HERE, f"{app}.json")


def load(app: str) -> dict:
    p = _registry_path(app)
    if not os.path.isfile(p):
        return {"app": app, "versions": {}}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(app: str, data: dict) -> str:
    p = _registry_path(app)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return p


def known_apps() -> list[str]:
    return sorted(
        f[:-5] for f in os.listdir(HERE)
        if f.endswith(".json") and not f.startswith("_")
    )


def baseline_for(app: str, version: str) -> Optional[dict]:
    """Exact version match, else the most recently recorded version."""
    reg = load(app)
    versions = reg.get("versions", {})
    if version in versions:
        return versions[version]
    if not versions:
        return None
    latest = sorted(versions.keys())[-1]
    return versions[latest]


def baseline_version(app: str, version: str) -> Optional[str]:
    reg = load(app)
    versions = reg.get("versions", {})
    if version in versions:
        return version
    return sorted(versions.keys())[-1] if versions else None


@dataclass
class Drift:
    app: str
    live_version: str
    baseline_version: str
    screen: str
    missing: list[str]
    added: list[str]
    matched: int
    absent_optional: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict:
        d = {
            "app": self.app,
            "live_version": self.live_version,
            "baseline_version": self.baseline_version,
            "screen": self.screen,
            "status": "OK" if self.ok else "DRIFT",
            "matched_ids": self.matched,
            "missing_ids": self.missing,
            "new_ids": self.added[:25],
            "note": (
                "Selectors still valid."
                if self.ok else
                "Expected resource-ids are absent. Extraction for the affected "
                "fields will return null. Re-verify with tools/map_fields.py and "
                "record a new baseline via record_baseline."
            ),
        }
        if self.absent_optional:
            d["absent_optional"] = self.absent_optional
            d["absent_optional_note"] = (
                "Known-intermittent anchors, absent this dump. Not drift."
            )
        return d


def detect_screen(app: str, version: str, live_ids: set[str]) -> Optional[str]:
    """Name the current screen by matching its required ids."""
    base = baseline_for(app, version)
    if not base:
        return None
    best, best_score = None, 0
    for name, spec in base.get("screens", {}).items():
        req = set(spec.get("requires", []))
        if not req:
            continue
        hit = len(req & live_ids)
        if hit == len(req) and hit > best_score:
            best, best_score = name, hit
    return best


# Framework / system-UI ids that appear on every screen and are never app
# selectors. Excluded from `new_ids` so a drift report shows only app changes.
_SYSTEM_ID = re.compile(
    r"^(battery|clock|icon|content|container|text\d|mobile_|wifi_|statusIcons?"
    r"|status_bar|nav_bar|navigationBar|dynamic_icon|action_bar_root"
    r"|decor|android_|system_)",
)


def _is_system_id(rid: str) -> bool:
    return bool(_SYSTEM_ID.match(rid))


def check_drift(app: str, version: str, screen: str,
                live_ids: set[str]) -> Drift:
    base = baseline_for(app, version) or {}
    bver = baseline_version(app, version) or "none"
    spec = base.get("screens", {}).get(screen, {})
    fields = spec.get("fields", {})

    # Anchors marked optional are intermittent by nature, not evidence of an app
    # change. Instagram's `scrubber` is present in only ~29% of dumps; treating
    # it as required makes every other dump look like drift and trains you to
    # ignore the warning that matters.
    required_anchors = {
        f["anchor"] for f in fields.values() if not f.get("optional")
    }
    expected = set(spec.get("requires", [])) | required_anchors
    optional = {f["anchor"] for f in fields.values() if f.get("optional")}

    missing = sorted(expected - live_ids)
    added = sorted(
        i for i in (live_ids - set(base.get("all_ids", [])))
        if not _is_system_id(i)
    )
    d = Drift(
        app=app, live_version=version, baseline_version=bver, screen=screen,
        missing=missing, added=added, matched=len(expected & live_ids),
    )
    d.absent_optional = sorted(optional - live_ids)
    return d


def parse_number(raw: str) -> Optional[int]:
    """Pull an integer out of a prose accessibility label.

    Real examples from Instagram 440.1.0.46.86:
        "The like number is 65469. View likes."  -> 65469
        "Comment number is2191. View comments"   -> 2191   (note: no space)
        "Reposted 500 times"                     -> 500
    """
    if not raw:
        return None
    m = _NUM.search(raw)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_fields(app: str, version: str, screen: str,
                   elements) -> dict[str, Any]:
    """Apply the registry's field spec to a parsed element list."""
    from .. import ui as ui_mod

    base = baseline_for(app, version) or {}
    spec = base.get("screens", {}).get(screen, {})
    fields = spec.get("fields", {})
    out: dict[str, Any] = {}
    unavailable: list[str] = []

    for name, f in fields.items():
        raw = ui_mod.first_value(elements, f["anchor"],
                                 prefer=f.get("prefer", "any"))
        if raw is None:
            unavailable.append(name)
            out[name] = None
            continue
        if f.get("type") == "number":
            out[name] = {"raw": raw, "value": parse_number(raw)}
        else:
            out[name] = raw
    if unavailable:
        out["_unavailable"] = unavailable
    return out


def record_baseline(app: str, version: str, screen: str, live_ids: list[str],
                    screens: Optional[dict] = None) -> str:
    """Record/refresh a baseline for an app version."""
    reg = load(app)
    versions = reg.setdefault("versions", {})
    entry = versions.setdefault(version, {"screens": {}, "all_ids": []})
    entry["recorded_at"] = datetime.now(timezone.utc).isoformat()
    entry["all_ids"] = sorted(set(entry.get("all_ids", [])) | set(live_ids))
    if screens:
        entry["screens"].update(screens)
    elif screen and screen not in entry["screens"]:
        entry["screens"][screen] = {"requires": [], "fields": {}}
    return save(app, reg)
