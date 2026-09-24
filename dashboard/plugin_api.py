"""Dashboard backend for the Android tab, mounted at /api/plugins/android-appliance/.

It runs androidctl and reports what androidctl reports. It keeps no state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

PLUGIN_ID = "android-appliance"
FALLBACK_PATH = "/run/current-system/sw/bin/androidctl"

# Start and restart return at once; the page polls the status.
ACTIONS: Dict[str, List[str]] = {
    "start": ["start", "--no-wait"],
    "suspend": ["suspend"],
    "resume": ["resume"],
    "stop": ["stop"],
    "restart": ["restart", "--no-wait"],
}


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        entry = ((load_config() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
        return entry.get("settings") or {}
    except Exception:
        return {}


def _androidctl() -> Optional[str]:
    for candidate in (
        _settings().get("androidctl"),
        os.environ.get("ANDROIDCTL"),
        shutil.which("androidctl"),
        FALLBACK_PATH,
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run(args: List[str], timeout: int) -> Dict[str, Any]:
    exe = _androidctl()
    if exe is None:
        return {"ok": False, "error": "androidctl is not installed on this host"}
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"androidctl {args[0]} timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or f"androidctl exited with {proc.returncode}"}
    return {"ok": True, "output": proc.stdout.strip()}


@router.get("/status")
def status() -> Dict[str, Any]:
    result = _run(["status"], timeout=30)
    if result["ok"]:
        result.update(part.split("=", 1) for part in result.pop("output").split() if "=" in part)
    display = _settings().get("display_url")
    if not display:
        shown = _run(["display"], timeout=10)
        display = shown.get("output") if shown["ok"] else None
    result["display_url"] = display
    return result


@router.post("/actions/{name}")
def action(name: str) -> Dict[str, Any]:
    if name not in ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown action {name}")
    return _run(ACTIONS[name], timeout=300)
