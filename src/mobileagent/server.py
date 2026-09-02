"""MobileAgentMCP - drive an Android device from an MCP client.

Phase 1 backend: adb + uiautomator2 from the host.
Phase 2 (planned): an on-device AccessibilityService app behind the SAME tool
surface, so agent-side code does not change. The two cannot run concurrently -
uiautomator2 is itself a UiAutomation, which is a special AccessibilityService,
and Android permits only one.

Design rules:
  * UI is returned as STRUCTURED ELEMENTS, never screenshots. A raw hierarchy
    for one Instagram Reel is ~70 KB of XML; the compact form is ~1-3 KB.
    `screenshot` returns a file PATH so it cannot flood an agent's context.
  * Selectors live in a versioned registry with drift detection, so an app
    update yields a precise diff instead of silently-wrong output.
  * Extraction fails loud: a missing anchor yields null plus `_unavailable`,
    never a guess.
"""

from __future__ import annotations

from mcp.server import MCPServer

from .tools import (device_tools, explore_tools, input_tools, registry_tools,
                    system_tools, thread_tools, ui_tools)
from .tools.apps import instagram as ig_tools
from .tools.apps import instagram_profile as ig_profile
from .tools.apps import instagram_comments as ig_comments
from .tools.apps import reel_capture as ig_capture
from .tools.apps import twitter as x_tools

mcp = MCPServer(
    name="mobileagent",
    instructions=(
        "Controls a physical Android device over ADB.\n"
        "Loop: ui_dump to see the screen -> tap/swipe/text_input to act -> "
        "re-dump. Tap by element index `i` from the most recent dump.\n"
        "Prefer ui_dump over screenshot: it is far cheaper and machine-readable. "
        "Use wait_for instead of blind sleeps after actions that trigger loading.\n"
        "extract_fields returns typed values for known screens; check_drift "
        "tells you whether the app UI changed under you."
    ),
)

for mod in (device_tools, ui_tools, input_tools, system_tools,
            explore_tools, thread_tools, registry_tools, ig_tools,
            ig_profile, ig_comments, ig_capture, x_tools):
    mod.register(mcp)
ig_profile.register_orchestrator(mcp)
ig_capture.register_full(mcp)
x_tools.register_nav(mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
