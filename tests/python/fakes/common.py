"""State shared by the fake emulator, adb, Xvnc and glxinfo.

$FAKE_ANDROID is the directory of one fake device:
  config.json    scenario (see DEFAULTS)
  emulator.pid   the running fake emulator
  booted         exists once the fake guest finished booting
  device/        the guest file system for push, pull and ls
  *.jsonl        what the fakes saw, for the tests
  rtc-bye        while it exists, the fake emulator ends WebRTC streams with a bye
  rtc-eof        while it exists, it ends them without one
"""

import json
import os
import time
from pathlib import Path

DEFAULTS = {
    # normal | hang (never boots) | crash (exits during boot) | renderer | fatal
    "mode": "normal",
    "boot_delay": 0.3,
    # get-state answers "unauthorized" this long (None: never; -1: forever).
    "unauthorized_for": None,
    "ignore_kill": False,
    "ignore_term": False,
    "stop_delay": 0.05,
    "ui_failures": 0,
    # The post-boot "svc" command hangs.
    "setup_hangs": False,
    "screenshot_fails": False,
    "xvnc_fails": False,
    "glx_renderer": "Mesa Intel(R) UHD Graphics 630 (CFL GT2)",
    # The shade of the fake display: a thumbnail of another shade is another picture.
    "display": 0,
    # Seconds after an answer until the fake emulator lets a WebRTC stream go,
    # like ICE that failed (None: never).
    "rtc_release_after": None,
    # turnserver: normal | crash (exits at once)
    "turn_mode": "normal",
}


def root() -> Path:
    return Path(os.environ["FAKE_ANDROID"])


def config() -> dict:
    try:
        return {**DEFAULTS, **json.loads((root() / "config.json").read_text())}
    except FileNotFoundError:
        return dict(DEFAULTS)


def record(name: str, entry: dict) -> None:
    with open(root() / f"{name}.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time(), **entry}) + "\n")


def emulator_pid() -> int | None:
    try:
        pid = int((root() / "emulator.pid").read_text())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    return pid
