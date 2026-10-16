"""emulator.py: GPU selection, the launch, locks, logs and the stop."""

import asyncio
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import replace

import pytest

from appliance import avd, emulator
from appliance.adb import Adb
from appliance.emulator import LaunchError, StopTiming

XVNC_ARGS = [":0", "-rendernode", os.devnull, "-geometry", "1080x1920", "-depth", "24", "-rfbport", "-1",
             "-rfbunixpath", "{tmp}/xvnc.sock", "-rfbunixmode", "0600", "-SecurityTypes", "None", "-nolisten", "tcp"]  # fmt: skip


async def test_gpu_modes_without_render_node(cfg, fake):
    assert await emulator.setup_gpu(cfg) == ("lavapipe", None)
    assert await emulator.setup_gpu(replace(cfg, gpu="lavapipe", render_node=os.devnull)) == ("lavapipe", None)
    with pytest.raises(LaunchError, match="no usable render node"):
        await emulator.setup_gpu(replace(cfg, gpu="host"))
    assert fake.xvnc() == []


async def test_gpu_host_starts_xvnc(gpu_cfg, fake):
    mode, xvnc = await emulator.setup_gpu(gpu_cfg)
    try:
        assert mode == "host" and xvnc is not None
        assert gpu_cfg.x11_socket.exists()
        assert fake.xvnc()[0]["argv"] == [a.format(tmp=gpu_cfg.tmp_dir) for a in XVNC_ARGS]
    finally:
        await xvnc.stop()
    assert xvnc.proc.returncode is not None
    assert not gpu_cfg.x11_socket.exists()


async def test_gpu_software_renderer_falls_back(gpu_cfg, fake):
    fake.configure(glx_renderer="llvmpipe (LLVM 21.0.0, 256 bits)")
    assert await emulator.setup_gpu(gpu_cfg) == ("lavapipe", None)
    assert not gpu_cfg.x11_socket.exists()
    with pytest.raises(LaunchError, match=r"no hardware OpenGL renderer.*llvmpipe"):
        await emulator.setup_gpu(replace(gpu_cfg, gpu="host"))


async def test_gpu_xvnc_failure(gpu_cfg, fake):
    fake.configure(xvnc_fails=True)
    assert await emulator.setup_gpu(gpu_cfg) == ("lavapipe", None)
    with pytest.raises(LaunchError, match="Xvnc did not start"):
        await emulator.setup_gpu(replace(gpu_cfg, gpu="host"))


def test_emulator_argv(cfg):
    common = ["-avd", "android", "-port", "5554", "-no-window", "-no-audio", "-no-boot-anim", "-no-metrics",
              "-crash-report-mode", "disabled"]  # fmt: skip
    assert emulator.emulator_argv(cfg, "host")[1:] == [
        *common, "-gpu", "host", "-feature", "-QuickbootFileBacked,-Vulkan", "-no-snapshot", "-grpc", "8554", "-grpc-use-token",
    ]  # fmt: skip
    assert emulator.emulator_argv(cfg, "lavapipe")[1:] == [
        *common, "-gpu", "lavapipe", "-feature", "-QuickbootFileBacked", "-no-snapshot", "-grpc", "8554", "-grpc-use-token",
    ]  # fmt: skip


def prepare(cfg):
    avd.create(cfg, 35)
    avd.configure(cfg, 35)
    cfg.android_home.mkdir(parents=True, exist_ok=True)


async def test_launch_environment_discovery_and_stop(cfg, fake):
    prepare(cfg)
    emu = await emulator.launch(cfg, "host")
    adb = Adb(cfg.adb_bin, cfg.android_env())
    try:
        for _ in range(100):
            if emu.discovery():
                break
            await asyncio.sleep(0.05)
        found = emu.discovery()
        assert int(found["grpc.port"]) > 0 and found["grpc.token"] == "fake-grpc-token/with+chars=="
        # A file that names the port but not yet the token is not usable.
        path = cfg.android_home / "avd" / "running" / f"pid_{emu.pid}.ini"
        text = path.read_text()
        path.write_text("grpc.port=8554\n")
        assert emu.discovery() is None
        path.write_text(text)
        launch = fake.launches()[0]
        assert launch["env"] == {
            "HOME": str(cfg.home),
            "ANDROID_USER_HOME": str(cfg.android_home),
            "ANDROID_EMULATOR_HOME": str(cfg.android_home),
            "ANDROID_AVD_HOME": str(cfg.registry_dir),
            "ANDROID_SDK_ROOT": str(cfg.sdk_root),
            "ANDROID_ADB_SERVER_PORT": "5037",
            "DISPLAY": ":0",
            "USER": os.environ.get("USER") or "android",
        }
        assert launch["cwd"] == str(cfg.home)
        assert emu.log_path == cfg.logs_dir / "emulator-1.log"
    finally:
        await emu.stop(adb, StopTiming(emu_kill=5, sigterm=1, sigkill=1))
    assert emu.returncode == 0
    assert fake.signals() == ["emu kill"]
    # The guest flushes its page cache before the kill.
    calls = [argv[2:] for argv in fake.adb_calls()]
    assert calls[-2:] == [["shell", "sync"], ["emu", "kill"]]
    assert "fake emulator stopped" in emulator.tail(emu.log_path, 5)


async def test_stop_escalates_once(cfg, fake):
    prepare(cfg)
    adb = Adb(cfg.adb_bin, cfg.android_env())
    fake.configure(ignore_kill=True)
    emu = await emulator.launch(cfg, "lavapipe")
    while fake.emulator_pid() is None:
        await asyncio.sleep(0.05)
    await emu.stop(adb, StopTiming(emu_kill=0.5, sigterm=1, sigkill=1))
    assert fake.signals() == ["emu kill", "SIGTERM"]
    assert emu.returncode == 0

    (fake.root / "signals.jsonl").unlink()
    fake.configure(ignore_kill=True, ignore_term=True)
    emu = await emulator.launch(cfg, "lavapipe")
    while fake.emulator_pid() != emu.pid:
        await asyncio.sleep(0.05)
    await emu.stop(adb, StopTiming(emu_kill=0.5, sigterm=0.5, sigkill=2))
    # One "emu kill", one SIGTERM, then SIGKILL; never a second kill request.
    assert fake.signals() == ["emu kill", "SIGTERM"]
    assert emu.returncode == -9


async def test_renderer_failure_is_killed(cfg, fake):
    prepare(cfg)
    fake.configure(mode="renderer")
    emu = await emulator.launch(cfg, "lavapipe")
    assert await asyncio.wait_for(emu.wait(), 10) == -9
    assert "Could not start renderer" in emu.hang_line


async def test_fatal_line(cfg, fake):
    prepare(cfg)
    fake.configure(mode="fatal")
    emu = await emulator.launch(cfg, "lavapipe")
    assert await emu.wait() == 1
    assert emu.fatal_line() == "FATAL | fake fatal error"


def test_log_rotation(cfg):
    paths = [emulator.new_log(cfg) for _ in range(7)]
    assert paths[-1].name == "emulator-7.log"
    assert sorted(p.name for p in cfg.logs_dir.glob("emulator-*.log")) == [f"emulator-{n}.log" for n in range(3, 8)]
    assert emulator.latest_log(cfg) == paths[-1]
    assert emulator.new_log(cfg).name == "emulator-8.log"
    assert sorted(p.name for p in cfg.logs_dir.glob("emulator-*.log")) == [f"emulator-{n}.log" for n in range(4, 9)]


def test_tail(tmp_path):
    path = tmp_path / "log"
    path.write_text("".join(f"line {n}\n" for n in range(1000)))
    assert emulator.tail(path, 3) == "line 997\nline 998\nline 999"
    assert emulator.tail(tmp_path / "missing", 3) == ""
    assert emulator.tail(tmp_path, 3) == ""


def stale_files(cfg):
    prepare(cfg)
    (cfg.avd_dir / "hardware-qemu.ini.lock").write_text("1")
    (cfg.avd_dir / "multiinstance.lock").touch()
    (cfg.avd_dir / "userdata-qemu.img.qcow2").write_text("qcow2")
    running = cfg.android_home / "avd" / "running"
    running.mkdir(parents=True)
    (running / "pid_74.ini").write_text("grpc.port=8554\n")
    return running


def test_stale_locks_are_removed(cfg):
    running = stale_files(cfg)
    emulator.remove_stale_locks(cfg)
    assert not (cfg.avd_dir / "hardware-qemu.ini.lock").exists()
    assert list(running.iterdir()) == []
    # multiinstance.lock is the emulator's own flock file; it stays.
    assert (cfg.avd_dir / "multiinstance.lock").exists()


HOLD_FLOCK = """
import fcntl, sys, time
f = open(sys.argv[1])
fcntl.flock(f, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(60)
"""

# qemu's image locks: OFD read locks on single bytes.
HOLD_OFD = """
import fcntl, os, struct, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.fcntl(fd, fcntl.F_OFD_SETLK, struct.pack("@hhqqi4x", fcntl.F_RDLCK, os.SEEK_SET, 100, 1, 0))
print("locked", flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("holder, target", [(HOLD_FLOCK, "multiinstance.lock"), (HOLD_OFD, "userdata-qemu.img.qcow2")])
def test_held_locks_refuse_and_keep_files(cfg, holder, target):
    running = stale_files(cfg)
    proc = subprocess.Popen([sys.executable, "-c", holder, str(cfg.avd_dir / target)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline() == "locked\n"
        with pytest.raises(LaunchError, match=f"another emulator uses the AVD.*{target}"):
            emulator.remove_stale_locks(cfg)
        assert (cfg.avd_dir / "hardware-qemu.ini.lock").exists()
        assert (running / "pid_74.ini").exists()
    finally:
        proc.kill()
        proc.wait()
    emulator.remove_stale_locks(cfg)
    assert not (cfg.avd_dir / "hardware-qemu.ini.lock").exists()


CHECK_IN_NAMESPACE = """
import sys
from pathlib import Path
from appliance import emulator
from appliance.config import Config
cfg = Config(data_dir=Path(sys.argv[1]))
ino = (cfg.avd_dir / "multiinstance.lock").stat().st_ino
visible = any(f":{ino} " in line for line in open("/proc/locks"))
print(emulator.avd_lock_holder(cfg), visible)
"""


@pytest.mark.skipif(shutil.which("unshare") is None, reason="needs unshare")
def test_locks_are_seen_from_another_pid_namespace(cfg):
    """A container does not see host locks in /proc/locks; the probe
    still finds them."""
    stale_files(cfg)
    proc = subprocess.Popen([sys.executable, "-c", HOLD_FLOCK, str(cfg.avd_dir / "multiinstance.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline() == "locked\n"
        check = subprocess.run(
            ["unshare", "-Urpf", "--mount-proc", sys.executable, "-c", textwrap.dedent(CHECK_IN_NAMESPACE), str(cfg.data_dir)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(os.path.dirname(os.path.dirname(emulator.__file__)))},
        )
        if check.returncode and "Operation not permitted" in check.stderr:
            pytest.skip("unprivileged namespaces are disabled")
        assert check.stdout.split() == [str(cfg.avd_dir / "multiinstance.lock"), "False"]
    finally:
        proc.kill()
        proc.wait()
