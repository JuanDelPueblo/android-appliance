"""Fixtures: a fake SDK, fake emulator/adb/Xvnc/glxinfo, and a config."""

import asyncio
import inspect
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from appliance.config import Config
from appliance.emulator import StopTiming
from appliance.supervisor import Timing

FAKES = Path(__file__).parent / "fakes"
# settle=0: a crash after the boot is reported at once; the retry tests
# set their own window.
FAST = Timing(
    poll=0.05, unauthorized=1.0, idle_check=0.1, setup_command=5, settle=0, stop=StopTiming(sync=2, emu_kill=2, sigterm=1, sigkill=2)
)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run `async def` tests, each in a fresh event loop."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    args = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(asyncio.wait_for(pyfuncitem.obj(**args), 60))
    return True


class Fake:
    """One fake device. Tests set the scenario and read what the fakes saw."""

    def __init__(self, root: Path, bin_dir: Path, sdk: Path):
        self.root = root
        self.bin = bin_dir
        self.sdk = sdk

    def configure(self, **values: object) -> None:
        path = self.root / "config.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**current, **values}))

    def _jsonl(self, name: str) -> list[dict]:
        path = self.root / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def launches(self) -> list[dict]:
        return self._jsonl("launches")

    def inputs(self) -> list[dict]:
        return self._jsonl("input")

    def adb_calls(self) -> list[list[str]]:
        return [entry["argv"] for entry in self._jsonl("adb")]

    def shell_commands(self) -> list[str]:
        return [" ".join(argv[3:]) for argv in self.adb_calls() if argv[2:3] == ["shell"]]

    def signals(self) -> list[str]:
        return [entry["signal"] for entry in self._jsonl("signals")]

    def xvnc(self) -> list[dict]:
        return self._jsonl("xvnc")

    def emulator_pid(self) -> int | None:
        try:
            return int((self.root / "emulator.pid").read_text())
        except FileNotFoundError:
            return None

    def device(self, path: str) -> Path:
        return self.root / "device" / path.lstrip("/")


def _wrapper(path: Path, fake_root: Path, script: str) -> str:
    path.write_text(f'#!/bin/sh\nFAKE_ANDROID={fake_root} exec {sys.executable} {FAKES / script} "$@"\n')
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    root = tmp_path / "fake"
    (root / "device" / "sdcard").mkdir(parents=True)
    (root / "device" / "data" / "local" / "tmp").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("emulator", "emulator.py"), ("adb", "adb.py"), ("Xvnc", "xvnc.py"), ("glxinfo", "glxinfo.py")):
        _wrapper(bin_dir / name, root, script)
    sdk = tmp_path / "sdk"
    (sdk / "system-images" / "android-35" / "google_apis_playstore" / "x86_64").mkdir(parents=True)
    return Fake(root, bin_dir, sdk)


@pytest.fixture
def cfg(tmp_path: Path, fake: Fake) -> Config:
    # A render node that does not exist: lavapipe, unless a test sets one.
    return Config(
        memory_mib=2048,
        cores=2,
        disk_size="4G",
        boot_timeout=20,
        http_port=0,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        tmp_dir=tmp_path / "tmp",
        sdk_root=fake.sdk,
        web_dir=tmp_path / "web",
        emulator_bin=str(fake.bin / "emulator"),
        adb_bin=str(fake.bin / "adb"),
        xvnc_bin=str(fake.bin / "Xvnc"),
        glxinfo_bin=str(fake.bin / "glxinfo"),
        render_node=str(tmp_path / "no-render-node"),
    )


@pytest.fixture
def gpu_cfg(cfg: Config) -> Config:
    # /dev/null is a readable and writable character device.
    return replace(cfg, render_node=os.devnull)
