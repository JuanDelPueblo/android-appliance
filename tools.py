"""Tool schemas and handlers. Each handler runs one androidctl command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

FALLBACK_PATH = "/run/current-system/sw/bin/androidctl"

# A cold boot of a fresh AVD can take several minutes.
LONG_TIMEOUT = 1200
SHORT_TIMEOUT = 60

_configured: str = ""


def configure(path: Optional[str]) -> None:
    global _configured
    _configured = path or ""


def find_androidctl() -> Optional[str]:
    """Return the androidctl path. The Hermes service PATH can be narrow, so
    fall back to the NixOS system profile."""
    for candidate in (_configured, os.environ.get("ANDROIDCTL", ""), shutil.which("androidctl"), FALLBACK_PATH):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def available() -> bool:
    return find_androidctl() is not None


def run(args: List[str], timeout: int = LONG_TIMEOUT) -> Dict[str, Any]:
    exe = find_androidctl()
    if exe is None:
        return {"ok": False, "error": "androidctl is not installed on this host"}
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"androidctl {args[0]} timed out after {timeout}s"}
    result: Dict[str, Any] = {"ok": proc.returncode == 0, "output": proc.stdout.strip()}
    if proc.returncode != 0:
        result["error"] = proc.stderr.strip() or f"androidctl exited with {proc.returncode}"
    return result


def parse_status(text: str) -> Dict[str, str]:
    """Parse ``state=running boot_completed=1 idle_seconds=5``."""
    return dict(part.split("=", 1) for part in text.split() if "=" in part)


def _reply(result: Dict[str, Any]) -> str:
    return json.dumps(result)


def _schema(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None):
    params: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


def _int(description: str) -> Dict[str, Any]:
    return {"type": "integer", "description": description}


_WAKE = " Starts or resumes Android first when it is stopped or suspended."

STATUS = _schema(
    "android_status",
    "Report the Android appliance state (stopped, starting, running, suspended, stopping), whether "
    "Android is ready (boot_completed=1) and the idle time. Never starts or wakes Android. For "
    "operations without a tool, run `androidctl` in the terminal; load skill "
    "android-appliance:android for the full command list.",
    {},
)
START = _schema(
    "android_start",
    "Start Android (Quick Boot restore when possible) or resume it, and wait until it is usable.",
    {},
)
STOP = _schema(
    "android_stop",
    "Shut Android down and save its Quick Boot state. Frees the host RAM. Idle Android stops by "
    "itself, so call this only when asked.",
    {},
)
SCREENSHOT = _schema(
    "android_screenshot",
    "Capture the Android screen to a PNG file and return its path. Inspect the image with "
    "vision_analyze." + _WAKE,
    {"path": {"type": "string", "description": "Optional output file path."}},
)
UI = _schema(
    "android_ui",
    "Return the current UI hierarchy as XML (uiautomator dump), with element text, resource ids "
    "and bounds. Use the bounds to compute tap coordinates." + _WAKE,
    {},
)
TAP = _schema(
    "android_tap",
    "Tap a screen coordinate in device pixels (1080x1920 portrait)." + _WAKE,
    {"x": _int("X coordinate."), "y": _int("Y coordinate.")},
    ["x", "y"],
)
SWIPE = _schema(
    "android_swipe",
    "Swipe from one screen coordinate to another in device pixels." + _WAKE,
    {
        "x1": _int("Start X."),
        "y1": _int("Start Y."),
        "x2": _int("End X."),
        "y2": _int("End Y."),
        "duration_ms": _int("Optional swipe duration in milliseconds."),
    },
    ["x1", "y1", "x2", "y2"],
)
TEXT = _schema(
    "android_text",
    "Type text into the focused input field." + _WAKE,
    {"text": {"type": "string", "description": "Text to type."}},
    ["text"],
)
KEY = _schema(
    "android_key",
    "Send an Android key event, for example HOME, BACK, ENTER, APP_SWITCH or a numeric keycode." + _WAKE,
    {"key": {"type": "string", "description": "Key event name without the KEYCODE_ prefix, or a keycode number."}},
    ["key"],
)


def android_status(args: Dict[str, Any], **_: Any) -> str:
    result = run(["status"], timeout=SHORT_TIMEOUT)
    if result.get("ok"):
        result["status"] = parse_status(result["output"])
    return _reply(result)


def android_start(args: Dict[str, Any], **_: Any) -> str:
    return _reply(run(["start"]))


def android_stop(args: Dict[str, Any], **_: Any) -> str:
    return _reply(run(["stop"], timeout=300))


def android_screenshot(args: Dict[str, Any], **_: Any) -> str:
    path = args.get("path")
    result = run(["screenshot", str(path)] if path else ["screenshot"])
    if result.get("ok"):
        result["path"] = result.pop("output")
    return _reply(result)


def android_ui(args: Dict[str, Any], **_: Any) -> str:
    return _reply(run(["ui"]))


def android_tap(args: Dict[str, Any], **_: Any) -> str:
    return _reply(run(["tap", str(int(args["x"])), str(int(args["y"]))]))


def android_swipe(args: Dict[str, Any], **_: Any) -> str:
    cmd = ["swipe"] + [str(int(args[k])) for k in ("x1", "y1", "x2", "y2")]
    if args.get("duration_ms") is not None:
        cmd.append(str(int(args["duration_ms"])))
    return _reply(run(cmd))


def android_text(args: Dict[str, Any], **_: Any) -> str:
    return _reply(run(["text", str(args["text"])]))


def android_key(args: Dict[str, Any], **_: Any) -> str:
    key = str(args["key"]).strip()
    if not key.isdigit() and not key.startswith("KEYCODE_"):
        key = "KEYCODE_" + key.upper()
    return _reply(run(["key", key]))


TOOLS = [
    ("android_status", STATUS, android_status, "📱"),
    ("android_start", START, android_start, "▶️"),
    ("android_stop", STOP, android_stop, "⏹️"),
    ("android_screenshot", SCREENSHOT, android_screenshot, "📸"),
    ("android_ui", UI, android_ui, "🧩"),
    ("android_tap", TAP, android_tap, "👆"),
    ("android_swipe", SWIPE, android_swipe, "👉"),
    ("android_text", TEXT, android_text, "⌨️"),
    ("android_key", KEY, android_key, "🔘"),
]
