"""Create, validate and adjust the AVD (port of the v1 avd-init).

The AVD format is two small ini files. Writing them directly avoids a JDK
and avdmanager at runtime. The emulator creates userdata on first boot.
"""

import os
import re
import secrets
import shutil
from pathlib import Path

from .config import DENSITY, HEIGHT, WIDTH, Config

TAG = "google_apis_playstore"
ABI = "x86_64"
# Written into a new AVD and removed after its first boot. Only an AVD
# with this marker may be recreated.
FIRST_BOOT_MARKER = "appliance-first-boot"


class AvdError(Exception):
    pass


def sysdir(api: int) -> str:
    return f"system-images/android-{api}/{TAG}/{ABI}"


def system_image(cfg: Config) -> int:
    """Return the API level of the Google Play image in the SDK."""
    root = cfg.sdk_root / "system-images"
    levels = sorted(
        int(m.group(1))
        for p in (root.iterdir() if root.is_dir() else [])
        if (m := re.fullmatch(r"android-([0-9]+)", p.name)) and (p / TAG / ABI).is_dir()
    )
    if cfg.api_level is not None:
        if cfg.api_level in levels:
            return cfg.api_level
        have = ", ".join(map(str, levels)) or "none"
        raise AvdError(
            f"APPLIANCE_API_LEVEL is {cfg.api_level}, but the image has the system images of API levels: {have}; "
            "build the image for this level with install.sh"
        )
    if len(levels) == 1:
        return levels[0]
    if not levels:
        raise AvdError(f"no {TAG} {ABI} system image in {root}")
    raise AvdError(f"the SDK has system images of API levels {', '.join(map(str, levels))}; set APPLIANCE_API_LEVEL")


def _write(path: Path, text: str) -> None:
    # Replace atomically, so a crash never leaves a half-written ini.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _registry(cfg: Config, api: int, rel: bool) -> str:
    lines = ["avd.ini.encoding=UTF-8", f"path={cfg.avd_dir}"]
    if rel:
        lines.append(f"path.rel=avd/{cfg.avd_name}.avd")
    lines.append(f"target=android-{api}")
    return "\n".join(lines) + "\n"


def exists(cfg: Config) -> bool:
    return (cfg.avd_dir / "config.ini").is_file()


def create(cfg: Config, api: int) -> str | None:
    """Write a new AVD. Return its first-boot marker, or None when the
    directory already held files (they are kept, and never deleted)."""
    fresh = not cfg.avd_dir.exists() or not any(cfg.avd_dir.iterdir())
    cfg.avd_dir.mkdir(parents=True, exist_ok=True)
    _write(cfg.avd_home / f"{cfg.avd_name}.ini", _registry(cfg, api, rel=True))
    _write(
        cfg.avd_dir / "config.ini",
        f"""avd.ini.encoding=UTF-8
AvdId={cfg.avd_name}
avd.ini.displayname={cfg.avd_name}
abi.type={ABI}
hw.cpu.arch={ABI}
image.sysdir.1={sysdir(api)}/
tag.id={TAG}
tag.display=Google Play
PlayStore.enabled=true
disk.dataPartition.size={cfg.disk_size}
fastboot.forceColdBoot=yes
fastboot.forceFastBoot=no
hw.lcd.width={WIDTH}
hw.lcd.height={HEIGHT}
hw.lcd.density={DENSITY}
hw.keyboard=yes
hw.mainKeys=no
hw.gpu.enabled=yes
hw.audioInput=no
hw.audioOutput=no
hw.camera.back=none
hw.camera.front=none
showDeviceFrame=no
""",
    )
    if not fresh:
        return None
    marker = secrets.token_hex(8)
    (cfg.avd_dir / FIRST_BOOT_MARKER).write_text(marker + "\n")
    return marker


def first_boot_marker(cfg: Config) -> str | None:
    try:
        return (cfg.avd_dir / FIRST_BOOT_MARKER).read_text().strip()
    except FileNotFoundError:
        return None


def first_boot_done(cfg: Config) -> None:
    (cfg.avd_dir / FIRST_BOOT_MARKER).unlink(missing_ok=True)


def delete(cfg: Config, marker: str) -> None:
    """Delete an AVD that create() wrote and that never booted."""
    if not marker or first_boot_marker(cfg) != marker:
        raise AvdError(f"refusing to delete {cfg.avd_dir}: it is not an unbooted AVD of this appliance")
    shutil.rmtree(cfg.avd_dir)


def _read_key(lines: list[str], key: str) -> str | None:
    # The emulator rewrites config.ini as "key = value" with spaces.
    pattern = re.compile(rf"^{re.escape(key)} *= *(.*)$")
    for line in lines:
        if m := pattern.match(line):
            return m.group(1)
    return None


def _set_key(lines: list[str], key: str, value: str) -> bool:
    current = _read_key(lines, key)
    # The emulator can write sizes back with a unit, for example 2048M.
    # Do not touch an equal value: a config change forces a cold boot.
    if current is not None and re.sub(r"^([0-9]+)[MG]$", r"\1", current) == value:
        return False
    if current is None:
        lines.append(f"{key}={value}")
    else:
        pattern = re.compile(rf"^{re.escape(key)} *=")
        lines[:] = [f"{key}={value}" if pattern.match(line) else line for line in lines]
    return True


def configure(cfg: Config, api: int) -> None:
    """Validate the AVD and set the managed keys. Write the registry that
    the emulator reads into cfg.registry_dir.

    A mismatched system image is refused before anything is written:
    swapping the OS under existing userdata would be unsafe.
    """
    config = cfg.avd_dir / "config.ini"
    lines = config.read_text().splitlines()
    image = (_read_key(lines, "image.sysdir.1") or "").rstrip("/")
    if image != sysdir(api):
        raise AvdError(
            f"the existing AVD '{cfg.avd_name}' uses '{image}', but the image provides API level {api} "
            f"({sysdir(api)}/). The API level does not upgrade existing AVDs; "
            "use an image of the matching level or recreate the AVD."
        )
    changed = False
    for key, value in (
        ("hw.ramSize", str(cfg.memory_mib)),
        ("hw.cpu.ncore", str(cfg.cores)),
        # Keep adopted AVDs at the native size of the display.
        ("hw.lcd.width", str(WIDTH)),
        ("hw.lcd.height", str(HEIGHT)),
        ("hw.lcd.density", str(DENSITY)),
        # Snapshots stay disabled for adopted AVDs as well as new ones.
        ("fastboot.forceColdBoot", "yes"),
        ("fastboot.forceFastBoot", "no"),
    ):
        changed |= _set_key(lines, key, value)
    if changed:
        _write(config, "\n".join(lines) + "\n")
    quickboot = cfg.avd_dir / "quickbootChoice.ini"
    if not quickboot.is_file() or quickboot.read_text() != "saveOnExit=false\n":
        _write(quickboot, "saveOnExit=false\n")
    # A v1 registry holds the host path; never edit it. The emulator reads
    # this one instead.
    cfg.registry_dir.mkdir(parents=True, exist_ok=True)
    _write(cfg.registry_dir / f"{cfg.avd_name}.ini", _registry(cfg, api, rel=False))
