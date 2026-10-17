"""session.py: the WebSocket protocol of the dashboard, against the fake emulator."""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import signal
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace

import httpx
import pytest

from appliance import api, session, webrtc
from appliance.auth import Auth
from appliance.backend import Backend, LogBuffer
from appliance.session import Hub
from appliance.supervisor import Supervisor, Timing
from appliance.turn import Turn
from asgi_ws import WebSocketClient
from conftest import FAST

ORIGIN = "http://127.0.0.1:6090"


@pytest.fixture(autouse=True)
def quick_errors(monkeypatch):
    """A test sees every error message."""
    monkeypatch.setattr(session, "ERROR_BURST", 1000)


@dataclass
class Daemon:
    cfg: object
    sup: Supervisor
    hub: Hub
    turn: Turn
    auth: Auth
    app: object

    def client(self, **headers: str) -> WebSocketClient:
        """A session over the unix socket (trusted)."""
        return WebSocketClient(api.local(self.app), headers=headers)

    def tcp(self, **headers: str) -> WebSocketClient:
        return WebSocketClient(self.app, headers=headers)

    async def running(self) -> None:
        await self.sup.start(timeout=20)


@contextlib.asynccontextmanager
async def daemon(cfg, timing: Timing = FAST) -> AsyncIterator[Daemon]:
    sup = Supervisor(cfg, timing)
    backend = Backend(sup, LogBuffer())
    turn = Turn(cfg)
    hub = Hub(backend, turn)
    await turn.start()
    auth = Auth(cfg)
    d = Daemon(cfg, sup, hub, turn, auth, api.create_app(backend, auth, hub))
    try:
        yield d
    finally:
        await sup.shutdown()
        await hub.close()
        await turn.stop()


async def eventually(condition: Callable[[], object], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


def touches(fake) -> list[tuple]:
    """(x, y, id, pressure) of every touch point the emulator got."""
    return [(t["x"], t["y"], t["id"], t["pressure"]) for e in fake.inputs() if e["type"] == "touch" for t in e["touches"]]


def keys(fake) -> list[tuple]:
    return [(e["key"], e["keyCode"], e["eventType"]) for e in fake.inputs() if e["type"] == "key"]


async def watch(ws: WebSocketClient, d: Daemon) -> dict:
    """Start a stream and read it up to the offer; return the configuration."""
    await ws.send({"type": "stream", "action": "start"})
    await ws.of_type("stream", state="starting")
    configured = await ws.of_type("stream", state="configured")
    await ws.until(lambda m: m["type"] == "jsep" and m["data"].get("type") == "offer")
    return configured


# Connecting and status


async def test_hello_and_the_status_follow_the_lifecycle(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        hello = await ws.receive()
        assert hello["type"] == "hello" and hello["device"] == {"width": 1080, "height": 1920}
        assert hello["version"]
        status = await ws.receive()
        assert status["type"] == "status" and status["state"] == "stopped" and status["viewers"] == 0
        assert set(status) - {"type"} == set(d.sup.status())
        await d.sup.start(wait=False)
        states = []
        # Every change is pushed; the changes of one step are one message.
        await ws.until(lambda m: m["type"] == "status" and states.append((m["state"], m["phase"])) is None and m["state"] == "running")
        assert states[0][0] == "starting" and states[-1] == ("running", None)
        assert ("starting", "booting") in states or ("starting", "waiting for adb") in states
        await d.sup.stop()
        stopped = await ws.of_type("status", state="stopped")
        assert stopped["boot_completed"] is False


async def test_status_is_pushed_again_now_and_then(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "STATUS_REFRESH", 0.2)
    async with daemon(cfg) as d, d.client() as ws:
        await ws.of_type("status")
        second = await ws.of_type("status")
        assert second["state"] == "stopped"


async def test_a_failed_boot_is_in_the_status(cfg, fake):
    fake.configure(mode="crash")
    async with daemon(cfg) as d, d.client() as ws:
        await ws.of_type("status")
        with pytest.raises(Exception, match="exited"):
            await d.sup.start(timeout=20)
        failed = await ws.until(lambda m: m["type"] == "status" and m["last_error"] is not None)
        assert failed["last_error"]["kind"] == "crash"


async def test_ping_is_answered(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await ws.send({"type": "ping", "t": 1234.5})
        assert await ws.of_type("pong") == {"type": "pong", "t": 1234.5}
        assert fake.launches() == []


# Authentication


async def test_a_session_needs_authentication(cfg, fake):
    async with daemon(cfg) as d:
        async with d.tcp() as ws:
            assert not ws.accepted and ws.close_code == 1008
        async with d.tcp(authorization="Bearer wrong") as ws:
            assert not ws.accepted and ws.close_code == 1008
        async with d.tcp(authorization=f"Bearer {d.auth.token}") as ws:
            assert ws.accepted and (await ws.receive())["type"] == "hello"
        async with d.client() as ws:
            assert ws.accepted


async def test_a_cookie_session_needs_our_origin(cfg, fake):
    async with daemon(cfg) as d:
        cookie = f"appliance_session={d.auth.new_session()}"
        async with d.tcp(cookie=cookie) as ws:
            assert not ws.accepted
        async with d.tcp(cookie=cookie, origin="http://evil.example") as ws:
            assert not ws.accepted
        async with d.tcp(cookie=cookie, origin=ORIGIN) as ws:
            assert ws.accepted
        # The bearer token is for programs: no Origin needed.
        async with d.tcp(authorization=f"Bearer {d.auth.token}") as ws:
            assert ws.accepted
        async with d.tcp(cookie="appliance_session=1.2.3", origin=ORIGIN) as ws:
            assert not ws.accepted


# Streams


def username_ok(secret: str, username: str, credential: str) -> bool:
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(credential, base64.b64encode(digest).decode())


async def test_stream_lifecycle(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "stream", "action": "start"})
        # The order of the contract: starting, configured, then the emulator's messages.
        assert await ws.next() == {"type": "stream", "state": "starting"}
        configured = await ws.next()
        assert configured["type"] == "stream" and configured["state"] == "configured"
        assert configured["iceTransportPolicy"] == "relay"
        [server] = configured["iceServers"]
        assert server["urls"] == ["turn:127.0.0.1:3478?transport=udp", "turn:127.0.0.1:3478?transport=tcp"]
        expiry, _, ident = server["username"].partition(":")
        assert 23 * 3600 < int(expiry) - time.time() <= 24 * 3600 and len(ident) >= 8
        assert username_ok(Auth(cfg).token and (cfg.secrets_dir / "turn-secret").read_text().strip(), server["username"], server["credential"])
        # The emulator's offer and candidates follow; its start message and
        # the empty end-of-candidates message do not.
        offer = await ws.next()
        assert offer["type"] == "jsep" and offer["data"]["type"] == "offer" and offer["data"]["sdp"].startswith("v=0")
        candidates = [await ws.next(), await ws.next()]
        assert [c["data"] for c in candidates] == [
            {"candidate": f"candidate:1 1 udp 2122194687 192.168.1.243 {port} typ host generation 0", "sdpMid": "0", "sdpMLineIndex": 0}
            for port in (34904, 40000)
        ]
        assert await ws.quiet(0.3) == []
        # The emulator is told to use no Google STUN server.
        [request] = fake.rtc("request")
        assert request["urls"] == ["stun:127.0.0.1:3478"]
        assert d.sup.viewers == 1
        assert [m for m in ws.messages if m["type"] == "status"][-1]["viewers"] == 1

        # The answer and the candidates go to the emulator; an empty candidate does not.
        await ws.send({"type": "jsep", "data": {"type": "answer", "sdp": "v=0\r\nanswer"}})
        await ws.send({"type": "jsep", "data": {"candidate": "candidate:9 1 udp 1 10.0.0.1 5 typ relay", "sdpMid": "0", "sdpMLineIndex": 0}})
        await ws.send({"type": "jsep", "data": {"candidate": "", "sdpMid": "0", "sdpMLineIndex": 0}})
        await eventually(lambda: len(fake.rtc("send")) == 2)
        await asyncio.sleep(0.2)
        sent = [e["message"] for e in fake.rtc("send")]
        assert sent == [
            {"type": "answer", "sdp": "v=0\r\nanswer"},
            {"candidate": "candidate:9 1 udp 1 10.0.0.1 5 typ relay", "sdpMid": "0", "sdpMLineIndex": 0},
        ]

        await ws.send({"type": "stream", "action": "stop"})
        assert (await ws.of_type("stream", state="ended"))["reason"] == "stopped"
        await eventually(lambda: d.sup.viewers == 0)
        # The emulator holds an answered stream until its ICE goes away (17 s on
        # the real one); the bridge keeps counting it until then.
        assert d.hub.bridge.streams == 1 and not fake.rtc("receive-closed")
        (fake.root / "rtc-bye").touch()
        await eventually(lambda: d.hub.bridge.streams == 0 and fake.rtc("receive-closed"))


async def test_stream_start_counts_as_activity(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        d.sup._activity = time.time() - 500
        assert d.sup.idle_seconds() >= 500
        await ws.send({"type": "ping", "t": 1})
        await ws.of_type("pong")
        await asyncio.sleep(0.2)
        assert d.sup.idle_seconds() >= 500
        await watch(ws, d)
        assert d.sup.idle_seconds() < 5


async def test_the_turn_host_is_the_host_of_the_page(cfg, fake):
    async with daemon(replace(cfg, turn_port=47081)) as d:
        await d.running()
        for host, expected in (
            ("android.example.net:6090", "android.example.net"),
            ("[fd7a::1]:6090", "[fd7a::1]"),
            ("192.168.1.9", "192.168.1.9"),
        ):
            async with d.client(host=host) as ws:
                configured = await watch(ws, d)
                assert configured["iceServers"][0]["urls"][0] == f"turn:{expected}:47081?transport=udp", host
        async with d.client(host="bad host<>") as ws:
            await ws.send({"type": "stream", "action": "start"})
            error = await ws.of_type("error")
            assert error["code"] == "bad_request" and "Host" in error["message"]


async def test_the_configured_turn_host_wins(cfg, fake):
    async with daemon(replace(cfg, turn_host="turn.example.org")) as d, d.client(host="android.example.net") as ws:
        await d.running()
        configured = await watch(ws, d)
        assert configured["iceServers"][0]["urls"][1] == "turn:turn.example.org:3478?transport=tcp"


async def test_the_proxy_host_names_the_turn_host(cfg, fake):
    async with daemon(replace(cfg, trust_proxy=True)) as d, d.client(**{"x-forwarded-host": "proxy.example.net"}) as ws:
        await d.running()
        configured = await watch(ws, d)
        assert configured["iceServers"][0]["urls"][0].startswith("turn:proxy.example.net:")


async def test_streams_need_a_running_android_and_never_start_it(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await ws.send({"type": "stream", "action": "start"})
        error = await ws.of_type("error")
        assert error["code"] == "not_running"
        assert (await ws.quiet(0.2)) == []
        assert fake.launches() == [] and d.sup.viewers == 0


async def test_streams_need_the_turn_server(cfg, fake):
    fake.configure(turn_mode="crash")
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await eventually(lambda: not d.turn.running)
        await ws.send({"type": "stream", "action": "start"})
        error = await ws.of_type("error")
        assert error["code"] == "unavailable" and "TURN" in error["message"]
        assert fake.rtc("request") == []


async def test_a_viewer_that_leaves_unanswered_gets_a_throwaway_answer(cfg, fake):
    fake.configure(rtc_release_after=0.2)
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        await ws.send({"type": "stream", "action": "stop"})
        await ws.of_type("stream", state="ended")
        await eventually(lambda: len(fake.rtc("send")) == 2)
        answer, candidate = (e["message"] for e in fake.rtc("send"))
        assert answer["type"] == "answer"
        sdp = answer["sdp"]
        # An answer for the three sections of the offer, bundled, that cannot connect.
        assert sdp.count("\nm=") == 3 and "a=group:BUNDLE 0 1 2" in sdp and sdp.count("a=setup:active") == 3
        assert "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n" in sdp and "a=rtpmap:96 VP8/90000" in sdp
        assert "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" in sdp and "m=application 9 UDP/DTLS/SCTP webrtc-datachannel" in sdp
        assert "a=sctp-port:5000" in sdp and sdp.count("a=recvonly") == 2 and "a=sendrecv" not in sdp
        assert len({line for line in sdp.split("\r\n") if line.startswith("a=ice-ufrag")}) == 1
        # The address is the emulator's own (the fake offers 192.168.1.243), on a port nobody uses.
        assert candidate["candidate"] == "candidate:1 1 udp 2113937151 192.168.1.243 40000 typ host generation 0"
        # The emulator let go of it, and the bridge forgot it.
        await eventually(lambda: d.hub.bridge.streams == 0)
        await eventually(lambda: fake.rtc("receive-closed"))


async def test_an_answered_stream_is_not_answered_again(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        await ws.send({"type": "jsep", "data": {"type": "answer", "sdp": "v=0\r\nreal"}})
        await eventually(lambda: fake.rtc("send"))
        await ws.send({"type": "stream", "action": "stop"})
        await ws.of_type("stream", state="ended")
        # The emulator ends it when ICE goes away (17 s on the real one).
        await asyncio.sleep(0.3)
        assert len(fake.rtc("send")) == 1
        assert d.hub.bridge.streams == 1
        (fake.root / "rtc-bye").touch()
        await eventually(lambda: d.hub.bridge.streams == 0)


async def test_the_emulator_ends_a_stream(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        (fake.root / "rtc-bye").touch()
        ended = await ws.of_type("stream", state="ended")
        assert ended["reason"] == "the emulator ended the stream: disconnected"
        await eventually(lambda: d.sup.viewers == 0 and d.hub.bridge.streams == 0)
        (fake.root / "rtc-bye").unlink()
        # The viewer starts again.
        await watch(ws, d)
        assert d.sup.viewers == 1


async def test_the_emulator_closes_the_stream_without_a_bye(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        (fake.root / "rtc-eof").touch()
        assert (await ws.of_type("stream", state="ended"))["reason"] == "the emulator ended the stream"


async def test_a_killed_emulator_ends_the_streams_at_once(cfg, fake):
    async with daemon(cfg) as d, d.client() as first, d.client() as second:
        await d.running()
        await watch(first, d)
        await watch(second, d)
        assert d.sup.viewers == 2
        started = time.monotonic()
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        for ws in (first, second):
            ended = await ws.of_type("stream", state="ended", timeout=5)
            assert "not available" in ended["reason"] or "not running" in ended["reason"]
        assert time.monotonic() - started < 5
        await eventually(lambda: d.sup.viewers == 0 and d.hub.bridge.streams == 0)


async def test_stopping_android_ends_the_streams(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        await d.sup.stop()
        ended = await ws.of_type("stream", state="ended")
        assert ended["reason"] in ("android is not running", "the emulator is not available")
        assert await ws.quiet(0.3) == []
        await eventually(lambda: d.sup.viewers == 0 and d.hub.bridge.streams == 0)
        # A new start is refused while Android is stopped, and does not start it.
        await ws.send({"type": "stream", "action": "start"})
        assert (await ws.of_type("error"))["code"] == "not_running"
        assert len(fake.launches()) == 1


async def test_closing_the_session_closes_the_stream(cfg, fake):
    fake.configure(rtc_release_after=0.1)
    async with daemon(cfg) as d:
        await d.running()
        ws = d.client()
        await ws.connect()
        await watch(ws, d)
        assert d.sup.viewers == 1
        await ws.close()
        await eventually(lambda: d.sup.viewers == 0)
        await eventually(lambda: d.hub.bridge.streams == 0)
        await eventually(lambda: fake.rtc("receive-closed"))
        assert len(fake.rtc("send")) == 2


async def test_a_new_start_replaces_the_stream(cfg, fake):
    fake.configure(rtc_release_after=0.1)
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        before = len(ws.messages)
        await watch(ws, d)
        # The old stream gets no 'ended': the viewer asked for the change.
        assert not [m for m in ws.messages[before:] if m["type"] == "stream" and m["state"] == "ended"]
        assert len(fake.rtc("request")) == 2 and d.sup.viewers == 1
        # The first one was answered with the throwaway answer and let go.
        await eventually(lambda: d.hub.bridge.streams == 1)


async def test_the_number_of_streams_is_limited(cfg, fake):
    fake.configure(rtc_release_after=0.1)
    async with daemon(cfg) as d:
        await d.running()
        sockets = [d.client() for _ in range(5)]
        try:
            for ws in sockets:
                await ws.connect()
            for ws in sockets[:4]:
                await watch(ws, d)
            assert d.sup.viewers == 4
            await sockets[4].send({"type": "stream", "action": "start"})
            error = await sockets[4].of_type("error")
            assert error["code"] == "stream_limit" and "4" in error["message"]
            assert len(fake.rtc("request")) == 4
            # A viewer that leaves makes room once the emulator let go of its stream.
            await sockets[0].send({"type": "stream", "action": "stop"})
            await eventually(lambda: d.hub.bridge.streams == 3)
            await watch(sockets[4], d)
            assert d.sup.viewers == 4
        finally:
            for ws in sockets:
                await ws.close()


async def test_streams_that_the_emulator_still_holds_count(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        for _ in range(webrtc.MAX_RELEASING):
            await watch(ws, d)
            await ws.send({"type": "stream", "action": "stop"})
            await ws.of_type("stream", state="ended")
        # Nobody let go of them (the fake emulator never does here).
        await ws.send({"type": "stream", "action": "start"})
        error = await ws.of_type("error")
        assert error["code"] == "stream_limit" and "still closing" in error["message"]


async def test_the_emulator_refusing_a_stream_is_an_end(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await d.sup.grpc.channel.close()
        await ws.send({"type": "stream", "action": "start"})
        ended = await ws.of_type("stream", state="ended")
        assert ended["reason"].startswith("the emulator refused the stream")
        await eventually(lambda: d.sup.viewers == 0 and d.hub.bridge.streams == 0)


async def test_late_signaling_after_the_end_is_ignored(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        await ws.send({"type": "stream", "action": "stop"})
        await ws.of_type("stream", state="ended")
        await ws.send({"type": "jsep", "data": {"candidate": "candidate:1 1 udp 1 10.0.0.1 5 typ relay", "sdpMid": "0", "sdpMLineIndex": 0}})
        assert await ws.quiet(0.3) == []
        assert len(fake.rtc("send")) <= 2


async def test_only_an_answer_or_a_candidate_goes_to_the_emulator(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await watch(ws, d)
        for data in ({"bye": "hangup"}, {"start": {}}, {"type": "offer", "sdp": "x"}, "text", None, {"candidate": 5},
                     {"candidate": "c", "sdpMid": 5}, {"candidate": "c", "sdpMLineIndex": "0"}, {"type": "answer"},
                     {"type": "answer", "sdp": "x" * (session.MAX_SDP + 1)}):  # fmt: skip
            await ws.send({"type": "jsep", "data": data})
            assert (await ws.of_type("error"))["code"] == "bad_request", data
            assert fake.rtc("send") == []


async def test_the_display_hash_follows_the_screen(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "DISPLAY_INTERVAL", 0.1)
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await asyncio.sleep(0.4)
        # Nobody watches: no samples.
        assert fake.thumbnails() == []
        await watch(ws, d)
        first = await ws.of_type("display")
        assert len(first["hash"]) == 16 and abs(first["t"] - time.time() * 1000) < 5000
        assert {(t["width"], t["height"]) for t in fake.thumbnails()} == {(54, 96)}
        same = await ws.of_type("display")
        assert same["hash"] == first["hash"] and same["t"] > first["t"]
        fake.configure(display=77)
        changed = await ws.until(lambda m: m["type"] == "display" and m["hash"] != first["hash"])
        assert changed["hash"] != first["hash"]
        # Stopping the stream stops the sampling.
        await ws.send({"type": "stream", "action": "stop"})
        await ws.of_type("stream", state="ended")
        await asyncio.sleep(0.3)
        count = len(fake.thumbnails())
        await asyncio.sleep(0.4)
        assert len(fake.thumbnails()) == count


# Input


async def not_running(ws: WebSocketClient, message: dict) -> None:
    await ws.send(message)
    assert (await ws.of_type("error"))["code"] == "not_running", message


async def test_input_is_refused_while_android_is_stopped(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await not_running(ws, {"type": "touch", "touches": [{"id": 1, "x": 5, "y": 5, "pressure": 1}]})
        for message in (
            {"type": "mouse", "x": 5, "y": 5, "buttons": 1},
            {"type": "wheel", "x": 5, "y": 5, "dx": 0, "dy": 100},
            {"type": "key", "key": "a", "code": "KeyA", "action": "down"},
            {"type": "text", "text": "hi"},
            {"type": "button", "name": "HOME", "action": "press"},
        ):
            await not_running(ws, message)
            # Input never starts Android.
        assert fake.launches() == [] and d.sup.state.value == "stopped"


async def test_error_messages_are_rate_limited(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "ERROR_BURST", 5)
    async with daemon(cfg) as d, d.client() as ws:
        touch = {"type": "touch", "touches": [{"id": 1, "x": 5, "y": 5, "pressure": 1}]}
        for _ in range(50):
            await ws.send(touch)
        await asyncio.sleep(0.3)
        errors = [m for m in await ws.quiet(0.3) if m["type"] == "error"]
        assert 1 <= len(errors) <= session.ERROR_BURST


async def test_touches_get_emulator_identifiers_of_their_own(cfg, fake):
    async with daemon(cfg) as d, d.client() as first, d.client() as second:
        await d.running()

        async def touch(ws: WebSocketClient, x: int, y: int, pressure: float) -> None:
            count = len(touches(fake))
            await ws.send({"type": "touch", "touches": [{"id": 0, "x": x, "y": y, "pressure": pressure}]})
            await eventually(lambda: len(touches(fake)) == count + 1)

        # Two sessions use the same number for different fingers.
        await touch(first, 100, 200, 0.5)
        await touch(second, 300, 400, 1)
        await touch(first, 110, 210, 0.5)
        await touch(first, 110, 210, 0)
        await touch(second, 300, 400, 0)
        events = touches(fake)
        a, b = events[0][2], events[1][2]
        assert a != b
        assert events == [(100, 200, a, 512), (300, 400, b, 1024), (110, 210, a, 512), (110, 210, a, 0), (300, 400, b, 0)]
        # A new finger with the number of a lifted one is a new contact.
        await touch(first, 1, 2, 1)
        assert touches(fake)[5][2] not in (a, b)


async def test_touch_points_are_clamped_and_may_come_in_groups(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send(
            {"type": "touch", "touches": [{"id": "a", "x": -50, "y": 99999, "pressure": 1}, {"id": "b", "x": 540.4, "y": 960.6, "pressure": 2}]}
        )
        await ws.send({"type": "touch", "touches": [{"id": "a", "x": 0, "y": 0, "pressure": 0}, {"id": "zz", "x": 1, "y": 1, "pressure": 0}]})
        await eventually(lambda: len(touches(fake)) == 3)
        (x1, y1, id1, p1), (x2, y2, id2, p2), (x3, y3, id3, p3) = touches(fake)
        assert (x1, y1, p1) == (0, 1919, 1024) and (x2, y2, p2) == (540, 961, 1024)
        # A lift of a finger that never touched is ignored.
        assert (x3, y3, id3, p3) == (0, 0, id1, 0)
        # One event for the two fingers of a message.
        events = [e for e in fake.inputs() if e["type"] == "touch"]
        assert [len(e["touches"]) for e in events] == [2, 1]


async def test_a_session_has_at_most_ten_contacts(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "touch", "touches": [{"id": i, "x": i, "y": i, "pressure": 1} for i in range(10)]})
        await ws.send({"type": "touch", "touches": [{"id": 10, "x": 5, "y": 5, "pressure": 1}]})
        await asyncio.sleep(0.3)
        assert len(touches(fake)) == 10


@pytest.mark.parametrize(
    "message",
    [
        {"type": "touch"},
        {"type": "touch", "touches": "x"},
        {"type": "touch", "touches": [1]},
        {"type": "touch", "touches": [{"id": 1, "x": 1, "y": 1}]},
        {"type": "touch", "touches": [{"id": True, "x": 1, "y": 1, "pressure": 1}]},
        {"type": "touch", "touches": [{"id": 1, "x": "1", "y": 1, "pressure": 1}]},
        {"type": "touch", "touches": [{"id": 1, "x": 1e999, "y": 1, "pressure": 1}]},
        {"type": "touch", "touches": [{"id": 1, "x": 1, "y": 1, "pressure": 1}] * 11},
        {"type": "touch", "touches": [{"id": [1], "x": 1, "y": 1, "pressure": 1}]},
        {"type": "mouse", "x": 1, "y": 1},
        {"type": "mouse", "x": None, "y": 1, "buttons": 0},
        {"type": "wheel", "x": 1, "y": 1, "dx": 1},
        {"type": "key", "key": "a"},
        {"type": "key", "key": "a", "action": "hold"},
        {"type": "key", "key": 5, "action": "down"},
        {"type": "key", "key": "Arrow Up", "action": "down"},
        {"type": "key", "key": "x" * 33, "action": "down"},
        {"type": "key", "key": "a\u0000", "action": "down"},
        {"type": "button", "name": "CAPS_LOCK", "action": "press"},
        {"type": "button", "name": "HOME", "action": "tap"},
        {"type": "text"},
        {"type": "text", "text": ""},
        {"type": "text", "text": 5},
        {"type": "text", "text": "a\u0000b"},
        {"type": "text", "text": "x" * (session.MAX_TEXT + 1)},
        {"type": "stream"},
        {"type": "stream", "action": "pause"},
        {"type": "nonsense"},
        {"type": 5},
        {"x": 1},
        [1, 2],
        "text",
    ],
)
async def test_bad_messages_are_answered_with_an_error(cfg, fake, message):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send(message if isinstance(message, dict) else __import__("json").dumps(message))
        error = await ws.of_type("error")
        assert error["code"] == "bad_request" and error["message"]
        assert touches(fake) == [] and keys(fake) == []
        # The session goes on.
        await ws.send({"type": "ping", "t": 1})
        await ws.of_type("pong")


async def test_unreadable_frames_are_answered_with_an_error(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await ws.send("{not json")
        assert (await ws.of_type("error"))["code"] == "bad_request"
        await ws.send(b"\x00\x01")
        assert (await ws.of_type("error"))["code"] == "bad_request"
        await ws.send('{"type":"text","text":"' + "x" * session.MAX_MESSAGE + '"}')
        assert (await ws.of_type("error"))["code"] == "bad_request"
        await ws.send({"type": "ping", "t": 1})
        await ws.of_type("pong")


async def test_the_mouse_is_a_finger_and_the_right_button_is_back(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        # Hovering does nothing.
        await ws.send({"type": "mouse", "x": 10, "y": 10, "buttons": 0})
        await ws.send({"type": "mouse", "x": 20, "y": 20, "buttons": 0})
        await ws.send({"type": "mouse", "x": 100, "y": 200, "buttons": 1})
        await ws.send({"type": "mouse", "x": 150, "y": 250, "buttons": 1})
        await ws.send({"type": "mouse", "x": 150, "y": 250, "buttons": 0})
        await eventually(lambda: len(touches(fake)) == 3)
        (_, _, i1, p1), (_, _, i2, p2), (x3, y3, i3, p3) = touches(fake)
        assert touches(fake) == [(100, 200, i1, 1024), (150, 250, i1, 1024), (150, 250, i1, 0)]
        # The right button is Back, once for each press.
        await ws.send({"type": "mouse", "x": 5, "y": 5, "buttons": 2})
        await ws.send({"type": "mouse", "x": 6, "y": 6, "buttons": 2})
        await ws.send({"type": "mouse", "x": 6, "y": 6, "buttons": 0})
        await ws.send({"type": "mouse", "x": 6, "y": 6, "buttons": 2})
        await ws.send({"type": "mouse", "x": 6, "y": 6, "buttons": 0})
        await eventually(lambda: len(keys(fake)) == 2)
        assert keys(fake) == [("GoBack", 0, "keypress")] * 2
        assert len(touches(fake)) == 3
        # Left and right together: a finger and Back.
        await ws.send({"type": "mouse", "x": 7, "y": 8, "buttons": 3})
        await ws.send({"type": "mouse", "x": 7, "y": 8, "buttons": 0})
        await eventually(lambda: len(keys(fake)) == 3 and len(touches(fake)) == 5)


async def test_a_wheel_notch_is_a_short_drag(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "wheel", "x": 540, "y": 960, "dx": 0, "dy": 120})
        await eventually(lambda: len(touches(fake)) >= 9, 5)
        await asyncio.sleep(0.2)
        points = touches(fake)
        ident = points[0][2]
        assert all(p[2] == ident for p in points)
        # Scrolling down moves the finger up: 168 px, then the lift where the finger ended.
        assert points[0][:2] == (540, 960) and points[-1][:2] == (540, 960 - 168)
        assert [p[3] for p in points] == [1024] * (len(points) - 1) + [0]
        assert [p[1] for p in points] == sorted((p[1] for p in points), reverse=True)
        # The next notch is another finger. Sideways and up.
        await ws.send({"type": "wheel", "x": 540, "y": 960, "dx": -120, "dy": -60})
        await eventually(lambda: touches(fake)[-1][3] == 0 and len(touches(fake)) > len(points), 5)
        second = touches(fake)[len(points) :]
        assert second[0][2] != ident and second[-1][:2] == (540 + 168, 960 + 84)


async def test_wheel_notches_that_come_during_a_drag_add_up(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        for _ in range(10):
            await ws.send({"type": "wheel", "x": 500, "y": 1000, "dx": 0, "dy": 30})
            await asyncio.sleep(0.01)
        await eventually(lambda: any(p[3] == 0 for p in touches(fake)))
        await asyncio.sleep(0.5)
        points = touches(fake)
        # Far fewer drags than notches, and the distance of all of them: 10 x 30 -> 420 px.
        lifts = [i for i, p in enumerate(points) if p[3] == 0]
        starts = [0] + [i + 1 for i in lifts[:-1]]
        assert 1 <= len(lifts) <= 3
        assert sum(points[start][1] - points[lift][1] for start, lift in zip(starts, lifts, strict=True)) == 420


async def test_a_tiny_wheel_delta_is_not_a_tap(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "wheel", "x": 500, "y": 1000, "dx": 0, "dy": 5})
        await ws.send({"type": "wheel", "x": 500, "y": 1000, "dx": 1, "dy": 0})
        await asyncio.sleep(0.4)
        assert touches(fake) == []
        # They add up to a drag once there is enough.
        await ws.send({"type": "wheel", "x": 500, "y": 1000, "dx": 0, "dy": 20})
        await eventually(lambda: len(touches(fake)) > 2)


async def test_a_wheel_drag_near_the_edge_keeps_its_length(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "wheel", "x": 540, "y": 20, "dx": 0, "dy": 120})
        await eventually(lambda: touches(fake) and touches(fake)[-1][3] == 0)
        points = touches(fake)
        assert points[0][1] - points[-1][1] == 168 and points[-1][1] == 0


async def test_keys(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()

        async def key(name: str, action: str) -> None:
            await ws.send({"type": "key", "key": name, "code": "X", "action": action})

        await key("a", "down")
        await key("a", "up")
        await key("A", "down")
        await key(" ", "down")
        await key("a", "down")  # a repeat of a printable key is another key press
        await key("Enter", "down")
        await key("Enter", "down")  # Android repeats the held key itself
        await key("Enter", "up")
        await key("ArrowUp", "press")
        await key("ContextMenu", "down")
        await key("ContextMenu", "up")
        await key("Menu", "press")
        await key("BrowserHome", "press")
        await key("Home", "press")
        await key("Control", "down")
        await key("Control", "up")
        await key("CapsLock", "down")
        await key("Dead", "down")
        await key("Process", "down")
        await key("Unidentified", "down")
        await key("F5", "down")
        await key("F5", "up")
        await eventually(lambda: len(keys(fake)) == 16, 10)
        await asyncio.sleep(0.2)
        assert keys(fake) == [
            ("a", 0, "keypress"),
            ("A", 0, "keypress"),
            (" ", 0, "keypress"),
            ("a", 0, "keypress"),
            ("Enter", 0, "keydown"),
            ("Enter", 0, "keyup"),
            ("ArrowUp", 0, "keypress"),
            ("", 139, "keydown"),
            ("", 139, "keyup"),
            ("", 139, "keypress"),
            ("GoHome", 0, "keypress"),
            ("Home", 0, "keypress"),
            ("Control", 0, "keydown"),
            ("Control", 0, "keyup"),
            ("F5", 0, "keydown"),
            ("F5", 0, "keyup"),
        ]


async def test_keys_have_a_gap(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        for ch in "hello world":
            await ws.send({"type": "key", "key": ch, "code": "X", "action": "down"})
        await eventually(lambda: len(keys(fake)) == 11)
        times = [e["t"] for e in fake.inputs() if e["type"] == "key"]
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert min(gaps) >= 0.003
        assert [k[0] for k in keys(fake)] == list("hello world")


async def test_buttons(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        for name in ("HOME", "BACK", "APP_SWITCH", "VOLUME_UP", "VOLUME_DOWN", "MENU"):
            await ws.send({"type": "button", "name": name, "action": "press"})
        # A hold: down, a pause, up. A repeat of the down is not another press.
        await ws.send({"type": "button", "name": "POWER", "action": "down"})
        await ws.send({"type": "button", "name": "POWER", "action": "down"})
        await asyncio.sleep(0.3)
        await ws.send({"type": "button", "name": "POWER", "action": "up"})
        await eventually(lambda: len(keys(fake)) == 8)
        assert keys(fake) == [
            ("GoHome", 0, "keypress"),
            ("GoBack", 0, "keypress"),
            ("AppSwitch", 0, "keypress"),
            ("AudioVolumeUp", 0, "keypress"),
            ("AudioVolumeDown", 0, "keypress"),
            ("", 139, "keypress"),
            ("Power", 0, "keydown"),
            ("Power", 0, "keyup"),
        ]
        times = [e["t"] for e in fake.inputs() if e["type"] == "key"]
        assert times[-1] - times[-2] >= 0.25


async def test_text_is_pasted_exactly(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "text", "text": "Grüße 日本 😀"})
        await eventually(lambda: [e for e in fake.inputs() if e["type"] == "setClipboard"][1:], 10)
        events = [e for e in fake.inputs() if e["type"] != "touch"]
        assert [(e["type"], e.get("text"), e.get("keyCode")) for e in events] == [
            ("getClipboard", None, None),
            ("setClipboard", "Grüße 日本 😀", None),
            ("key", None, 135),
            ("setClipboard", "user-clipboard", None),
        ]


async def test_a_paste_lets_go_of_the_modifiers_that_are_held(cfg, fake):
    """The Ctrl+V of a browser: Control is down when the text arrives, and Android ignores a paste key with a modifier down."""
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "key", "key": "Control", "code": "ControlLeft", "action": "down"})
        await ws.send({"type": "text", "text": "abc"})
        await ws.send({"type": "key", "key": "Control", "code": "ControlLeft", "action": "up"})
        await eventually(lambda: len(keys(fake)) == 5, 10)
        assert keys(fake) == [
            ("Control", 0, "keydown"),
            ("Control", 0, "keyup"),  # let go for the paste
            ("", 135, "keypress"),
            ("Control", 0, "keydown"),  # down again: the client has not let go yet
            ("Control", 0, "keyup"),
        ]
        # Keys that are not modifiers are not touched.
        await ws.send({"type": "key", "key": "ArrowLeft", "code": "ArrowLeft", "action": "down"})
        await ws.send({"type": "text", "text": "d"})
        await ws.send({"type": "key", "key": "ArrowLeft", "code": "ArrowLeft", "action": "up"})
        await eventually(lambda: len(keys(fake)) == 8, 10)
        assert keys(fake)[5:] == [("ArrowLeft", 0, "keydown"), ("", 135, "keypress"), ("ArrowLeft", 0, "keyup")]


async def test_a_character_outside_ascii_is_pasted(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "key", "key": "é", "code": "Quote", "action": "down"})
        await ws.send({"type": "key", "key": "é", "code": "Quote", "action": "up"})
        await eventually(lambda: [e for e in fake.inputs() if e["type"] == "setClipboard"][1:], 10)
        assert [e["text"] for e in fake.inputs() if e["type"] == "setClipboard"] == ["é", "user-clipboard"]


async def test_keys_wait_for_a_paste(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "text", "text": "abc"})
        await ws.send({"type": "key", "key": "x", "code": "KeyX", "action": "down"})
        await eventually(lambda: len(keys(fake)) == 2, 10)
        # The key came after the paste, not in the middle of it.
        assert [k[1] if k[1] else k[0] for k in keys(fake)] == [135, "x"]


async def test_input_counts_as_activity_and_a_watching_session_does_not(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()

        async def idle_after(message: dict) -> int:
            d.sup._activity = time.time() - 500
            await ws.send(message)
            await asyncio.sleep(0.15)
            return d.sup.idle_seconds()

        assert await idle_after({"type": "ping", "t": 1}) >= 500
        for message in (
            {"type": "touch", "touches": [{"id": 1, "x": 1, "y": 1, "pressure": 1}]},
            {"type": "mouse", "x": 1, "y": 1, "buttons": 0},
            {"type": "wheel", "x": 1, "y": 1, "dx": 0, "dy": 1},
            {"type": "key", "key": "a", "code": "KeyA", "action": "down"},
            {"type": "button", "name": "HOME", "action": "press"},
            {"type": "text", "text": "a"},
        ):
            assert await idle_after(message) < 5, message
        # A message that is refused is no use of the device.
        assert await idle_after({"type": "key", "key": 5, "action": "down"}) >= 500


async def test_closing_a_session_lifts_its_touches_and_keys(cfg, fake):
    async with daemon(cfg) as d:
        await d.running()
        ws = d.client()
        await ws.connect()
        await ws.send({"type": "touch", "touches": [{"id": 3, "x": 11, "y": 22, "pressure": 1}, {"id": 4, "x": 33, "y": 44, "pressure": 1}]})
        await ws.send({"type": "mouse", "x": 55, "y": 66, "buttons": 1})
        await ws.send({"type": "key", "key": "Shift", "code": "ShiftLeft", "action": "down"})
        await ws.send({"type": "button", "name": "POWER", "action": "down"})
        await ws.send({"type": "key", "key": "q", "code": "KeyQ", "action": "down"})
        await eventually(lambda: len(touches(fake)) == 3 and len(keys(fake)) == 3, 10)
        await ws.close()
        # Every contact is lifted where it was last seen, and the held keys let go.
        await eventually(lambda: len(touches(fake)) == 6 and len(keys(fake)) == 5, 10)
        lifts = touches(fake)[3:]
        assert {(x, y) for x, y, _, p in lifts} == {(11, 22), (33, 44), (55, 66)} and {p for *_, p in lifts} == {0}
        assert {i for _, _, i, _ in lifts} == {i for _, _, i, _ in touches(fake)[:3]}
        assert sorted(keys(fake)[3:]) == [("Power", 0, "keyup"), ("Shift", 0, "keyup")]


async def test_android_stopping_forgets_the_touches(cfg, fake):
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "touch", "touches": [{"id": 1, "x": 1, "y": 2, "pressure": 1}]})
        await eventually(lambda: len(touches(fake)) == 1)
        await d.sup.stop()
        await ws.of_type("status", state="stopped")
        await d.sup.start(timeout=20)
        # The new emulator never saw the finger: lifting it is nothing, and nothing is lifted later.
        await ws.send({"type": "touch", "touches": [{"id": 1, "x": 1, "y": 2, "pressure": 0}]})
        await ws.send({"type": "ping", "t": 1})
        await ws.of_type("pong")
        assert len(touches(fake)) == 1


async def test_input_after_the_emulator_died_is_an_error(cfg, fake):
    async with daemon(cfg, replace(FAST, settle=5)) as d, d.client() as ws:
        await d.running()
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await ws.of_type("status", phase="restarting")
        await ws.send({"type": "touch", "touches": [{"id": 1, "x": 1, "y": 2, "pressure": 1}]})
        assert (await ws.of_type("error"))["code"] == "not_running"


async def test_too_many_messages_close_the_session(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "RATE", 5)
    monkeypatch.setattr(session, "BURST", 20)
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        for _ in range(40):
            await ws.send({"type": "ping", "t": 1})
        assert await ws.closed() == 1008
        assert d.hub.sessions == set()


async def test_a_flood_of_keys_closes_the_session_and_lets_go_of_them(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "LANE", 5)
    async with daemon(cfg) as d, d.client() as ws:
        await d.running()
        await ws.send({"type": "key", "key": "Shift", "code": "ShiftLeft", "action": "down"})
        await asyncio.sleep(0.2)
        for _ in range(200):
            await ws.send({"type": "key", "key": "a", "code": "KeyA", "action": "down"})
        assert await ws.closed() == 1008
        await eventually(lambda: ("Shift", 0, "keyup") in keys(fake))


async def test_too_many_sessions(cfg, fake, monkeypatch):
    monkeypatch.setattr(session, "MAX_SESSIONS", 2)
    async with daemon(cfg) as d, d.client() as one, d.client():
        async with d.client() as three:
            assert await three.closed() == 1013
        await one.send({"type": "ping", "t": 1})
        await one.of_type("pong")


# Over a real connection


@contextlib.asynccontextmanager
async def listening(d: Daemon) -> AsyncIterator[str]:
    """The TCP listener of the daemon, with its watchdog, on a free port: its address."""
    import socket

    from appliance import __main__ as main

    sock = socket.create_server(("127.0.0.1", 0))
    server = main._server(d.app, 256)
    tasks = [asyncio.create_task(server.serve(sockets=[sock])), asyncio.create_task(main._close_silent(server))]
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield f"127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_session_over_a_real_websocket(cfg, fake):
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosedError, InvalidStatus

    async with daemon(cfg) as d, listening(d) as address:
        url = f"ws://{address}/api/v1/session"
        # Without credentials, or with the cookie of another origin, there is no WebSocket.
        with pytest.raises(InvalidStatus) as refused:
            await connect(url)
        assert refused.value.response.status_code == 403
        cookie = {"Cookie": f"appliance_session={d.auth.new_session()}"}
        with pytest.raises(InvalidStatus):
            await connect(url, additional_headers={**cookie, "Origin": "http://evil.example"})
        async with connect(url, additional_headers={**cookie, "Origin": f"http://{address}"}) as ws:
            assert json.loads(await ws.recv())["type"] == "hello"
        async with connect(url, additional_headers={"Authorization": f"Bearer {d.auth.token}"}) as ws:
            assert json.loads(await ws.recv())["type"] == "hello"
            assert json.loads(await ws.recv())["state"] == "stopped"
            await d.running()
            await ws.send(json.dumps({"type": "stream", "action": "start"}))
            seen = []
            while not (seen and seen[-1].get("type") == "jsep" and seen[-1]["data"].get("type") == "offer"):
                seen.append(json.loads(await ws.recv()))
            configured = next(m for m in seen if m.get("state") == "configured")
            assert configured["iceServers"][0]["urls"][0].startswith(f"turn:127.0.0.1:{cfg.turn_port}")
            await ws.send(json.dumps({"type": "touch", "touches": [{"id": 1, "x": 7, "y": 8, "pressure": 1}]}))
            await eventually(lambda: len(touches(fake)) == 1)
            assert touches(fake)[0][:2] == (7, 8)
            # A message that is too large ends the connection.
            await ws.send("x" * (session.MAX_MESSAGE * 2))
            with pytest.raises(ConnectionClosedError) as closed:
                while True:
                    await ws.recv()
            assert closed.value.rcvd.code == 1009
        # The session ended with the connection: the finger is up, the viewer gone.
        await eventually(lambda: len(touches(fake)) == 2 and touches(fake)[1][3] == 0)
        await eventually(lambda: d.sup.viewers == 0 and d.hub.sessions == set())


async def test_an_open_session_is_not_a_silent_connection(cfg, fake, monkeypatch):
    """The watchdog of the listener closes connections that send no request;
    a WebSocket session that sends nothing for a while is not one of them."""
    from websockets.asyncio.client import connect

    from appliance import __main__ as main

    monkeypatch.setattr(main, "REQUEST_TIMEOUT", 0.6)
    async with daemon(cfg) as d, listening(d) as address:
        headers = {"Authorization": f"Bearer {d.auth.token}"}
        async with connect(f"ws://{address}/api/v1/session", additional_headers=headers) as ws:
            await asyncio.sleep(2)
            await ws.send(json.dumps({"type": "ping", "t": 5}))
            while json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] != "pong":
                pass


async def test_rotating_the_token_ends_the_open_sessions(cfg, fake):
    async with daemon(cfg) as d:
        await d.running()
        local = d.client()
        remote = d.tcp(authorization=f"Bearer {d.auth.token}")
        async with local, remote:
            await watch(remote, d)
            assert d.sup.viewers == 1
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.local(d.app)), base_url="http://appliance") as http:
                assert (await http.post("/api/v1/auth/token/rotate")).status_code == 200
            assert await remote.closed() == 1008 and await local.closed() == 1008
            await eventually(lambda: d.hub.sessions == set() and d.sup.viewers == 0)
            # The old token does not open a new one.
            async with d.tcp(authorization=f"Bearer {d.auth.token}") as again:
                assert again.accepted
            async with WebSocketClient(d.app, headers={"authorization": "Bearer old"}) as old:
                assert not old.accepted
