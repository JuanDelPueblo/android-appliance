"""Read and validate the APPLIANCE_* configuration.

In the container the configuration file is the environment file, so the
values arrive as environment variables. APPLIANCE_CONFIG may name the
file itself (development runs); its KEY=value lines fill keys that the
environment does not set, with the v1 rules: one key per line, no
spaces, no quotes, '#' starts a comment line.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

GPU_MODES = ("auto", "host", "lavapipe")
# The managed display of every AVD (see avd.py and input.py).
WIDTH, HEIGHT, DENSITY = 1080, 1920, 420
SERIAL = "emulator-5554"
ADB_SERVER_PORT = 5037
GRPC_PORT = 8554
# coturn listens on this port in the container. The host publishes it as
# APPLIANCE_TURN_PORT, and the browser uses that port.
TURN_PORT = 3478


# A host name, an IPv4 address or an IPv6 address (without brackets or port).
HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    avd_name: str = "android"
    api_level: int | None = None
    gpu: str = "auto"
    idle_stop_minutes: int = 60
    memory_mib: int = 4096
    cores: int = 4
    disk_size: str = "32G"
    boot_timeout: int = 900
    max_upload_mib: int = 4096
    trust_proxy: bool = False
    # Where the host publishes the HTTP port; used for login links.
    listen_address: str = "127.0.0.1"
    port: int = 6090
    # The TURN port that the host publishes, and the host name of the TURN
    # URLs ("" means the host name of the page).
    turn_port: int = 3478
    turn_host: str = ""
    # Container-internal paths and programs. Tests and development runs
    # override them.
    http_port: int = 8080
    data_dir: Path = Path("/data")
    run_dir: Path = Path("/run/appliance")
    tmp_dir: Path = Path("/tmp")
    sdk_root: Path = Path("/opt/android-sdk")
    web_dir: Path = Path(__file__).resolve().parent.parent / "web"
    emulator_bin: str = ""
    adb_bin: str = ""
    xvnc_bin: str = "Xvnc"
    glxinfo_bin: str = "glxinfo"
    turnserver_bin: str = "turnserver"
    # None: the first /dev/dri/renderD* node.
    render_node: str | None = None

    @property
    def avd_home(self) -> Path:
        return self.data_dir / "avd"

    @property
    def avd_dir(self) -> Path:
        return self.avd_home / f"{self.avd_name}.avd"

    @property
    def home(self) -> Path:
        return self.data_dir / "home"

    @property
    def android_home(self) -> Path:
        return self.home / ".android"

    @property
    def appliance_dir(self) -> Path:
        return self.data_dir / "appliance"

    @property
    def state_file(self) -> Path:
        return self.appliance_dir / "state.json"

    @property
    def secrets_dir(self) -> Path:
        return self.appliance_dir / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.appliance_dir / "logs"

    @property
    def upload_dir(self) -> Path:
        # Large uploads go to the state disk; /tmp is a small tmpfs.
        return self.appliance_dir / "tmp"

    @property
    def lock_file(self) -> Path:
        return self.appliance_dir / "daemon.lock"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "api.sock"

    @property
    def registry_dir(self) -> Path:
        # The registry that the emulator reads (ANDROID_AVD_HOME). It lives
        # on tmpfs, so the registry in the state directory stays untouched.
        return self.tmp_dir / "appliance" / "avd"

    @property
    def x11_socket(self) -> Path:
        return self.tmp_dir / ".X11-unix" / "X0"

    @property
    def xvnc_socket(self) -> Path:
        return self.tmp_dir / "xvnc.sock"

    @property
    def idle_stop_seconds(self) -> int:
        return self.idle_stop_minutes * 60

    def android_env(self) -> dict[str, str]:
        """The environment of the emulator and of every adb call."""
        env = dict(os.environ)
        env.update(
            HOME=str(self.home),
            ANDROID_USER_HOME=str(self.android_home),
            ANDROID_EMULATOR_HOME=str(self.android_home),
            ANDROID_AVD_HOME=str(self.registry_dir),
            ANDROID_SDK_ROOT=str(self.sdk_root),
            ANDROID_HOME=str(self.sdk_root),
            ANDROID_ADB_SERVER_PORT=str(ADB_SERVER_PORT),
            # Without USER the emulator writes to /tmp/android-unknown and
            # logs errors about it.
            USER=os.environ.get("USER") or "android",
        )
        env.pop("DISPLAY", None)
        return env


def read_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines with the rules of the v1 appliance-env."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values.setdefault(key, value)
    return values


def load(environ: Mapping[str, str] | None = None) -> Config:
    env = dict(os.environ if environ is None else environ)
    if file := env.get("APPLIANCE_CONFIG"):
        for key, value in read_file(Path(file)).items():
            env.setdefault(key, value)

    def get(key: str) -> str | None:
        value = env.get(key)
        return value if value else None

    def integer(key: str, default: int, minimum: int) -> int:
        raw = get(key)
        if raw is None:
            return default
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) < minimum:
            raise ConfigError(f"{key} must be an integer of at least {minimum}, not '{raw}'")
        return int(raw)

    name = get("APPLIANCE_AVD_NAME") or "android"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError(f"APPLIANCE_AVD_NAME may contain only letters, digits, '.', '_' and '-', not '{name}'")
    gpu = get("APPLIANCE_GPU") or "auto"
    if gpu not in GPU_MODES:
        raise ConfigError(f"APPLIANCE_GPU must be auto, host or lavapipe, not '{gpu}'")
    disk = get("APPLIANCE_DISK_SIZE") or "32G"
    if not re.fullmatch(r"[0-9]+[KMG]?", disk):
        raise ConfigError(f"APPLIANCE_DISK_SIZE must be a size such as 32G, not '{disk}'")
    trust = get("APPLIANCE_TRUST_PROXY") or "0"
    if trust not in ("0", "1"):
        raise ConfigError(f"APPLIANCE_TRUST_PROXY must be 0 or 1, not '{trust}'")
    turn_host = (get("APPLIANCE_TURN_HOST") or "").strip("[]")
    if turn_host and not HOST.fullmatch(turn_host):
        raise ConfigError(f"APPLIANCE_TURN_HOST must be a host name or an IP address without a port, not '{turn_host}'")
    api = get("APPLIANCE_API_LEVEL")
    sdk = Path(get("APPLIANCE_SDK_ROOT") or "/opt/android-sdk")

    defaults = Config()
    return Config(
        avd_name=name,
        api_level=integer("APPLIANCE_API_LEVEL", 0, 1) if api else None,
        gpu=gpu,
        idle_stop_minutes=integer("APPLIANCE_IDLE_STOP_MINUTES", 60, 0),
        memory_mib=integer("APPLIANCE_MEMORY_MIB", 4096, 512),
        cores=integer("APPLIANCE_CORES", 4, 1),
        disk_size=disk,
        boot_timeout=integer("APPLIANCE_BOOT_TIMEOUT", 900, 1),
        max_upload_mib=integer("APPLIANCE_MAX_UPLOAD_MIB", 4096, 1),
        trust_proxy=trust == "1",
        listen_address=get("APPLIANCE_LISTEN_ADDRESS") or "127.0.0.1",
        port=integer("APPLIANCE_PORT", 6090, 1),
        turn_port=integer("APPLIANCE_TURN_PORT", 3478, 1),
        turn_host=turn_host,
        http_port=integer("APPLIANCE_HTTP_PORT", 8080, 0),
        data_dir=Path(get("APPLIANCE_DATA_DIR") or "/data"),
        run_dir=Path(get("APPLIANCE_RUN_DIR") or "/run/appliance"),
        tmp_dir=Path(get("APPLIANCE_TMP_DIR") or "/tmp"),
        sdk_root=sdk,
        web_dir=Path(get("APPLIANCE_WEB_DIR") or defaults.web_dir),
        emulator_bin=get("APPLIANCE_EMULATOR") or str(sdk / "emulator" / "emulator"),
        adb_bin=get("APPLIANCE_ADB") or str(sdk / "platform-tools" / "adb"),
        xvnc_bin=get("APPLIANCE_XVNC") or "Xvnc",
        glxinfo_bin=get("APPLIANCE_GLXINFO") or "glxinfo",
        turnserver_bin=get("APPLIANCE_TURNSERVER") or "turnserver",
        render_node=get("APPLIANCE_RENDER_NODE"),
    )
