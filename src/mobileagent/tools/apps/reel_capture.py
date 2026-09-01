"""Segmented reel recording: one consent, one MP4 per reel.

MediaProjection consent is per SESSION and cannot be legitimately skipped, so
the design gives the caller exactly ONE consent tap per run and then records
unattended. The on-device SegmentedCaptureService keeps that one projection
alive and, on a broadcast "cut", finalizes the current file and opens a fresh
one - so each reel lands as its own MP4 with no ffmpeg or post-processing.

Flow:
    ig_capture_start()                 -> user taps consent ONCE
    ig_capture_cut(name="reel_00")     -> begin file for this reel
    (swipe to next reel, dwell)
    ig_capture_cut(name="reel_01")     -> finalize reel_00, begin reel_01
    ...
    ig_capture_stop()                  -> finalize last, tear down
    ig_capture_pull()                  -> copy the MP4s to the host

KNOWN TUNING ISSUE: a PAUSED reel produces a near-static VirtualDisplay, so the
content-driven encoder emits few frames and the video track ends up much shorter
than the audio (observed 3-4s video vs 6.4s audio). Before dwelling on a reel,
ensure it is actually PLAYING (reels autoplay on land, but a stray tap pauses
them - check play_state via extract_fields) and dwell at least the reel's length.
"""

from __future__ import annotations

import os
import time

from ... import device as dev
from ... import state

CAP_PKG = "dev.reelcap.audiotest"
SEG_DIR = f"/sdcard/Android/data/{CAP_PKG}/files/segments"


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Start ONE segmented capture session. Launches the capture app and "
            "requests MediaProjection consent - the ONLY manual tap in the whole "
            "run. After the operator taps 'Start now', every reel is recorded to "
            "its own file with no further prompts. Poll ig_capture_status until "
            "ready before cutting."
        )
    )
    def ig_capture_start(clear_previous: bool = True) -> dict:
        if clear_previous:
            dev.shell(f"rm -f {SEG_DIR}/*.mp4 2>/dev/null")
        dev.shell(f"am force-stop {CAP_PKG}")
        time.sleep(0.5)
        dev.adb("logcat", "-c")
        dev.shell(f"am start -S -n {CAP_PKG}/.MainActivity --es mode seg")
        return {"started": True,
                "action_required": "Tap 'Start now' on the phone's capture "
                                    "prompt. This is the only manual step.",
                "next": "poll ig_capture_status until state == ready"}

    @mcp.tool(
        description="Check whether the segmented capture session is live yet "
                    "(after the consent tap). Returns state ready|waiting|failed."
    )
    def ig_capture_status() -> dict:
        log = dev.adb("logcat", "-d", "-s", "audiocap")
        if "SEGMENTED CAPTURE READY" in log:
            return {"state": "ready"}
        if "projection FAILED" in log or "projection null" in log:
            return {"state": "failed",
                    "hint": "consent denied or projection error; restart"}
        fg = dev.foreground()
        return {"state": "waiting", "foreground": fg.get("package"),
                "hint": "tap 'Start now' on the phone if the prompt is showing"}

    @mcp.tool(
        description=(
            "Cut to a new segment: finalize the current reel's file and begin "
            "recording the next under `name`. Call this once per reel, right "
            "after the reel is on screen. Names become <name>.mp4."
        )
    )
    def ig_capture_cut(name: str) -> dict:
        safe = "".join(c for c in name if c.isalnum() or c in "_-")[:40]
        dev.shell(f"am broadcast -a dev.reelcap.CUT --es name {safe}")
        return {"cut_to": safe}

    @mcp.tool(
        description="Stop the capture session: finalize the last segment and "
                    "release the projection. Call once at the end of a run."
    )
    def ig_capture_stop() -> dict:
        dev.shell("am broadcast -a dev.reelcap.STOP")
        time.sleep(1.5)
        log = dev.adb("logcat", "-d", "-s", "audiocap")
        segs = log.count("SEGMENT DONE")
        return {"stopped": True, "segments_finalized": segs}

    @mcp.tool(
        description="List the recorded segment files on the device with sizes.")
    def ig_capture_list() -> dict:
        out = dev.shell(f"ls -la {SEG_DIR} 2>/dev/null")
        files = []
        for line in out.splitlines():
            p = line.split(None, 7)
            if len(p) >= 8 and p[7].endswith(".mp4"):
                files.append({"name": p[7], "bytes": int(p[4])
                              if p[4].isdigit() else p[4]})
        return {"dir": SEG_DIR, "count": len(files), "files": files}

    @mcp.tool(
        description="Copy all recorded segment MP4s to the host artifacts "
                    "directory. Returns local paths and sizes."
    )
    def ig_capture_pull(subdir: str = "reels") -> dict:
        dest = os.path.join(state.ARTIFACT_DIR, subdir)
        os.makedirs(dest, exist_ok=True)
        listing = dev.shell(f"ls {SEG_DIR} 2>/dev/null")
        names = [l.strip() for l in listing.splitlines()
                 if l.strip().endswith(".mp4")]
        pulled = []
        for nm in names:
            local = os.path.join(dest, nm)
            try:
                dev.adb("pull", f"{SEG_DIR}/{nm}", local)
                pulled.append({"name": nm, "path": local,
                               "bytes": os.path.getsize(local)})
            except dev.DeviceError as e:
                pulled.append({"name": nm, "error": str(e)})
        return {"pulled": len(pulled), "dest": dest, "files": pulled}
