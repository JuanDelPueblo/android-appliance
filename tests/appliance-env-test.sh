#!/usr/bin/env bash
# Tests for the appliance-env configuration loader: file parsing,
# derivation and precedence.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
helper=$here/../src/appliance-env
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Probe the helper in a clean environment and print the derived values.
# The probe script only uses bash builtins, so the PATH never matters.
probe() {
  local config=$1
  shift
  # shellcheck disable=SC2016  # the probe keeps its own quoting on purpose
  env -i PATH="$PATH" HOME=/probe-home ANDROID_APPLIANCE_CONFIG="$config" "$@" \
    bash -c '
      source "$1"
      printf "%s\n" "$ANDROID_APPLIANCE_STATE_DIR" "$ANDROID_APPLIANCE_USER" \
        "$AVD_SYSTEM_IMAGE" "$ANDROID_APPLIANCE_IDLE_STOP" \
        "$ANDROID_APPLIANCE_DISPLAY_URL" "$ANDROID_APPLIANCE_X_DISPLAY" \
        "$ANDROID_APPLIANCE_ADB" "$HOME"
    ' probe "$helper"
}

assert_same() {
  local name=$1 want=$2 got=$3
  if [ "$want" = "$got" ]; then
    echo "ok   $name"
  else
    echo "FAIL $name" >&2
    echo "want:" >&2
    printf '%s\n' "$want" | sed 's/^/  /' >&2
    echo "got:" >&2
    printf '%s\n' "$got" | sed 's/^/  /' >&2
    exit 1
  fi
}

cat >"$tmp/appliance.conf" <<'EOF'
# A comment line that must be ignored.
APPLIANCE_STATE_DIR=/tmp/state
APPLIANCE_USER=tester
APPLIANCE_API_LEVEL=35
APPLIANCE_IDLE_STOP_MINUTES=5
APPLIANCE_DISPLAY_ENABLED=1
APPLIANCE_DISPLAY_PORT=6091
APPLIANCE_ANDROID_HOME=/tmp/sdk
this line has no assignment
EOF

want='/tmp/state
tester
system-images/android-35/google_apis_playstore/x86_64
300
http://127.0.0.1:6091/vnc.html?autoconnect=true&resize=scale
:57
/tmp/sdk/platform-tools/adb
/probe-home'
assert_same "config file drives the derivation" "$want" "$(probe "$tmp/appliance.conf")"

# An exported variable wins over the configuration file.
got=$(
  probe "$tmp/appliance.conf" ANDROID_APPLIANCE_STATE_DIR=/override/state |
    sed -n 1p
)
assert_same "environment wins over the file" "/override/state" "$got"

# A disabled display leaves the display URL and the X display unset.
sed -i 's/^APPLIANCE_DISPLAY_ENABLED=1$/APPLIANCE_DISPLAY_ENABLED=0/' "$tmp/appliance.conf"
got=$(probe "$tmp/appliance.conf")
assert_same "display disabled" "" "$(printf '%s\n' "$got" | sed -n 5p)"
assert_same "x display disabled" "" "$(printf '%s\n' "$got" | sed -n 6p)"

# Without a configuration file the built-in defaults apply, and no
# display is configured.
want='/var/lib/android-appliance

system-images/android-36/google_apis_playstore/x86_64
3600


adb
/probe-home'
assert_same "defaults without a file" "$want" "$(probe /nonexistent-appliance-env-test)"

echo "appliance-env tests passed"
