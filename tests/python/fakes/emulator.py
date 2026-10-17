"""Fake Android emulator: discovery file, AVD locks and a fake
EmulatorController gRPC server. The scenario comes from config.json."""

import fcntl
import io
import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from concurrent import futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import appliance._proto  # noqa: F401  (puts the stubs on sys.path)
import emulator_controller_pb2 as pb
import emulator_controller_pb2_grpc as pb_grpc
import grpc
import rtc_service_v2_pb2 as rtc_pb
import rtc_service_v2_pb2_grpc as rtc_grpc
from google.protobuf import empty_pb2
from PIL import Image

import common

done = threading.Event()
# Like -grpc-use-token: calls need "authorization: Bearer <token>", and the
# discovery file names the token.
TOKEN = "fake-grpc-token/with+chars=="


class RequireToken(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        if dict(handler_call_details.invocation_metadata).get("authorization") == f"Bearer {TOKEN}":
            return continuation(handler_call_details)

        def refuse(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing the 'authorization' header with security credentials.")

        # The refusal has the kind of the method that was called.
        handler = continuation(handler_call_details)
        if handler is not None and handler.response_streaming:
            return grpc.unary_stream_rpc_method_handler(refuse)
        return grpc.unary_unary_rpc_method_handler(refuse)


def png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, "PNG")
    return out.getvalue()


class Controller(pb_grpc.EmulatorControllerServicer):
    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.clipboard = "user-clipboard"
        self.screen = png(1080, 1920)

    def getStatus(self, request, context):
        return pb.EmulatorStatus(version="fake", booted=(common.root() / "booted").exists())

    def getScreenshot(self, request, context):
        if self.scenario["screenshot_fails"]:
            context.abort(grpc.StatusCode.UNAVAILABLE, "screenshots are broken")
        if request.format == pb.ImageFormat.RGB888:
            # A scaled picture: one shade, set by the scenario.
            shade = int(common.config()["display"]) % 256
            width, height = request.width or 1080, request.height or 1920
            common.record("thumbnail", {"width": width, "height": height})
            return pb.Image(format=pb.ImageFormat(format=request.format, width=width, height=height), image=bytes([shade, 0, 0]) * width * height)
        return pb.Image(format=pb.ImageFormat(format=pb.ImageFormat.PNG, width=1080, height=1920), image=self.screen)

    def getClipboard(self, request, context):
        common.record("input", {"type": "getClipboard"})
        return pb.ClipData(text=self.clipboard)

    def setClipboard(self, request, context):
        self.clipboard = request.text
        common.record("input", {"type": "setClipboard", "text": request.text})
        return empty_pb2.Empty()

    def sendKey(self, request, context):
        common.record("input", {"type": "key", **key_fields(request)})
        return empty_pb2.Empty()

    def streamInputEvent(self, request_iterator, context):
        for event in request_iterator:
            if event.HasField("touch_event"):
                touches = [{"x": t.x, "y": t.y, "id": t.identifier, "pressure": t.pressure} for t in event.touch_event.touches]
                common.record("input", {"type": "touch", "touches": touches})
            elif event.HasField("key_event"):
                common.record("input", {"type": "key", **key_fields(event.key_event)})
        return empty_pb2.Empty()


OFFER = (Path(__file__).parent / "offer.sdp").read_text().replace("\n", "\r\n")


class Rtc(rtc_grpc.RtcServicer):
    """The WebRTC signaling of the emulator: an offer and candidates for
    each stream, then whatever the viewer sends. A stream ends when the
    scenario says so (see common.py), and the fake records what it saw."""

    def __init__(self):
        self.streams: dict[str, dict] = {}

    def RequestRtcStream(self, request, context):
        guid = str(uuid.uuid4())
        urls = [url for server in request.ice_server_config.ice_servers for url in server.urls]
        outbox = queue.Queue()
        # The start message echoes the ICE configuration, as the real one does.
        outbox.put({"start": {"ice_servers": [{"urls": urls}]}})
        outbox.put({"sdp": OFFER, "type": "offer"})
        for port in (34904, 40000):
            outbox.put({"candidate": f"candidate:1 1 udp 2122194687 192.168.1.243 {port} typ host generation 0", "sdpMid": "0", "sdpMLineIndex": 0})
        outbox.put({"candidate": "", "sdpMid": "0", "sdpMLineIndex": 0})
        self.streams[guid] = {"outbox": outbox, "released": None}
        common.record("rtc", {"event": "request", "guid": guid, "urls": urls})
        return rtc_pb.RtcStreamResponse(id=rtc_pb.Id(guid=guid))

    def ReceiveJsepMessageStream(self, request, context):
        guid = request.id.guid
        stream = self.streams.get(guid)
        if stream is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such stream")
        common.record("rtc", {"event": "receive", "guid": guid})
        context.add_callback(lambda: common.record("rtc", {"event": "receive-closed", "guid": guid}))
        while context.is_active():
            if (common.root() / "rtc-bye").exists() or (stream["released"] and time.time() >= stream["released"]):
                yield rtc_pb.ReceiveJsepMessageResponse(jsep_msg=rtc_pb.JsepMsg(id=request.id, message=json.dumps({"bye": "disconnected"})))
                return
            if (common.root() / "rtc-eof").exists():
                return
            try:
                message = stream["outbox"].get(timeout=0.02)
            except queue.Empty:
                continue
            yield rtc_pb.ReceiveJsepMessageResponse(jsep_msg=rtc_pb.JsepMsg(id=request.id, message=json.dumps(message)))

    def SendJsepMessage(self, request, context):
        guid = request.jsep_msg.id.guid
        stream = self.streams.get(guid)
        if stream is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such stream")
        message = json.loads(request.jsep_msg.message)
        common.record("rtc", {"event": "send", "guid": guid, "message": message})
        delay = common.config()["rtc_release_after"]
        if message.get("type") == "answer" and delay is not None:
            stream["released"] = time.time() + delay
        return rtc_pb.SendJsepMessageResponse()


def key_fields(event: pb.KeyboardEvent) -> dict:
    return {
        "key": event.key,
        "keyCode": event.keyCode,
        "codeType": pb.KeyboardEvent.KeyCodeType.Name(event.codeType),
        "eventType": pb.KeyboardEvent.KeyEventType.Name(event.eventType),
    }


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def main() -> int:
    argv = sys.argv[1:]
    scenario = common.config()
    env = {k: os.environ.get(k) for k in ("HOME", "ANDROID_USER_HOME", "ANDROID_EMULATOR_HOME", "ANDROID_AVD_HOME",
                                         "ANDROID_SDK_ROOT", "ANDROID_ADB_SERVER_PORT", "DISPLAY", "USER")}  # fmt: skip
    name = option(argv, "-avd")
    registry = Path(os.environ["ANDROID_AVD_HOME"]) / f"{name}.ini"
    common.record("launches", {"argv": argv, "env": env, "registry": registry.read_text(), "cwd": os.getcwd()})
    paths = dict(line.split("=", 1) for line in registry.read_text().splitlines() if "=" in line)
    avd = Path(paths["path"])
    if not (avd / "config.ini").is_file():
        print(f"FATAL | Unknown AVD name [{name}]", flush=True)
        return 1
    # Like the real emulator: a lock file that names a live process means
    # another emulator uses the AVD.
    lock = avd / "hardware-qemu.ini.lock"
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip()), 0)
            print("FATAL | Running multiple emulators with the same AVD is an experimental feature.", flush=True)
            return 1
        except (ValueError, ProcessLookupError):
            pass
    lock.write_text(str(os.getpid()))
    # Held until the process exits, as the real emulator does.
    multi = open(avd / "multiinstance.lock", "w")  # noqa: SIM115
    fcntl.flock(multi, fcntl.LOCK_EX)

    print("INFO | fake emulator starting", flush=True)
    if scenario["mode"] == "fatal":
        print("FATAL | fake fatal error", flush=True)
        return 1
    if scenario["mode"] == "renderer":
        # The real qemu hangs here and ignores SIGTERM.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ERROR | Could not start renderer! (Error: -2)", flush=True)
        while True:
            time.sleep(1)

    # A signaling stream holds a thread for its whole life.
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=32), interceptors=[RequireToken()])
    pb_grpc.add_EmulatorControllerServicer_to_server(Controller(scenario), server)
    rtc_grpc.add_RtcServicer_to_server(Rtc(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    running = Path(os.environ["ANDROID_EMULATOR_HOME"]) / "avd" / "running"
    running.mkdir(parents=True, exist_ok=True)
    discovery = running / f"pid_{os.getpid()}.ini"
    # The real emulator writes the file in steps; the token comes late.
    discovery.write_text(f"grpc.port={port}\ngrpc.token={TOKEN}\nport.adb=5555\nport.serial=5554\navd.name={name}\n")
    root = common.root()
    (root / "booted").unlink(missing_ok=True)
    (root / "emulator.pid").write_text(str(os.getpid()))

    def on_kill(signum, frame):
        common.record("signals", {"signal": "emu kill"})
        if not scenario["ignore_kill"]:
            threading.Timer(scenario["stop_delay"], done.set).start()

    def on_term(signum, frame):
        common.record("signals", {"signal": "SIGTERM"})
        if not scenario["ignore_term"]:
            done.set()

    def on_poweroff(signum, frame):
        # The guest powers off (reboot -p): qemu exits 0 on its own.
        done.set()

    signal.signal(signal.SIGUSR1, on_kill)
    signal.signal(signal.SIGUSR2, on_poweroff)
    signal.signal(signal.SIGTERM, on_term)

    def boot():
        if done.wait(scenario["boot_delay"]) or scenario["mode"] == "hang":
            return
        if scenario["mode"] == "crash":
            print("fake crash during boot", flush=True)
            os._exit(3)
        (root / "booted").touch()
        print("INFO | Boot completed", flush=True)

    threading.Thread(target=boot, daemon=True).start()
    while not done.wait(0.1):
        pass
    server.stop(0)
    discovery.unlink(missing_ok=True)
    lock.unlink(missing_ok=True)
    for path in ("booted", "emulator.pid"):
        (root / path).unlink(missing_ok=True)
    print("INFO | fake emulator stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
