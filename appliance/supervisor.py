"""Lifecycle state machine, idle policy, leases and crashes."""

import asyncio
import contextlib
import enum
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, avd, emulator
from .adb import Adb, AdbError
from .config import Config
from .emulator import Emulator, LaunchError, StopTiming, Xvnc
from .grpc_client import EmulatorGrpc

log = logging.getLogger(__name__)

# The activity time is saved at most this often; a daemon restart loses
# less than that.
ACTIVITY_SAVE_INTERVAL = 10.0
CRASH_HISTORY = 20
LOG_TAIL = 40
# The appliance has no physical power button: keep the screen on and
# unlocked, so taps and the display work at once. The emulated
# touchscreen also reports itself as a stylus, so Gboard opens its
# "Try out your stylus" sheet at the first focus of a text field, and
# that sheet takes the next text entry; the setting prevents it.
POST_BOOT = ("svc power stayon true", "wm dismiss-keyguard", "settings put secure stylus_handwriting_enabled 0")


class _Watched[T]:
    """A field of the status. Setting a new value tells the subscribers."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.attr = f"_{name}"

    def __get__(self, obj: "Supervisor | None", owner: type | None = None) -> T:
        return self if obj is None else getattr(obj, self.attr)  # type: ignore[return-value]

    def __set__(self, obj: "Supervisor", value: T) -> None:
        if getattr(obj, self.attr, _UNSET) != value:
            setattr(obj, self.attr, value)
            obj._changed()


_UNSET = object()


class State(enum.StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class LifecycleError(Exception):
    """A lifecycle request failed. `code` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _Failure(Exception):
    def __init__(self, kind: str, message: str, exit_status: int | None = None, code: str = "boot_failed"):
        super().__init__(message)
        self.kind = kind
        self.exit_status = exit_status
        self.code = code


@dataclass(frozen=True)
class Timing:
    poll: float = 1.0
    # A guest that answers "unauthorized" this long trusts another key.
    unauthorized: float = 30.0
    idle_check: float = 30.0
    setup_command: float = 20.0
    # A crash this soon after the boot counts as a failed boot: it gets
    # one more boot, like a crash during the boot.
    settle: float = 30.0
    stop: StopTiming = field(default_factory=StopTiming)


class StateStore:
    """appliance/state.json: AVD origin, first-boot flag, activity, crashes."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = self._load()
        self._unwritable = False

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        """Write the file. A full or read-only disk must not stop the
        lifecycle: the data stays in memory and the next call tries again."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            if not self._unwritable:
                log.error("cannot write %s: %s; keeping the state in memory until the disk works again", self.path, e)
            self._unwritable = True
        else:
            if self._unwritable:
                log.info("%s is writable again", self.path)
            self._unwritable = False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _exit_text(code: int) -> str:
    return f"was killed by signal {-code}" if code < 0 else f"exited with status {code}"


class Supervisor:
    # The status fields that subscribers hear about.
    state = _Watched[State]()
    phase = _Watched[str | None]()
    gpu = _Watched[str | None]()
    api_level = _Watched[int | None]()
    # Dashboard viewers; the session front end counts them.
    viewers = _Watched[int]()
    last_error = _Watched[dict[str, Any] | None]()

    def __init__(self, cfg: Config, timing: Timing | None = None):
        self.cfg = cfg
        self.timing = timing or Timing()
        self._subscribers: list[Callable[[], None]] = []
        self._notify_pending = False
        self.store = StateStore(cfg.state_file)
        self.adb = Adb(cfg.adb_bin, cfg.android_env())
        self.state = State.STOPPED
        self.phase = None
        self.gpu = None
        self.api_level = None
        self.viewers = 0
        self.last_error = None
        self.grpc: EmulatorGrpc | None = None
        self._emu: Emulator | None = None
        self._xvnc: Xvnc | None = None
        self._boot_task: asyncio.Task[None] | None = None
        self._boot_done: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._jobs: list[tuple[float, Callable[[], Awaitable[None]]]] = []
        self._job_tasks: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[None]] = set()
        self._leases = 0
        self._launched_at: float | None = None
        self._running_since: float | None = None
        # One retry per start request, so a crash never loops.
        self._retried = False
        self._closing = False
        self._activity: float | None = self.store.data.get("activity")
        self._activity_saved = self._activity or 0.0
        with contextlib.suppress(avd.AvdError, OSError):
            self.api_level = avd.system_image(cfg)
        self.every(self.timing.idle_check, self._idle_check)

    # Status and activity

    def idle_seconds(self) -> int:
        return 0 if self._activity is None else max(0, int(time.time() - self._activity))

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "boot_completed": self.state is State.RUNNING,
            "idle_seconds": self.idle_seconds(),
            "idle_stop_seconds": self.cfg.idle_stop_seconds,
            "uptime_seconds": 0 if self._launched_at is None else int(time.monotonic() - self._launched_at),
            "phase": self.phase,
            "gpu": self.gpu,
            "api_level": self.api_level,
            "avd": self.cfg.avd_name,
            "viewers": self.viewers,
            "last_error": self.last_error,
            "version": __version__,
        }

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Call `callback` after the status changed: state, phase, gpu,
        viewers or last_error. Changes in one step of the event loop give
        one call. The idle and uptime seconds do not count as changes."""
        self._subscribers.append(callback)

    def _changed(self) -> None:
        if not self._subscribers or self._notify_pending:
            return
        self._notify_pending = True
        asyncio.get_running_loop().call_soon(self._notify)

    def _notify(self) -> None:
        self._notify_pending = False
        for callback in self._subscribers:
            try:
                callback()
            except Exception:
                log.exception("a status subscriber failed")

    def status_line(self) -> str:
        boot = 1 if self.state is State.RUNNING else 0
        return f"state={self.state.value} boot_completed={boot} idle_seconds={self.idle_seconds()}"

    def touch_activity(self) -> None:
        """Record use: automation, start, dashboard input, a new stream."""
        self._activity = time.time()
        if self._activity - self._activity_saved >= ACTIVITY_SAVE_INTERVAL:
            self._save_activity()

    def _save_activity(self) -> None:
        if self._activity is not None and self._activity != self.store.data.get("activity"):
            self.store.data["activity"] = self._activity
            self.store.save()
            self._activity_saved = self._activity

    # Requests

    async def start(self, wait: bool = True, timeout: float | None = None) -> None:
        self.touch_activity()
        await self._start(wait, timeout)

    async def stop(self, wait: bool = True) -> None:
        if self.state in (State.STARTING, State.RUNNING):
            self._begin_stop()
        if wait and self.state is State.STOPPING and self._stop_task:
            await asyncio.shield(self._stop_task)

    async def restart(self, wait: bool = True, timeout: float | None = None) -> None:
        self.touch_activity()
        if self.state in (State.STARTING, State.RUNNING):
            self._begin_stop()
        if wait:
            await self._start(True, timeout)
        else:
            self._spawn(self._start(False, None))

    async def wait(self, timeout: float | None = None) -> None:
        if self.state is State.RUNNING:
            return
        if self.state is not State.STARTING:
            raise LifecycleError("conflict", f"android is {self.state.value}")
        await self._join_boot(timeout)

    @contextlib.asynccontextmanager
    async def lease(self, wake: bool = True) -> AsyncIterator[EmulatorGrpc]:
        """Start Android if needed and keep the idle stop away while the
        body runs. Success counts as activity. With `wake` false (a look at
        the screen, not use) the lease never starts Android, fails with
        `conflict` while it does not run, and does not count as activity."""
        self._leases += 1
        try:
            if wake:
                await self._start(True, None)
            elif self.state is not State.RUNNING:
                raise LifecycleError("conflict", f"android is {self.state.value}")
            if self.grpc is None:
                raise LifecycleError("unavailable", f"android is {self.state.value}")
            yield self.grpc
            if wake:
                self.touch_activity()
        finally:
            self._leases -= 1

    def every(self, interval: float, job: Callable[[], Awaitable[None]]) -> None:
        """Run `job` every `interval` seconds while Android is running."""
        self._jobs.append((interval, job))
        if self.state is State.RUNNING:
            self._job_tasks.append(asyncio.create_task(self._repeat(interval, job)))

    async def shutdown(self) -> None:
        """Stop Android gracefully and refuse new starts (daemon exit)."""
        self._closing = True
        await self.stop()
        self._save_activity()
        await self.adb.kill_server()

    # State machine

    async def _start(self, wait: bool, timeout: float | None) -> None:
        while True:
            if self._closing:
                raise LifecycleError("unavailable", "the appliance daemon is shutting down")
            match self.state:
                case State.RUNNING:
                    return
                case State.STOPPING:
                    if not wait:
                        # v1's asynchronous start returned at once, however long the stop takes.
                        self._spawn(self._start(False, None))
                        return
                    assert self._stop_task
                    await asyncio.shield(self._stop_task)
                    continue
                case State.STOPPED:
                    self._begin_boot()
            if wait:
                await self._join_boot(timeout)
            return

    async def _join_boot(self, timeout: float | None) -> None:
        done = self._boot_done
        assert done
        finished, _ = await asyncio.wait({done}, timeout=timeout)
        if not finished:
            raise LifecycleError("timeout", f"android did not finish booting in {timeout:g}s; it is still starting")
        done.result()

    def _begin_boot(self) -> None:
        self.state = State.STARTING
        self.phase = "preparing"
        self.last_error = None
        self._retried = False
        self._boot_done = self._new_boot_future()
        self._boot_task = asyncio.create_task(self._boot())

    def _new_boot_future(self) -> asyncio.Future[None]:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # Starts without a waiter must not log "exception never retrieved".
        done.add_done_callback(lambda f: f.cancelled() or f.exception())
        return done

    def _begin_stop(self, error: LifecycleError | None = None) -> None:
        """Stop Android. A start in progress fails with `error` once
        Android is stopped."""
        self.state = State.STOPPING
        self.phase = None
        self._cancel_jobs()
        boot = self._boot_task
        if boot and not boot.done() and boot is not asyncio.current_task():
            boot.cancel()
        error = error or LifecycleError("conflict", "android was stopped during the start")
        self._stop_task = asyncio.create_task(self._stop(boot, self._boot_done, error))

    def _cancel_jobs(self) -> None:
        for task in self._job_tasks:
            task.cancel()
        self._job_tasks.clear()

    async def _boot(self, previous: asyncio.Task[None] | None = None) -> None:
        try:
            if previous is not None:
                # The crashed boot unwinds before its leftovers go away.
                # wait() does not raise for the outcome of `previous`, but
                # a stop that cancels this boot still gets through.
                await asyncio.wait({previous})
                await self._teardown()
            await self._launch()
        except _Failure as f:
            # When qemu is gone, the watcher reports the exit and decides
            # about a retry; a failure seen here is only its consequence.
            if self._emu is None or self._emu.returncode is None:
                self._fail(f)
            return
        except Exception as e:
            log.exception("unexpected error while starting android")
            self._fail(_Failure("internal", f"internal error while starting: {e}"))
            return
        self.phase = None
        self.state = State.RUNNING
        self._running_since = time.monotonic()
        assert self._boot_done
        self._boot_done.set_result(None)
        for interval, job in self._jobs:
            self._job_tasks.append(asyncio.create_task(self._repeat(interval, job)))
        log.info("android is running (%ds after launch)", self.status()["uptime_seconds"])

    def _record(self, f: _Failure, retried: bool = False) -> dict[str, Any]:
        """Add a failure to the crash history; return it as last_error."""
        error = {
            "kind": f.kind,
            "message": str(f),
            "exit_status": f.exit_status,
            "log_tail": emulator.tail(self._emu.log_path, LOG_TAIL) if self._emu else "",
            "time": _now(),
        }
        crashes = self.store.data.setdefault("crashes", [])
        crashes.append({k: error[k] for k in ("kind", "message", "exit_status", "time")} | ({"retried": True} if retried else {}))
        del crashes[:-CRASH_HISTORY]
        self.store.save()
        return error

    def _fail(self, f: _Failure) -> None:
        self.last_error = self._record(f)
        log.error("android failed (%s): %s", f.kind, f)
        self._begin_stop(LifecycleError(f.code, str(f)))

    def _settling(self) -> bool:
        """Whether a crash now still counts as a failed boot."""
        if self.state is State.STARTING:
            return True
        return self._running_since is not None and time.monotonic() - self._running_since < self.timing.settle

    def _retry(self, f: _Failure) -> None:
        """Boot once more after a crash; a waiting start keeps waiting."""
        self._retried = True
        self._record(f, retried=True)
        log.warning("%s %ds after the launch: starting android again", f, self.status()["uptime_seconds"])
        self._cancel_jobs()
        boot = self._boot_task
        if boot and not boot.done():
            boot.cancel()
        self.state = State.STARTING
        self.phase = "restarting"
        if self._boot_done is None or self._boot_done.done():
            self._boot_done = self._new_boot_future()
        self._boot_task = asyncio.create_task(self._boot(previous=boot))

    async def _stop(self, boot: asyncio.Task[None] | None, done: asyncio.Future[None] | None, error: LifecycleError) -> None:
        try:
            if boot is not None:
                # Let a cancelled boot unwind before tearing down.
                try:
                    await boot
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # The leftovers still have to go away.
                    log.exception("the boot ended with an error")
            await self._teardown()
        except Exception:
            log.exception("error while stopping android")
        finally:
            self.state = State.STOPPED
            self.phase = None
            self._launched_at = None
            self._running_since = None
            # Waiters first: nothing after this may keep them waiting.
            if done and not done.done():
                done.set_exception(error)
            self._save_activity()

    async def _teardown(self) -> None:
        emu = self._emu
        if emu is not None:
            if emu.returncode is None:
                log.info("stopping android")
            await emu.stop(self.adb, self.timing.stop)
            if emu.returncode is None:
                log.error("qemu (pid %d) did not exit after SIGKILL", emu.pid)
            else:
                log.info("qemu %s", _exit_text(emu.returncode))
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        if self.grpc is not None:
            await self.grpc.close()
            self.grpc = None
        if self._xvnc is not None:
            await self._xvnc.stop()
            self._xvnc = None
        self._emu = None

    async def _watch(self, emu: Emulator) -> None:
        code = await emu.wait()
        if self._emu is not emu or self.state not in (State.STARTING, State.RUNNING):
            return
        try:
            self._exited(emu, code)
        except Exception:
            # Never leave a dead qemu behind a "running" state.
            log.exception("cannot handle the exit of the emulator")
            if self.state in (State.STARTING, State.RUNNING):
                self._begin_stop(LifecycleError("unavailable", "the emulator exited and the daemon could not handle it"))

    def _exited(self, emu: Emulator, code: int) -> None:
        if emu.hang_line:
            self._fail(_Failure("renderer", f"the emulator failed and was killed: {emu.hang_line}", code))
            return
        line = emu.fatal_line()
        if code == 0 and not line:
            # A power-off from the guest (reboot -p): the user asked for it.
            log.info("android powered off")
            self._begin_stop(LifecycleError("conflict", "android powered off during the start"))
            return
        message = f"the emulator {_exit_text(code)}" + (f": {line}" if line else "")
        failure = _Failure("crash", message, code)
        # Measured on the GPU: 2 of 23 first boots died in gfxstream a few
        # seconds after boot_completed, and the next boot was fine. A
        # FATAL line is a refusal that would repeat.
        if line or self._retried or not self._settling():
            self._fail(failure)
        else:
            self._retry(failure)

    async def _repeat(self, interval: float, job: Callable[[], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await job()
            except Exception:
                log.exception("periodic job failed")

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        async def run() -> None:
            try:
                await coro
            except LifecycleError as e:
                log.warning("%s", e)

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _idle_check(self) -> None:
        limit = self.cfg.idle_stop_seconds
        if not limit or self._leases or self.state is not State.RUNNING:
            return
        if (idle := self.idle_seconds()) >= limit:
            log.info("idle for %ds: stopping android", idle)
            self._begin_stop()

    # Launch

    async def _launch(self) -> None:
        cfg = self.cfg
        deadline = asyncio.timeout(cfg.boot_timeout)
        try:
            async with deadline:
                await self._prepare()
                await self.adb.start_server()
                self.phase = "gpu"
                self.gpu, self._xvnc = await emulator.setup_gpu(cfg)
                self.phase = "launching"
                try:
                    self._emu = await emulator.launch(cfg, self.gpu)
                except OSError as e:
                    raise _Failure("launch", f"cannot run the emulator: {e}") from e
                self._launched_at = time.monotonic()
                self._watcher = asyncio.create_task(self._watch(self._emu))
                await self._connect(self._emu)
                await self._wait_device()
                self.phase = "booting"
                while await self.adb.getprop("sys.boot_completed") != "1":
                    await asyncio.sleep(self.timing.poll)
                self._booted()
                self.phase = "setting up"
                await self._setup()
        except TimeoutError as e:
            if deadline.expired():
                raise _Failure("timeout", f"android did not finish booting in {cfg.boot_timeout}s", code="timeout") from e
            raise
        except LaunchError as e:
            raise _Failure(e.kind, str(e)) from e
        except AdbError as e:
            raise _Failure("adb", str(e)) from e

    async def _prepare(self) -> None:
        cfg = self.cfg
        # Before anything touches the AVD: an emulator that runs on it
        # (a v1 appliance, a copy of a live state directory) keeps its
        # configuration.
        emulator.remove_stale_locks(cfg)
        try:
            self.api_level = api = avd.system_image(cfg)
            record = self.store.data.get("avd")
            if not isinstance(record, dict) or record.get("name") != cfg.avd_name:
                record = None
            if avd.exists(cfg) and record and record.get("created") and not record.get("booted"):
                marker = record.get("marker")
                if marker and avd.first_boot_marker(cfg) == marker:
                    # A killed first boot leaves userdata that answers
                    # "unauthorized" for good.
                    log.warning("the first boot of AVD '%s' did not complete: recreating it", cfg.avd_name)
                    avd.delete(cfg, marker)
                else:
                    record = None
            if not avd.exists(cfg):
                log.info("creating AVD '%s' in %s", cfg.avd_name, cfg.avd_dir)
                marker = avd.create(cfg, api)
                record = {"name": cfg.avd_name, "created": marker is not None, "booted": False, "marker": marker}
            elif record is None:
                log.info("adopting the existing AVD '%s'; keeping userdata", cfg.avd_name)
                record = {"name": cfg.avd_name, "created": False, "booted": True}
            self.store.data["avd"] = record
            self.store.save()
            avd.configure(cfg, api)
        except avd.AvdError as e:
            raise _Failure("avd", str(e)) from e
        cfg.android_home.mkdir(parents=True, exist_ok=True)

    async def _connect(self, emu: Emulator) -> None:
        self.phase = "connecting"
        while (found := emu.discovery()) is None:
            await asyncio.sleep(self.timing.poll / 4)
        self.grpc = EmulatorGrpc(int(found["grpc.port"]), found["grpc.token"])
        while True:
            try:
                await self.grpc.status()
                return
            except Exception:
                await asyncio.sleep(self.timing.poll / 4)

    async def _wait_device(self) -> None:
        self.phase = "waiting for adb"
        loop = asyncio.get_running_loop()
        since: float | None = None
        while (state := await self.adb.state()) != "device":
            if state != "unauthorized":
                since = None
            elif since is None:
                since = loop.time()
            elif loop.time() - since > self.timing.unauthorized:
                raise _Failure(
                    "unauthorized",
                    "device unauthorized: the AVD trusts another adb key; copy the old key into "
                    f"{self.cfg.android_home}/ (home/.android/ in the state directory) then restart",
                )
            await asyncio.sleep(self.timing.poll)

    def _booted(self) -> None:
        # Recorded as soon as the guest reports the boot: the userdata of
        # a completed first boot is kept whatever happens afterwards.
        record = self.store.data.get("avd") or {}
        if not record.get("booted"):
            record["booted"] = True
            record.pop("marker", None)
            self.store.save()
            try:
                avd.first_boot_done(self.cfg)
            except OSError as e:
                # The record says "booted", so the marker alone deletes nothing.
                log.warning("cannot remove the first-boot marker: %s", e)

    async def _setup(self) -> None:
        # Best effort, as in v1: a failed setting does not fail the boot.
        for command in POST_BOOT:
            try:
                result = await self.adb.shell(command, timeout=self.timing.setup_command)
            except AdbError as e:
                log.warning("post-boot '%s' failed: %s", command, e)
                continue
            if result.exit_code:
                log.warning("post-boot '%s' failed: %s", command, result.output)
        self.touch_activity()
