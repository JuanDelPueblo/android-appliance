#!/usr/bin/env bash
# Lifecycle tests for androidctl. They use fake systemctl, adb and
# xprintidle commands, so they need no KVM and no emulator.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
androidctl=${ANDROIDCTL:-$here/../src/androidctl}
failures=0

setup() {
  FAKE=$(mktemp -d)
  export FAKE
  mkdir -p "$FAKE/state/home"
  echo inactive >"$FAKE/unit"
  echo 0 >"$FAKE/boot"
  : >"$FAKE/calls"
  export PATH="$here/fakes:$PATH"
  export ANDROID_APPLIANCE_STATE_DIR=$FAKE/state
  export ANDROID_APPLIANCE_ADB=$here/fakes/adb
  export ANDROID_APPLIANCE_XPRINTIDLE=$here/fakes/xprintidle
  export ANDROID_APPLIANCE_X_DISPLAY=:99
  export ANDROID_APPLIANCE_IDLE_SUSPEND=600
  export ANDROID_APPLIANCE_IDLE_STOP=3600
  export ANDROID_APPLIANCE_BOOT_TIMEOUT=5
  unset ANDROID_APPLIANCE_USER
}

running() {
  echo active >"$FAKE/unit"
  echo 1 >"$FAKE/boot"
}

suspended() {
  running
  touch "$FAKE/paused" "$FAKE/state/suspended-at"
}

# Set the last androidctl activity to N seconds ago.
idle_for() { touch -d "@$(($(date +%s) - $1))" "$FAKE/state/last-activity"; }

ctl() { "$androidctl" "$@"; }

called() { grep -qxF -- "$1" "$FAKE/calls"; }

# `! cmd` does not trip errexit, so negate explicitly.
not() { if "$@"; then return 1; fi; }

state_is() { [[ "$(ctl status)" == "state=$1 "* ]]; }

check() {
  local name=$1
  shift
  setup
  local rc=0
  # errexit is ignored inside an `if` condition, so capture the status.
  set +e
  (
    set -e
    "$@"
  ) >"$FAKE/out" 2>&1
  rc=$?
  set -e
  if [ "$rc" = 0 ]; then
    echo "ok   $name"
  else
    echo "FAIL $name"
    sed 's/^/     /' "$FAKE/out" "$FAKE/calls"
    failures=$((failures + 1))
  fi
  rm -rf "$FAKE"
}

t_status_stopped() {
  [ "$(ctl status)" = "state=stopped boot_completed=0 idle_seconds=0" ]
  not grep -q '^systemctl start' "$FAKE/calls"
  not grep -q '^adb' "$FAKE/calls"
  [ ! -e "$FAKE/state/last-activity" ]
}

t_status_running() {
  running
  [[ "$(ctl status)" == "state=running boot_completed=1 "* ]]
}

t_status_starting() {
  echo activating >"$FAKE/unit"
  [[ "$(ctl status)" == "state=starting boot_completed=0 "* ]]
}

t_status_suspended_does_not_wake() {
  suspended
  idle_for 100
  state_is suspended
  not called "adb emu avd start"
  not called "adb shell getprop sys.boot_completed"
  # status does not refresh activity.
  [ "$(($(date +%s) - $(stat -c %Y "$FAKE/state/last-activity")))" -ge 99 ]
}

t_start_from_stopped() {
  ctl start
  grep -q '^systemctl start android-appliance-emulator.service$' "$FAKE/calls"
  [ -e "$FAKE/state/last-activity" ]
  state_is running
}

t_start_no_wait() {
  ctl start --no-wait
  called "systemctl start --no-block android-appliance-emulator.service"
}

t_start_resumes_suspended() {
  suspended
  ctl start
  called "adb emu avd start"
  not grep -q '^systemctl start' "$FAKE/calls"
  [ ! -e "$FAKE/state/suspended-at" ]
}

t_start_running_is_noop() {
  running
  ctl start
  not grep -q '^systemctl start' "$FAKE/calls"
  not called "adb emu avd start"
}

t_tap_starts_stopped() {
  ctl tap 10 20
  grep -q '^systemctl start' "$FAKE/calls"
  called "adb shell input tap 10 20"
}

t_tap_resumes_suspended() {
  suspended
  idle_for 900
  ctl tap 10 20
  called "adb emu avd start"
  called "adb shell input tap 10 20"
  # The interaction refreshed the activity time.
  [ "$(($(date +%s) - $(stat -c %Y "$FAKE/state/last-activity")))" -lt 5 ]
}

t_screenshot() {
  running
  [ "$(ctl screenshot "$FAKE/shot.png")" = "$FAKE/shot.png" ]
  [ "$(cat "$FAKE/shot.png")" = PNGDATA ]
}

t_ui_retries_after_null_root() {
  running
  [[ "$(ctl ui)" == *"<hierarchy"* ]]
  [ "$(grep -c 'uiautomator dump' "$FAKE/calls")" = 2 ]
}

t_text_is_quoted() {
  running
  ctl text "it's a test"
  called "adb shell input text 'it'\\''s%sa%stest'"
}

t_install_puts_apk_last() {
  running
  ctl install app.apk -r -g
  called "adb install -r -g app.apk"
}

t_suspend_running() {
  running
  ctl suspend
  called "adb emu avd stop"
  [ -e "$FAKE/state/suspended-at" ]
  state_is suspended
}

t_suspend_stopped_fails() {
  not ctl suspend
}

t_resume() {
  suspended
  ctl resume
  called "adb emu avd start"
  state_is running
}

t_stop() {
  running
  ctl stop
  called "systemctl stop android-appliance-emulator.service"
  state_is stopped
}

t_wait_stopped_fails() {
  not ctl wait
}

t_wait_running() {
  running
  ctl wait
}

t_stop_hook_resumes_then_kills() {
  suspended
  MAINPID='' ctl stop-hook
  [ "$(grep '^adb emu' "$FAKE/calls")" = "adb emu avd start
adb emu kill" ]
}

t_usage_error() {
  local rc=0
  ctl tap 1 >/dev/null 2>&1 || rc=$?
  [ "$rc" = 2 ]
}

t_idle_recent_does_nothing() {
  running
  idle_for 60
  ctl idle-check
  not called "adb emu avd stop"
  not grep -q '^systemctl stop' "$FAKE/calls"
}

t_idle_suspends() {
  running
  idle_for 700
  ctl idle-check
  called "adb emu avd stop"
  not grep -q '^systemctl stop' "$FAKE/calls"
}

t_idle_hibernates_suspended() {
  suspended
  idle_for 3700
  ctl idle-check
  called "systemctl stop --no-block android-appliance-emulator.service"
}

t_idle_hibernates_running() {
  running
  idle_for 3700
  ctl idle-check
  called "systemctl stop --no-block android-appliance-emulator.service"
}

t_idle_display_input_keeps_running() {
  running
  idle_for 700
  echo 3000 >"$FAKE/xidle"
  ctl idle-check
  not called "adb emu avd stop"
}

t_idle_display_input_resumes() {
  suspended
  idle_for 700
  touch -d "@$(($(date +%s) - 60))" "$FAKE/state/suspended-at"
  echo 5000 >"$FAKE/xidle"
  ctl idle-check
  called "adb emu avd start"
}

t_idle_manual_suspend_sticks() {
  suspended
  idle_for 30
  # The last display input came before the suspend.
  echo 120000 >"$FAKE/xidle"
  ctl idle-check
  not called "adb emu avd start"
}

t_idle_stopped_does_nothing() {
  idle_for 99999
  ctl idle-check
  [ "$(grep -c '^systemctl' "$FAKE/calls")" = 1 ]
}

for t in $(declare -F | awk '{print $3}' | grep '^t_'); do
  check "${t#t_}" "$t"
done

if [ "$failures" -gt 0 ]; then
  echo "$failures test(s) failed"
  exit 1
fi
echo "all tests passed"
