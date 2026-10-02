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

# A second run with the same configuration is idempotent.
bash "$here/../install.sh" --root "$root" >/dev/null
grep_file '^User=tester$' "$dropin"
echo "ok   idempotent re-run"

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

echo "install tests passed"
