"""turn.py: TURN credentials, the coturn configuration and the process."""

import asyncio
import base64
import hashlib
import hmac
import os
import signal
import stat
from dataclasses import replace

import pytest

from appliance import turn
from appliance.auth import secret
from appliance.turn import Turn

IP_OUTPUT = """\
2: eno1    inet 192.168.1.243/24 brd 192.168.1.255 scope global noprefixroute eno1\\       valid_lft forever preferred_lft forever
2: eno1    inet6 fd7a:115c:a1e0::d038:d57/128 scope global \\       valid_lft forever preferred_lft forever
3: tap0    inet 10.0.2.100/24 scope global tap0\\       valid_lft forever preferred_lft forever
"""


@pytest.fixture(autouse=True)
def addresses(monkeypatch):
    async def listed() -> list[str]:
        return ["192.168.1.243", "fd7a:115c:a1e0::d038:d57"]

    monkeypatch.setattr(turn, "container_addresses", listed)


async def eventually(condition, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


def test_a_credential_is_the_hmac_of_its_user_name():
    username, credential = turn.credentials("s3cret", ttl=100, now=1_000_000)
    expiry, _, ident = username.partition(":")
    assert expiry == "1000100" and len(ident) == 16
    assert credential == base64.b64encode(hmac.new(b"s3cret", username.encode(), hashlib.sha1).digest()).decode()
    # Every call is a new identity.
    assert turn.credentials("s3cret", now=1_000_000)[0] != turn.credentials("s3cret", now=1_000_000)[0]


def test_credentials_last_a_day_at_most():
    assert turn.CREDENTIAL_TTL <= 24 * 3600


def test_addresses_come_from_ip():
    assert turn.parse_addresses(IP_OUTPUT) == ["192.168.1.243", "fd7a:115c:a1e0::d038:d57", "10.0.2.100"]
    assert turn.parse_addresses("") == []
    assert turn.parse_addresses("2: eno1 inet6 fe80::1/64 scope link") == ["fe80::1"]


def test_the_configuration_relays_only_to_the_container():
    text = turn.config_text("the-secret", ["192.168.1.243", "fd7a::1"], "/tmp/appliance/turndb", "/tmp/appliance/turn.pid")
    lines = text.splitlines()
    assert "listening-port=3478" in lines and "min-port=49152" in lines and "max-port=49251" in lines
    assert "relay-ip=192.168.1.243" in lines
    assert "use-auth-secret" in lines and "static-auth-secret=the-secret" in lines
    assert "no-tcp-relay" in lines and "no-multicast-peers" in lines and "no-tls" in lines
    # Everything is denied, and the container's own addresses are allowed.
    assert lines.index("denied-peer-ip=0.0.0.0-255.255.255.255") < lines.index("allowed-peer-ip=192.168.1.243")
    assert "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff" in lines
    assert [line for line in lines if line.startswith("allowed-peer-ip=")] == ["allowed-peer-ip=192.168.1.243", "allowed-peer-ip=fd7a::1"]
    # Quotas: a stream takes two allocations, four streams make eight.
    assert f"user-quota={turn.USER_QUOTA}" in lines and f"total-quota={turn.TOTAL_QUOTA}" in lines
    # A browser that was killed leaves its relays behind only for a short while.
    assert f"max-allocate-lifetime={turn.ALLOCATION_LIFETIME}" in lines
    assert turn.USER_QUOTA >= 2 and 8 <= turn.TOTAL_QUOTA <= 49251 - 49152 + 1
    # coturn 4.18 rejects these two options; both are the default.
    assert not any(line.startswith(("no-dtls", "no-cli")) for line in lines)
    # The root file system is read-only.
    assert "userdb=/tmp/appliance/turndb" in lines and "pidfile=/tmp/appliance/turn.pid" in lines
    assert "log-file=stdout" in lines


def test_the_relay_address_is_ipv4_when_there_is_one():
    assert "relay-ip=10.0.0.5" in turn.config_text("s", ["fd7a::1", "10.0.0.5"], "u", "p").splitlines()
    assert "relay-ip=fd7a::1" in turn.config_text("s", ["fd7a::1"], "u", "p").splitlines()
    text = turn.config_text("s", [], "u", "p")
    assert "relay-ip" not in text and "allowed-peer-ip" not in text and "denied-peer-ip=0.0.0.0-255.255.255.255" in text


def test_turn_urls():
    assert turn.urls("android.example.net", 3478) == ["turn:android.example.net:3478?transport=udp", "turn:android.example.net:3478?transport=tcp"]
    assert turn.urls("fd7a::1", 47081)[0] == "turn:[fd7a::1]:47081?transport=udp"
    assert turn.urls("127.0.0.1", 44178)[1] == "turn:127.0.0.1:44178?transport=tcp"


async def test_the_server_gets_credentials_for_the_secret_in_the_state(cfg, fake):
    server = Turn(cfg)
    [ice] = server.ice_servers("example.net")
    assert ice["urls"] == turn.urls("example.net", 3478)
    key = (cfg.secrets_dir / "turn-secret").read_text().strip()
    assert ice["credential"] == base64.b64encode(hmac.new(key.encode(), ice["username"].encode(), hashlib.sha1).digest()).decode()
    assert stat.S_IMODE((cfg.secrets_dir / "turn-secret").stat().st_mode) == 0o600
    # The secret survives a restart of the daemon.
    assert secret(cfg.secrets_dir, "turn-secret") == key == Turn(cfg)._secret
    with pytest.raises(ValueError, match="Host"):
        server.ice_servers("")


async def test_the_server_starts_turnserver_with_a_private_configuration(cfg, fake):
    server = Turn(cfg)
    assert not server.running
    await server.start()
    try:
        await eventually(lambda: fake.turn_starts())
        [start] = fake.turn_starts()
        conf = cfg.tmp_dir / "appliance" / "turnserver.conf"
        assert start["argv"] == ["-c", str(conf)] and start["mode"] == 0o600
        key = (cfg.secrets_dir / "turn-secret").read_text().strip()
        assert f"static-auth-secret={key}" in start["conf"] and "allowed-peer-ip=fd7a:115c:a1e0::d038:d57" in start["conf"]
        assert server.running
    finally:
        await server.stop()
    assert not server.running


async def test_stop_ends_turnserver(cfg, fake):
    server = Turn(cfg)
    await server.start()
    proc = server._proc
    await eventually(lambda: fake.turn_starts())
    await server.stop()
    assert proc.returncode == 0 and not server.running
    # Stopping twice is fine.
    await server.stop()


async def test_a_turnserver_that_dies_is_started_again(cfg, fake, monkeypatch):
    monkeypatch.setattr(turn, "FIRST_DELAY", 0.05)
    server = Turn(cfg)
    await server.start()
    try:
        await eventually(lambda: fake.turn_starts())
        os.kill(server._proc.pid, signal.SIGKILL)
        await eventually(lambda: len(fake.turn_starts()) == 2)
        await eventually(lambda: server.running)
    finally:
        await server.stop()


async def test_a_crashing_turnserver_is_retried_with_growing_delays(cfg, fake, monkeypatch, caplog):
    caplog.set_level("INFO")
    fake.configure(turn_mode="crash")
    monkeypatch.setattr(turn, "FIRST_DELAY", 0.05)
    monkeypatch.setattr(turn, "MAX_DELAY", 0.2)
    server = Turn(cfg)
    await server.start()
    try:
        await eventually(lambda: len(fake.turn_starts()) >= 5)
    finally:
        await server.stop()
    assert any("exited with status 1" in r.message for r in caplog.records)
    assert any("cannot bind" in r.message and r.message.startswith("turnserver: ") for r in caplog.records)


async def test_a_missing_turnserver_is_an_error_not_a_crash(cfg, fake, monkeypatch, caplog):
    monkeypatch.setattr(turn, "FIRST_DELAY", 0.05)
    server = Turn(replace(cfg, turnserver_bin="/nonexistent/turnserver"))
    await server.start()
    await asyncio.sleep(0.3)
    await server.stop()
    assert not server.running and any("cannot run turnserver" in r.message for r in caplog.records)


async def test_the_log_of_turnserver_is_limited(cfg, fake, monkeypatch, caplog):
    server = Turn(cfg)
    caplog.set_level("INFO")
    for n in range(turn.LOG_LINES + 25):
        server._log_line(f"line {n}")
    lines = [r.message for r in caplog.records if r.message.startswith("turnserver: ")]
    assert len(lines) == turn.LOG_LINES
    # The window passes: the count of the lines that were dropped is told.
    monkeypatch.setattr(turn, "LOG_WINDOW", 0)
    server._log_line("later")
    assert caplog.records[-2].message == "turnserver: 25 more lines suppressed" and caplog.records[-1].message == "turnserver: later"
