"""Fake Xvnc: creates the X socket next to -rfbunixpath and waits."""

import signal
import socket
import sys
import threading
from pathlib import Path

import common


def main() -> int:
    argv = sys.argv[1:]
    common.record("xvnc", {"argv": argv})
    if common.config()["xvnc_fails"]:
        print("Fatal server error: fake failure", file=sys.stderr)
        return 1
    tmp = Path(argv[argv.index("-rfbunixpath") + 1]).parent
    x0 = tmp / ".X11-unix" / "X0"
    x0.parent.mkdir(parents=True, exist_ok=True)
    x0.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(x0))
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    done.wait()
    sock.close()
    x0.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
