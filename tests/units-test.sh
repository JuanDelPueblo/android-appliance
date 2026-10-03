#!/usr/bin/env bash
# Tests for the static systemd units. They check the shipped unit files
# and the launchers, so they need no installed appliance. When the
# appliance is installed and systemd runs, systemd-analyze verifies the
# unit set as well.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
units=$here/../systemd
src=$here/../src
failures=0

check() {
  local name=$1 pattern=$2 file=$3
  if grep -qE -- "$pattern" "$file"; then
    echo "ok   $name"
  else
    echo "FAIL $name: missing '$pattern' in $file" >&2
    failures=$((failures + 1))
  fi
}

check_absent() {
  local name=$1 pattern=$2 file=$3
  if grep -qE -- "$pattern" "$file"; then
    echo "FAIL $name: '$pattern' must not be in $file" >&2
    failures=$((failures + 1))
  else
    echo "ok   $name"
  fi
}

emu=$units/android-appliance-emulator.service
idle=$units/android-appliance-idle.service
timer=$units/android-appliance-idle.timer
xvnc=$units/android-appliance-xvnc.service
scrcpy=$units/android-appliance-scrcpy.service
socket=$units/android-appliance-display.socket
display=$units/android-appliance-display.service

# The emulator unit delegates to the launchers and the hooks.
check "emulator ExecStart" '^ExecStart=/usr/local/libexec/android-appliance/emulator-launch$' "$emu"
check "emulator ExecStartPre" '^ExecStartPre=/usr/local/libexec/android-appliance/avd-init$' "$emu"
check "emulator boot hook" '^ExecStartPost=/usr/local/bin/androidctl boot-hook$' "$emu"
check "emulator stop hook" '^ExecStop=/usr/local/bin/androidctl stop-hook$' "$emu"
check "emulator wants the idle timer" '^Wants=android-appliance-idle\.timer$' "$emu"
check "emulator after network" '^After=network\.target$' "$emu"
check "emulator hardening" '^ProtectSystem=strict$' "$emu"
check "emulator private home" '^ProtectHome=true$' "$emu"
check "emulator private tmp" '^PrivateTmp=true$' "$emu"
check "emulator kvm group" '^SupplementaryGroups=kvm$' "$emu"
check "emulator timeouts" '^TimeoutStartSec=20min$' "$emu"
check "emulator no restart" '^Restart=no$' "$emu"
# The emulator is never part of a boot target; deployment values come
# from the installer drop-in, not from the base unit.
check_absent "emulator no boot target" '^WantedBy=' "$emu"
check_absent "emulator no User in base" '^User=' "$emu"
check_absent "emulator no display dependency in base" 'xvnc' "$emu"

# The launcher keeps every start a cold, anonymous-boot start.
check "launcher cold boot" '-no-snapshot' "$src/emulator-launch"
check "launcher anonymous ram" 'features=-QuickbootFileBacked' "$src/emulator-launch"
check "launcher vulkan off for host" 'features=-QuickbootFileBacked,-Vulkan' "$src/emulator-launch"
check "launcher console port" '-port 5554' "$src/emulator-launch"
check "launcher headless" '-no-window' "$src/emulator-launch"
check "launcher metrics off" '-no-metrics' "$src/emulator-launch"
# shellcheck disable=SC2016  # the pattern matches a literal $ in the file
check "launcher gpu from config" '-gpu "\$gpu"' "$src/emulator-launch"

# The idle policy runs only while the emulator unit is active.
check "timer bound to emulator" '^BindsTo=android-appliance-emulator\.service$' "$timer"
check "timer cadence" '^OnUnitActiveSec=30s$' "$timer"
# The emulator wants the timer. An enabled timer would start the
# emulator at boot through BindsTo=, so the timer has no [Install].
check_absent "timer not enabled at boot" '^\[Install\]' "$timer"
check "idle runs androidctl" '^ExecStart=/usr/local/bin/androidctl idle-check$' "$idle"

# The virtual display serves the device native size on a local socket.
check "xvnc geometry" '1080x1920' "$xvnc"
check "xvnc unix socket" '-rfbunixpath /run/android-appliance/vnc.sock' "$xvnc"
check "xvnc socket mode" '-rfbunixmode 0600' "$xvnc"
check "xvnc no auth" '-SecurityTypes None' "$xvnc"
check "xvnc no tcp" '-nolisten tcp' "$xvnc"
check "xvnc waits" 'xdpyinfo' "$xvnc"
check "xvnc part of emulator" '^PartOf=android-appliance-emulator\.service$' "$xvnc"

# scrcpy mirrors at the native size into the appliance X display.
check "scrcpy bound to emulator" '^BindsTo=android-appliance-emulator\.service android-appliance-xvnc\.service$' "$scrcpy"
check "scrcpy restarts" '^Restart=always$' "$scrcpy"
check "scrcpy no start limit" '^StartLimitIntervalSec=0$' "$scrcpy"
check "scrcpy native width" '--window-width=1080' "$src/display-scrcpy"
check "scrcpy native height" '--window-height=1920' "$src/display-scrcpy"
check "scrcpy max size" '--max-size=1920' "$src/display-scrcpy"
check "scrcpy software render" '--render-driver=software' "$src/display-scrcpy"
# shellcheck disable=SC2016  # the pattern matches a literal $ in the file
check "scrcpy serial" '--serial="\$ANDROID_APPLIANCE_SERIAL"' "$src/display-scrcpy"

# The browser socket activates the noVNC chain and starts Android.
check "socket activation" '^StandardInput=socket$' "$display"
check "display queues start" '^ExecStartPre=/usr/local/bin/androidctl start --no-wait$' "$display"
check "display websockify" '^ExecStart=/usr/bin/websockify --inetd --web=/usr/share/novnc --unix-target=/run/android-appliance/vnc.sock$' "$display"
check "display needs xvnc" '^Requires=android-appliance-xvnc\.service$' "$display"
check "display wants scrcpy" '^Wants=android-appliance-scrcpy\.service$' "$display"
check "display part of emulator" '^PartOf=android-appliance-emulator\.service$' "$display"
check "socket in sockets target" '^WantedBy=sockets\.target$' "$socket"

# Only the display socket is enabled at boot.
for unit in "$emu" "$idle" "$timer" "$xvnc" "$scrcpy" "$display"; do
  check_absent "no boot target: $(basename "$unit")" '^WantedBy=' "$unit"
done

# When the standalone appliance is installed, verify the real unit set
# with systemd-analyze. On a NixOS machine the units there belong to the
# NixOS module, so they are not this project's unit set.
installed=/etc/systemd/system/android-appliance-emulator.service
if command -v systemd-analyze >/dev/null 2>&1 &&
  [ -f "$installed" ] &&
  grep -q '^ExecStart=/usr/local/libexec/android-appliance/emulator-launch$' "$installed"; then
  if systemd-analyze verify \
    /etc/systemd/system/android-appliance-emulator.service \
    /etc/systemd/system/android-appliance-idle.service \
    /etc/systemd/system/android-appliance-idle.timer \
    /etc/systemd/system/android-appliance-xvnc.service \
    /etc/systemd/system/android-appliance-scrcpy.service \
    /etc/systemd/system/android-appliance-display.service \
    /etc/systemd/system/android-appliance-display.socket; then
    echo "ok   systemd-analyze verify"
  else
    echo "FAIL systemd-analyze verify" >&2
    failures=$((failures + 1))
  fi
else
  echo "skip systemd-analyze verify (the standalone appliance is not installed)"
fi

if [ "$failures" -gt 0 ]; then
  echo "$failures unit check(s) failed"
  exit 1
fi
echo "all unit checks passed"
