"""supervisor.py: the lifecycle rules, with the fake emulator and adb."""

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import pytest

from appliance import avd
from appliance.supervisor import LifecycleError, State, Supervisor
from conftest import FAST


async def until(predicate: Callable[[], object], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def supervisor(cfg, timing=FAST) -> AsyncIterator[Supervisor]:
    sup = Supervisor(cfg, timing)
    try:
        yield sup
    finally:
        await sup.shutdown()


def state_json(cfg) -> dict:
    return json.loads(cfg.state_file.read_text())


async def test_start_boots_and_sets_up(cfg, fake):
    async with supervisor(cfg) as sup:
        assert sup.status()["state"] == "stopped"
        await sup.start()
        status = sup.status()
        assert (status["state"], status["boot_completed"], status["gpu"], status["api_level"]) == ("running", True, "lavapipe", 35)
        assert status["phase"] is None and status["last_error"] is None and status["viewers"] == 0
        assert sup.status_line().startswith("state=running boot_completed=1 idle_seconds=")
        assert len(fake.launches()) == 1
        # The adb server starts before the emulator, so the adb key exists.
        first_adb = fake.adb_calls()[0]
        assert first_adb == ["start-server"] and (cfg.android_home / "adbkey").exists()
        setup = {"svc power stayon true", "wm dismiss-keyguard", "settings put secure stylus_handwriting_enabled 0"}
        assert setup <= set(fake.shell_commands())
        record = state_json(cfg)["avd"]
        assert record["created"] and record["booted"] and "marker" not in record
        assert avd.first_boot_marker(cfg) is None
        assert sup.idle_seconds() < 5
    assert sup.state is State.STOPPED
    assert fake.signals() == ["emu kill"]


async def test_post_boot_setup_is_best_effort(cfg, fake):
    fake.configure(setup_hangs=True)
    async with supervisor(cfg, replace(FAST, setup_command=0.3)) as sup:
        await sup.start()
        assert sup.state is State.RUNNING
        assert "wm dismiss-keyguard" in fake.shell_commands()


async def test_concurrent_starts_share_one_boot(cfg, fake):
    fake.configure(boot_delay=0.5)
    async with supervisor(cfg) as sup:

        async def command():
            async with sup.lease():
                return sup.state

        states = await asyncio.gather(*(command() for _ in range(5)), sup.start())
        assert states[:5] == [State.RUNNING] * 5
        assert len(fake.launches()) == 1


async def test_stop_during_start(cfg, fake):
    fake.configure(boot_delay=30)
    async with supervisor(cfg) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        await sup.stop()
        with pytest.raises(LifecycleError, match="stopped during the start") as err:
            await waiter
        assert err.value.code == "conflict"
        assert sup.state is State.STOPPED
        assert fake.signals() == ["emu kill"]
        assert fake.emulator_pid() is None


async def test_start_during_stop_waits_then_boots(cfg, fake):
    fake.configure(stop_delay=0.5)
    async with supervisor(cfg) as sup:
        await sup.start()
        await sup.stop(wait=False)
        assert sup.state is State.STOPPING
        await sup.start()
        assert sup.state is State.RUNNING
        assert len(fake.launches()) == 2
        assert fake.signals() == ["emu kill"]


async def test_restart(cfg, fake):
    async with supervisor(cfg) as sup:
        await sup.start()
        first = fake.emulator_pid()
        sup._activity = time.time() - 500
        await sup.restart()
        assert sup.state is State.RUNNING and fake.emulator_pid() != first
        assert len(fake.launches()) == 2
        assert sup.idle_seconds() < 5
        await sup.restart(wait=False)
        assert sup.state is State.STOPPING
        await until(lambda: sup.state is State.RUNNING)
        assert len(fake.launches()) == 3


async def test_wait(cfg, fake):
    fake.configure(boot_delay=0.8)
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match="stopped") as err:
            await sup.wait()
        assert err.value.code == "conflict"
        # A start with a short wait times out, and the boot continues.
        with pytest.raises(LifecycleError) as err:
            await sup.start(timeout=0.1)
        assert err.value.code == "timeout" and sup.state is State.STARTING
        await sup.wait()
        assert sup.state is State.RUNNING
        await sup.wait()


async def test_idle_stop_respects_leases(cfg, fake):
    async with supervisor(replace(cfg, idle_stop_minutes=1)) as sup:
        held = asyncio.Event()
        release = asyncio.Event()

        async def long_command():
            async with sup.lease():
                held.set()
                await release.wait()

        task = asyncio.create_task(long_command())
        await held.wait()
        sup._activity = time.time() - 120
        await asyncio.sleep(0.5)
        assert sup.state is State.RUNNING
        release.set()
        await task
        # A successful command is activity.
        assert sup.idle_seconds() < 5
        sup._activity = time.time() - 120
        await until(lambda: sup.state is State.STOPPED)
        assert fake.signals() == ["emu kill"]


async def test_idle_zero_disables(cfg, fake):
    async with supervisor(replace(cfg, idle_stop_minutes=0)) as sup:
        await sup.start()
        sup._activity = time.time() - 10**6
        await asyncio.sleep(0.5)
        assert sup.state is State.RUNNING


async def test_activity_survives_a_restart_of_the_daemon(cfg, fake):
    async with supervisor(cfg) as sup:
        sup.touch_activity()
        sup._activity -= 300
    async with supervisor(cfg) as again:
        assert 299 <= again.idle_seconds() <= 305


async def test_periodic_jobs_run_only_while_running(cfg, fake):
    async with supervisor(cfg) as sup:
        runs = []

        async def job():
            runs.append(sup.state)

        sup.every(0.05, job)
        await asyncio.sleep(0.2)
        assert runs == []
        await sup.start()
        await until(lambda: len(runs) >= 2)
        await sup.stop()
        count = len(runs)
        await asyncio.sleep(0.2)
        assert len(runs) == count and set(runs) == {State.RUNNING}


async def test_crash_while_running_then_clean_start(cfg, fake):
    async with supervisor(cfg) as sup:
        await sup.start()
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await until(lambda: sup.state is State.STOPPED)
        error = sup.status()["last_error"]
        assert error["kind"] == "crash" and error["exit_status"] == -9
        assert "killed by signal 9" in error["message"]
        assert "Boot completed" in error["log_tail"]
        assert state_json(cfg)["crashes"][-1]["kind"] == "crash"
        # The killed emulator leaves its lock. A live process with that
        # PID (here: this test) would make the next emulator refuse.
        assert (cfg.avd_dir / "hardware-qemu.ini.lock").exists()
        (cfg.avd_dir / "hardware-qemu.ini.lock").write_text(str(os.getpid()))
        async with sup.lease():
            assert sup.state is State.RUNNING
        assert sup.status()["last_error"] is None
        assert len(fake.launches()) == 2


async def test_crash_while_booting_is_retried_once(cfg, fake):
    fake.configure(mode="crash")
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match="exited with status 3") as err:
            await sup.start()
        assert err.value.code == "boot_failed"
        assert sup.state is State.STOPPED
        assert sup.status()["last_error"]["kind"] == "crash"
        assert len(fake.launches()) == 2
        assert [c.get("retried", False) for c in state_json(cfg)["crashes"]] == [True, False]


async def test_crash_during_the_boot_boots_again_for_the_waiter(cfg, fake):
    fake.configure(boot_delay=1)
    async with supervisor(cfg) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await waiter
        assert sup.state is State.RUNNING and sup.status()["last_error"] is None
        assert len(fake.launches()) == 2
        crashes = state_json(cfg)["crashes"]
        assert [(c["kind"], c["exit_status"], c["retried"]) for c in crashes] == [("crash", -9, True)]
        assert state_json(cfg)["avd"]["booted"]


async def test_crash_just_after_the_boot_is_retried_once(cfg, fake):
    async with supervisor(cfg, replace(FAST, settle=5)) as sup:
        await sup.start()
        first = fake.emulator_pid()
        # The first boot completed: its userdata stays through the retry.
        (cfg.avd_dir / "userdata-qemu.img").write_text("first boot")
        os.kill(first, signal.SIGKILL)
        await until(lambda: sup.state is State.STARTING)
        # Commands join the new boot.
        async with sup.lease():
            assert sup.state is State.RUNNING
        assert fake.emulator_pid() != first and len(fake.launches()) == 2
        assert sup.status()["last_error"] is None
        assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "first boot"
        assert state_json(cfg)["crashes"][-1]["retried"]
        # Only once per start: the next crash is reported.
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await until(lambda: sup.state is State.STOPPED)
        assert sup.status()["last_error"]["kind"] == "crash"
        assert len(fake.launches()) == 2
        assert [c.get("retried", False) for c in state_json(cfg)["crashes"]] == [True, False]
        # A new start request gets a retry of its own.
        await sup.start()
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await until(lambda: len(fake.launches()) == 4)
        await sup.wait()
        assert sup.state is State.RUNNING


async def test_crash_after_the_settle_window_is_reported(cfg, fake):
    async with supervisor(cfg, replace(FAST, settle=0.3)) as sup:
        await sup.start()
        await asyncio.sleep(0.5)
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await until(lambda: sup.state is State.STOPPED)
        assert sup.status()["last_error"]["kind"] == "crash"
        assert len(fake.launches()) == 1


async def test_crash_while_setting_up_keeps_the_first_boot(cfg, fake):
    fake.configure(setup_hangs=True)
    async with supervisor(cfg, replace(FAST, setup_command=2)) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "setting up")
        assert state_json(cfg)["avd"]["booted"] and avd.first_boot_marker(cfg) is None
        (cfg.avd_dir / "userdata-qemu.img").write_text("first boot")
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await waiter
        assert sup.state is State.RUNNING and len(fake.launches()) == 2
        assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "first boot"


async def test_boot_timeout_stops_the_emulator(cfg, fake):
    fake.configure(mode="hang")
    async with supervisor(replace(cfg, boot_timeout=1)) as sup:
        with pytest.raises(LifecycleError, match="did not finish booting in 1s") as err:
            await sup.start()
        assert err.value.code == "timeout"
        assert sup.state is State.STOPPED and sup.status()["last_error"]["kind"] == "timeout"
        assert fake.signals() == ["emu kill"] and fake.emulator_pid() is None


async def test_renderer_failure_kills_qemu(cfg, fake):
    fake.configure(mode="renderer")
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match="Could not start renderer"):
            await sup.start()
        error = sup.status()["last_error"]
        assert (error["kind"], error["exit_status"]) == ("renderer", -9)
        assert fake.signals() == []


async def test_fatal_exit_reports_the_line(cfg, fake):
    fake.configure(mode="fatal")
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match=re.escape("exited with status 1: FATAL | fake fatal error")):
            await sup.start()


async def test_unauthorized_fails_with_guidance(cfg, fake):
    fake.configure(unauthorized_for=-1)
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match="device unauthorized: the AVD trusts another adb key; copy the old key into"):
            await sup.start()
        assert sup.status()["last_error"]["kind"] == "unauthorized"
        assert fake.signals() == ["emu kill"]


async def test_short_unauthorized_is_fine(cfg, fake):
    fake.configure(unauthorized_for=0.5)
    async with supervisor(cfg) as sup:
        await sup.start()
        assert sup.state is State.RUNNING


async def test_interrupted_first_boot_is_recreated(cfg, fake):
    fake.configure(boot_delay=30)
    async with supervisor(cfg) as sup:
        task = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        await sup.stop()
        with pytest.raises(LifecycleError):
            await task
    record = state_json(cfg)["avd"]
    assert record["created"] and not record["booted"]
    (cfg.avd_dir / "userdata-qemu.img").write_text("half-initialized")
    fake.configure(boot_delay=0.2)
    async with supervisor(cfg) as sup:
        await sup.start()
        assert not (cfg.avd_dir / "userdata-qemu.img").exists()
        assert state_json(cfg)["avd"]["booted"]
        # A completed first boot is never recreated.
        (cfg.avd_dir / "userdata-qemu.img").write_text("userdata")
        await sup.restart()
        assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "userdata"


async def test_adopted_avd_is_never_deleted(cfg, fake):
    avd.create(cfg, 35)
    avd.first_boot_done(cfg)
    (cfg.avd_dir / "userdata-qemu.img").write_text("v1 userdata")
    fake.configure(boot_delay=30)
    async with supervisor(cfg) as sup:
        task = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        await sup.stop()
        with pytest.raises(LifecycleError):
            await task
    assert state_json(cfg)["avd"] == {"name": "android", "created": False, "booted": True}
    fake.configure(boot_delay=0.2)
    async with supervisor(cfg) as sup:
        await sup.start()
    assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "v1 userdata"


async def test_replaced_avd_without_marker_is_kept(cfg, fake):
    fake.configure(boot_delay=30)
    async with supervisor(cfg) as sup:
        task = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        await sup.stop()
        with pytest.raises(LifecycleError):
            await task
    # Someone put another AVD in place of the unbooted one.
    avd.first_boot_done(cfg)
    (cfg.avd_dir / "userdata-qemu.img").write_text("other userdata")
    fake.configure(boot_delay=0.2)
    async with supervisor(cfg) as sup:
        await sup.start()
    assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "other userdata"


async def test_api_mismatch_fails_before_launch(cfg, fake):
    avd.create(cfg, 36)
    async with supervisor(cfg) as sup:
        with pytest.raises(LifecycleError, match="does not upgrade existing AVDs") as err:
            await sup.start()
        assert err.value.code == "boot_failed" and sup.status()["last_error"]["kind"] == "avd"
    assert fake.launches() == []


async def test_held_lock_refuses_the_launch_and_leaves_the_avd_alone(cfg, fake):
    avd.create(cfg, 35)
    before = (cfg.avd_dir / "config.ini").read_text()
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); print(1, flush=True); time.sleep(60)",
         str(cfg.avd_dir / "multiinstance.lock")],
        stdout=subprocess.PIPE,
    )  # fmt: skip
    try:
        holder.stdout.readline()
        async with supervisor(cfg) as sup:
            with pytest.raises(LifecycleError, match="another emulator uses the AVD"):
                await sup.start()
            assert sup.status()["last_error"]["kind"] == "locks"
    finally:
        holder.kill()
        holder.wait()
    assert fake.launches() == []
    # The AVD of the other emulator keeps its configuration.
    assert (cfg.avd_dir / "config.ini").read_text() == before
    assert not (cfg.avd_dir / "quickbootChoice.ini").exists()
    assert "avd" not in state_json(cfg)


async def test_gpu_mode_runs_xvnc_until_stop(gpu_cfg, fake):
    async with supervisor(gpu_cfg) as sup:
        await sup.start()
        assert sup.status()["gpu"] == "host"
        assert fake.launches()[0]["env"]["DISPLAY"] == ":0"
        assert gpu_cfg.x11_socket.exists()
        await sup.stop()
        assert not gpu_cfg.x11_socket.exists()


async def test_shutdown_stops_gracefully_and_refuses_starts(cfg, fake):
    sup = Supervisor(cfg, FAST)
    await sup.start()
    await sup.shutdown()
    assert sup.state is State.STOPPED and fake.signals() == ["emu kill"]
    with pytest.raises(LifecycleError) as err:
        await sup.start()
    assert err.value.code == "unavailable"
    assert fake.adb_calls()[-1] == ["kill-server"]


async def test_shutdown_during_start(cfg, fake):
    fake.configure(boot_delay=30)
    sup = Supervisor(cfg, FAST)
    await sup.start(wait=False)
    await until(lambda: sup.phase == "booting")
    await sup.shutdown()
    assert sup.state is State.STOPPED and fake.emulator_pid() is None


# The state directory refuses writes (a full or read-only disk): the
# lifecycle goes on, with the state kept in memory.

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")


@contextlib.contextmanager
def unwritable(cfg):
    """appliance/ refuses new files, so state.json cannot be replaced."""
    cfg.appliance_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.appliance_dir, 0o555)
    try:
        yield
    finally:
        os.chmod(cfg.appliance_dir, 0o755)


@needs_non_root
async def test_crash_with_an_unwritable_state_stops_android(cfg, fake):
    async with supervisor(cfg) as sup:
        await sup.start()
        with unwritable(cfg):
            os.kill(fake.emulator_pid(), signal.SIGKILL)
            await until(lambda: sup.state is State.STOPPED)
        assert sup.status()["last_error"]["kind"] == "crash"
        # The history was kept in memory and is written by the next save.
        assert "crashes" not in state_json(cfg)
        async with sup.lease():
            pass
        assert state_json(cfg)["crashes"][-1]["kind"] == "crash"


@needs_non_root
async def test_failed_boot_with_an_unwritable_state_wakes_the_waiter(cfg, fake):
    fake.configure(boot_delay=1)
    async with supervisor(cfg) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        fake.configure(mode="crash")
        with unwritable(cfg):
            os.kill(fake.emulator_pid(), signal.SIGKILL)
            with pytest.raises(LifecycleError, match="exited with status 3"):
                await asyncio.wait_for(waiter, 10)
            assert sup.state is State.STOPPED and sup._leases == 0


@needs_non_root
async def test_stop_with_an_unwritable_state_wakes_the_waiter(cfg, fake):
    fake.configure(boot_delay=30)
    async with supervisor(cfg) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        # Not saved by itself: less than ACTIVITY_SAVE_INTERVAL after the last save.
        sup.touch_activity()
        with unwritable(cfg):
            await asyncio.wait_for(sup.stop(), 10)
            with pytest.raises(LifecycleError, match="stopped during the start"):
                await asyncio.wait_for(waiter, 10)
        assert sup.state is State.STOPPED and fake.emulator_pid() is None


@needs_non_root
async def test_boot_with_an_unwritable_state_completes_and_stops_cleanly(cfg, fake):
    fake.configure(boot_delay=1)
    async with supervisor(replace(cfg, idle_stop_minutes=1)) as sup:
        with unwritable(cfg):
            async with sup.lease():
                assert sup.state is State.RUNNING
            assert sup._leases == 0
            # The idle stop still works: no lease is left behind.
            sup._activity = time.time() - 600
            await until(lambda: sup.state is State.STOPPED)
        assert fake.emulator_pid() is None and fake.signals() == ["emu kill"]
        # The first boot is on disk once the disk works again.
        await sup.start()
        assert state_json(cfg)["avd"]["booted"]
    assert fake.emulator_pid() is None


@needs_non_root
async def test_start_that_cannot_write_its_log_fails_and_releases_the_lease(cfg, fake):
    cfg.appliance_dir.mkdir(parents=True)
    os.chmod(cfg.appliance_dir, 0o555)
    try:
        async with supervisor(cfg) as sup:
            with pytest.raises(LifecycleError, match="cannot run the emulator"):
                async with sup.lease():
                    pass
            assert sup.state is State.STOPPED and sup._leases == 0
    finally:
        os.chmod(cfg.appliance_dir, 0o755)


async def test_poweroff_of_the_guest_is_not_a_crash(cfg, fake):
    async with supervisor(cfg, replace(FAST, settle=30)) as sup:
        await sup.start()
        os.kill(fake.emulator_pid(), signal.SIGUSR2)
        await until(lambda: sup.state is State.STOPPED)
        # Not retried inside the settle window, not an error.
        assert len(fake.launches()) == 1
        assert sup.status()["last_error"] is None
        assert "crashes" not in state_json(cfg)
        await sup.start()
        assert len(fake.launches()) == 2


async def test_poweroff_during_the_boot_ends_the_start(cfg, fake):
    fake.configure(boot_delay=30)
    async with supervisor(cfg, replace(FAST, settle=30)) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        os.kill(fake.emulator_pid(), signal.SIGUSR2)
        with pytest.raises(LifecycleError, match="powered off during the start"):
            await waiter
        assert sup.state is State.STOPPED and len(fake.launches()) == 1 and sup.status()["last_error"] is None


async def test_start_without_waiting_returns_during_a_stop(cfg, fake):
    fake.configure(stop_delay=2)
    async with supervisor(cfg) as sup:
        await sup.start()
        await sup.stop(wait=False)
        assert sup.state is State.STOPPING
        started = time.monotonic()
        await sup.start(wait=False)
        assert time.monotonic() - started < 1
        # The start still happens once the stop is done.
        await until(lambda: sup.state is State.RUNNING, timeout=20)
        assert len(fake.launches()) == 2


async def test_stop_during_the_unwind_of_a_crashed_boot_wins(cfg, fake, monkeypatch):
    from appliance.adb import Adb

    real = Adb.getprop

    async def slow_unwind(self, name):
        try:
            return await real(self, name)
        except asyncio.CancelledError:
            await asyncio.sleep(0.7)
            raise

    monkeypatch.setattr(Adb, "getprop", slow_unwind)
    fake.configure(boot_delay=5)
    async with supervisor(cfg, replace(FAST, settle=5)) as sup:
        waiter = asyncio.create_task(sup.start())
        await until(lambda: sup.phase == "booting")
        os.kill(fake.emulator_pid(), signal.SIGKILL)
        await until(lambda: sup.phase == "restarting")
        await sup.stop()
        with pytest.raises(LifecycleError, match="stopped during the start"):
            await waiter
        # The retry boot never reached the launch.
        assert sup.state is State.STOPPED and len(fake.launches()) == 1
