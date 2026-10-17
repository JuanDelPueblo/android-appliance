"""Run the daemon against the fake emulator of tests/python/fakes.

The dashboard tests use it to talk to the real session, login and static
file code. It needs the Python environment of the daemon.

    python tests/web/daemon.py <work directory> <http port>

The fake device of the run is <work>/fake: input.jsonl, rtc.jsonl and
launches.jsonl list what the fake emulator saw.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FAKES = REPO / "tests" / "python" / "fakes"

work = Path(sys.argv[1])
port = sys.argv[2]
fake = work / "fake"
bin_dir = work / "bin"
sdk = work / "sdk"
for directory in (fake / "device" / "sdcard", fake / "device" / "data" / "local" / "tmp", bin_dir, work / "data", work / "run", work / "tmp"):
    directory.mkdir(parents=True, exist_ok=True)
(sdk / "system-images" / "android-35" / "google_apis_playstore" / "x86_64").mkdir(parents=True, exist_ok=True)

programs = {"emulator": "emulator.py", "adb": "adb.py", "Xvnc": "xvnc.py", "glxinfo": "glxinfo.py", "turnserver": "turnserver.py"}
for name, script in programs.items():
    wrapper = bin_dir / name
    wrapper.write_text(f'#!/bin/sh\nFAKE_ANDROID={fake} exec {sys.executable} {FAKES / script} "$@"\n')
    wrapper.chmod(0o755)

env = {
    **os.environ,
    "PYTHONPATH": str(REPO),
    "APPLIANCE_DATA_DIR": str(work / "data"),
    "APPLIANCE_RUN_DIR": str(work / "run"),
    "APPLIANCE_TMP_DIR": str(work / "tmp"),
    "APPLIANCE_SDK_ROOT": str(sdk),
    "APPLIANCE_WEB_DIR": str(REPO / "web"),
    "APPLIANCE_HTTP_PORT": port,
    "APPLIANCE_PORT": port,
    "APPLIANCE_EMULATOR": str(bin_dir / "emulator"),
    "APPLIANCE_ADB": str(bin_dir / "adb"),
    "APPLIANCE_XVNC": str(bin_dir / "Xvnc"),
    "APPLIANCE_GLXINFO": str(bin_dir / "glxinfo"),
    "APPLIANCE_TURNSERVER": str(bin_dir / "turnserver"),
    # No render node: software rendering, so the fake needs no GPU.
    "APPLIANCE_RENDER_NODE": str(work / "no-render-node"),
    "APPLIANCE_MEMORY_MIB": "2048",
    "APPLIANCE_CORES": "2",
    "APPLIANCE_DISK_SIZE": "4G",
}
os.execve(sys.executable, [sys.executable, "-m", "appliance", "serve"], env)
