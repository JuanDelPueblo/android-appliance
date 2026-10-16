"""Xvnc, the emulator process, discovery, logs and AVD locks."""

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb, AdbError
from .config import GRPC_PORT, HEIGHT, WIDTH, Config

log = logging.getLogger(__name__)

KEEP_LOGS = 5
# After these lines qemu hangs and ignores SIGTERM.
HANG_LINE = re.compile(r"Could not start renderer|Could not initialize emulated framebuffer|\bFATAL\b")
SOFTWARE_GL = re.compile(r"llvmpipe|softpipe|swrast", re.IGNORECASE)


class LaunchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class StopTiming:
    sync: float = 15.0
    # Measured: qemu exits 1-3 s after "emu kill".
    emu_kill: float = 60.0
    sigterm: float = 10.0
    sigkill: float = 10.0


def render_node(cfg: Config) -> str | None:
    if cfg.render_node is not None:
        candidates = [Path(cfg.render_node)]
    else:
        candidates = sorted(Path("/dev/dri").glob("renderD*"))
    for node in candidates:
        try:
            if stat.S_ISCHR(node.stat().st_mode) and os.access(node, os.R_OK | os.W_OK):
                return str(node)
        except OSError:
            continue
    return None


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


class Xvnc:
    """An X server that is only a GPU-backed GLX server for the emulator.

    No VNC client connects: VNC listens only on a private unix socket
    (Xvnc refuses to start without any VNC listener).
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc

    @classmethod
    async def start(cls, cfg: Config, node: str, timeout: float = 10.0) -> "Xvnc":
        # The daemon is the only X user in the container, so whatever is
        # left is from an Xvnc that died; its PID may belong to a live
        # process again.
        for stale in (cfg.x11_socket, cfg.tmp_dir / ".X0-lock"):
            stale.unlink(missing_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.logs_dir / "xvnc.log", "wb") as out:
            proc = await asyncio.create_subprocess_exec(
                cfg.xvnc_bin, ":0", "-rendernode", node, "-geometry", f"{WIDTH}x{HEIGHT}", "-depth", "24",
                "-rfbport", "-1", "-rfbunixpath", str(cfg.xvnc_socket), "-rfbunixmode", "0600",
                "-SecurityTypes", "None", "-nolisten", "tcp",
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
            )  # fmt: skip
        xvnc = cls(proc)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while not cfg.x11_socket.exists():
                if proc.returncode is not None or loop.time() > deadline:
                    raise LaunchError("gpu", f"Xvnc did not start (see {cfg.logs_dir / 'xvnc.log'})")
                await asyncio.sleep(0.05)
        except BaseException:
            await xvnc.stop()
            raise
        return xvnc

    async def stop(self) -> None:
        await _terminate(self.proc, 5.0)


async def gl_renderer(cfg: Config) -> str:
    env = dict(os.environ, DISPLAY=":0")
    proc = await asyncio.create_subprocess_exec(
        cfg.glxinfo_bin, "-B", env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        return ""
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    m = re.search(r"OpenGL renderer string: *(.*)", out.decode(errors="replace"))
    return m.group(1).strip() if m else ""


async def setup_gpu(cfg: Config) -> tuple[str, Xvnc | None]:
    """Resolve APPLIANCE_GPU. Return the mode and, for host, the Xvnc."""
    if cfg.gpu == "lavapipe":
        return "lavapipe", None
    node = render_node(cfg)
    if node is None:
        if cfg.gpu == "host":
            raise LaunchError("gpu", "APPLIANCE_GPU=host, but the container has no usable render node (/dev/dri/renderD*)")
        log.info("no render node: using lavapipe (software rendering)")
        return "lavapipe", None
    try:
        xvnc = await Xvnc.start(cfg, node)
    except LaunchError:
        if cfg.gpu == "host":
            raise
        log.warning("Xvnc did not start: using lavapipe (software rendering)")
        return "lavapipe", None
    try:
        renderer = await gl_renderer(cfg)
    except BaseException:
        await xvnc.stop()
        raise
    if renderer and not SOFTWARE_GL.search(renderer):
        log.info("GPU rendering on %s (%s)", node, renderer)
        return "host", xvnc
    await xvnc.stop()
    found = renderer or "none"
    if cfg.gpu == "host":
        raise LaunchError("gpu", f"APPLIANCE_GPU=host, but {node} gives no hardware OpenGL renderer (renderer: {found})")
    log.info("no hardware OpenGL on %s (renderer: %s): using lavapipe", node, found)
    return "lavapipe", None


def _flock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            return True
        raise
    finally:
        os.close(fd)  # also releases the probe lock
    return False


def _ofd_locked(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        query = struct.pack("@hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
        kind = struct.unpack("@hhqqi4x", fcntl.fcntl(fd, fcntl.F_OFD_GETLK, query))[0]
    finally:
        os.close(fd)
    return kind != fcntl.F_UNLCK


def avd_lock_holder(cfg: Config) -> Path | None:
    """Return an AVD file that another process locks, or None.

    A running emulator holds flock() on multiinstance.lock, and its qemu
    holds OFD locks on the disk images. /proc/locks hides locks of
    processes in other PID namespaces (another container, or the host
    seen from a container), so probe the locks themselves: that works
    across namespaces.
    """
    lock = cfg.avd_dir / "multiinstance.lock"
    if _flock_held(lock):
        return lock
    for path in sorted(cfg.avd_dir.glob("*")):
        if path.name.endswith((".img", ".qcow2")) and path.is_file() and _ofd_locked(path):
            return path
    return None


def remove_stale_locks(cfg: Config) -> None:
    """Remove the lock and discovery files of a dead emulator.

    After a kill, hardware-qemu.ini.lock keeps the old qemu PID. Container
    PIDs are small and repeat, so that PID is often alive again and the
    emulator would refuse to start.
    """
    if holder := avd_lock_holder(cfg):
        raise LaunchError(
            "locks",
            f"another emulator uses the AVD ({holder} is locked). Stop it first, for example a v1 appliance on the host.",
        )
    (cfg.avd_dir / "hardware-qemu.ini.lock").unlink(missing_ok=True)
    for stale in (cfg.android_home / "avd" / "running").glob("pid_*.ini"):
        stale.unlink(missing_ok=True)


def new_log(cfg: Config) -> Path:
    """Create the log of the next boot; keep the last KEEP_LOGS logs."""
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    numbered = sorted(
        (int(m.group(1)), p) for p in cfg.logs_dir.glob("emulator-*.log") if (m := re.fullmatch(r"emulator-([0-9]+)\.log", p.name))
    )
    path = cfg.logs_dir / f"emulator-{numbered[-1][0] + 1 if numbered else 1}.log"
    for _, old in numbered[: max(0, len(numbered) - KEEP_LOGS + 1)]:
        old.unlink(missing_ok=True)
    path.touch()
    return path


def latest_log(cfg: Config) -> Path | None:
    numbered = sorted(
        (int(m.group(1)), p) for p in cfg.logs_dir.glob("emulator-*.log") if (m := re.fullmatch(r"emulator-([0-9]+)\.log", p.name))
    )
    return numbered[-1][1] if numbered else None


def tail(path: Path | None, lines: int) -> str:
    if path is None or lines <= 0:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * lines))
            data = f.read()
    except OSError:
        return ""
    return "\n".join(data.decode(errors="replace").splitlines()[-lines:])


class Emulator:
    """The qemu process. The launcher execs qemu, so its PID is qemu's."""

    def __init__(self, cfg: Config, proc: asyncio.subprocess.Process, log_path: Path):
        self.cfg = cfg
        self.proc = proc
        self.log_path = log_path
        # The first line after which qemu hangs; it was killed for it.
        self.hang_line: str | None = None
        self.stopping = False
        self._exited = asyncio.create_task(proc.wait())
        self._watch = asyncio.create_task(self._watch_log())

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    async def wait(self) -> int:
        # Shielded: a cancelled waiter must not cancel the exit watch.
        code = await asyncio.shield(self._exited)
        await asyncio.shield(self._watch)
        return code

    def discovery(self) -> dict[str, str] | None:
        path = self.cfg.android_home / "avd" / "running" / f"pid_{self.pid}.ini"
        try:
            text = path.read_text(errors="replace")
        except FileNotFoundError:
            return None
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        # The emulator writes the file in steps; it is usable once it
        # names the gRPC port and its token.
        return values if values.get("grpc.port") and values.get("grpc.token") else None

    def fatal_line(self) -> str | None:
        for line in reversed(tail(self.log_path, 200).splitlines()):
            if HANG_LINE.search(line):
                return line.strip()
        return None

    async def _watch_log(self) -> None:
        offset = 0
        pending = b""
        while self.proc.returncode is None:
            try:
                with open(self.log_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
            except FileNotFoundError:
                chunk = b""
            offset += len(chunk)
            *lines, pending = (pending + chunk).split(b"\n")
            for raw in lines:
                line = raw.decode(errors="replace")
                if HANG_LINE.search(line) and not self.stopping:
                    self.hang_line = line.strip()
                    log.error("emulator failed (%s): killing qemu", self.hang_line)
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.kill()
                    return
            await asyncio.wait({self._exited}, timeout=0.5)

    async def stop(self, adb: Adb, timing: StopTiming) -> None:
        """Flush the guest, then one 'emu kill', then SIGTERM, then SIGKILL.

        Never send a second kill request while qemu tears down: that has
        aborted qemu with SIGABRT.
        """
        self.stopping = True
        if self.returncode is None:
            # "emu kill" does not flush the guest page cache: files written
            # in the last seconds were lost (missing, or empty) without this.
            with contextlib.suppress(AdbError):
                await adb.shell("sync", timeout=timing.sync)
        if self.returncode is None and await adb.emu_kill():
            if await self._exited_within(timing.emu_kill):
                return
            log.warning("qemu did not exit %ss after 'emu kill': sending SIGTERM", timing.emu_kill)
        if self.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            if await self._exited_within(timing.sigterm):
                return
            log.warning("qemu ignored SIGTERM for %ss: sending SIGKILL", timing.sigterm)
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        await self._exited_within(timing.sigkill)

    async def _exited_within(self, seconds: float) -> bool:
        await asyncio.wait({self._exited}, timeout=seconds)
        return self._exited.done()


def emulator_argv(cfg: Config, gpu: str) -> list[str]:
    features = "-QuickbootFileBacked,-Vulkan" if gpu == "host" else "-QuickbootFileBacked"
    # Without "-crash-report-mode disabled" the emulator runs crashpad_handler
    # with an upload URL at Google and writes minidumps into /tmp (tmpfs).
    # -no-metrics withholds the upload consent, but this keeps the handler
    # and the dumps away altogether.
    # "-grpc-use-token": every program in the guest reaches the gRPC port
    # through 10.0.2.2, and without a token it could read the screen and
    # type. The emulator writes a new random token into the discovery file,
    # which the guest cannot read. The option also binds gRPC to localhost.
    return [
        cfg.emulator_bin, "-avd", cfg.avd_name, "-port", "5554", "-no-window", "-no-audio", "-no-boot-anim",
        "-no-metrics", "-crash-report-mode", "disabled", "-gpu", gpu, "-feature", features, "-no-snapshot",
        "-grpc", str(GRPC_PORT), "-grpc-use-token",
    ]  # fmt: skip


async def launch(cfg: Config, gpu: str) -> Emulator:
    env = cfg.android_env()
    if gpu == "host":
        env["DISPLAY"] = ":0"
    log_path = new_log(cfg)
    with open(log_path, "wb") as out:
        # A new session keeps signals for the daemon (for example from
        # the container's init) away from qemu.
        proc = await asyncio.create_subprocess_exec(
            *emulator_argv(cfg, gpu), env=env, cwd=cfg.home,
            stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )  # fmt: skip
    log.info("emulator started (pid %d, -gpu %s, log %s)", proc.pid, gpu, log_path)
    return Emulator(cfg, proc, log_path)
