"""api.py and auth.py: every endpoint, authentication and the text formats."""

import asyncio
import base64
import contextlib
import io
import os
import shutil
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import httpx
import pytest
from PIL import Image

from appliance import api, backend
from appliance.auth import Auth
from appliance.backend import Backend, LogBuffer
from appliance.supervisor import Supervisor, Timing
from conftest import FAST

ORIGIN = "http://127.0.0.1:6090"


@dataclass
class Clients:
    local: httpx.AsyncClient
    tcp: httpx.AsyncClient
    sup: Supervisor
    auth: Auth

    def bearer(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.auth.token}"}


@contextlib.asynccontextmanager
async def clients(cfg, timing: Timing = FAST) -> AsyncIterator[Clients]:
    sup = Supervisor(cfg, timing)
    auth = Auth(cfg)
    app = api.create_app(Backend(sup, LogBuffer()), auth)
    local = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.local(app)), base_url="http://appliance")
    tcp = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    try:
        yield Clients(local, tcp, sup, auth)
    finally:
        await local.aclose()
        await tcp.aclose()
        await sup.shutdown()


async def test_health_needs_no_auth(cfg):
    async with clients(cfg) as c:
        r = await c.tcp.get("/api/v1/health")
        assert r.status_code == 200 and r.json() == {"ok": True, "version": r.json()["version"]}


async def test_tcp_needs_auth_and_unix_socket_does_not(cfg):
    async with clients(cfg) as c:
        r = await c.tcp.get("/api/v1/status")
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert r.json()["error"]["code"] == "unauthorized"
        r = await c.tcp.get("/api/v1/status", headers={"Accept": "text/plain"})
        assert r.status_code == 401 and r.headers["content-type"].startswith("text/plain")
        assert r.text.startswith("authentication required")
        assert (await c.tcp.get("/api/v1/status", headers=c.bearer())).status_code == 200
        assert (await c.local.get("/api/v1/status")).status_code == 200
        # A browser page redirects to the login page.
        r = await c.tcp.get("/")
        assert (r.status_code, r.headers["location"]) == (303, "/login")


async def test_wrong_tokens_are_rate_limited(cfg):
    async with clients(cfg) as c:
        for _ in range(5):
            assert (await c.tcp.get("/api/v1/status", headers=c.bearer("wrong"))).status_code == 401
        r = await c.tcp.get("/api/v1/status", headers=c.bearer("wrong"))
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        # The failures of others (everything on the host's loopback is one
        # client) never lock out the right token.
        assert (await c.tcp.get("/api/v1/status", headers=c.bearer())).status_code == 200
        # The local socket is not affected.
        assert (await c.local.get("/api/v1/status")).status_code == 200


async def test_rate_limit_keys_on_the_address_the_proxy_added(cfg):
    async with clients(replace(cfg, trust_proxy=True)) as c:

        def guess(forwarded: str) -> httpx.Response:
            return c.tcp.get("/api/v1/status", headers={**c.bearer("wrong"), "X-Forwarded-For": forwarded})

        # A client that makes up the leftmost entry does not get new guesses.
        statuses = [(await guess(f"10.0.0.{i}, 203.0.113.9")).status_code for i in range(7)]
        assert statuses == [401] * 5 + [429] * 2
        assert (await guess("10.0.0.1, 203.0.113.10")).status_code == 401


async def test_status_and_lifecycle(cfg, fake):
    async with clients(cfg) as c:
        status = (await c.local.get("/api/v1/status")).json()
        assert set(status) == {"state", "boot_completed", "idle_seconds", "idle_stop_seconds", "uptime_seconds", "phase",
                               "gpu", "api_level", "avd", "viewers", "last_error", "version"}  # fmt: skip
        assert (status["state"], status["idle_stop_seconds"], status["avd"]) == ("stopped", 3600, "android")
        r = await c.local.get("/api/v1/status", params={"format": "text"})
        assert r.text == "state=stopped boot_completed=0 idle_seconds=0\n"
        r = await c.local.post("/api/v1/wait")
        assert r.status_code == 409 and r.json()["error"] == {"code": "conflict", "message": "android is stopped"}
        r = await c.local.post("/api/v1/start", params={"format": "text"})
        assert r.status_code == 200 and r.text.startswith("state=running boot_completed=1 idle_seconds=")
        r = await c.local.post("/api/v1/wait", json={"timeout": 5})
        assert r.json()["state"] == "running"
        r = await c.local.post("/api/v1/restart", json={"wait": True})
        assert r.json()["state"] == "running" and len(fake.launches()) == 2
        r = await c.local.post("/api/v1/stop", json={"wait": False})
        assert r.json()["state"] == "stopping"
        r = await c.local.post("/api/v1/stop", params={"format": "text"})
        assert r.text.startswith("state=stopped boot_completed=0")
        r = await c.local.post("/api/v1/start", json={"wait": False})
        assert r.json()["state"] == "starting"
        for body in ({"wait": "yes"}, {"timeout": -1}, [1]):
            r = await c.local.post("/api/v1/start", json=body)
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
        r = await c.local.post("/api/v1/start", content=b"{nope")
        assert r.status_code == 400


async def test_boot_failure_is_503(cfg, fake):
    fake.configure(mode="crash")
    async with clients(cfg) as c:
        r = await c.local.post("/api/v1/input/tap", json={"x": 1, "y": 2}, headers={"Accept": "text/plain"})
        assert r.status_code == 503 and r.text == "the emulator exited with status 3\n"
        status = (await c.local.get("/api/v1/status")).json()
        assert status["state"] == "stopped" and status["last_error"]["kind"] == "crash"


async def test_screenshot(cfg, fake):
    async with clients(cfg) as c:
        r = await c.local.get("/api/v1/screenshot")
        assert r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
        assert (r.headers["x-device-width"], r.headers["x-device-height"], r.headers["x-image-scale"]) == ("1080", "1920", "1")
        r = await c.local.get("/api/v1/screenshot", params={"format": "jpeg", "quality": 50, "max_size": 480})
        image = Image.open(io.BytesIO(r.content))
        assert (r.headers["content-type"], image.format, image.size) == ("image/jpeg", "JPEG", (270, 480))
        assert r.headers["x-image-scale"] == "0.25" and r.headers["x-device-width"] == "1080"
        r = await c.local.get("/api/v1/screenshot", params={"max_size": 960})
        assert Image.open(io.BytesIO(r.content)).size == (540, 960)
        assert (await c.local.get("/api/v1/screenshot", params={"format": "gif"})).status_code == 400
        assert (await c.local.get("/api/v1/screenshot", params={"max_size": "x"})).status_code == 400


async def test_screenshot_falls_back_to_adb(cfg, fake):
    fake.configure(screenshot_fails=True)
    async with clients(cfg) as c:
        r = await c.local.get("/api/v1/screenshot")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG") and r.headers["x-device-width"] == "1080"
    assert ["-s", "emulator-5554", "exec-out", "screencap -p"] in fake.adb_calls()


async def test_ui(cfg, fake, monkeypatch):
    monkeypatch.setattr(backend, "UI_RETRY_DELAY", 0)
    fake.configure(ui_failures=2)
    async with clients(cfg) as c:
        r = await c.local.get("/api/v1/ui")
        assert r.headers["content-type"].startswith("application/xml") and "<hierarchy" in r.text
        dumps = [cmd for cmd in fake.shell_commands() if cmd.startswith("uiautomator dump")]
        assert len(dumps) == 3
        r = await c.local.get("/api/v1/ui", params={"format": "compact"})
        assert r.json()["nodes"] == [
            {"class": "EditText", "text": "First name", "id": "com.example:id/name", "bounds": [100, 200, 980, 320],
             "center": [540, 260], "flags": ["clickable", "focused"]},
            {"class": "ImageButton", "desc": "Search", "bounds": [900, 40, 1040, 180], "center": [970, 110], "flags": ["clickable"]},
        ]  # fmt: skip
    # The dump files are removed.
    assert list(fake.device("/data/local/tmp").iterdir()) == []


async def test_ui_without_hierarchy_fails(cfg, fake, monkeypatch):
    monkeypatch.setattr(backend, "UI_RETRY_DELAY", 0)
    fake.configure(ui_failures=100)
    async with clients(cfg) as c:
        r = await c.local.get("/api/v1/ui")
        assert r.status_code == 503 and "did not produce a hierarchy" in r.json()["error"]["message"]


def touches(fake) -> list[tuple]:
    return [tuple(t.values()) for e in fake.inputs() if e["type"] == "touch" for t in e["touches"]]


async def test_touch_input(cfg, fake):
    async with clients(cfg) as c:
        assert (await c.local.post("/api/v1/input/tap", json={"x": 100, "y": 200})).json() == {}
        tap = touches(fake)
        assert [(x, y, p) for x, y, _, p in tap] == [(100, 200, 1024), (100, 200, 0)]
        assert tap[0][2] == tap[1][2]
        await c.local.post("/api/v1/input/long-press", json={"x": 2000, "y": -4, "duration_ms": 100})
        assert [(x, y, p) for x, y, _, p in touches(fake)[2:]] == [(1079, 0, 1024), (1079, 0, 0)]
        await c.local.post("/api/v1/input/swipe", json={"x1": 500, "y1": 1500, "x2": 500, "y2": 500, "duration_ms": 100})
        swipe = touches(fake)[4:]
        assert swipe[0][:2] == (500, 1500) and swipe[-1] == (500, 500, swipe[0][2], 0) and len(swipe) == 8
        ids = {t[2] for t in touches(fake)}
        assert len(ids) == 3
        for body in ({"x": 1}, {"x": "1", "y": 2}, {"x": True, "y": 2}, {"x": 1, "y": 2, "duration_ms": 70000}):
            path = "/api/v1/input/long-press"
            assert (await c.local.post(path, json=body)).status_code == 400
    # Touch never goes through adb input.
    assert not any(cmd.startswith("input ") for cmd in fake.shell_commands())


async def test_text_input(cfg, fake):
    async with clients(cfg) as c:
        assert (await c.local.post("/api/v1/input/text", json={"text": "Grüße 日本"})).status_code == 200
        events = [e for e in fake.inputs() if e["type"] != "touch"]
        assert [(e["type"], e.get("text"), e.get("keyCode")) for e in events] == [
            ("getClipboard", None, None),
            ("setClipboard", "Grüße 日本", None),
            ("key", None, 135),
            ("setClipboard", "user-clipboard", None),
        ]
        r = await c.local.post("/api/v1/input/text", params={"method": "keys"}, content=b"Hi!\n",
                               headers={"Content-Type": "text/plain; charset=utf-8"})  # fmt: skip
        assert r.status_code == 200
        keys = [e["key"] for e in fake.inputs() if e["type"] == "key"][1:]
        assert keys == ["H", "i", "!", "Enter"]
        for body in ({"text": "é", "method": "keys"}, {"text": ""}, {"text": "a", "method": "morse"}, {"text": 5}):
            r = await c.local.post("/api/v1/input/text", json=body)
            assert r.status_code == 400, body


async def test_keys(cfg, fake):
    async with clients(cfg) as c:
        await c.local.post("/api/v1/input/key", json={"key": "HOME"})
        await c.local.post("/api/v1/input/key", json={"key": "KEYCODE_MENU"})
        keys = [(e["key"], e["keyCode"], e["codeType"]) for e in fake.inputs() if e["type"] == "key"]
        assert keys == [("GoHome", 0, "Usb"), ("", 139, "Evdev")]
        assert (await c.local.post("/api/v1/input/key", json={"key": "66"})).status_code == 200
        assert "input keyevent 66" in fake.shell_commands()
        # Names outside the table go to Android, which ignores unknown ones.
        assert (await c.local.post("/api/v1/input/key", json={"key": "media_play_pause"})).status_code == 200
        assert "input keyevent KEYCODE_MEDIA_PLAY_PAUSE" in fake.shell_commands()
        r = await c.local.post("/api/v1/input/key", json={"key": "CAPS_LOCK"})
        assert r.status_code == 400 and "sticks" in r.json()["error"]["message"]
        assert (await c.local.post("/api/v1/input/key", json={"key": "HOME;reboot"})).status_code == 400


async def test_shell(cfg, fake):
    async with clients(cfg) as c:
        r = await c.local.post("/api/v1/shell", json={"command": "echo out; echo err >&2; exit 3"})
        assert r.json() == {"exit_code": 3, "stdout": "out\n", "stderr": "err\n"}
        r = await c.local.post("/api/v1/shell", content=b"printf 'a b'", headers={"Content-Type": "text/plain"})
        assert r.json()["stdout"] == "a b"
        r = await c.local.post("/api/v1/shell", params={"format": "raw"}, json={"command": "printf '\\001\\002'; echo oops >&2; exit 7"})
        assert r.content == b"\x01\x02" and r.headers["x-exit-code"] == "7"
        assert base64.b64decode(r.headers["x-stderr"]) == b"oops\n" and "x-stderr-truncated" not in r.headers
        r = await c.local.post("/api/v1/shell", params={"format": "raw"}, json={"command": "head -c 20000 /dev/zero | tr '\\0' x >&2"})
        assert len(base64.b64decode(r.headers["x-stderr"])) == 16384 and r.headers["x-stderr-truncated"] == "1"
        r = await c.local.post("/api/v1/shell", json={"command": "sleep 5", "timeout": 0.3})
        assert r.status_code == 503 and r.json()["error"]["code"] == "timeout"
        assert (await c.local.post("/api/v1/shell", json={"command": " "})).status_code == 400
        assert (await c.local.post("/api/v1/shell", json={})).status_code == 400


async def test_apps(cfg, fake):
    async with clients(cfg) as c:
        assert (await c.local.get("/api/v1/apps")).json() == {"packages": []}
        r = await c.local.post("/api/v1/apps/install", params={"replace": 1, "grant": 1}, content=b"PKcom.example.app",
                               headers={"Content-Type": "application/vnd.android.package-archive"})  # fmt: skip
        assert r.status_code == 200 and "Success" in r.json()["output"]
        assert fake._jsonl("installs")[-1]["flags"] == ["-r", "-g"]
        assert list(cfg.upload_dir.iterdir()) == []
        r = await c.local.post("/api/v1/apps/install", content=b"PKcom.example.app", params={"format": "text"})
        assert r.status_code == 400 and "INSTALL_FAILED_ALREADY_EXISTS" in r.json()["error"]["message"]
        assert (await c.local.post("/api/v1/apps/install", content=b"not an apk")).status_code == 400
        assert (await c.local.post("/api/v1/apps/install", content=b"")).status_code == 400
        assert (await c.local.get("/api/v1/apps")).json() == {"packages": ["com.example.app"]}
        r = await c.local.get("/api/v1/apps", params={"all": 1, "format": "text"})
        assert r.text == "com.android.settings\ncom.example.app\n"
        assert (await c.local.post("/api/v1/apps/com.example.app/launch")).json() == {}
        assert "am start -n com.example.app/.MainActivity" in fake.shell_commands()
        assert (await c.local.post("/api/v1/apps/com.example.app/stop")).json() == {}
        assert "am force-stop com.example.app" in fake.shell_commands()
        r = await c.local.delete("/api/v1/apps/com.example.app", params={"keep_data": 1, "format": "text"})
        assert r.text == "Success\n" and "pm uninstall -k com.example.app" in fake.shell_commands()
        for method, path in (("POST", "/apps/com.gone.app/launch"), ("POST", "/apps/com.gone.app/stop"), ("DELETE", "/apps/com.gone.app")):
            r = await c.local.request(method, "/api/v1" + path)
            assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
        assert (await c.local.post("/api/v1/apps/bad;name/launch")).status_code == 400


async def test_files(cfg, fake):
    async with clients(cfg) as c:
        data = bytes(range(256)) * 1000
        r = await c.local.put("/api/v1/files", params={"path": "/sdcard/a file.bin"}, content=data)
        assert r.json() == {"bytes": len(data)}
        assert fake.device("/sdcard/a file.bin").read_bytes() == data
        r = await c.local.get("/api/v1/files", params={"path": "/sdcard/a file.bin"})
        assert r.content == data and r.headers["content-length"] == str(len(data))
        assert list(cfg.upload_dir.iterdir()) == []
        fake.device("/sdcard/Download").mkdir()
        entries = (await c.local.get("/api/v1/files/list", params={"path": "/sdcard"})).json()["entries"]
        assert [(e["name"], e["type"]) for e in entries] == [("Download", "dir"), ("a file.bin", "file")]
        assert entries[1]["size"] == len(data)
        assert all(isinstance(e["mtime"], int) for e in entries)
        r = await c.local.get("/api/v1/files/list", params={"path": "/sdcard/a file.bin", "format": "text"})
        assert r.text == f"file {len(data)} a file.bin\n"
        assert (await c.local.get("/api/v1/files", params={"path": "/sdcard/missing"})).status_code == 404
        assert (await c.local.get("/api/v1/files/list", params={"path": "/sdcard/missing"})).status_code == 404
        assert (await c.local.get("/api/v1/files", params={"path": "/sdcard/Download"})).status_code == 400
        assert (await c.local.put("/api/v1/files", params={"path": "/sdcard/Download"}, content=b"x")).status_code == 400
        # With a name, a directory takes the file (adb push does the same).
        r = await c.local.put("/api/v1/files", params={"path": "/sdcard/Download", "name": "in dir.bin"}, content=b"abc")
        assert r.json() == {"bytes": 3} and fake.device("/sdcard/Download/in dir.bin").read_bytes() == b"abc"
        r = await c.local.put("/api/v1/files", params={"path": "/sdcard/Download/", "name": "slash.bin"}, content=b"abc")
        assert r.status_code == 200 and fake.device("/sdcard/Download/slash.bin").exists()
        for bad in ("", "..", "a/b"):
            r = await c.local.put("/api/v1/files", params={"path": "/sdcard/Download", "name": bad}, content=b"x")
            assert r.status_code == 400, bad
        r = await c.local.put("/api/v1/files", params={"path": "/sdcard/Download/", "name": "x"}, content=b"x")
        assert r.status_code == 200
        r = await c.local.put("/api/v1/files", params={"path": "/sdcard/none/", "name": "x"}, content=b"x")
        assert r.status_code == 400 and "not a directory" in r.json()["error"]["message"]
        assert (await c.local.put("/api/v1/files", params={"path": "/sdcard/Download/"}, content=b"x")).status_code == 400
        assert (await c.local.put("/api/v1/files", params={"path": "/nodir/x"}, content=b"x")).status_code == 400
        assert (await c.local.get("/api/v1/files", params={"path": "relative"})).status_code == 400
        assert (await c.local.get("/api/v1/files")).status_code == 400


async def test_logs(cfg, fake):
    async with clients(cfg) as c:
        await c.local.post("/api/v1/start")
        r = await c.local.get("/api/v1/logs", params={"lines": 1})
        assert r.headers["content-type"].startswith("text/plain") and r.text.count("\n") == 1
        r = await c.local.get("/api/v1/logs", params={"source": "emulator"})
        assert "Boot completed" in r.text
        r = await c.local.get("/api/v1/logs", params={"source": "daemon"})
        assert r.status_code == 200
        assert (await c.local.get("/api/v1/logs", params={"source": "kernel"})).status_code == 400


async def test_daemon_log_buffer():
    import logging

    buffer = LogBuffer(size=3)
    buffer.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test-buffer")
    logger.addHandler(buffer)
    for n in range(5):
        logger.warning("line %d", n)
    assert list(buffer.lines) == ["line 2", "line 3", "line 4"]


async def test_local_only_auth_endpoints_and_rotation(cfg):
    async with clients(cfg) as c:
        for method, path in (("POST", "/auth/login-link"), ("GET", "/auth/token"), ("POST", "/auth/token/rotate")):
            r = await c.tcp.request(method, "/api/v1" + path, headers=c.bearer())
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        old = (await c.local.get("/api/v1/auth/token")).json()["token"]
        assert old == (cfg.secrets_dir / "api-token").read_text().strip() and len(old) >= 43
        assert (await c.local.get("/api/v1/auth/token", params={"format": "text"})).text == old + "\n"
        assert (cfg.secrets_dir / "api-token").stat().st_mode & 0o777 == 0o600
        assert cfg.secrets_dir.stat().st_mode & 0o777 == 0o700
        session = await login_with_link(c)
        pending = (await c.local.post("/api/v1/auth/login-link")).json()["url"].removeprefix(ORIGIN)
        new = (await c.local.post("/api/v1/auth/token/rotate")).json()["token"]
        # A link made before the rotation is dead.
        r = await c.tcp.get(pending)
        assert r.status_code == 401 and "set-cookie" not in r.headers
        assert new != old and (cfg.secrets_dir / "api-token").read_text().strip() == new
        assert (await c.tcp.get("/api/v1/status", headers=c.bearer(old))).status_code == 401
        assert (await c.tcp.get("/api/v1/status", headers=c.bearer(new))).status_code == 200
        # Rotation ends the dashboard sessions too.
        c.tcp.cookies.set("appliance_session", session)
        assert (await c.tcp.get("/api/v1/status")).status_code == 401
    # The secrets survive a daemon restart.
    assert Auth(cfg).token == new


async def login_with_link(c: Clients) -> str:
    url = (await c.local.post("/api/v1/auth/login-link")).json()["url"]
    assert url.startswith("http://127.0.0.1:6090/login?code=")
    r = await c.tcp.get(url.removeprefix("http://127.0.0.1:6090"))
    assert r.status_code == 200 and 'http-equiv="refresh"' in r.text
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
    c.tcp.cookies.clear()
    return r.cookies["appliance_session"]


async def test_login_link_is_single_use(cfg):
    async with clients(cfg) as c:
        r = await c.local.post("/api/v1/auth/login-link", params={"format": "text"})
        path = r.text.strip().removeprefix("http://127.0.0.1:6090")
        assert (await c.tcp.get(path)).status_code == 200
        c.tcp.cookies.clear()
        r = await c.tcp.get(path)
        assert r.status_code == 401 and "invalid or expired" in r.text and "set-cookie" not in r.headers


async def test_session_cookie_and_origin_check(cfg, fake):
    async with clients(cfg) as c:
        c.tcp.cookies.set("appliance_session", await login_with_link(c))
        assert (await c.tcp.get("/api/v1/status")).status_code == 200
        assert (await c.tcp.get("/")).status_code == 200
        r = await c.tcp.post("/api/v1/stop")
        assert r.status_code == 403 and r.json()["error"]["message"] == "cross-origin request refused"
        r = await c.tcp.post("/api/v1/stop", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = await c.tcp.post("/api/v1/stop", headers={"Origin": ORIGIN})
        assert r.status_code == 200
        c.tcp.cookies.set("appliance_session", "1.2.3")
        assert (await c.tcp.get("/api/v1/status")).status_code == 401
        # The bearer token needs no Origin.
        c.tcp.cookies.clear()
        assert (await c.tcp.post("/api/v1/stop", headers=c.bearer())).status_code == 200


async def test_origin_behind_a_trusted_proxy(cfg, fake):
    async with clients(replace(cfg, trust_proxy=True)) as c:
        c.tcp.cookies.set("appliance_session", await login_with_link(c))
        headers = {"Origin": "https://android.example.net", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "android.example.net"}
        assert (await c.tcp.post("/api/v1/stop", headers=headers)).status_code == 200
        assert (await c.tcp.post("/api/v1/stop", headers={**headers, "Origin": ORIGIN})).status_code == 403
        # Over HTTPS the cookie is Secure.
        c.tcp.cookies.clear()
        url = (await c.local.post("/api/v1/auth/login-link")).json()["url"]
        r = await c.tcp.get(url.removeprefix("http://127.0.0.1:6090"), headers={"X-Forwarded-Proto": "https"})
        assert "Secure" in r.headers["set-cookie"]


async def test_login_form(cfg):
    async with clients(cfg) as c:
        r = await c.tcp.get("/login")
        assert r.status_code == 200 and 'name="token"' in r.text
        form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
        r = await c.tcp.post("/login", content=f"token={c.auth.token}", headers=form)
        assert r.status_code == 200 and "appliance_session" in r.cookies
        c.tcp.cookies.clear()
        r = await c.tcp.post("/login", content=f"token={c.auth.token}", headers={**form, "Origin": "http://evil.example"})
        assert r.status_code == 403 and "set-cookie" not in r.headers
        for _ in range(5):
            assert (await c.tcp.post("/login", content="token=wrong", headers=form)).status_code == 401
        assert (await c.tcp.post("/login", content="token=wrong", headers=form)).status_code == 429
        # The right token still logs in.
        r = await c.tcp.post("/login", content=f"token={c.auth.token}", headers=form)
        assert r.status_code == 200 and "set-cookie" in r.headers


async def test_dashboard_files(cfg):
    cfg.web_dir.mkdir()
    (cfg.web_dir / "index.html").write_text("<h1>dashboard</h1>")
    async with clients(cfg) as c:
        assert (await c.local.get("/")).text == "<h1>dashboard</h1>"
        assert (await c.tcp.get("/")).status_code == 303
        r = await c.local.get("/api/v1/nothing")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_unknown_routes(cfg):
    async with clients(cfg) as c:
        r = await c.local.get("/api/v1/nothing", headers={"Accept": "text/plain"})
        assert r.status_code == 404 and r.text == "Not Found\n"
        r = await c.local.get("/api/v1/start")
        assert r.status_code == 405 and r.json()["error"]["code"] == "bad_request"
        assert "dashboard is not installed" in (await c.local.get("/")).text


@pytest.mark.parametrize("how", ["kill", "kill in the settle window", "stop"])
async def test_gesture_cut_off_by_the_end_of_the_emulator_is_503(cfg, fake, how):
    """A command that talks to the emulator when it goes away gets
    'unavailable', not an internal error."""
    timing = replace(FAST, settle=5) if how == "kill in the settle window" else FAST
    async with clients(cfg, timing) as c:
        assert (await c.local.post("/api/v1/input/tap", json={"x": 1, "y": 2})).status_code == 200
        body = {"x1": 100, "y1": 100, "x2": 900, "y2": 900, "duration_ms": 1500}
        swipe = asyncio.create_task(c.local.post("/api/v1/input/swipe", json=body))
        await asyncio.sleep(0.4)
        if how == "stop":
            await c.sup.stop(wait=False)
        else:
            os.kill(fake.emulator_pid(), signal.SIGKILL)
        r = await asyncio.wait_for(swipe, 30)
        assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable", r.text


@pytest.mark.parametrize("timeout", ["nan", "inf", "-1", "0", "1e400", "86401", "two"])
async def test_shell_timeout_must_be_a_usable_number(cfg, fake, timeout):
    # NaN or infinity in asyncio.timeout() can stop the event loop of the daemon.
    async with clients(cfg) as c:
        r = await c.local.post("/api/v1/shell", params={"timeout": timeout}, content=b"echo hi", headers={"Content-Type": "text/plain"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", r.text
    assert fake.launches() == []


async def test_json_timeouts_must_be_usable_numbers(cfg, fake):
    async with clients(cfg) as c:
        json_type = {"Content-Type": "application/json"}
        for path, body in (
            ("/shell", b'{"command":"id","timeout":NaN}'),
            ("/shell", b'{"command":"id","timeout":1e999}'),
            ("/shell", b'{"command":"id","timeout":100000}'),
            ("/start", b'{"timeout":100000}'),
            ("/wait", b'{"timeout":NaN}'),
            ("/restart", b'{"timeout":-5}'),
        ):
            r = await c.local.post("/api/v1" + path, content=body, headers=json_type)
            assert r.status_code == 400, (path, body, r.text)
        r = await c.local.post("/api/v1/shell", content=b'{"command":"echo ok","timeout":86400}', headers=json_type)
        assert r.status_code == 200


async def test_odd_characters_are_400_not_500(cfg, fake):
    async with clients(cfg) as c:
        for path, params in (("/screenshot", {"quality": "²"}), ("/screenshot", {"max_size": "²"}), ("/logs", {"lines": "²"})):
            r = await c.local.get("/api/v1" + path, params=params)
            assert r.status_code == 400, (path, r.text)
        json_type = {"Content-Type": "application/json"}
        for body in (b'{"command":"echo a\\u0000b"}', b'{"command":"\\u0000"}', b'{"command":"\\ud83d"}'):
            r = await c.local.post("/api/v1/shell", content=body, headers=json_type)
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", (body, r.text)
        r = await c.local.post("/api/v1/input/text", content=b'{"text":"a\\ud83d"}', headers=json_type)
        assert r.status_code == 400
        r = await c.local.post("/api/v1/shell", content=b"echo a\x00b", headers={"Content-Type": "text/plain"})
        assert r.status_code == 400
    assert fake.launches() == []


@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param("\xb2.aaa.bbb", id="superscript digit"),
        pytest.param("99999999999.aaa.\xe9", id="non-ASCII mac"),
        pytest.param("9" * 5000 + ".aaa.bbb", id="huge number"),
        pytest.param("1.2", id="short"),
        pytest.param("x.y.z", id="letters"),
    ],
)
async def test_crafted_session_cookies_are_just_refused(cfg, cookie):
    async with clients(cfg) as c:
        raw = [(b"cookie", b"appliance_session=" + cookie.encode("latin-1"))]
        r = await c.tcp.get("/api/v1/status", headers=raw)
        assert r.status_code == 401
        assert (await c.tcp.get("/", headers=raw)).status_code == 303


async def test_uploads_are_limited(cfg, fake, monkeypatch):
    async with clients(replace(cfg, max_upload_mib=1)) as c:
        params = {"path": "/sdcard/big.bin"}
        r = await c.local.put("/api/v1/files", params=params, content=b"x" * (1024**2))
        assert r.status_code == 200
        # Declared too large: refused before anything is read.
        r = await c.local.put("/api/v1/files", params=params, content=b"x" * (1024**2 + 1))
        assert r.status_code == 413 and r.json()["error"]["code"] == "too_large" and "APPLIANCE_MAX_UPLOAD_MIB" in r.text
        # Not declared (chunked): refused while it streams.
        async def chunks():
            for _ in range(5):
                yield b"x" * 300000

        r = await c.local.put("/api/v1/files", params=params, content=chunks())
        assert r.status_code == 413
        r = await c.local.post("/api/v1/apps/install", content=b"P" * (1024**2 + 1))
        assert r.status_code == 413
        assert list(cfg.upload_dir.iterdir()) == []
    # The state disk keeps some space for the AVD.
    full = shutil._ntuple_diskusage(10 * 1024**3, 10 * 1024**3 - 1024**3 - 10, 1024**3 + 10)
    monkeypatch.setattr(api.shutil, "disk_usage", lambda path: full)
    async with clients(cfg) as c:
        assert (await c.local.put("/api/v1/files", params=params, content=b"x" * 10)).status_code == 200
        assert (await c.local.put("/api/v1/files", params=params, content=b"x" * 11)).status_code == 413


async def test_responses_carry_security_headers(cfg, fake):
    async with clients(cfg) as c:
        for r in (
            await c.tcp.get("/login"),
            await c.tcp.get("/api/v1/health"),
            await c.tcp.get("/api/v1/status"),
            await c.local.get("/api/v1/status"),
            await c.local.get("/"),
        ):
            assert r.headers["x-frame-options"] == "DENY", r.url
            assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"
        assert (await c.local.get("/api/v1/logs")).headers["cache-control"] == "no-store"
        assert (await c.tcp.get("/login")).headers["cache-control"] == "no-store"
        assert (await c.local.get("/api/v1/screenshot")).headers["cache-control"] == "no-store"
