"""webrtc.py: the throwaway answer, the handling of the emulator's messages and the limits."""

import asyncio
import json
from pathlib import Path

import pytest

from appliance import session, webrtc
from appliance.webrtc import Bridge, StreamLimit

OFFER = (Path(__file__).parent / "fakes" / "offer.sdp").read_text().replace("\n", "\r\n")


def sections(sdp: str) -> list[list[str]]:
    found: list[list[str]] = []
    for line in sdp.split("\r\n"):
        if line.startswith("m="):
            found.append([])
        if found:
            found[-1].append(line)
    return found


def test_the_throwaway_answer_follows_the_offer():
    answer, candidate = webrtc.throwaway_answer(OFFER, "192.168.1.243")
    assert answer.endswith("\r\n") and "\n\n" not in answer and answer.startswith("v=0\r\n")
    lines = answer.split("\r\n")
    assert "a=group:BUNDLE 0 1 2" in lines and "t=0 0" in lines
    video, audio, data = sections(answer)
    assert video[0] == "m=video 9 UDP/TLS/RTP/SAVPF 96" and "a=rtpmap:96 VP8/90000" in video
    assert audio[0] == "m=audio 9 UDP/TLS/RTP/SAVPF 111" and "a=rtpmap:111 opus/48000/2" in audio
    assert data[0] == "m=application 9 UDP/DTLS/SCTP webrtc-datachannel"
    assert "a=sctp-port:5000" in data and "a=max-message-size:262144" in data
    for section, mid in zip((video, audio, data), "012", strict=True):
        assert f"a=mid:{mid}" in section and "a=setup:active" in section and "c=IN IP4 0.0.0.0" in section
    # One set of ICE and DTLS values for the bundle, none of them the offer's.
    for prefix in ("a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:sha-256 "):
        values = {line for line in lines if line.startswith(prefix)}
        assert len(values) == 1 and not values & {line for line in OFFER.split("\r\n") if line.startswith(prefix)}
    assert answer.count("a=recvonly") == 2 and "a=sendrecv" not in answer and "a=sendonly" not in answer
    # The candidate points at a port of the emulator's own address where nobody listens.
    assert json.loads(candidate) == {"candidate": "candidate:1 1 udp 2113937151 192.168.1.243 40000 typ host generation 0", "sdpMid": "0", "sdpMLineIndex": 0}


def test_two_throwaway_answers_differ():
    assert webrtc.throwaway_answer(OFFER, "10.0.0.1")[0] != webrtc.throwaway_answer(OFFER, "10.0.0.1")[0]


def test_the_throwaway_answer_copes_with_other_offers():
    offer = "v=0\no=- 1 2 IN IP4 127.0.0.1\ns=-\nt=0 0\nm=video 9 UDP/TLS/RTP/SAVPF 100 101\na=mid:v\na=rtpmap:101 VP9/90000\n"
    answer, candidate = webrtc.throwaway_answer(offer, "fd7a::1")
    [video] = sections(answer)
    assert "a=group:BUNDLE v" in answer and video[0] == "m=video 9 UDP/TLS/RTP/SAVPF 100" and "a=mid:v" in video
    # The first payload type has no rtpmap here (a static type): the section is still valid.
    assert not [line for line in video if line.startswith("a=rtpmap")]
    assert json.loads(candidate)["sdpMid"] == "v"


class Listener:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.ends: list[str] = []


def stream(bridge: Bridge, listener: Listener, live: bool = True) -> webrtc.Stream:
    s = bridge.create(None, listener.messages.append, listener.ends.append)  # type: ignore[arg-type]
    s.live = live
    return s


async def test_messages_of_the_emulator():
    bridge, listener = Bridge(), Listener()
    s = stream(bridge, listener)
    jsep = lambda **fields: json.dumps(fields)  # noqa: E731
    assert s._handle(jsep(start={"ice_servers": [{"urls": ["stun:x"], "credential": "secret"}]})) is None
    assert s._handle(jsep(type="offer", sdp="v=0")) is None
    assert s._handle(jsep(candidate="candidate:1", sdpMid="0", sdpMLineIndex=1, extra="dropped")) is None
    assert s._address == webrtc.FALLBACK_ADDRESS
    assert s._handle(jsep(candidate="", sdpMid="0", sdpMLineIndex=0)) is None
    assert s._handle(jsep(candidate=None)) is None
    assert s._handle(jsep(type="answer", sdp="x")) is None
    assert s._handle(jsep(type="offer", sdp=5)) is None
    assert s._handle("not json") is None and s._handle("[1]") is None and s._handle("5") is None
    assert listener.messages == [
        {"type": "offer", "sdp": "v=0"},
        {"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 1},
    ]
    assert s._offer.result() == "v=0"
    assert s._handle(jsep(bye="disconnected")) == "disconnected"
    # Once the viewer is gone nothing reaches it.
    s.live = False
    assert s._handle(jsep(candidate="candidate:2", sdpMid="0", sdpMLineIndex=0)) is None
    assert len(listener.messages) == 2


async def test_the_address_of_the_emulator_is_the_first_udp_host_candidate():
    s = stream(Bridge(), Listener())
    host = "candidate:%s 1 {} 2122194687 {} 4000 typ {} generation 0 ufrag x network-id 1"
    for kind, address, protocol in (("srflx", "203.0.113.5", "udp"), ("host", "192.168.1.9", "tcp"), ("host", "192.168.1.243", "udp"), ("host", "fd7a::1", "udp")):
        s._handle(json.dumps({"candidate": host.format(protocol, address, kind) % "1", "sdpMid": "0", "sdpMLineIndex": 0}))
    assert s._address == "192.168.1.243"
    s._handle(json.dumps({"candidate": "candidate:short", "sdpMid": "0", "sdpMLineIndex": 0}))
    assert s._address == "192.168.1.243"


async def test_the_limits_count_viewers_and_streams_that_are_closing():
    bridge, listener = Bridge(), Listener()
    live = [stream(bridge, listener) for _ in range(webrtc.MAX_STREAMS)]
    with pytest.raises(StreamLimit, match=f"{webrtc.MAX_STREAMS} streams are open"):
        stream(bridge, listener)
    # A viewer that leaves: its stream still counts, but as a closing one, and makes room.
    for n in range(webrtc.MAX_RELEASING - 1):
        live.pop().live = False
        live.append(stream(bridge, listener))
        assert bridge.streams == webrtc.MAX_STREAMS + n + 1
    with pytest.raises(StreamLimit, match="streams are open"):
        stream(bridge, listener)
    # When every viewer is gone, only the streams that the emulator holds count.
    for s in live:
        s.live = False
    with pytest.raises(StreamLimit, match="still closing"):
        stream(bridge, listener)
    # The emulator lets some go.
    for s in list(bridge._streams)[: bridge.streams - webrtc.MAX_RELEASING + 1]:
        bridge.forget(s)
    assert stream(bridge, listener)


def test_the_message_checks_of_the_session():
    assert session.jsep({"type": "answer", "sdp": "v=0", "extra": 1}) == {"type": "answer", "sdp": "v=0"}
    assert session.jsep({"candidate": "c", "sdpMid": "0", "sdpMLineIndex": 2, "usernameFragment": "x"}) == {
        "candidate": "c", "sdpMid": "0", "sdpMLineIndex": 2,
    }  # fmt: skip
    assert session.jsep({"candidate": "c"}) == {"candidate": "c", "sdpMid": None, "sdpMLineIndex": None}
    for bad in (None, "x", [], {}, {"type": "offer", "sdp": "x"}, {"type": "answer"}, {"candidate": 1}, {"candidate": "c", "sdpMLineIndex": True}):
        with pytest.raises(session.Reject) as e:
            session.jsep(bad)
        assert e.value.code == "bad_request"


def test_the_token_bucket():
    bucket = session.Bucket(rate=1000, burst=3)
    assert [bucket.take() for _ in range(5)] == [True, True, True, False, False]
    bucket._time -= 0.0021
    assert bucket.take() and bucket.take() and not bucket.take()


async def test_a_lane_runs_jobs_in_order_and_survives_a_failing_one(caplog):
    lane = session.Lane()
    done: list[int] = []

    def job(n: int):
        async def run() -> None:
            await asyncio.sleep(0.01 if n == 1 else 0)
            if n == 2:
                raise RuntimeError("boom")
            done.append(n)

        return run

    for n in range(5):
        assert lane.put(job(n))
    while len(done) < 4:
        await asyncio.sleep(0.01)
    assert done == [0, 1, 3, 4] and "a session job failed" in caplog.text
    await lane.close()


async def test_a_full_lane_refuses_jobs(monkeypatch):
    monkeypatch.setattr(session, "LANE", 2)
    lane = session.Lane()
    gate = asyncio.Event()

    async def wait() -> None:
        await gate.wait()

    # One job runs, two wait; the next one does not fit.
    assert lane.put(wait)
    await asyncio.sleep(0.05)
    assert lane.put(wait) and lane.put(wait) and not lane.put(wait)
    gate.set()
    await lane.close()


class Call:
    """A receive stream of the emulator: the given messages, then an error."""

    def __init__(self, messages: list[str], error: Exception | None):
        self._messages, self._error = messages, error

    def cancel(self) -> bool:
        return True

    def __aiter__(self):
        return self._run()

    async def _run(self):
        for raw in self._messages:
            yield type("Response", (), {"jsep_msg": type("Msg", (), {"message": raw})()})()
        if self._error:
            raise self._error


class Emulator:
    def __init__(self, call: Call):
        self.call = call
        self.sent: list[str] = []

    async def request_rtc_stream(self, stun_url: str) -> str:
        return "guid-1"

    def receive_jsep(self, guid: str) -> Call:
        return self.call

    async def send_jsep(self, guid: str, message: str) -> None:
        self.sent.append(message)


async def opened(emulator: Emulator, listener: Listener) -> tuple[Bridge, webrtc.Stream]:
    bridge = Bridge()
    s = bridge.create(emulator, listener.messages.append, listener.ends.append)  # type: ignore[arg-type]
    await s.open()
    return bridge, s


async def test_a_stream_ends_when_the_emulator_ends_it():
    listener = Listener()
    bridge, s = await opened(Emulator(Call([json.dumps({"type": "offer", "sdp": "v=0"})], None)), listener)
    await asyncio.wait_for(s._pump, 5)
    assert listener.messages == [{"type": "offer", "sdp": "v=0"}]
    assert listener.ends == ["the emulator ended the stream"] and bridge.streams == 0 and not s.live


async def test_a_failing_stream_is_an_end_not_a_hang(caplog):
    """Whatever goes wrong in the handling of a message, the viewer hears of it."""
    listener = Listener()

    def explode(message: dict) -> None:
        raise RuntimeError("boom")

    bridge = Bridge()
    s = bridge.create(Emulator(Call(['{"type":"offer","sdp":"v=0"}'], None)), explode, listener.ends.append)  # type: ignore[arg-type]
    await s.open()
    await asyncio.wait_for(s._pump, 5)
    assert listener.ends == ["internal error; see the daemon log"] and bridge.streams == 0
    assert "boom" in caplog.text


async def test_a_stream_ends_with_the_reason_of_the_emulator():
    listener = Listener()
    bridge, s = await opened(Emulator(Call([json.dumps({"bye": "disconnected"}), json.dumps({"type": "offer", "sdp": "late"})], None)), listener)
    await asyncio.wait_for(s._pump, 5)
    assert listener.ends == ["the emulator ended the stream: disconnected"] and listener.messages == []


async def test_a_stream_the_viewer_left_is_forgotten_when_the_emulator_ends_it():
    listener = Listener()
    emulator = Emulator(Call([json.dumps({"type": "offer", "sdp": OFFER}), json.dumps({"bye": "disconnected"})], None))
    bridge, s = await opened(emulator, listener)
    s.close()
    assert not s.live and bridge.streams == 1
    await asyncio.wait_for(s._release, 5)
    # Nobody answered: the emulator got the throwaway answer first; the viewer heard nothing.
    assert [json.loads(m).get("type") for m in emulator.sent if "type" in json.loads(m)] == ["answer"]
    assert listener.ends == [] and listener.messages == [] and bridge.streams == 0


class StuckCall(Call):
    """The emulator keeps a stream until it is given a candidate that cannot work, then it says bye."""

    def __init__(self, messages: list[str]):
        super().__init__(messages, None)
        self.release = asyncio.Event()

    async def _run(self):
        async for response in super()._run():
            yield response
        await self.release.wait()
        yield type("Response", (), {"jsep_msg": type("Msg", (), {"message": json.dumps({"bye": "disconnected"})})()})()


class StuckEmulator(Emulator):
    async def send_jsep(self, guid: str, message: str) -> None:
        await super().send_jsep(guid, message)
        if f" {webrtc.DEAD_PORT} " in message:
            self.call.release.set()  # type: ignore[attr-defined]


HOST = "candidate:1 1 udp 2122194687 192.168.1.243 4000 typ host generation 0"


async def test_a_stream_that_the_emulator_keeps_is_pushed_to_a_failed_ice(monkeypatch, caplog):
    monkeypatch.setattr(webrtc, "STUCK_AFTER", 0.2)
    caplog.set_level("INFO")
    listener = Listener()
    messages = [json.dumps({"type": "offer", "sdp": OFFER}), json.dumps({"candidate": HOST, "sdpMid": "0", "sdpMLineIndex": 0})]
    emulator = StuckEmulator(StuckCall(messages))
    bridge, s = await opened(emulator, listener)
    # The viewer answered, and then vanished before its ICE connected.
    await s.send({"type": "answer", "sdp": "v=0"})
    s.close()
    await asyncio.sleep(0.1)
    assert bridge.streams == 1 and len(emulator.sent) == 1
    await asyncio.wait_for(s._release, 5)
    dead = json.loads(emulator.sent[-1])
    assert dead == {"candidate": f"candidate:1 1 udp 2113937151 192.168.1.243 {webrtc.DEAD_PORT} typ host generation 0", "sdpMid": "0", "sdpMLineIndex": 0}
    assert bridge.streams == 0 and listener.ends == []
    assert "sent a candidate that cannot work" in caplog.text and "still holds it after" not in caplog.text


async def test_a_stream_that_the_emulator_never_ends_is_forgotten_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(webrtc, "STUCK_AFTER", 0.1)
    monkeypatch.setattr(webrtc, "RELEASE_TIMEOUT", 0.2)
    listener = Listener()
    emulator = Emulator(StuckCall([json.dumps({"type": "offer", "sdp": OFFER})]))
    bridge, s = await opened(emulator, listener)
    await s.send({"type": "answer", "sdp": "v=0"})
    s.close()
    await asyncio.wait_for(s._release, 5)
    # No candidate came from the emulator: the pair has the documentation address.
    assert json.loads(emulator.sent[-1])["candidate"].split()[4] == webrtc.FALLBACK_ADDRESS
    assert bridge.streams == 0 and "the emulator still holds it after" in caplog.text


async def test_closing_everything():
    listener = Listener()
    emulator = Emulator(Call([], None))
    bridge, s = await opened(emulator, listener)
    other = bridge.create(emulator, listener.messages.append, listener.ends.append)  # type: ignore[arg-type]
    await bridge.aclose()
    assert bridge.streams == 0 and not s.live and not other.live


class SlowEmulator(Emulator):
    """RequestRtcStream answers when the test says so."""

    def __init__(self, call: Call):
        super().__init__(call)
        self.answer = asyncio.Event()
        self.requests = 0

    async def request_rtc_stream(self, stun_url: str) -> str:
        self.requests += 1
        await self.answer.wait()
        return "guid-late"


async def test_a_viewer_that_leaves_during_the_request_still_releases_the_stream(monkeypatch):
    """The emulator creates the stream although the viewer is gone, and would
    hold it for good: the bridge waits for its id and sends the throwaway
    answer, as for any other stream nobody answered."""
    monkeypatch.setattr(webrtc, "STUCK_AFTER", 1.0)
    listener = Listener()
    offer = json.dumps({"type": "offer", "sdp": OFFER})
    emulator = SlowEmulator(Call([offer, json.dumps({"bye": "disconnected"})], None))
    bridge = Bridge()
    s = bridge.create(emulator, listener.messages.append, listener.ends.append)  # type: ignore[arg-type]
    job = asyncio.create_task(s.open())
    await asyncio.sleep(0.05)
    assert emulator.requests == 1
    # The session drops the stream and cancels its job (a reload, a reconnect).
    s.close()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    # Nothing is forgotten before the emulator answered: the limit must count it.
    assert bridge.streams == 1 and not s.live
    emulator.answer.set()
    await asyncio.wait_for(s._release, 5)
    answer = json.loads(emulator.sent[0])
    assert answer["type"] == "answer" and "a=setup:active" in answer["sdp"]
    assert bridge.streams == 0 and listener.messages == [] and listener.ends == []


async def test_a_stream_closed_during_the_request_is_released_when_it_opens():
    """close() without a cancellation: open() finishes and lets the stream go."""
    listener = Listener()
    emulator = SlowEmulator(Call([json.dumps({"type": "offer", "sdp": OFFER}), json.dumps({"bye": "disconnected"})], None))
    bridge = Bridge()
    s = bridge.create(emulator, listener.messages.append, listener.ends.append)  # type: ignore[arg-type]
    job = asyncio.create_task(s.open())
    await asyncio.sleep(0.05)
    s.close()
    assert bridge.streams == 1
    emulator.answer.set()
    await job
    await asyncio.wait_for(s._release, 5)
    assert json.loads(emulator.sent[0])["type"] == "answer" and bridge.streams == 0 and listener.messages == []
