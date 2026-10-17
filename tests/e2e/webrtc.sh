#!/usr/bin/env bash
# webrtc.sh: end-to-end test of the dashboard in a real browser against a
# real appliance. It needs KVM, rootless Podman with Quadlet, a systemd user
# session, a built image (./install.sh builds one), Node.js and Playwright
# with Chromium. It installs its own instance from that image with
# install.sh --no-build, runs tests/e2e/webrtc.mjs (which drives Chromium
# and checks every effect against Android through androidctl), and removes
# the instance again with --uninstall --purge. The image stays. It prints
# PASS or FAIL per step and stops at the first failure. A run takes about
# 20 minutes: the first boot creates a new AVD, Android boots six more times,
# and some steps wait: a hidden tab for its 60 s grace period, the soak for
# 2 minutes, the idle stop for 3 minutes.
#
#   tests/e2e/webrtc.sh
#
# Playwright: set AA_PLAYWRIGHT to a node_modules directory that has
# `playwright` 1.62.1 with its Chromium, or run `npm ci` in tests/e2e and
# `npx playwright install chromium`.
#
# Settings (environment):
#   E2E_IMAGE      image to test (default localhost/android-appliance:<VERSION>-api36)
#   E2E_NAME       instance name (default aa-e2e-rtc)
#   E2E_DIR        scratch directory: configuration, state, androidctl
#                  (default /var/tmp/<E2E_NAME>; keep it off a tmpfs)
#   E2E_PORT       HTTP port on 127.0.0.1 (default 47082)
#   E2E_TURN_PORT  TURN port on 127.0.0.1 (default 47083)
#   E2E_GPU        auto, host or lavapipe (default auto)
#   E2E_MEMORY_MIB guest RAM (default 3072)
#   E2E_SOAK_SECONDS  length of the soak step (default 120; 0 skips it)
#   E2E_STEPS      run only the steps whose names match this regular
#                  expression (for development, on a kept instance)
#   E2E_KEEP=1     keep the instance after the run, to look at it
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
name=${E2E_NAME:-aa-e2e-rtc}
dir=${E2E_DIR:-/var/tmp/$name}
port=${E2E_PORT:-47082}
turn_port=${E2E_TURN_PORT:-47083}
image=${E2E_IMAGE:-localhost/android-appliance:$(cat "$repo/VERSION")-api36}
gpu=${E2E_GPU:-auto}
memory=${E2E_MEMORY_MIB:-3072}
runtime=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
conf=$dir/appliance.conf
work=$dir/work
sock=$runtime/$name/api.sock

# Only this instance's configuration counts.
unset "${!APPLIANCE_@}" ANDROID_APPLIANCE_URL ANDROID_APPLIANCE_TOKEN ANDROID_APPLIANCE_SOCKET
export ANDROID_APPLIANCE_CONFIG=$conf XDG_RUNTIME_DIR=$runtime

die() {
  echo "webrtc: $*" >&2
  exit 1
}

[ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is missing or not usable"
command -v podman >/dev/null 2>&1 || die "podman is not installed"
command -v node >/dev/null 2>&1 || die "node is not installed"
systemctl --user show-environment >/dev/null 2>&1 || die "there is no systemd user session"
podman image exists "$image" || die "the image $image does not exist; build it with ./install.sh first, or set E2E_IMAGE"
api=$(podman image inspect --format '{{index .Labels "android-appliance.api-level"}}' "$image")
[[ $api =~ ^[0-9]+$ ]] || die "the image $image has no android-appliance.api-level label"
reason=$(node "$repo/tests/web/has-playwright.mjs") || die "$reason"

installer() { "$repo/install.sh" --config "$conf" "$@" >>"$work/install.log" 2>&1; }

cleanup() {
  local rc=$?
  # The purge deletes the logs of a failure; keep them with the work files.
  if [ "$rc" != 0 ] && [ -d "$dir/state/appliance/logs" ]; then
    cp -a "$dir/state/appliance/logs" "$work/" || true
    journalctl --user -u "$name.service" --since "$started_at" --no-pager -o cat >"$work/journal.txt" 2>&1 || true
  fi
  if [ "${E2E_KEEP:-0}" = 1 ]; then
    echo "webrtc: kept the instance $name (ANDROID_APPLIANCE_CONFIG=$conf)"
  elif [ -f "$conf" ]; then
    "$repo/install.sh" --config "$conf" --uninstall --purge >/dev/null 2>&1 || echo "webrtc: the clean-up of $name failed" >&2
  fi
  if [ "$rc" = 0 ]; then rm -rf "$work"; fi
  rmdir "$dir/bin" "$dir" 2>/dev/null || true
}

# A run that was interrupted leaves its instance behind.
if [ -f "$conf" ]; then
  "$repo/install.sh" --config "$conf" --uninstall --purge >/dev/null 2>&1 || die "cannot remove the old instance of $conf"
fi
mkdir -p "$dir" "$work"
started_at=$(date '+%Y-%m-%d %H:%M:%S')
trap cleanup EXIT

cat >"$conf" <<EOF
# The instance of tests/e2e/webrtc.sh.
APPLIANCE_NAME=$name
APPLIANCE_STATE_DIR=$dir/state
APPLIANCE_BIN_DIR=$dir/bin
APPLIANCE_QUADLET_DIR=$runtime/containers/systemd
APPLIANCE_LISTEN_ADDRESS=127.0.0.1
APPLIANCE_PORT=$port
APPLIANCE_TURN_PORT=$turn_port
APPLIANCE_IMAGE=$image
APPLIANCE_API_LEVEL=$api
APPLIANCE_GPU=$gpu
APPLIANCE_MEMORY_MIB=$memory
APPLIANCE_IDLE_STOP_MINUTES=60
EOF
chmod 0600 "$conf"

echo "webrtc: $name on 127.0.0.1:$port (TURN $turn_port) with $image (API $api, GPU $gpu)"
if ! installer --no-build; then
  tail -n 8 "$work/install.log" >&2
  die "install.sh failed"
fi
systemctl --user is-active --quiet "$name.service" || die "$name.service is not active"

export E2E_URL=http://127.0.0.1:$port E2E_NAME=$name E2E_CONFIG=$conf E2E_SOCKET=$sock E2E_WORK=$work E2E_TURN_PORT=$turn_port
export E2E_ANDROIDCTL=$dir/bin/androidctl E2E_INSTALL=$repo/install.sh E2E_SINCE=$started_at
node "$repo/tests/e2e/webrtc.mjs"
echo "webrtc: all steps passed"
