"""REST API, authentication, the login page and the dashboard files."""

import base64
import html
import json
import logging
import math
import shutil
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import SESSION_COOKIE, SESSION_TTL, Auth
from .backend import Backend, OperationError, compact
from .supervisor import LifecycleError

log = logging.getLogger(__name__)

API = "/api/v1"
STATUS = {
    "bad_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "rate_limited": 429,
    "internal": 500,
    "boot_failed": 503,
    "timeout": 503,
    "unavailable": 503,
}
JSON_LIMIT = 1024 * 1024
STDERR_LIMIT = 16 * 1024
# No timeout may exceed a day: a request that never ends holds a lease and
# keeps the idle stop away.
MAX_TIMEOUT = 24 * 3600
# An upload leaves this much of the state disk free for the AVD.
DISK_RESERVE = 1024**3
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = (f"{API}/health", "/login")
# Set by the unix socket listener: its requests are trusted.
LOCAL = "appliance.local"
PRINCIPAL = "appliance.principal"
# The pages carry no framing, sniffing or referrer; API answers and the
# login are never cached.
SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def local(app: ASGIApp) -> ASGIApp:
    """Mark every request of a listener as coming from the unix socket."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, LOCAL: True}
        await app(scope, receive, send)

    return wrapped


def error(conn: HTTPConnection, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    status = STATUS.get(code, 500)
    if "text/plain" in conn.headers.get("accept", ""):
        return PlainTextResponse(message + "\n", status, headers=headers)
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _forwarded(conn: HTTPConnection, name: str, trust: bool) -> str | None:
    value = conn.headers.get(name) if trust else None
    # The last entry is the one the proxy in front of us added; the
    # entries before it come from the client.
    return value.split(",")[-1].strip() if value else None


def client_key(conn: HTTPConnection, trust_proxy: bool) -> str:
    return _forwarded(conn, "x-forwarded-for", trust_proxy) or (conn.client.host if conn.client else "unknown")


def own_origin(conn: HTTPConnection, trust_proxy: bool) -> str:
    scheme = _forwarded(conn, "x-forwarded-proto", trust_proxy) or conn.url.scheme
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    host = _forwarded(conn, "x-forwarded-host", trust_proxy) or conn.headers.get("host", "")
    return f"{scheme}://{host}".lower()


class AuthMiddleware:
    """TCP requests need the bearer token or a session cookie. Requests
    with a cookie that change state must come from our own origin."""

    def __init__(self, app: ASGIApp, auth: Auth):
        self.app = app
        self.auth = auth
        self.trust_proxy = auth.cfg.trust_proxy

    def _principal(self, conn: HTTPConnection) -> str | None:
        if conn.scope.get(LOCAL):
            return "local"
        authorization = conn.headers.get("authorization", "")
        if authorization[:7].lower() == "bearer ":
            # The right token always works: clients behind one address
            # (everything on the host's loopback) share a failure count.
            if self.auth.check_token(authorization[7:].strip()):
                return "token"
            client = client_key(conn, self.trust_proxy)
            if self.auth.blocked(client):
                return "blocked"
            self.auth.failed(client)
            return None
        cookie = conn.cookies.get(SESSION_COOKIE)
        return "session" if cookie and self.auth.check_session(cookie) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        principal = self._principal(conn)
        response: Response | None = None
        public = scope["path"] in PUBLIC_PATHS
        if principal == "blocked" and not public:
            response = error(conn, "rate_limited", "too many failed logins; wait a minute")
        elif principal is None and not public:
            if scope["path"].startswith("/api/"):
                response = error(
                    conn,
                    "unauthorized",
                    "authentication required: send Authorization: Bearer <token> (androidctl token prints it)",
                    {"WWW-Authenticate": "Bearer"},
                )
            else:
                response = Response(status_code=303, headers={"Location": "/login"})
        elif (
            principal == "session"
            and (scope["type"] == "websocket" or scope["method"] not in SAFE_METHODS)
            and conn.headers.get("origin", "").lower() != own_origin(conn, self.trust_proxy)
        ):
            response = error(conn, "forbidden", "cross-origin request refused")
        if response is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await response(scope, receive, send)
            return
        await self.app({**scope, PRINCIPAL: principal}, receive, send)


async def _read(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise ApiError("bad_request", f"the request body exceeds {limit} bytes")
    return bytes(data)


def _is_text(request: Request) -> bool:
    return request.headers.get("content-type", "").split(";")[0].strip() == "text/plain"


def _clean(key: str, value: str) -> str:
    """Refuse text that cannot reach a process argument or a protobuf string."""
    if "\0" in value:
        raise ApiError("bad_request", f"'{key}' must not contain a NUL character")
    try:
        value.encode()
    except UnicodeEncodeError as e:
        raise ApiError("bad_request", f"'{key}' is not valid Unicode text") from e
    return value


async def _text(request: Request) -> str:
    try:
        return _clean("body", (await _read(request, JSON_LIMIT)).decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ApiError("bad_request", "the body is not UTF-8 text") from e


async def _json(request: Request) -> dict[str, Any]:
    body = await _read(request, JSON_LIMIT)
    if not body.strip():
        return {}
    try:
        data = json.loads(body)
    except ValueError as e:
        raise ApiError("bad_request", "the body is not valid JSON") from e
    if not isinstance(data, dict):
        raise ApiError("bad_request", "the body must be a JSON object")
    return data


_MISSING: Any = object()


def _number(data: dict[str, Any], key: str, default: Any = _MISSING) -> Any:
    value = data.get(key, default)
    if value is _MISSING:
        raise ApiError("bad_request", f"'{key}' is required")
    if value is default:
        return value
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ApiError("bad_request", f"'{key}' must be a number")
    return value


def _duration(data: dict[str, Any], key: str, default: int) -> int:
    value = _number(data, key, default)
    if not 0 <= value <= 60000:
        raise ApiError("bad_request", f"'{key}' must be between 0 and 60000")
    return int(value)


def _timeout(value: Any) -> float | None:
    # Written so that NaN fails too: asyncio.timeout(nan) can stop the event loop.
    if value is not None and not 0 < value <= MAX_TIMEOUT:
        raise ApiError("bad_request", f"'timeout' must be more than 0 and at most {MAX_TIMEOUT} seconds")
    return value


def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ApiError("bad_request", f"'{key}' must be true or false")
    return value


def _string(data: dict[str, Any], key: str, default: Any = _MISSING) -> Any:
    value = data.get(key, default)
    if value is _MISSING:
        raise ApiError("bad_request", f"'{key}' is required")
    if value is not default and not isinstance(value, str):
        raise ApiError("bad_request", f"'{key}' must be a string")
    return _clean(key, value) if isinstance(value, str) else value


def _query_int(request: Request, key: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise ApiError("bad_request", f"'{key}' must be an integer from {low} to {high}")
    return int(raw)


def _flag(request: Request, key: str) -> bool:
    return request.query_params.get(key, "").lower() in ("1", "true", "yes")


def _query_path(request: Request) -> str:
    path = request.query_params.get("path")
    if not path:
        raise ApiError("bad_request", "'path' is required")
    return path


def _as_text(request: Request) -> bool:
    return request.query_params.get("format") == "text"


async def _save(request: Request, path: Path, limit: int) -> int:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise ApiError("too_large", _too_large(limit))
    size = 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise ApiError("too_large", _too_large(limit))
            f.write(chunk)
    return size


def _too_large(limit: int) -> str:
    return f"the upload exceeds the limit of {limit // 1024**2} MiB (APPLIANCE_MAX_UPLOAD_MIB, and the free space of the state disk)"


async def _send_file(path: Path) -> AsyncIterator[bytes]:
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk
    finally:
        path.unlink(missing_ok=True)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem}}
input{{width:100%;padding:.4rem;margin:.5rem 0}}button{{padding:.4rem 1rem}}.msg{{color:#b00}}</style>
</head><body>{body}</body></html>
""",
        status,
    )


class Api:
    def __init__(self, backend: Backend, auth: Auth):
        self.backend = backend
        self.auth = auth
        self.trust_proxy = auth.cfg.trust_proxy

    def _status(self, request: Request) -> Response:
        if _as_text(request):
            return PlainTextResponse(self.backend.sup.status_line() + "\n")
        return JSONResponse(self.backend.status())

    def _empty(self) -> Response:
        return JSONResponse({})

    def _output(self, request: Request, output: str) -> Response:
        return PlainTextResponse(output + "\n") if _as_text(request) else JSONResponse({"output": output})

    def _upload_limit(self) -> int:
        """The largest upload: the configured maximum, and what the state
        disk can take while it keeps some space for the AVD."""
        cfg = self.backend.cfg
        cfg.upload_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(cfg.upload_dir).free - DISK_RESERVE
        return max(0, min(cfg.max_upload_mib * 1024**2, free))

    @staticmethod
    def _local_only(request: Request) -> None:
        if request.scope.get(PRINCIPAL) != "local":
            raise ApiError("forbidden", "this request is accepted only on the local socket")

    # Lifecycle

    async def health(self, request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__})

    async def status(self, request: Request) -> Response:
        return self._status(request)

    async def start(self, request: Request) -> Response:
        data = await _json(request)
        await self.backend.start(_bool(data, "wait", True), _timeout(_number(data, "timeout", None)))
        return self._status(request)

    async def stop(self, request: Request) -> Response:
        await self.backend.stop(_bool(await _json(request), "wait", True))
        return self._status(request)

    async def restart(self, request: Request) -> Response:
        data = await _json(request)
        await self.backend.restart(_bool(data, "wait", True), _timeout(_number(data, "timeout", None)))
        return self._status(request)

    async def wait(self, request: Request) -> Response:
        await self.backend.wait(_timeout(_number(await _json(request), "timeout", None)))
        return self._status(request)

    # Screen and input

    async def screenshot(self, request: Request) -> Response:
        fmt = request.query_params.get("format", "png")
        if fmt not in ("png", "jpeg"):
            raise ApiError("bad_request", "'format' must be png or jpeg")
        quality = _query_int(request, "quality", 85, 1, 100)
        max_size = _query_int(request, "max_size", 0, 16, 100000) or None
        shot = await self.backend.screenshot(fmt, quality, max_size)
        headers = {
            "X-Device-Width": str(shot.device_width),
            "X-Device-Height": str(shot.device_height),
            "X-Image-Scale": f"{shot.scale:.6g}",
            "Cache-Control": "no-store",
        }
        return Response(shot.data, media_type=shot.content_type, headers=headers)

    async def ui(self, request: Request) -> Response:
        fmt = request.query_params.get("format", "xml")
        if fmt not in ("xml", "compact"):
            raise ApiError("bad_request", "'format' must be xml or compact")
        xml = await self.backend.ui()
        if fmt == "compact":
            return JSONResponse({"nodes": compact(xml)})
        return Response(xml, media_type="application/xml")

    async def tap(self, request: Request) -> Response:
        data = await _json(request)
        await self.backend.tap(_number(data, "x"), _number(data, "y"))
        return self._empty()

    async def long_press(self, request: Request) -> Response:
        data = await _json(request)
        await self.backend.long_press(_number(data, "x"), _number(data, "y"), _duration(data, "duration_ms", 800))
        return self._empty()

    async def swipe(self, request: Request) -> Response:
        data = await _json(request)
        points = [_number(data, key) for key in ("x1", "y1", "x2", "y2")]
        await self.backend.swipe(*points, _duration(data, "duration_ms", 300))
        return self._empty()

    async def text(self, request: Request) -> Response:
        if _is_text(request):
            text, method = await _text(request), request.query_params.get("method", "paste")
        else:
            data = await _json(request)
            text, method = _string(data, "text"), _string(data, "method", "paste")
        await self.backend.text(text, method)
        return self._empty()

    async def key(self, request: Request) -> Response:
        await self.backend.key(_string(await _json(request), "key"))
        return self._empty()

    # Shell and apps

    async def shell(self, request: Request) -> Response:
        if _is_text(request):
            command = await _text(request)
            raw = request.query_params.get("timeout")
            try:
                timeout = float(raw) if raw else 300.0
            except ValueError as e:
                raise ApiError("bad_request", "'timeout' must be a number") from e
        else:
            data = await _json(request)
            command, timeout = _string(data, "command"), _number(data, "timeout", 300)
        result = await self.backend.shell(command, _timeout(timeout) or 300)
        if request.query_params.get("format") == "raw":
            headers = {
                "X-Exit-Code": str(result.exit_code),
                "X-Stderr": base64.b64encode(result.stderr[:STDERR_LIMIT]).decode(),
            }
            if len(result.stderr) > STDERR_LIMIT:
                headers["X-Stderr-Truncated"] = "1"
            return Response(result.stdout, media_type="application/octet-stream", headers=headers)
        return JSONResponse(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace"),
            }
        )

    async def apps(self, request: Request) -> Response:
        packages = await self.backend.apps(_flag(request, "all"))
        if _as_text(request):
            return PlainTextResponse("".join(f"{p}\n" for p in packages))
        return JSONResponse({"packages": packages})

    async def install(self, request: Request) -> Response:
        apk = self.backend.temp_path().with_suffix(".apk")
        try:
            if await _save(request, apk, self._upload_limit()) == 0:
                raise ApiError("bad_request", "the body must be the APK file")
            output = await self.backend.install(
                apk, _flag(request, "replace"), _flag(request, "grant"), _flag(request, "downgrade"), _flag(request, "test")
            )
        finally:
            apk.unlink(missing_ok=True)
        return self._output(request, output)

    async def launch(self, request: Request) -> Response:
        await self.backend.launch(request.path_params["package"])
        return self._empty()

    async def force_stop(self, request: Request) -> Response:
        await self.backend.force_stop(request.path_params["package"])
        return self._empty()

    async def uninstall(self, request: Request) -> Response:
        output = await self.backend.uninstall(request.path_params["package"], _flag(request, "keep_data"))
        return self._output(request, output)

    # Files and logs

    async def pull(self, request: Request) -> Response:
        local = await self.backend.pull(_query_path(request))
        headers = {"Content-Length": str(local.stat().st_size)}
        return StreamingResponse(_send_file(local), media_type="application/octet-stream", headers=headers)

    async def push(self, request: Request) -> Response:
        path = _query_path(request)
        local = self.backend.temp_path()
        try:
            await _save(request, local, self._upload_limit())
            size = await self.backend.push(local, path, request.query_params.get("name"))
        finally:
            local.unlink(missing_ok=True)
        return JSONResponse({"bytes": size})

    async def list_files(self, request: Request) -> Response:
        entries = await self.backend.list_files(_query_path(request))
        if _as_text(request):
            return PlainTextResponse("".join(f"{e['type']} {e['size']} {e['name']}\n" for e in entries))
        return JSONResponse({"entries": entries})

    async def logs(self, request: Request) -> Response:
        text = self.backend.logs(request.query_params.get("source", "emulator"), _query_int(request, "lines", 200, 1, 100000))
        return PlainTextResponse(text + "\n" if text else "")

    # Authentication

    async def login_link(self, request: Request) -> Response:
        self._local_only(request)
        url = self.auth.login_link()
        return PlainTextResponse(url + "\n") if _as_text(request) else JSONResponse({"url": url})

    async def token(self, request: Request) -> Response:
        self._local_only(request)
        token = self.auth.token
        return PlainTextResponse(token + "\n") if _as_text(request) else JSONResponse({"token": token})

    async def rotate_token(self, request: Request) -> Response:
        self._local_only(request)
        token = self.auth.rotate()
        log.info("the API token was rotated; dashboard sessions ended")
        return PlainTextResponse(token + "\n") if _as_text(request) else JSONResponse({"token": token})

    def _login_form(self, message: str = "", status: int = 200) -> HTMLResponse:
        note = f'<p class="msg">{html.escape(message)}</p>' if message else ""
        return _page(
            "Android appliance: log in",
            f"""<h1>Android appliance</h1>{note}
<form method="post" action="/login">
<label>API token <input type="password" name="token" autocomplete="current-password" autofocus required></label>
<button>Log in</button></form>
<p>Print the token with <code>androidctl token</code>, or open a one-time link from <code>androidctl dashboard</code>.</p>""",
            status,
        )

    def _logged_in(self, request: Request) -> Response:
        # Move on with a page, not a redirect: a SameSite=Strict cookie set
        # on a navigation from another site (a link in a terminal or chat)
        # is not sent on that navigation's redirect.
        response = _page("Android appliance", '<meta http-equiv="refresh" content="0;url=/"><p><a href="/">Continue</a></p>')
        https = own_origin(request, self.trust_proxy).startswith("https:")
        response.set_cookie(
            SESSION_COOKIE, self.auth.new_session(), max_age=SESSION_TTL, path="/", secure=https, httponly=True, samesite="strict"
        )
        return response

    async def login_page(self, request: Request) -> Response:
        code = request.query_params.get("code")
        if code is None:
            return self._login_form()
        if self.auth.use_login_code(code):
            return self._logged_in(request)
        return self._login_form("This login link is invalid or expired. Get a new one with: androidctl dashboard", 401)

    async def login(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin.lower() != own_origin(request, self.trust_proxy):
            return self._login_form("Cross-origin login refused.", 403)
        form = urllib.parse.parse_qs((await _read(request, 4096)).decode(errors="replace"))
        if self.auth.check_token(form.get("token", [""])[0].strip()):
            return self._logged_in(request)
        client = client_key(request, self.trust_proxy)
        if self.auth.blocked(client):
            return self._login_form("Too many failed logins. Wait a minute.", 429)
        self.auth.failed(client)
        return self._login_form("Wrong token.", 401)

    async def placeholder(self, request: Request) -> Response:
        return _page(
            "Android appliance",
            f'<h1>Android appliance</h1><p>The dashboard is not installed. The API is at <a href="{API}/status">{API}/status</a>.</p>',
        )

    def routes(self, web_dir: Path) -> list[BaseRoute]:
        def r(path: str, endpoint: Callable[[Request], Awaitable[Response]], method: str) -> Route:
            return Route(path, endpoint, methods=[method])

        api = [
            r("/health", self.health, "GET"),
            r("/status", self.status, "GET"),
            r("/start", self.start, "POST"),
            r("/stop", self.stop, "POST"),
            r("/restart", self.restart, "POST"),
            r("/wait", self.wait, "POST"),
            r("/screenshot", self.screenshot, "GET"),
            r("/ui", self.ui, "GET"),
            r("/input/tap", self.tap, "POST"),
            r("/input/long-press", self.long_press, "POST"),
            r("/input/swipe", self.swipe, "POST"),
            r("/input/text", self.text, "POST"),
            r("/input/key", self.key, "POST"),
            r("/shell", self.shell, "POST"),
            r("/apps", self.apps, "GET"),
            r("/apps/install", self.install, "POST"),
            r("/apps/{package}/launch", self.launch, "POST"),
            r("/apps/{package}/stop", self.force_stop, "POST"),
            r("/apps/{package}", self.uninstall, "DELETE"),
            r("/files", self.pull, "GET"),
            r("/files", self.push, "PUT"),
            r("/files/list", self.list_files, "GET"),
            r("/logs", self.logs, "GET"),
            r("/auth/login-link", self.login_link, "POST"),
            r("/auth/token", self.token, "GET"),
            r("/auth/token/rotate", self.rotate_token, "POST"),
        ]
        routes: list[BaseRoute] = [
            Mount(API, routes=api),
            Route("/login", self.login_page, methods=["GET"]),
            Route("/login", self.login, methods=["POST"]),
        ]
        if (web_dir / "index.html").is_file():
            routes.append(Mount("/", StaticFiles(directory=web_dir, html=True)))
        else:
            routes.append(Route("/", self.placeholder, methods=["GET"]))
        return routes


async def _handle(request: Request, exc: Exception) -> Response:
    if isinstance(exc, ApiError | OperationError | LifecycleError):
        return error(request, exc.code, str(exc))
    if isinstance(exc, HTTPException):
        code = {404: "not_found", 401: "unauthorized", 403: "forbidden"}.get(exc.status_code, "bad_request")
        response = error(request, code, exc.detail)
        response.status_code = exc.status_code
        return response
    # Starlette re-raises the exception after this response, and uvicorn logs it.
    return error(request, "internal", "internal error; see the daemon log")


def hardened(app: ASGIApp) -> ASGIApp:
    """Add the security headers to every HTTP response."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        private = scope["path"].startswith(API) or scope["path"] == "/login"

        async def send_hardened(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if private:
                    headers.setdefault("cache-control", "no-store")
            await send(message)

        await app(scope, receive, send_hardened)

    return wrapped


def create_app(backend: Backend, auth: Auth) -> ASGIApp:
    routes = Api(backend, auth).routes(backend.cfg.web_dir)
    handlers = {cls: _handle for cls in (ApiError, OperationError, LifecycleError, HTTPException, Exception)}
    app = Starlette(routes=routes, exception_handlers=handlers)  # type: ignore[arg-type]
    return hardened(AuthMiddleware(app, auth))
