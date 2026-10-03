#!/usr/bin/env bash
# install.sh: install the Android appliance on a systemd host.
#
# Run as root: ./install.sh
# Staging mode for tests: ./install.sh --root DIR
#
# The configuration lives in /etc/android-appliance/appliance.conf. The
# installer creates it from conf/appliance.conf.example when it is
# missing, then applies it. It also installs the pinned Android SDK
# with scripts/provision-sdk.sh. Edit the file and run the installer
# again to apply a change; it is idempotent.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)

root=
case ${1:-} in
  --root) root=${2:?--root needs a directory} ;;
  "") ;;
  *)
    echo "install.sh: unknown argument $1" >&2
    exit 2
    ;;
esac

config_file=$root/etc/android-appliance/appliance.conf
state_home=/usr/local
libexec=$state_home/libexec/android-appliance
bindir=$state_home/bin
unit_dir=/etc/systemd/system
run_dir=/run/android-appliance

die() {
  echo "install.sh: $*" >&2
  exit 1
}

if [ -z "$root" ] && [ "$(id -u)" != 0 ]; then
  die "run this script as root (or use --root DIR for staging)"
fi

# Read the configuration into APPLIANCE_* variables.
if [ ! -f "$config_file" ]; then
  install -Dm644 "$here/conf/appliance.conf.example" "$config_file"
  echo "install.sh: created $config_file from the example; edit it and re-run to customize"
fi
export ANDROID_APPLIANCE_CONFIG=$config_file
# shellcheck source=src/appliance-env
source "$here/src/appliance-env"

state_dir=${APPLIANCE_STATE_DIR:-/var/lib/android-appliance}
user=${APPLIANCE_USER:-android-appliance}
group=${APPLIANCE_GROUP:-android-appliance}
gpu=${APPLIANCE_GPU:-swiftshader}
display_port=${APPLIANCE_DISPLAY_PORT:-6090}
android_home=${APPLIANCE_ANDROID_HOME:-/opt/android-sdk}

case ${APPLIANCE_SDK_MANAGED:-1} in
  1 | true | yes) sdk_managed=1 ;;
  0 | false | no) sdk_managed=0 ;;
  *) die "APPLIANCE_SDK_MANAGED must be 1 or 0" ;;
esac

case ${APPLIANCE_DISPLAY_ENABLED:-1} in
  1 | true | yes) display_enabled=1 ;;
  0 | false | no) display_enabled=0 ;;
  *) die "APPLIANCE_DISPLAY_ENABLED must be 1 or 0" ;;
esac
case $gpu in
  swiftshader | host | software | lavapipe | swangle | auto) ;;
  *) die "APPLIANCE_GPU '$gpu' is not a known -gpu mode" ;;
esac

# Stage the scripts and their links.
install -Dm755 "$here/src/androidctl" "$root$libexec/androidctl"
install -Dm755 "$here/src/avd-init" "$root$libexec/avd-init"
install -Dm755 "$here/src/appliance-env" "$root$libexec/appliance-env"
install -Dm755 "$here/src/emulator-launch" "$root$libexec/emulator-launch"
install -Dm755 "$here/src/display-scrcpy" "$root$libexec/display-scrcpy"
install -Dm755 "$here/src/display-idle" "$root$libexec/display-idle"
install -d "$root$bindir"
ln -sfn "$libexec/androidctl" "$root$bindir/androidctl"
ln -sfn "$libexec/avd-init" "$root$bindir/android-avd-init"

# Install the unit files.
for unit in \
  android-appliance-emulator.service \
  android-appliance-idle.service \
  android-appliance-idle.timer \
  android-appliance-xvnc.service \
  android-appliance-scrcpy.service \
  android-appliance-display.service \
  android-appliance-display.socket; do
  install -Dm644 "$here/systemd/$unit" "$root$unit_dir/$unit"
done

# Deployment drop-in: the user, the group and the state paths.
service_dropin() {
  local unit=$1 service_extra=${2:-} unit_extra=${3:-}
  mkdir -p "$root$unit_dir/$unit.d"
  {
    echo "# Managed by install.sh. Edit appliance.conf and re-run install.sh."
    if [ -n "$unit_extra" ]; then
      printf '[Unit]\n%s\n' "$unit_extra"
    fi
    printf '[Service]\nUser=%s\nGroup=%s\nReadWritePaths=%s %s /tmp\n' "$user" "$group" "$state_dir" "$run_dir"
    if [ -n "$service_extra" ]; then
      printf '%s\n' "$service_extra"
    fi
  } >"$root$unit_dir/$unit.d/50-appliance.conf"
}
service_dropin android-appliance-emulator.service "WorkingDirectory=$state_dir" "RequiresMountsFor=$state_dir"
service_dropin android-appliance-idle.service
service_dropin android-appliance-xvnc.service
service_dropin android-appliance-scrcpy.service
service_dropin android-appliance-display.service

# The socket listens on the configured loopback port. The base unit has
# no ListenStream, so this is the only listener.
mkdir -p "$root$unit_dir/android-appliance-display.socket.d"
cat >"$root$unit_dir/android-appliance-display.socket.d/50-appliance.conf" <<EOF
# Managed by install.sh. Edit appliance.conf and re-run install.sh.
[Socket]
ListenStream=127.0.0.1:$display_port
EOF

# Host GPU rendering: the X server, the extra groups and the socket bind.
emu_dropin=$root$unit_dir/android-appliance-emulator.service.d
if [ "$gpu" = host ]; then
  cat >"$emu_dropin/51-gpu-host.conf" <<EOF
# Managed by install.sh. Edit appliance.conf and re-run install.sh.
[Unit]
Requires=android-appliance-xvnc.service
After=android-appliance-xvnc.service
[Service]
SupplementaryGroups=kvm render video
BindPaths=/tmp/.X11-unix
EOF
else
  rm -f "$emu_dropin/51-gpu-host.conf"
fi

# The emulator unit wants the idle timer, so the timer is never enabled.
# An earlier install.sh enabled it; remove that link. Because of BindsTo=,
# the link made timers.target start Android at every boot.
rm -f "$root$unit_dir/timers.target.wants/android-appliance-idle.timer"

# The state directories and their ownership.
mkdir -p "$root/etc/tmpfiles.d"
cat >"$root/etc/tmpfiles.d/android-appliance.conf" <<EOF
# Managed by install.sh. Edit appliance.conf and re-run install.sh.
d $state_dir 0750 $user $group -
d $state_dir/home 0750 $user $group -
d $state_dir/screenshots 0750 $user $group -
d $run_dir 0750 $user $group -
EOF

# Only the appliance user may start, stop or restart the emulator unit.
mkdir -p "$root/etc/polkit-1/rules.d"
cat >"$root/etc/polkit-1/rules.d/49-android-appliance.rules" <<EOF
// Managed by install.sh. Edit appliance.conf and re-run install.sh.
polkit.addRule(function (action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      subject.user == "$user" &&
      action.lookup("unit") == "android-appliance-emulator.service" &&
      ["start", "stop", "restart"].indexOf(action.lookup("verb")) >= 0) {
    return polkit.Result.YES;
  }
});
EOF

if [ -n "$root" ]; then
  echo "install.sh: staged the appliance under $root; the SDK is not provisioned"
  exit 0
fi

# The appliance user and group.
getent group "$group" >/dev/null || groupadd --system "$group"
if ! id -u "$user" >/dev/null 2>&1; then
  useradd --system --gid "$group" --home-dir "$state_dir" --no-create-home --shell /usr/sbin/nologin "$user"
  echo "install.sh: created the system user $user"
fi

# Check the operating-system packages of the enabled parts.
missing=()
missing_scrcpy=0
need_binary() {
  command -v "$1" >/dev/null 2>&1 || missing+=("$2")
}
need_file() {
  [ -e "$1" ] || missing+=("$2")
}
if [ "$display_enabled" = 1 ] || [ "$gpu" = host ]; then
  need_binary Xvnc tigervnc-x11-server
  need_binary xdpyinfo xdpyinfo
fi
if [ "$display_enabled" = 1 ]; then
  # Fedora 44 ships no scrcpy; the zeno/scrcpy COPR provides it.
  command -v scrcpy >/dev/null 2>&1 || missing_scrcpy=1
  need_binary websockify python3-websockify
  need_file /usr/share/novnc/vnc.html novnc
  need_binary python3 python3
  ldconfig -p 2>/dev/null | grep -q libXss.so || missing+=(libXScrnSaver)
fi
if [ "$sdk_managed" = 1 ]; then
  need_binary curl curl
  need_binary unzip unzip
fi
if [ "${#missing[@]}" -gt 0 ]; then
  die "missing packages: ${missing[*]}; run: dnf install ${missing[*]}"
fi
if [ "$missing_scrcpy" = 1 ]; then
  die "scrcpy is missing; run: dnf install dnf-plugins-core && dnf copr enable zeno/scrcpy && dnf install scrcpy"
fi

[ -c /dev/kvm ] || echo "install.sh: WARNING /dev/kvm is missing; the emulator needs KVM" >&2

# The Android SDK. provision-sdk.sh installs the pinned packages; it
# downloads nothing when they are in place. Both modes check the SDK,
# including the host libraries of the emulator.
if [ "$sdk_managed" = 1 ]; then
  "$here/scripts/provision-sdk.sh" || die "the SDK installation failed; correct the problem and re-run install.sh"
elif [ -x "$android_home/emulator/emulator" ]; then
  "$here/scripts/provision-sdk.sh" --check || die "the SDK check failed; correct the problem and re-run install.sh"
else
  echo "install.sh: WARNING the SDK is not installed at $android_home, and APPLIANCE_SDK_MANAGED=0" >&2
fi

# Give the installed files their SELinux labels.
if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled && command -v restorecon >/dev/null 2>&1; then
  restorecon -R "$libexec" "$bindir/androidctl" "$bindir/android-avd-init" \
    "$unit_dir"/android-appliance-* /etc/tmpfiles.d/android-appliance.conf \
    /etc/polkit-1/rules.d/49-android-appliance.rules /etc/android-appliance
fi

# Apply the unit set and the state directories.
systemd-tmpfiles --create "$root/etc/tmpfiles.d/android-appliance.conf"
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  if [ "$display_enabled" = 1 ]; then
    systemctl enable --now android-appliance-display.socket
  else
    systemctl disable --now android-appliance-display.socket 2>/dev/null || true
  fi
else
  echo "install.sh: systemd is not running; skipped daemon-reload and unit enablement" >&2
fi

echo
echo "install.sh: installed the Android appliance"
echo "install.sh:   androidctl:    $bindir/androidctl"
echo "install.sh:   configuration: $config_file"
echo "install.sh:   state:         $state_dir (user $user, group $group)"
echo "install.sh:   gpu:           $gpu, display: $display_enabled (port $display_port)"
echo "install.sh: start it with: androidctl start"
