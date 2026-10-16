"""Fake adb for one fake emulator (see common.py)."""

import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import common

UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">\
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example" content-desc="" \
clickable="false" enabled="true" focused="false" scrollable="false" bounds="[0,0][1080,1920]">\
<node index="0" text="First name" resource-id="com.example:id/name" class="android.widget.EditText" \
package="com.example" content-desc="" clickable="true" enabled="true" focused="true" scrollable="false" \
bounds="[100,200][980,320]" />\
<node index="1" text="" resource-id="" class="android.widget.ImageButton" package="com.example" \
content-desc="Search" clickable="true" enabled="true" focused="false" scrollable="false" bounds="[900,40][1040,180]" />\
<node index="2" text="" resource-id="" class="android.view.View" package="com.example" content-desc="" \
clickable="false" enabled="true" focused="false" scrollable="false" bounds="[0,400][1080,500]" />\
</node></hierarchy>"""


def device_path(path: str) -> Path:
    return common.root() / "device" / path.lstrip("/")


def packages() -> dict[str, bool]:
    """Installed packages and whether each is a system package."""
    try:
        return json.loads((common.root() / "packages.json").read_text())
    except FileNotFoundError:
        return {"com.android.settings": True}


def save_packages(value: dict[str, bool]) -> None:
    (common.root() / "packages.json").write_text(json.dumps(value))


def png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(out, "PNG")
    return out.getvalue()


def file_type(path: Path) -> str:
    if path.is_symlink():
        return "symbolic link"
    if path.is_dir():
        return "directory"
    return "regular file" if path.stat().st_size else "regular empty file"


def stat_line(path: Path, shown: str) -> str:
    st = path.lstat()
    return f"{file_type(path)}|{st.st_size}|{int(st.st_mtime)}|{shown}"


def shell(command: str) -> int:
    out = sys.stdout.buffer
    if command == "getprop sys.boot_completed":
        print("1" if (common.root() / "booted").exists() else "")
        return 0
    if m := re.fullmatch(r"rm -f (\S+)", command):
        device_path(m.group(1)).unlink(missing_ok=True)
        return 0
    if m := re.fullmatch(r"uiautomator dump (\S+)", command):
        counter = common.root() / "ui-failures"
        failures = int(counter.read_text()) if counter.exists() else common.config()["ui_failures"]
        if failures > 0:
            counter.write_text(str(failures - 1))
            print("ERROR: null root node returned by UiTestAutomationBridge.", file=sys.stderr)
            return 1
        target = device_path(m.group(1))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(UI_XML)
        print(f"UI hierchary dumped to: {m.group(1)}")
        return 0
    if m := re.fullmatch(r"cat (\S+)", command):
        try:
            out.write(device_path(m.group(1)).read_bytes())
        except FileNotFoundError:
            print(f"cat: {m.group(1)}: No such file or directory", file=sys.stderr)
            return 1
        return 0
    if command == "screencap -p":
        out.write(png())
        return 0
    if m := re.fullmatch(r"pm list packages( -3)?", command):
        for name, system in sorted(packages().items()):
            if not (m.group(1) and system):
                print(f"package:{name}")
        return 0
    if m := re.fullmatch(r"pm path (\S+)", command):
        if m.group(1) in packages():
            print(f"package:/data/app/{m.group(1)}/base.apk")
        return 0
    if m := re.fullmatch(r"cmd package resolve-activity --brief .* (\S+)", command):
        print("priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false")
        print(f"{m.group(1)}/.MainActivity" if m.group(1) in packages() else "No activity found")
        return 0
    if m := re.fullmatch(r"am start -n (\S+)", command):
        print(f"Starting: Intent {{ cmp={m.group(1)} }}")
        return 0
    if command.startswith("svc ") and common.config()["setup_hangs"]:
        time.sleep(30)
    if re.fullmatch(r"am force-stop \S+", command) or re.fullmatch(r"(svc|wm|settings) .*", command):
        return 0
    if m := re.fullmatch(r"pm uninstall (-k )?(\S+)", command):
        installed = packages()
        if installed.pop(m.group(2), None) is None:
            print("Failure [DELETE_FAILED_INTERNAL_ERROR]")
            return 1
        save_packages(installed)
        print("Success")
        return 0
    if command == "sync":
        return 0
    if re.fullmatch(r"input keyevent \S+", command):
        # Android ignores names it does not know: no output, status 0.
        return 0
    if command.startswith("stat -L -c %F "):
        path = device_path(shlex.split(command)[-1])
        if not path.exists():
            print(f"stat: '{shlex.split(command)[-1]}': No such file or directory", file=sys.stderr)
            return 1
        print(file_type(path.resolve()))
        return 0
    if command.startswith("if [ -d "):
        # The backend's listing command: list a directory, or stat a file.
        target = shlex.split(command)[3]
        path = device_path(target)
        if not path.exists():
            print(f"stat: '{target}': No such file or directory", file=sys.stderr)
            return 1
        if path.is_dir():
            for child in sorted(path.iterdir()):
                print(stat_line(child, target.rstrip("/") + "//" + child.name))
        else:
            print(stat_line(path, target))
        return 0
    # Anything else runs in a real shell, for exit codes and stderr.
    return subprocess.run(["sh", "-c", command], cwd=common.root() / "device").returncode


def main() -> int:
    argv = sys.argv[1:]
    common.record("adb", {"argv": argv})
    if argv[:1] == ["-s"]:
        argv = argv[2:]
    command, args = argv[0], argv[1:]
    if command == "start-server":
        home = Path(os.environ["ANDROID_USER_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        if not (home / "adbkey").exists():
            (home / "adbkey").write_text("fake private key\n")
            (home / "adbkey.pub").write_text("fake public key\n")
        return 0
    if command == "kill-server":
        return 0
    pid = common.emulator_pid()
    if pid is None:
        print("adb: device 'emulator-5554' not found", file=sys.stderr)
        return 1
    if command == "get-state":
        limit = common.config()["unauthorized_for"]
        started = (common.root() / "emulator.pid").stat().st_mtime
        if limit is not None and (limit < 0 or time.time() - started < limit):
            print("error: device unauthorized.", file=sys.stderr)
            return 1
        print("device")
        return 0
    if command == "emu" and args == ["kill"]:
        os.kill(pid, signal.SIGUSR1)
        print("OK: killing emulator, bye bye\nOK")
        return 0
    if command in ("shell", "exec-out"):
        return shell(" ".join(args))
    if command == "install":
        apk = Path(args[-1])
        data = apk.read_bytes()
        if not data.startswith(b"PK"):
            print("adb: failed to install: Failure [INSTALL_PARSE_FAILED_NOT_APK]")
            return 1
        name = data[2:].decode(errors="replace").strip() or "com.example.app"
        installed = packages()
        if name in installed and "-r" not in args:
            print("adb: failed to install: Failure [INSTALL_FAILED_ALREADY_EXISTS]")
            return 1
        installed[name] = False
        save_packages(installed)
        common.record("installs", {"flags": args[:-1], "package": name})
        print("Performing Streamed Install\nSuccess")
        return 0
    if command == "push":
        target = device_path(args[1])
        if not target.parent.is_dir():
            print(f"adb: error: failed to copy '{args[0]}' to '{args[1]}': remote No such file or directory", file=sys.stderr)
            return 1
        shutil.copyfile(args[0], target)
        print(f"{args[0]}: 1 file pushed, 0 skipped.")
        return 0
    if command == "pull":
        source = device_path(args[0])
        if not source.is_file():
            print(f"adb: error: failed to stat remote object '{args[0]}': No such file or directory", file=sys.stderr)
            return 1
        shutil.copyfile(source, args[1])
        print(f"{args[0]}: 1 file pulled, 0 skipped.")
        return 0
    print(f"fake adb: unsupported command {argv}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
