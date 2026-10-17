"""python -m appliance serve end to end: both listeners, the client,
the healthcheck, the single-daemon lock and the SIGTERM shutdown."""

import asyncio
import contextlib
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from appliance.__main__ import main
from appliance.client import Client, ClientError

REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def daemon_env(cfg, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "APPLIANCE_DATA_DIR": str(cfg.data_dir),
        "APPLIANCE_RUN_DIR": str(cfg.run_dir),
        "APPLIANCE_TMP_DIR": str(cfg.tmp_dir),
        "APPLIANCE_SDK_ROOT": str(cfg.sdk_root),
        "APPLIANCE_WEB_DIR": str(cfg.web_dir),
        "APPLIANCE_HTTP_PORT": str(port),
        "APPLIANCE_EMULATOR": cfg.emulator_bin,
        "APPLIANCE_ADB": cfg.adb_bin,
        "APPLIANCE_XVNC": cfg.xvnc_bin,
        "APPLIANCE_GLXINFO": cfg.glxinfo_bin,
        "APPLIANCE_TURNSERVER": cfg.turnserver_bin,
        "APPLIANCE_RENDER_NODE": cfg.render_node,
        "APPLIANCE_PORT": str(port),
    }


@contextlib.contextmanager
def daemon(cfg, env: dict[str, str]) -> Iterator[subprocess.Popen]:
    proc = subprocess.Popen([sys.executable, "-m", "appliance", "serve"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while healthcheck(env) != 0:
            assert proc.poll() is None, proc.stderr.read()
            assert time.monotonic() < deadline, "the daemon did not answer"
            time.sleep(0.1)
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def healthcheck(env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, "-m", "appliance", "healthcheck"], env=env, capture_output=True).returncode


def test_daemon_end_to_end(cfg, fake, tmp_path):
    port = free_port()
    env = daemon_env(cfg, port)
    with daemon(cfg, env) as proc:
        assert stat.S_IMODE(cfg.socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(cfg.run_dir.stat().st_mode) == 0o700
        # The state holds the accounts of the device: closed to other users.
        assert stat.S_IMODE(cfg.data_dir.stat().st_mode) == 0o700

        # A second daemon on the same state refuses to run.
        other = dict(env, APPLIANCE_RUN_DIR=str(tmp_path / "run2"), APPLIANCE_HTTP_PORT=str(free_port()))
        second = subprocess.run([sys.executable, "-m", "appliance", "serve"], env=other, capture_output=True, text=True, timeout=20)
        assert second.returncode == 1 and "another appliance daemon uses" in second.stderr

        asyncio.run(exercise(str(cfg.socket_path), port, tmp_path))
        assert len(fake.launches()) == 1
        # coturn runs with the daemon, not with Android.
        [turn] = fake.turn_starts()
        assert turn["argv"][0] == "-c"
        turn_pid = int((fake.root / "turnserver.pid").read_text())

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        log = proc.stderr.read()
    assert "stopping android, then the daemon" in log
    # turnserver ended with the daemon.
    with pytest.raises(ProcessLookupError):
        os.kill(turn_pid, 0)
    # Android stopped gracefully: one "emu kill" and a clean exit.
    assert fake.signals() == ["emu kill"]
    assert fake.emulator_pid() is None
    assert not cfg.socket_path.exists()
    assert healthcheck(env) == 1


async def exercise(socket_path: str, port: int, tmp_path: Path) -> None:
    async with Client(socket_path=socket_path) as local:
        assert (await local.status())["state"] == "stopped"
        assert (await local.start())["state"] == "running"
        await local.tap(10, 20)
        await local.long_press(10, 20, 50)
        await local.swipe(1, 2, 3, 4, 50)
        await local.text("Grüße 日本")
        await local.key("BACK")
        shell = await local.shell("echo hi; exit 4")
        assert (shell.exit_code, shell.stdout) == (4, b"hi\n")
        source = tmp_path / "upload.bin"
        source.write_bytes(os.urandom(300000))
        assert await local.push(source, "/sdcard/upload.bin") == 300000
        assert await local.pull("/sdcard/upload.bin", tmp_path / "back.bin") == 300000
        assert (tmp_path / "back.bin").read_bytes() == source.read_bytes()
        assert [e["name"] for e in await local.list_files("/sdcard")] == ["upload.bin"]
        apk = tmp_path / "app.apk"
        apk.write_bytes(b"PKorg.example.e2e")
        assert "Success" in await local.install(apk, replace=True)
        assert await local.apps() == ["org.example.e2e"]
        await local.launch("org.example.e2e")
        await local.force_stop("org.example.e2e")
        assert "Success" in await local.uninstall("org.example.e2e")
        shot = await local.screenshot("jpeg", 70, 192)
        assert (shot.content_type, shot.device_width, shot.scale) == ("image/jpeg", 1080, 0.1)
        assert "<hierarchy" in await local.ui()
        assert (await local.ui_nodes())[0]["text"] == "First name"
        assert "Boot completed" in await local.logs("emulator")
        assert "android is running" in await local.logs("daemon", 50)
        with pytest.raises(ClientError) as err:
            await local.pull("/sdcard/missing", tmp_path / "x")
        assert (err.value.status, err.value.code) == (404, "not_found")
        token = await local.token()
        assert (await local.login_link()).startswith(f"http://127.0.0.1:{port}/login?code=")
        assert (await local.wait())["state"] == "running"

    async with Client(url=f"http://127.0.0.1:{port}", token=token) as remote:
        assert (await remote.health())["ok"]
        assert (await remote.status())["state"] == "running"
        with pytest.raises(ClientError) as err:
            await remote.token()
        assert err.value.status == 403
    async with Client(url=f"http://127.0.0.1:{port}") as anonymous:
        with pytest.raises(ClientError) as err:
            await anonymous.status()
        assert (err.value.status, err.value.code) == (401, "unauthorized")
    async with Client(socket_path=socket_path) as local:
        rotated = await local.rotate_token()
    async with Client(url=f"http://127.0.0.1:{port}", token=rotated) as remote:
        assert (await remote.status())["state"] == "running"


def test_check_config_is_what_install_sh_relies_on(monkeypatch, capsys):
    monkeypatch.setenv("APPLIANCE_MEMORY_MIB", "lots")
    assert main(["check-config"]) == 1
    assert "APPLIANCE_MEMORY_MIB must be an integer of at least 512, not 'lots'" in capsys.readouterr().err
    monkeypatch.setenv("APPLIANCE_MEMORY_MIB", "3072")
    assert main(["check-config"]) == 0
    assert capsys.readouterr().err == ""
