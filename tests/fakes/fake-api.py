#!/usr/bin/env python3
"""Fake appliance API for the androidctl tests.

It serves the HTTP contract of docs/ARCHITECTURE.md, including the text
formats for shell clients, on a unix socket and/or on TCP. TCP requests
need the bearer token, as on the real daemon. A tiny device lives in
memory and under STATE/device. Every request appends one line to
STATE/calls ("METHOD TARGET [CONTENT-TYPE BODY]") and its headers to
STATE/headers. Files in STATE switch on failures: boot_fail and
screenshot_fail.

Usage: fake-api.py --state DIR [--socket PATH] [--tcp HOST:PORT] [--token T]
"""

import argparse
import base64
import json
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = "2.0.0-fake"
# The smallest valid PNG: a 1x1 transparent pixel.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
STDERR_LIMIT = 16 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Device:
    state_dir: Path
    token: str
    state: str = "stopped"
    last_activity: float = field(default_factory=time.time)
    packages: set[str] = field(default_factory=lambda: {"com.example.app"})
    lock: threading.Lock = field(default_factory=threading.Lock)

    def flag(self, name: str) -> bool:
        return (self.state_dir / name).exists()

    def status(self) -> dict:
        running = self.state == "running"
        return {
            "state": self.state,
            "boot_completed": 1 if running else 0,
            "idle_seconds": int(time.time() - self.last_activity),
            "version": VERSION,
        }

    def status_line(self) -> str:
        s = self.status()
        return f"state={s['state']} boot_completed={s['boot_completed']} idle_seconds={s['idle_seconds']}\n"

    def boot(self) -> None:
        if self.flag("boot_fail"):
            self.state = "stopped"
            raise ApiError(503, "boot_failed", "android did not finish booting in 900s (adb:offline)")
        self.state = "running"

    def touch(self) -> None:
        self.last_activity = time.time()

    def device_path(self, remote: str) -> Path:
        if not remote.startswith("/") or ".." in remote.split("/"):
            raise ApiError(400, "bad_request", f"{remote}: not an absolute device path")
        return self.state_dir / "device" / remote.lstrip("/")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    device: Device
    tcp: bool

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.dispatch()

    def do_POST(self) -> None:
        self.dispatch()

    def do_PUT(self) -> None:
        self.dispatch()

    def do_DELETE(self) -> None:
        self.dispatch()

    def dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length) if length else b""
        self.record()
        url = urlsplit(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if not url.path.startswith("/api/v1/"):
                raise ApiError(404, "not_found", f"{url.path} is not an API path")
            route = url.path.removeprefix("/api/v1")
            if route != "/health" and self.tcp:
                if self.headers.get("Authorization") != f"Bearer {self.device.token}":
                    raise ApiError(401, "unauthorized", "unauthorized: a valid API token is required")
            with self.device.lock:
                self.route(self.command, route)
        except ApiError as e:
            if "text/plain" in self.headers.get("Accept", ""):
                self.send(e.status, e.message.encode(), "text/plain; charset=utf-8")
            else:
                self.send_json({"error": {"code": e.code, "message": e.message}}, e.status)

    def record(self) -> None:
        line = f"{self.command} {self.path}"
        ctype = self.headers.get("Content-Type", "")
        if self.body or ctype:
            if ctype.startswith("application/json"):
                shown = self.body.decode()
            elif ctype.startswith("text/plain"):
                shown = json.dumps(self.body.decode(), ensure_ascii=False)
            else:
                shown = f"<{len(self.body)} bytes>"
            line += f" {ctype} {shown}"
        state_dir = self.device.state_dir
        with open(state_dir / "calls", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(state_dir / "headers", "a", encoding="utf-8") as f:
            f.write(json.dumps({k.lower(): v for k, v in self.headers.items()}) + "\n")

    def send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: object, status: int = 200) -> None:
        self.send(status, json.dumps(value).encode(), "application/json")

    def send_text(self, text: str) -> None:
        self.send(200, text.encode(), "text/plain; charset=utf-8")

    def json_body(self) -> dict:
        try:
            value = json.loads(self.body or b"{}")
        except json.JSONDecodeError as e:
            raise ApiError(400, "bad_request", f"the body is not JSON: {e}") from None
        if not isinstance(value, dict):
            raise ApiError(400, "bad_request", "the body must be a JSON object")
        return value

    def text_or_json(self, key: str) -> str:
        if self.headers.get("Content-Type", "").startswith("text/plain"):
            return self.body.decode()
        value = self.json_body().get(key)
        if not isinstance(value, str):
            raise ApiError(400, "bad_request", f"{key} is missing")
        return value

    def ints(self, *keys: str) -> None:
        body = self.json_body()
        for key in keys:
            if not isinstance(body.get(key), int):
                raise ApiError(400, "bad_request", f"{key} must be an integer")

    def lifecycle_reply(self) -> None:
        if self.query.get("format") == "text":
            self.send_text(self.device.status_line())
        else:
            self.send_json(self.device.status())

    def automation(self) -> None:
        # Automation starts Android when necessary and counts as activity.
        if self.device.state != "running":
            self.device.boot()
        self.device.touch()

    def route(self, method: str, route: str) -> None:
        d = self.device
        parts = route.strip("/").split("/")
        match method, parts:
            case "GET", ["health"]:
                self.send_json({"ok": True, "version": VERSION})
            case "GET", ["status"]:
                self.lifecycle_reply()
            case "POST", ["start"]:
                self.json_body()
                d.touch()
                d.boot()
                self.lifecycle_reply()
            case "POST", ["stop"]:
                d.state = "stopped"
                self.lifecycle_reply()
            case "POST", ["restart"]:
                d.touch()
                d.state = "stopped"
                d.boot()
                self.lifecycle_reply()
            case "POST", ["wait"]:
                if d.state == "stopped":
                    raise ApiError(409, "conflict", "android is stopped")
                self.lifecycle_reply()
            case "GET", ["screenshot"]:
                self.automation()
                if d.flag("screenshot_fail"):
                    raise ApiError(503, "unavailable", "the screenshot failed")
                headers = {"X-Device-Width": "1080", "X-Device-Height": "1920", "X-Image-Scale": "1"}
                self.send(200, PNG, "image/png", headers)
            case "GET", ["ui"]:
                self.automation()
                if self.query.get("format", "xml") == "xml":
                    self.send(200, b'<?xml version="1.0"?><hierarchy rotation="0"/>', "application/xml")
                else:
                    self.send_json({"nodes": []})
            case "POST", ["input", "tap"]:
                self.ints("x", "y")
                self.automation()
                self.send_json({})
            case "POST", ["input", "long-press"]:
                self.ints("x", "y")
                self.automation()
                self.send_json({})
            case "POST", ["input", "swipe"]:
                self.ints("x1", "y1", "x2", "y2")
                self.automation()
                self.send_json({})
            case "POST", ["input", "text"]:
                self.text_or_json("text")
                self.automation()
                self.send_json({})
            case "POST", ["input", "key"]:
                self.json_body()
                self.automation()
                self.send_json({})
            case "POST", ["shell"]:
                self.shell(self.text_or_json("command"))
            case "GET", ["apps"]:
                self.automation()
                packages = sorted(d.packages | ({"android", "com.android.settings"} if self.query.get("all") == "1" else set()))
                if self.query.get("format") == "text":
                    self.send_text("".join(p + "\n" for p in packages))
                else:
                    self.send_json({"packages": packages})
            case "POST", ["apps", "install"]:
                if self.headers.get("Content-Type") != "application/vnd.android.package-archive":
                    raise ApiError(400, "bad_request", "the body must be an APK")
                self.automation()
                d.packages.add("com.example.installed")
                self.send_json({"output": "Performing Streamed Install\nSuccess\n"})
            case "POST", ["apps", package, ("launch" | "stop")]:
                self.automation()
                if package not in d.packages:
                    raise ApiError(404, "not_found", f"package {package} is not installed")
                self.send_json({})
            case "DELETE", ["apps", package]:
                self.automation()
                if package not in d.packages:
                    raise ApiError(404, "not_found", f"package {package} is not installed")
                d.packages.discard(package)
                self.send_json({"output": "Success"})
            case "GET", ["files"]:
                self.automation()
                path = d.device_path(self.query.get("path", ""))
                if not path.is_file():
                    raise ApiError(404, "not_found", f"{self.query.get('path')}: No such file or directory")
                self.send(200, path.read_bytes(), "application/octet-stream")
            case "PUT", ["files"]:
                self.automation()
                path = d.device_path(self.query.get("path", ""))
                if path.is_dir():
                    if not self.query.get("name"):
                        raise ApiError(400, "bad_request", f"{self.query['path']} is a directory; give the path of the file")
                    path = path / self.query["name"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.body)
                self.send_json({"bytes": len(self.body)})
            case "GET", ["files", "list"]:
                self.automation()
                path = d.device_path(self.query.get("path", ""))
                if not path.is_dir():
                    raise ApiError(404, "not_found", f"{self.query.get('path')}: No such file or directory")
                entries = sorted(path.iterdir())
                if self.query.get("format") == "text":
                    lines = (f"{'dir' if e.is_dir() else 'file'} {e.stat().st_size if e.is_file() else 0} {e.name}\n" for e in entries)
                    self.send_text("".join(lines))
                else:
                    self.send_json({"entries": [{"name": e.name} for e in entries]})
            case "GET", ["logs"]:
                source = self.query.get("source", "emulator")
                lines = int(self.query.get("lines", "200"))
                self.send_text("".join(f"{source} line {i}\n" for i in range(1, min(lines, 3) + 1)))
            case "POST", ["auth", "login-link"]:
                self.local_only()
                self.send_text("http://127.0.0.1:6090/login?code=fake-code\n")
            case "GET", ["auth", "token"]:
                self.local_only()
                self.send_text(d.token + "\n")
            case "POST", ["auth", "token", "rotate"]:
                self.local_only()
                d.token = "rotated-token"
                self.send_text(d.token + "\n")
            case _:
                raise ApiError(404, "not_found", f"no route for {method} {route}")

    def local_only(self) -> None:
        if self.tcp:
            raise ApiError(403, "forbidden", "forbidden: only the unix socket may manage authentication")

    def shell(self, command: str) -> None:
        self.automation()
        result = subprocess.run(
            ["sh", "-c", command], capture_output=True, timeout=10, cwd=self.device.state_dir
        )
        if self.query.get("format") != "raw":
            self.send_json(
                {
                    "exit_code": result.returncode,
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace"),
                }
            )
            return
        headers = {"X-Exit-Code": str(result.returncode)}
        stderr = result.stderr
        if len(stderr) > STDERR_LIMIT:
            stderr = stderr[:STDERR_LIMIT]
            headers["X-Stderr-Truncated"] = "1"
        if stderr:
            headers["X-Stderr"] = base64.b64encode(stderr).decode()
        self.send(200, result.stdout, "application/octet-stream", headers)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler(device: Device, tcp: bool) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"device": device, "tcp": tcp})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--tcp", help="HOST:PORT; port 0 picks a free port, written to STATE/port")
    parser.add_argument("--token", default="fake-token")
    args = parser.parse_args()
    if not args.socket and not args.tcp:
        parser.error("give --socket, --tcp or both")

    args.state.mkdir(parents=True, exist_ok=True)
    device = Device(args.state, args.token)
    servers: list[socketserver.BaseServer] = []
    if args.socket:
        if args.socket.is_socket():
            args.socket.unlink()
        servers.append(UnixServer(str(args.socket), handler(device, tcp=False)))
    if args.tcp:
        host, _, port = args.tcp.rpartition(":")
        tcp = TcpServer((host, int(port)), handler(device, tcp=True))
        (args.state / "port").write_text(f"{tcp.server_address[1]}\n")
        servers.append(tcp)
    # Like the daemon, a container stop ends with exit status 0.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    (args.state / "ready").write_text(f"{os.getpid()}\n")
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
