"""The signaling bridge: emulator Rtc v2 gRPC streams for the browsers.

The emulator offers and the browser answers. The bridge relays the offer
and the emulator's candidates to the browser, and the browser's answer and
candidates to the emulator, and tells the viewer when the stream is over.

The emulator ends a stream only when the browser's ICE goes away, 15-17 s
after it did. It ignores a `bye` from the client, and it keeps a stream
that nobody answered for good, with its threads and descriptors. So a
viewer that leaves early gets an answer that cannot connect, and the
stream stays counted until the emulator lets go of it. It also keeps a
stream whose viewer vanished before its ICE connected; that one gets a
candidate that cannot work, once it has outlived the usual 15-17 s.
"""

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

import grpc

from .config import TURN_PORT
from .grpc_client import EmulatorGrpc

log = logging.getLogger(__name__)

# Viewers with a stream, and streams that the emulator has not released yet.
MAX_STREAMS = 4
MAX_RELEASING = 8
# The emulator's STUN server: the container's own coturn. It keeps the
# emulator off Google's STUN server and out of the TURN relay.
STUN_URL = f"stun:127.0.0.1:{TURN_PORT}"
# Where the candidate of a throwaway answer points: a port of the container's own address.
DEAD_PORT = 40000
# The address if the emulator named none (a documentation address: it goes nowhere).
FALLBACK_ADDRESS = "192.0.2.1"
# A stream whose viewer left ends by itself 15-17 s later. One whose viewer
# vanished before its ICE connected (a browser that was killed) the emulator
# keeps for good: measured, 5 such streams were still there after 5 minutes.
# After STUCK_AFTER seconds the stream gets a candidate that cannot work, its
# ICE check fails, and the emulator ends it (measured: 13 s later). The bridge
# gives it RELEASE_TIMEOUT more seconds, then forgets it.
STUCK_AFTER = 25.0
RELEASE_TIMEOUT = 30.0
# The offer follows the request within milliseconds.
OFFER_TIMEOUT = 5.0
GRPC_ERRORS = (grpc.aio.AioRpcError, grpc.aio.BaseError)
# What a gRPC call reports when the emulator is gone (CANCELLED: the channel was closed).
UNAVAILABLE = (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.CANCELLED)

OnMessage = Callable[[dict[str, Any]], None]
OnEnd = Callable[[str], None]


class StreamLimit(Exception):
    pass


def dead_candidate(address: str, mid: str) -> str:
    """A candidate on a port of `address` where nobody listens: the pair fails,
    and with no other pair the emulator's ICE fails and it ends the stream.
    The port must not be 9 (libwebrtc ignores that one) and the address must
    be the emulator's own (anything else needs a timeout instead of a refusal)."""
    candidate = {"candidate": f"candidate:1 1 udp 2113937151 {address} {DEAD_PORT} typ host generation 0", "sdpMid": mid, "sdpMLineIndex": 0}
    return json.dumps(candidate)


def throwaway_answer(offer: str, address: str) -> tuple[str, str]:
    """An answer to `offer` that can never connect, and a candidate on
    `address` where nobody listens, so that ICE fails and the emulator
    lets the stream go (measured: 15 s later, with its bye).

    Every media section is accepted with the first codec of the offer and
    one set of made-up ICE and DTLS values, as in a BUNDLE. Without a
    candidate, or with all sections rejected, the emulator keeps the
    stream for good; so does a candidate with port 9 (measured), which
    libwebrtc does not use.
    """
    ufrag, pwd = secrets.token_hex(2), secrets.token_hex(12)
    fingerprint = ":".join(f"{b:02X}" for b in secrets.token_bytes(32))
    sections: list[list[str]] = []
    for line in offer.splitlines():
        if line.startswith("m="):
            sections.append([])
        if sections:
            sections[-1].append(line)
    lines = ["v=0", f"o=- {secrets.randbelow(10**15)} 2 IN IP4 127.0.0.1", "s=-", "t=0 0"]
    mids = [next((ln[6:] for ln in s if ln.startswith("a=mid:")), str(i)) for i, s in enumerate(sections)]
    lines += [f"a=group:BUNDLE {' '.join(mids)}", "a=msid-semantic: WMS"]
    for mid, section in zip(mids, sections, strict=True):
        media, _, rest = section[0][2:].partition(" ")
        formats = rest.split(" ")[2:]
        lines += [f"m={media} 9 {rest.split(' ')[1]} {formats[0] if media != 'application' else ' '.join(formats)}", "c=IN IP4 0.0.0.0"]
        lines += [f"a=ice-ufrag:{ufrag}", f"a=ice-pwd:{pwd}", f"a=fingerprint:sha-256 {fingerprint}", "a=setup:active", f"a=mid:{mid}"]
        if media == "application":
            lines += [ln for ln in section if ln.startswith(("a=sctp-port:", "a=max-message-size:"))]
        else:
            codec = next((ln for ln in section if ln.startswith(f"a=rtpmap:{formats[0]} ")), None)
            lines += ["a=recvonly", "a=rtcp-mux", *([codec] if codec else [])]
    return "\r\n".join(lines) + "\r\n", dead_candidate(address, mids[0])


class Stream:
    """One emulator stream for one viewer."""

    def __init__(self, bridge: "Bridge", grpc_client: EmulatorGrpc, on_message: OnMessage, on_end: OnEnd):
        self._bridge = bridge
        self._grpc = grpc_client
        self._on_message = on_message
        self._on_end = on_end
        self._guid = ""
        self._pump: asyncio.Task[None] | None = None
        self._release: asyncio.Task[None] | None = None
        self._offer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        # An address of the emulator, from its first UDP host candidate.
        self._address = FALLBACK_ADDRESS
        self._address_known = asyncio.Event()
        self._answered = False
        # False once the viewer is gone or the stream is over: nothing reaches the viewer any more.
        self.live = True

    async def open(self) -> None:
        """Ask the emulator for the stream. Its messages follow through
        `on_message`, from the next time that the caller waits."""
        self._guid = await self._grpc.request_rtc_stream(STUN_URL)
        log.info("stream %s requested", self._guid)
        self._pump = asyncio.create_task(self._receive())

    async def send(self, message: dict[str, Any]) -> None:
        """Pass the viewer's answer or candidate on to the emulator."""
        if self._pump is None or self._pump.done():
            return
        try:
            await self._grpc.send_jsep(self._guid, json.dumps(message))
        except GRPC_ERRORS as e:
            # A broken emulator ends the stream by itself.
            log.info("stream %s: cannot send %s: %s", self._guid, "an answer" if "sdp" in message else "a candidate", e)
            return
        self._answered = self._answered or "sdp" in message

    def close(self) -> None:
        """The viewer leaves. The emulator has to let the stream go."""
        if not self.live:
            return
        self.live = False
        if self._pump is not None and not self._pump.done():
            self._release = asyncio.create_task(self._let_go())
        else:
            self._bridge.forget(self)

    async def aclose(self) -> None:
        self.close()
        for task in (self._release, self._pump):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (self._release, self._pump) if t), return_exceptions=True)
        self._bridge.forget(self)

    async def _let_go(self) -> None:
        assert self._pump
        try:
            if not self._answered:
                await self._answer_nobody()
            # The pump ends with the stream: the emulator's bye, or its end.
            try:
                await asyncio.wait_for(asyncio.shield(self._pump), STUCK_AFTER)
            except TimeoutError:
                await self._fail_ice()
                await asyncio.wait_for(asyncio.shield(self._pump), RELEASE_TIMEOUT)
        except TimeoutError:
            log.warning("stream %s: the emulator still holds it after %ds", self._guid, STUCK_AFTER + RELEASE_TIMEOUT)
        except GRPC_ERRORS:
            pass
        finally:
            self._pump.cancel()
            self._bridge.forget(self)

    async def _fail_ice(self) -> None:
        """Push a stream that the emulator does not end to a failed ICE check."""
        offer = self._offer.result() if self._offer.done() else ""
        mid = next((line[6:] for line in offer.splitlines() if line.startswith("a=mid:")), "0")
        await self._grpc.send_jsep(self._guid, dead_candidate(self._address, mid))
        log.info("stream %s: the emulator still holds it; sent a candidate that cannot work", self._guid)

    async def _answer_nobody(self) -> None:
        try:
            offer = await asyncio.wait_for(asyncio.shield(self._offer), OFFER_TIMEOUT)
        except TimeoutError:
            log.warning("stream %s: the emulator sent no offer", self._guid)
            return
        # The candidates follow the offer within milliseconds.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._address_known.wait(), OFFER_TIMEOUT / 5)
        answer, candidate = throwaway_answer(offer, self._address)
        await self._grpc.send_jsep(self._guid, json.dumps({"type": "answer", "sdp": answer}))
        await self._grpc.send_jsep(self._guid, candidate)
        log.info("stream %s: nobody answered; sent an answer that cannot connect", self._guid)

    async def _receive(self) -> None:
        call = self._grpc.receive_jsep(self._guid)
        reason = "the emulator ended the stream"
        try:
            async for response in call:
                if (bye := self._handle(response.jsep_msg.message)) is not None:
                    reason = f"the emulator ended the stream: {bye}"
                    break
        except grpc.aio.AioRpcError as e:
            reason = "the emulator is not available" if e.code() in UNAVAILABLE else f"the emulator failed: {e.details()}"
        except grpc.aio.BaseError:
            reason = "the emulator is not available"
        except Exception:
            # The viewer must not wait for a stream that nobody serves.
            log.exception("stream %s failed", self._guid)
            reason = "internal error; see the daemon log"
        except asyncio.CancelledError:
            call.cancel()
            self._bridge.forget(self)
            raise
        call.cancel()
        log.info("stream %s ended: %s", self._guid, reason)
        if self.live:
            self.live = False
            self._on_end(reason)
        self._bridge.forget(self)

    def _handle(self, raw: str) -> str | None:
        """Relay one message of the emulator; return the text of a bye."""
        try:
            message = json.loads(raw)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            log.warning("stream %s: unreadable message from the emulator", self._guid)
            return None
        if "bye" in message:
            return str(message["bye"])
        if message.get("type") == "offer" and isinstance(message.get("sdp"), str):
            if not self._offer.done():
                self._offer.set_result(message["sdp"])
            self._forward({"type": "offer", "sdp": message["sdp"]})
        elif isinstance(message.get("candidate"), str) and message["candidate"]:
            # No end-of-candidates message exists, and an empty candidate is
            # not one: never pass one on.
            self._note_address(message["candidate"])
            self._forward({k: message.get(k) for k in ("candidate", "sdpMid", "sdpMLineIndex")})
        # "start" echoes the ICE configuration of the request: nothing for the viewer.
        return None

    def _note_address(self, candidate: str) -> None:
        """Remember the address of the emulator's first UDP host candidate:
        "candidate:<id> <component> udp <priority> <address> <port> typ host ...".
        """
        fields = candidate.split()
        if not self._address_known.is_set() and len(fields) > 7 and fields[2].lower() == "udp" and fields[6:8] == ["typ", "host"]:
            self._address = fields[4]
            self._address_known.set()

    def _forward(self, message: dict[str, Any]) -> None:
        if self.live:
            self._on_message(message)


class Bridge:
    """The streams of all viewers."""

    def __init__(self) -> None:
        self._streams: set[Stream] = set()

    def create(self, grpc_client: EmulatorGrpc, on_message: OnMessage, on_end: OnEnd) -> Stream:
        """A new stream for a viewer; call open() on it. At most MAX_STREAMS
        viewers watch at once, and streams that the emulator still holds
        after their viewer left count against a second limit."""
        live = sum(s.live for s in self._streams)
        if live >= MAX_STREAMS:
            raise StreamLimit(f"{MAX_STREAMS} streams are open; close one first")
        if len(self._streams) - live >= MAX_RELEASING:
            raise StreamLimit("the emulator is still closing earlier streams; try again in a few seconds")
        stream = Stream(self, grpc_client, on_message, on_end)
        self._streams.add(stream)
        return stream

    def forget(self, stream: Stream) -> None:
        self._streams.discard(stream)

    @property
    def streams(self) -> int:
        return len(self._streams)

    async def aclose(self) -> None:
        for stream in list(self._streams):
            await stream.aclose()
