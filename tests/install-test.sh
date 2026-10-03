#!/usr/bin/env bash
# Tests for install.sh: a staged installation with a custom
# configuration and the default configuration.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

grep_file() {
  local pattern=$1 file=$2
  grep -qE -- "$pattern" "$file"
}

# A custom configuration drives the staged files.
mkdir -p "$root/etc/android-appliance"
cat >"$root/etc/android-appliance/appliance.conf" <<'EOF'
APPLIANCE_STATE_DIR=/var/appliance-test
APPLIANCE_USER=tester
APPLIANCE_GROUP=Testers
APPLIANCE_GPU=host
APPLIANCE_DISPLAY_PORT=6091
EOF
bash "$here/../install.sh" --root "$root" >/dev/null

libexec=$root/usr/local/libexec/android-appliance
for file in androidctl avd-init appliance-env emulator-launch display-scrcpy display-idle; do
  [ -x "$libexec/$file" ] || {
    echo "FAIL missing $libexec/$file" >&2
    exit 1
  }
done
echo "ok   staged scripts"
[ "$(readlink "$root/usr/local/bin/androidctl")" = "/usr/local/libexec/android-appliance/androidctl" ] || {
  echo "FAIL androidctl link is wrong" >&2
  exit 1
}
echo "ok   androidctl link"

units=$root/etc/systemd/system
for unit in \
  android-appliance-emulator.service \
  android-appliance-idle.service \
  android-appliance-idle.timer \
  android-appliance-xvnc.service \
  android-appliance-scrcpy.service \
  android-appliance-display.service \
  android-appliance-display.socket; do
  [ -f "$units/$unit" ] || {
    echo "FAIL missing unit $unit" >&2
    exit 1
  }
done
echo "ok   staged units"

dropin=$units/android-appliance-emulator.service.d/50-appliance.conf
grep_file '^User=tester$' "$dropin"
grep_file '^Group=Testers$' "$dropin"
grep_file '^ReadWritePaths=/var/appliance-test /run/android-appliance /tmp$' "$dropin"
grep_file '^WorkingDirectory=/var/appliance-test$' "$dropin"
grep_file '^RequiresMountsFor=/var/appliance-test$' "$dropin"
echo "ok   emulator drop-in"

gpu_dropin=$units/android-appliance-emulator.service.d/51-gpu-host.conf
grep_file '^Requires=android-appliance-xvnc\.service$' "$gpu_dropin"
grep_file '^After=android-appliance-xvnc\.service$' "$gpu_dropin"
grep_file '^SupplementaryGroups=kvm render video$' "$gpu_dropin"
grep_file '^BindPaths=/tmp/\.X11-unix$' "$gpu_dropin"
echo "ok   host gpu drop-in"

grep_file '^ListenStream=127\.0\.0\.1:6091$' \
  "$units/android-appliance-display.socket.d/50-appliance.conf"
echo "ok   display socket drop-in"

for unit in android-appliance-idle.service android-appliance-xvnc.service \
  android-appliance-scrcpy.service android-appliance-display.service; do
  grep_file '^User=tester$' "$units/$unit.d/50-appliance.conf" || {
    echo "FAIL drop-in missing for $unit" >&2
    exit 1
  }
done
echo "ok   service drop-ins"

tmpfiles=$root/etc/tmpfiles.d/android-appliance.conf
grep_file '^d /var/appliance-test 0750 tester Testers -$' "$tmpfiles"
grep_file '^d /run/android-appliance 0750 tester Testers -$' "$tmpfiles"
echo "ok   tmpfiles"

polkit=$root/etc/polkit-1/rules.d/49-android-appliance.rules
grep_file 'subject.user == "tester"' "$polkit"
grep -q 'action.lookup("unit") == "android-appliance-emulator.service"' "$polkit"
echo "ok   polkit rule"

# A second run with the same configuration is idempotent. It also
# removes the boot link of the idle timer that an earlier install.sh
# made.
mkdir -p "$units/timers.target.wants"
ln -s "$units/android-appliance-idle.timer" "$units/timers.target.wants/android-appliance-idle.timer"
bash "$here/../install.sh" --root "$root" >/dev/null
grep_file '^User=tester$' "$dropin"
echo "ok   idempotent re-run"
if [ -L "$units/timers.target.wants/android-appliance-idle.timer" ]; then
  echo "FAIL the idle timer is still enabled at boot" >&2
  exit 1
fi
echo "ok   idle timer not enabled at boot"

# --uninstall removes the installed files. It keeps a local drop-in,
# the configuration and the state directory.
mkdir -p "$root/var/appliance-test/avd"
echo userdata >"$root/var/appliance-test/avd/userdata.img"
echo "[Service]" >"$units/android-appliance-emulator.service.d/60-local.conf"
mkdir -p "$units/sockets.target.wants"
ln -s "$units/android-appliance-display.socket" "$units/sockets.target.wants/android-appliance-display.socket"
bash "$here/../install.sh" --root "$root" --uninstall >/dev/null 2>&1
for path in \
  usr/local/libexec/android-appliance usr/local/bin/androidctl usr/local/bin/android-avd-init \
  etc/tmpfiles.d/android-appliance.conf etc/polkit-1/rules.d/49-android-appliance.rules \
  etc/systemd/system/sockets.target.wants/android-appliance-display.socket \
  etc/systemd/system/android-appliance-xvnc.service.d \
  etc/systemd/system/android-appliance-emulator.service.d/50-appliance.conf \
  etc/systemd/system/android-appliance-emulator.service.d/51-gpu-host.conf; do
  if [ -e "$root/$path" ] || [ -L "$root/$path" ]; then
    echo "FAIL --uninstall left $path" >&2
    exit 1
  fi
done
for unit in "$units"/android-appliance-*; do
  case $unit in
    */android-appliance-emulator.service.d) ;;
    *)
      echo "FAIL --uninstall left $unit" >&2
      exit 1
      ;;
  esac
done
echo "ok   uninstall removes the installed files"
[ -f "$units/android-appliance-emulator.service.d/60-local.conf" ] || {
  echo "FAIL --uninstall removed a local drop-in" >&2
  exit 1
}
if [ ! -f "$root/var/appliance-test/avd/userdata.img" ] || [ ! -f "$root/etc/android-appliance/appliance.conf" ]; then
  echo "FAIL --uninstall removed the state directory or the configuration" >&2
  exit 1
fi
echo "ok   uninstall keeps local drop-ins, the configuration and the state"

# --purge works only with --uninstall.
if bash "$here/../install.sh" --root "$root" --purge >/dev/null 2>&1; then
  echo "FAIL --purge without --uninstall must fail" >&2
  exit 1
fi

# --uninstall --purge also deletes the state directory and the
# configuration.
bash "$here/../install.sh" --root "$root" --uninstall --purge >/dev/null 2>&1
if [ -e "$root/var/appliance-test" ] || [ -e "$root/etc/android-appliance" ]; then
  echo "FAIL --purge kept the state directory or the configuration" >&2
  exit 1
fi
echo "ok   uninstall --purge deletes the state and the configuration"

# --purge refuses a state directory that is not a dedicated directory.
mkdir -p "$root/etc/android-appliance" "$root/var/lib/other"
echo "APPLIANCE_STATE_DIR=/var/lib/" >"$root/etc/android-appliance/appliance.conf"
if bash "$here/../install.sh" --root "$root" --uninstall --purge >/dev/null 2>&1; then
  echo "FAIL --purge must refuse APPLIANCE_STATE_DIR=/var/lib/" >&2
  exit 1
fi
[ -d "$root/var/lib/other" ] || {
  echo "FAIL --purge deleted /var/lib" >&2
  exit 1
}
echo "APPLIANCE_STATE_DIR=/home/tester" >"$root/etc/android-appliance/appliance.conf"
mkdir -p "$root/home/tester"
if bash "$here/../install.sh" --root "$root" --uninstall --purge >/dev/null 2>&1 || [ ! -d "$root/home/tester" ]; then
  echo "FAIL --purge must refuse a home directory" >&2
  exit 1
fi
echo "ok   uninstall --purge refuses a shared directory"

# Without a configuration the installer stages the example file and its
# defaults: software GPU, so no host gpu drop-in.
root2=$(mktemp -d)
bash "$here/../install.sh" --root "$root2" >/dev/null
[ -f "$root2/etc/android-appliance/appliance.conf" ] || {
  echo "FAIL the example configuration was not staged" >&2
  exit 1
}
[ ! -e "$root2/etc/systemd/system/android-appliance-emulator.service.d/51-gpu-host.conf" ] || {
  echo "FAIL host gpu drop-in must not exist for swiftshader" >&2
  exit 1
}
grep_file '^ListenStream=127\.0\.0\.1:6090$' \
  "$root2/etc/systemd/system/android-appliance-display.socket.d/50-appliance.conf"
echo "ok   default staging"
rm -rf "$root2"

# A wrong SDK mode stops the installer before it changes a file.
root3=$(mktemp -d)
mkdir -p "$root3/etc/android-appliance"
echo "APPLIANCE_SDK_MANAGED=maybe" >"$root3/etc/android-appliance/appliance.conf"
if bash "$here/../install.sh" --root "$root3" >/dev/null 2>&1; then
  echo "FAIL APPLIANCE_SDK_MANAGED=maybe must stop the installer" >&2
  exit 1
fi
[ ! -e "$root3/usr/local/libexec" ] || {
  echo "FAIL the installer changed files after a configuration error" >&2
  exit 1
}
rm -rf "$root3"
echo "ok   wrong SDK mode stops"

echo "install tests passed"
