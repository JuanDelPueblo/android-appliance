"""Tests for the Hermes plugin and dashboard backend. They use a fake
androidctl, so they need neither Hermes nor an emulator."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_ANDROIDCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  status) echo "state=running boot_completed=1 idle_seconds=7" ;;
  screenshot) echo "${2:-/tmp/shot.png}" ;;
  display) echo "${FAKE_DISPLAY_URL:-http://127.0.0.1:6090/vnc.html}" ;;
  stop) if [ "$FAKE_STOP_FAIL" = 1 ]; then echo "androidctl: shutdown failed" >&2; exit 1; fi ;;
esac
"""


def load_package():
    spec = importlib.util.spec_from_file_location(
        "android_appliance_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeAndroidctl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.exe = tmp / "androidctl"
        self.exe.write_text(FAKE_ANDROIDCTL)
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IEXEC)
        self.log = tmp / "log"
        os.environ["ANDROIDCTL"] = str(self.exe)
        os.environ["FAKE_LOG"] = str(self.log)

    def tearDown(self):
        for key in ("ANDROIDCTL", "FAKE_LOG", "FAKE_DISPLAY_URL", "FAKE_STOP_FAIL"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class ToolTests(FakeAndroidctl):
    def setUp(self):
        super().setUp()
        self.pkg = load_package()
        self.tools = self.pkg.tools
        self.tools.configure("")

    def test_status_parses_fields(self):
        result = json.loads(self.tools.android_status({}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["state"], "running")
        self.assertEqual(result["status"]["boot_completed"], "1")

    def test_tap_and_swipe_arguments(self):
        self.tools.android_tap({"x": 10, "y": 20})
        self.tools.android_swipe({"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 300})
        self.assertEqual(self.calls(), ["tap 10 20", "swipe 1 2 3 4 300"])

    def test_key_names_get_prefix(self):
        self.tools.android_key({"key": "home"})
        self.tools.android_key({"key": "66"})
        self.assertEqual(self.calls(), ["key KEYCODE_HOME", "key 66"])

    def test_screenshot_returns_path(self):
        result = json.loads(self.tools.android_screenshot({"path": "/tmp/a.png"}))
        self.assertEqual(result["path"], "/tmp/a.png")

    def test_failure_reports_stderr(self):
        os.environ["FAKE_STOP_FAIL"] = "1"
        result = self.tools.run(["stop"])
        self.assertFalse(result["ok"])
        self.assertIn("shutdown failed", result["error"])

    def test_missing_androidctl(self):
        os.environ["ANDROIDCTL"] = "/nonexistent"
        self.tools.FALLBACK_PATH = "/nonexistent"
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            self.assertFalse(self.tools.available())
            self.assertFalse(json.loads(self.tools.android_status({}))["ok"])
        finally:
            os.environ["PATH"] = old_path

    def test_register_matches_manifest(self):
        import yaml  # noqa: PLC0415

        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())

        class Ctx:
            def __init__(self):
                self.tools, self.skills = [], []

            def get_config(self, key, default=None):
                return default

            def register_tool(self, name, toolset, schema, handler, **kwargs):
                assert schema["name"] == name
                self.tools.append(name)

            def register_skill(self, name, path, description=""):
                assert Path(path).is_file()
                self.skills.append(name)

        ctx = Ctx()
        self.pkg.register(ctx)
        self.assertEqual(sorted(ctx.tools), sorted(manifest["provides_tools"]))
        self.assertEqual(ctx.skills, ["android"])


try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi is not installed")
class DashboardTestBase(FakeAndroidctl):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("android_dashboard_api", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        self.api = api
        self.settings = {}
        self.settings_patch = patch.object(api, "_settings", lambda: self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.enabled_patch = patch.object(api, "_plugin_enabled", return_value=True)
        self.enabled_mock = self.enabled_patch.start()
        self.addCleanup(self.enabled_patch.stop)
        app = fastapi.FastAPI()
        app.state.auth_required = True
        app.include_router(api.router, prefix="/api/plugins/android-appliance")
        self.client = TestClient(app)



@unittest.skipIf(TestClient is None, "fastapi is not installed")
class DashboardTests(DashboardTestBase):
    def test_status(self):
        data = self.client.get("/api/plugins/android-appliance/status").json()
        self.assertEqual(data["state"], "running")
        self.assertTrue(data["display_url"].startswith("/api/plugins/android-appliance/display/vnc.html?"))
        self.assertNotIn("127.0.0.1", data["display_url"])

    def test_start_does_not_block(self):
        data = self.client.post("/api/plugins/android-appliance/actions/start").json()
        self.assertTrue(data["ok"])
        self.assertIn("start --no-wait", self.calls())

    def test_unknown_action(self):
        response = self.client.post("/api/plugins/android-appliance/actions/wipe")
        self.assertEqual(response.status_code, 404)

    def test_removed_actions(self):
        for action in ("suspend", "resume"):
            self.assertEqual(self.client.post(f"/api/plugins/android-appliance/actions/{action}").status_code, 404)

    def test_failed_action(self):
        os.environ["FAKE_STOP_FAIL"] = "1"
        data = self.client.post("/api/plugins/android-appliance/actions/stop").json()
        self.assertFalse(data["ok"])
        self.assertIn("shutdown failed", data["error"])

    def test_execution_error_is_reported(self):
        with patch.object(self.api.subprocess, "run", side_effect=PermissionError("denied")):
            data = self.client.get("/api/plugins/android-appliance/status").json()
        self.assertFalse(data["ok"])
        self.assertIn("cannot run androidctl", data["error"])

    def test_public_url_override(self):
        self.settings["display_url"] = "https://android.example.net/vnc.html?autoconnect=true"
        data = self.client.post("/api/plugins/android-appliance/display/session").json()
        self.assertEqual(data["url"], self.settings["display_url"])
        self.assertEqual(self.calls(), [])

    def test_invalid_public_url(self):
        self.settings["display_url"] = "javascript:alert(1)"
        data = self.client.get("/api/plugins/android-appliance/status").json()
        self.assertIsNone(data["display_url"])
        self.assertIn("HTTP(S)", data["display_error"])

    def test_malformed_urls_report_configuration_error(self):
        for url in ("http://[invalid", "https://example.net:invalid/vnc.html"):
            self.settings["display_url"] = url
            data = self.client.get("/api/plugins/android-appliance/status").json()
            self.assertIsNone(data["display_url"])
            self.assertIn("malformed", data["display_error"])
        self.settings.clear()
        os.environ["FAKE_DISPLAY_URL"] = "http://[invalid"
        response = self.client.post("/api/plugins/android-appliance/display/session")
        self.assertEqual(response.status_code, 503)

    def test_local_target_cannot_be_remote(self):
        os.environ["FAKE_DISPLAY_URL"] = "http://example.net/vnc.html"
        response = self.client.post("/api/plugins/android-appliance/display/session")
        self.assertEqual(response.status_code, 503)

    def test_token_only_remote_dashboard_needs_public_url(self):
        self.client.app.state.auth_required = False
        response = self.client.post("/api/plugins/android-appliance/display/session")
        self.assertEqual(response.status_code, 503)

    def test_missing_wrong_origin_expired_and_disabled_tickets_rejected(self):
        from starlette.websockets import WebSocketDisconnect

        def rejected(ticket="", origin="http://testserver"):
            with self.assertRaises(WebSocketDisconnect) as exc:
                with self.client.websocket_connect(
                    f"/api/plugins/android-appliance/display/websockify?ticket={ticket}",
                    headers={"origin": origin},
                ):
                    pass
            self.assertEqual(exc.exception.code, 1008)

        rejected()
        self.api._tickets["foreign"] = (self.api.time.monotonic() + 60, "testserver")
        rejected("foreign", "https://attacker.example")
        self.api._tickets["expired"] = (0, "testserver")
        rejected("expired")
        self.enabled_mock.return_value = False
        self.api._tickets["disabled"] = (self.api.time.monotonic() + 60, "testserver")
        rejected("disabled")
        self.assertEqual(self.calls(), [])


class DisplayServer:
    """A real loopback HTTP/WebSocket peer; no emulator or host units."""

    def __enter__(self):
        from aiohttp import web

        self.headers = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

        async def handler(request):
            self.headers.append(dict(request.headers))
            if request.path == "/websockify":
                ws = web.WebSocketResponse(protocols=["binary"])
                await ws.prepare(request)
                await ws.send_bytes(b"RFB 003.008\n")
                async for msg in ws:
                    if isinstance(msg.data, bytes):
                        await ws.send_bytes(msg.data)
                return ws
            if request.path == "/redirect":
                raise web.HTTPFound("http://example.net/")
            return web.Response(text="noVNC" if request.path == "/vnc.html" else "export default 1;", content_type="text/html" if request.path == "/vnc.html" else "text/javascript")

        async def start():
            app = web.Application()
            app.router.add_get("/{path:.*}", handler)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.ready.set()

        def serve():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("display test server did not start")
        os.environ["FAKE_DISPLAY_URL"] = f"http://127.0.0.1:{self.port}/vnc.html"
        return self

    def __exit__(self, *args):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


@unittest.skipIf(TestClient is None, "fastapi is not installed")
class DisplayProxyTests(DashboardTestBase):
    def test_proxy_assets_and_redirect_boundary(self):
        with DisplayServer() as peer:
            for path in ("vnc.html", "core/rfb.js"):
                response = self.client.get("/api/plugins/android-appliance/display/" + path, headers={"Cookie": "secret=abc", "Authorization": "Bearer secret"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.client.get("/api/plugins/android-appliance/display/redirect").status_code, 502)
        for headers in peer.headers:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
        self.assertEqual(self.client.get("/api/plugins/android-appliance/display/core/%2e%2e/secret").status_code, 404)
        self.assertEqual(self.client.get("/api/plugins/android-appliance/display/websockify").status_code, 404)

    def test_websocket_binary_roundtrip_and_ticket_replay(self):
        from starlette.websockets import WebSocketDisconnect

        with DisplayServer() as peer:
            response = self.client.post("/api/plugins/android-appliance/display/session")
            url = response.json()["url"]
            path = parse_qs(urlsplit(url).query)["path"][0]
            with self.client.websocket_connect(path, subprotocols=["binary"], headers={"origin": "http://testserver"}) as ws:
                self.assertEqual(ws.accepted_subprotocol, "binary")
                self.assertEqual(ws.receive_bytes(), b"RFB 003.008\n")
                ws.send_bytes(b"input")
                self.assertEqual(ws.receive_bytes(), b"input")
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(path, headers={"origin": "http://testserver"}):
                    pass
        self.assertTrue(any(headers.get("Upgrade") == "websocket" for headers in peer.headers))


if __name__ == "__main__":
    unittest.main()
