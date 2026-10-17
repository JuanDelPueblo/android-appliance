"""mcp_server.py: the tools over the API client, in-process and over stdio."""

import base64
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from mcp import Client as McpClient
from mcp.client.stdio import StdioServerParameters

from appliance import api, mcp_server
from appliance.auth import Auth
from appliance.backend import Backend, LogBuffer
from appliance.client import Client
from appliance.session import Hub
from appliance.supervisor import Supervisor
from appliance.turn import Turn
from conftest import FAST

REPO = Path(__file__).resolve().parents[2]


@contextlib.asynccontextmanager
async def tools(cfg) -> AsyncIterator[McpClient]:
    """An MCP client connected in-process to the server, which talks to the
    daemon's local API, which drives the fake device."""
    sup = Supervisor(cfg, FAST)
    backend = Backend(sup, LogBuffer())
    app = api.create_app(backend, Auth(cfg), Hub(backend, Turn(cfg)))
    client = Client(transport=httpx2.ASGITransport(app=api.local(app)))
    try:
        async with McpClient(mcp_server.create_server(client)) as mc:
            yield mc
    finally:
        await client.close()
        await sup.shutdown()


def text(result) -> str:
    assert not result.is_error, result.content
    return result.content[0].text


def error(result) -> str:
    assert result.is_error, result.content
    return result.content[0].text


async def test_tools_are_listed_with_descriptions(cfg):
    async with tools(cfg) as mc:
        listed = (await mc.list_tools()).tools
        names = {t.name for t in listed}
        assert len(listed) == 22 and all(n.startswith("android_") for n in names)
        assert {"android_status", "android_start", "android_screenshot", "android_ui", "android_tap", "android_shell", "android_push"} <= names
        assert all(t.description for t in listed)
        tap = next(t for t in listed if t.name == "android_tap")
        assert set(tap.input_schema["required"]) == {"x", "y"}


async def test_status_start_with_progress_and_stop(cfg, monkeypatch):
    monkeypatch.setattr(mcp_server, "PROGRESS_INTERVAL", 0.01)
    progress = []

    async def on_progress(current, total, message):
        progress.append((current, total, message))

    async with tools(cfg) as mc:
        status = json.loads(text(await mc.call_tool("android_status", {})))
        assert status["state"] == "stopped" and status["boot_completed"] is False
        status = json.loads(text(await mc.call_tool("android_start", {}, progress_callback=on_progress)))
        assert status["state"] == "running" and status["boot_completed"] is True
        assert progress and all(0 <= p[0] <= p[1] and "android is starting" in p[2] for p in progress)
        status = json.loads(text(await mc.call_tool("android_stop", {})))
        assert status["state"] == "stopped"


async def test_a_bounded_start_returns_the_current_state(cfg):
    async with tools(cfg) as mc:
        status = json.loads(text(await mc.call_tool("android_start", {"timeout": 0})))
        assert status["state"] == "starting" and status["note"].startswith("still starting after 0s")
        status = json.loads(text(await mc.call_tool("android_wait", {})))
        assert status["state"] == "running"
        status = json.loads(text(await mc.call_tool("android_restart", {"wait": False})))
        assert status["state"] in ("stopping", "restarting", "starting")
        assert json.loads(text(await mc.call_tool("android_wait", {})))["state"] == "running"


async def test_wait_without_a_boot_is_an_error(cfg):
    async with tools(cfg) as mc:
        assert "(conflict)" in error(await mc.call_tool("android_wait", {}))


async def test_screenshot_is_an_image_with_the_device_size(cfg):
    async with tools(cfg) as mc:
        r = await mc.call_tool("android_screenshot", {"max_size": 300})
        assert not r.is_error and [c.type for c in r.content] == ["image", "text"]
        assert r.content[0].mime_type == "image/jpeg" and len(base64.b64decode(r.content[0].data)) > 100
        assert r.content[1].text.startswith("device 1080x1920 pixels; the image is scaled by 0.1562: multiply image coordinates by 6.4")
        r = await mc.call_tool("android_screenshot", {"format": "png", "max_size": 0})
        assert r.content[0].mime_type == "image/png" and r.content[1].text == "device 1080x1920 pixels"
        assert "format must be" in error(await mc.call_tool("android_screenshot", {"format": "gif"}))


async def test_ui_formats(cfg):
    async with tools(cfg) as mc:
        compact = text(await mc.call_tool("android_ui", {}))
        assert 'EditText "First name" id=com.example:id/name center=540,260 bounds=100,200,980,320 flags=clickable,focused' in compact
        nodes = json.loads(text(await mc.call_tool("android_ui", {"format": "json"})))
        assert nodes[0]["center"] == [540, 260]
        assert text(await mc.call_tool("android_ui", {"format": "xml"})).lstrip().startswith("<?xml")
        assert "format must be" in error(await mc.call_tool("android_ui", {"format": "html"}))


async def test_input_tools_reach_the_device(cfg, fake):
    async with tools(cfg) as mc:
        assert text(await mc.call_tool("android_tap", {"x": 540, "y": 260})) == "tapped 540,260"
        assert text(await mc.call_tool("android_long_press", {"x": 10, "y": 20, "duration_ms": 50})) == "held 10,20 for 50 ms"
        assert text(await mc.call_tool("android_swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 50})) == "swiped 1,2 to 3,4 in 50 ms"
        assert text(await mc.call_tool("android_key", {"key": "HOME"})) == "sent HOME"
        assert text(await mc.call_tool("android_text", {"text": "héllo"})) == "typed 5 characters"
        kinds = [e["type"] for e in fake.inputs()]
        assert "touch" in kinds and "key" in kinds
        touches = [t for e in fake.inputs() if e["type"] == "touch" for t in e["touches"]]
        assert any(t["x"] == 540 and t["y"] == 260 for t in touches)
        assert "(bad_request)" in error(await mc.call_tool("android_key", {"key": "NO SUCH KEY!"}))
        # Arguments of the wrong type never reach the device.
        assert (await mc.call_tool("android_tap", {"x": "left", "y": 1})).is_error


async def test_shell_output_and_its_bound(cfg, monkeypatch):
    async with tools(cfg) as mc:
        assert text(await mc.call_tool("android_shell", {"command": "pm list packages"})).startswith("package:")
        out = text(await mc.call_tool("android_shell", {"command": "cat /no/such/file"}))
        assert out.startswith("exit code 1\n--- stdout ---\n") and "No such file" in out
        monkeypatch.setattr(mcp_server, "MAX_SHELL_OUTPUT", 4)
        out = text(await mc.call_tool("android_shell", {"command": "pm list packages"}))
        assert out.startswith("pack\n[... ") and out.endswith(" more bytes not shown]")
        assert "(bad_request)" in error(await mc.call_tool("android_shell", {"command": "  "}))


async def test_apps_install_launch_stop_uninstall(cfg, fake, monkeypatch):
    async with tools(cfg) as mc:
        before = text(await mc.call_tool("android_apps", {}))
        assert "com.example.app" not in before
        apk = base64.b64encode(b"PKcom.example.app").decode()
        assert "Success" in text(await mc.call_tool("android_install", {"apk_base64": apk, "grant": True}))
        assert "com.example.app" in text(await mc.call_tool("android_apps", {})).splitlines()
        assert text(await mc.call_tool("android_launch", {"package": "com.example.app"})) == "launched com.example.app"
        assert text(await mc.call_tool("android_force_stop", {"package": "com.example.app"})) == "stopped com.example.app"
        assert "Success" in text(await mc.call_tool("android_uninstall", {"package": "com.example.app"}))
        assert "(not_found)" in error(await mc.call_tool("android_launch", {"package": "com.example.app"}))
        assert "not valid base64" in error(await mc.call_tool("android_install", {"apk_base64": "***"}))
        monkeypatch.setattr(mcp_server, "MAX_PUSH", 4)
        assert "the limit is 4" in error(await mc.call_tool("android_install", {"apk_base64": apk}))
        assert fake.adb_calls()  # the fake saw the install
        assert before == "(no third-party apps)"
        assert "com.android.settings" in text(await mc.call_tool("android_apps", {"all": True})).splitlines()


async def test_files_push_pull_ls(cfg, fake, monkeypatch):
    async with tools(cfg) as mc:
        assert text(await mc.call_tool("android_push", {"path": "/sdcard/note.txt", "text": "héllo"})) == "wrote 6 bytes to /sdcard/note.txt"
        assert text(await mc.call_tool("android_pull", {"path": "/sdcard/note.txt"})) == "héllo"
        assert text(await mc.call_tool("android_pull", {"path": "/sdcard/note.txt", "encoding": "base64"})) == base64.b64encode("héllo".encode()).decode()
        listing = text(await mc.call_tool("android_ls", {"path": "/sdcard"}))
        assert any(line.split() == ["file", "6", "note.txt"] for line in listing.splitlines())
        binary = b"\xff\xfe\x00binary"
        assert "(bad_request)" in error(await mc.call_tool("android_push", {"path": "/sdcard/", "content_base64": base64.b64encode(binary).decode()}))
        text(await mc.call_tool("android_push", {"path": "/sdcard/blob.bin", "content_base64": base64.b64encode(binary).decode()}))
        pulled = text(await mc.call_tool("android_pull", {"path": "/sdcard/blob.bin"}))
        assert pulled == f"[binary, {len(binary)} bytes, base64]\n{base64.b64encode(binary).decode()}"
        assert "not UTF-8 text" in error(await mc.call_tool("android_pull", {"path": "/sdcard/blob.bin", "encoding": "text"}))
        assert "(not_found)" in error(await mc.call_tool("android_pull", {"path": "/sdcard/missing"}))
        assert "give either text or content_base64" in error(await mc.call_tool("android_push", {"path": "/sdcard/x"}))
        assert "give either" in error(await mc.call_tool("android_push", {"path": "/sdcard/x", "text": "a", "content_base64": "YQ=="}))
        monkeypatch.setattr(mcp_server, "MAX_PULL", 2)
        assert "the limit is 2" in error(await mc.call_tool("android_pull", {"path": "/sdcard/note.txt"}))
        assert "(bad_request)" in error(await mc.call_tool("android_ls", {"path": "relative"}))


async def test_logs(cfg):
    async with tools(cfg) as mc:
        r = await mc.call_tool("android_logs", {"source": "daemon", "lines": 5})
        assert not r.is_error
        assert "(bad_request)" in error(await mc.call_tool("android_logs", {"source": "kernel"}))


def _daemon_env(cfg) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "APPLIANCE_DATA_DIR": str(cfg.data_dir),
        "APPLIANCE_RUN_DIR": str(cfg.run_dir),
        "APPLIANCE_TMP_DIR": str(cfg.tmp_dir),
        "APPLIANCE_SDK_ROOT": str(cfg.sdk_root),
        "APPLIANCE_WEB_DIR": str(REPO / "web"),
        "APPLIANCE_HTTP_PORT": "0",
        "APPLIANCE_EMULATOR": cfg.emulator_bin,
        "APPLIANCE_ADB": cfg.adb_bin,
        "APPLIANCE_XVNC": cfg.xvnc_bin,
        "APPLIANCE_GLXINFO": cfg.glxinfo_bin,
        "APPLIANCE_TURNSERVER": cfg.turnserver_bin,
        "APPLIANCE_RENDER_NODE": cfg.render_node,
        "APPLIANCE_MEMORY_MIB": "2048",
        "APPLIANCE_CORES": "2",
        "APPLIANCE_DISK_SIZE": "4G",
    }


async def test_stdio_server_talks_to_a_running_daemon(cfg):
    """`python -m appliance mcp` (what `androidctl mcp` runs in the container)
    serves the tools over stdio against the daemon's unix socket."""
    env = _daemon_env(cfg)
    daemon = subprocess.Popen([sys.executable, "-m", "appliance", "serve"], env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not cfg.socket_path.exists():
            if daemon.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"the daemon did not start: {daemon.stderr.read().decode() if daemon.stderr else ''}")
            time.sleep(0.05)
        params = StdioServerParameters(command=sys.executable, args=["-m", "appliance", "mcp"], env=env, cwd=str(REPO))
        async with McpClient(params) as mc:
            assert {t.name for t in (await mc.list_tools()).tools} >= {"android_status", "android_tap"}
            status = json.loads(text(await mc.call_tool("android_status", {})))
            assert status["state"] == "stopped"
            assert "(conflict)" in error(await mc.call_tool("android_wait", {}))
    finally:
        daemon.terminate()
        try:
            daemon.wait(15)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
