"""Tests for the Hermes plugin and dashboard backend. They use a fake
androidctl, so they need neither Hermes nor an emulator."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_ANDROIDCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  status) echo "state=running boot_completed=1 idle_seconds=7" ;;
  screenshot) echo "${2:-/tmp/shot.png}" ;;
  display) echo "http://127.0.0.1:6080/vnc.html" ;;
  suspend) echo "androidctl: android is not running" >&2; exit 1 ;;
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
        os.environ.pop("ANDROIDCTL", None)
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
        result = self.tools.run(["suspend"])
        self.assertFalse(result["ok"])
        self.assertIn("not running", result["error"])

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
class DashboardTests(FakeAndroidctl):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("android_dashboard_api", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = fastapi.FastAPI()
        app.include_router(api.router, prefix="/api/plugins/android-appliance")
        self.client = TestClient(app)

    def test_status(self):
        data = self.client.get("/api/plugins/android-appliance/status").json()
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["display_url"], "http://127.0.0.1:6080/vnc.html")

    def test_start_does_not_block(self):
        data = self.client.post("/api/plugins/android-appliance/actions/start").json()
        self.assertTrue(data["ok"])
        self.assertIn("start --no-wait", self.calls())

    def test_unknown_action(self):
        response = self.client.post("/api/plugins/android-appliance/actions/wipe")
        self.assertEqual(response.status_code, 404)

    def test_failed_action(self):
        data = self.client.post("/api/plugins/android-appliance/actions/suspend").json()
        self.assertFalse(data["ok"])
        self.assertIn("not running", data["error"])


if __name__ == "__main__":
    unittest.main()
