"""Fake coturn: records its configuration, prints a line and runs until SIGTERM."""

import os
import signal
import sys
import threading
from pathlib import Path

import common


def main() -> int:
    argv = sys.argv[1:]
    conf = Path(argv[argv.index("-c") + 1])
    common.record("turn", {"argv": argv, "conf": conf.read_text(), "mode": conf.stat().st_mode & 0o777})
    (common.root() / "turnserver.pid").write_text(str(os.getpid()))
    if common.config()["turn_mode"] == "crash":
        print("ERROR: cannot bind the listening port", flush=True)
        return 1
    print("0: log file opened: fake turnserver", flush=True)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    done.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
