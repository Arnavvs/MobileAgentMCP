"""Device connection and low-level ADB helpers.

One shared uiautomator2 connection is reused across tool calls; reconnecting per
call costs ~1.4 s and the agent may make dozens of calls per task.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

ADB_CANDIDATES = [
    os.environ.get("ADB_PATH", ""),
    r"C:\Users\HP\AppData\Local\Android\Sdk\platform-tools\adb.exe",
    "adb",
]

DEFAULT_SERIAL = os.environ.get("MOBILEAGENT_SERIAL", "")


def _find_adb() -> str:
    for c in ADB_CANDIDATES:
        if not c:
            continue
        if os.path.isfile(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    raise RuntimeError(
        "adb not found. Set ADB_PATH or put adb on PATH."
    )


ADB = _find_adb()


class DeviceError(RuntimeError):
    pass


def adb(*args: str, serial: str = "", timeout: int = 60) -> str:
    """Run an adb command and return stdout."""
    cmd = [ADB]
    s = serial or DEFAULT_SERIAL
    if s:
        cmd += ["-s", s]
    cmd += list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise DeviceError(
            f"adb {' '.join(args)} failed ({p.returncode}): "
            f"{(p.stderr or p.stdout).strip()[:400]}"
        )
    return p.stdout


def shell(cmd: str, serial: str = "", timeout: int = 60) -> str:
    return adb("shell", cmd, serial=serial, timeout=timeout)


def list_devices() -> list[dict]:
    out = adb("devices", "-l")
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        rows.append({
            "serial": parts[0],
            "state": parts[1] if len(parts) > 1 else "?",
            "detail": " ".join(parts[2:]),
        })
    return rows


@dataclass
class DeviceInfo:
    serial: str
    model: str
    android: str
    sdk: str
    build: str
    screen: str
    density: str
    battery: str


def device_info(serial: str = "") -> DeviceInfo:
    props = shell(
        "getprop ro.product.model; getprop ro.build.version.release; "
        "getprop ro.build.version.sdk; getprop ro.build.display.id",
        serial=serial,
    ).strip().splitlines()
    props += [""] * (4 - len(props))
    size = shell("wm size", serial=serial).strip().split(":")[-1].strip()
    dens = shell("wm density", serial=serial).strip().split(":")[-1].strip()
    batt = ""
    try:
        for line in shell("dumpsys battery", serial=serial).splitlines():
            if "level:" in line:
                batt = line.split(":")[-1].strip()
                break
    except DeviceError:
        pass
    devs = list_devices()
    return DeviceInfo(
        serial=serial or (devs[0]["serial"] if devs else ""),
        model=props[0], android=props[1], sdk=props[2], build=props[3],
        screen=size, density=dens, battery=batt,
    )


def app_version(package: str, serial: str = "") -> Optional[str]:
    """versionName of an installed package, or None."""
    try:
        out = shell(f"dumpsys package {package} | grep -m1 versionName",
                    serial=serial)
    except DeviceError:
        return None
    for line in out.splitlines():
        if "versionName=" in line:
            return line.split("versionName=")[-1].strip()
    return None


def foreground(serial: str = "") -> dict:
    """Currently resumed package/activity."""
    try:
        out = shell(
            "dumpsys activity activities | grep -m1 topResumedActivity",
            serial=serial,
        )
    except DeviceError:
        return {"package": None, "activity": None}
    # ...ActivityRecord{hash u0 pkg/activity taskId}
    for tok in out.split():
        if "/" in tok and "." in tok:
            pkg, _, act = tok.partition("/")
            if act.startswith("."):
                act = pkg + act
            return {"package": pkg, "activity": act}
    return {"package": None, "activity": None}


# --- uiautomator2 -----------------------------------------------------------

_u2_conn = None
_u2_serial = None


def u2(serial: str = ""):
    """Shared uiautomator2 connection.

    NOTE: uiautomator2 is itself a UiAutomation (a special AccessibilityService)
    and CANNOT coexist with a custom AccessibilityService. When the ReelNode app
    lands in phase 2, this backend gets swapped out, not run alongside.
    """
    global _u2_conn, _u2_serial
    s = serial or DEFAULT_SERIAL
    if _u2_conn is not None and _u2_serial == s:
        return _u2_conn
    import uiautomator2
    _u2_conn = uiautomator2.connect(s) if s else uiautomator2.connect()
    _u2_serial = s
    return _u2_conn
