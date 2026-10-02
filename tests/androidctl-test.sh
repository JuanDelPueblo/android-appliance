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
  export ANDROID_APPLIANCE_IDLE_STOP=3600
  export ANDROID_APPLIANCE_BOOT_TIMEOUT=5
  unset ANDROID_APPLIANCE_USER
  # Keep the tests independent of any host configuration file.
  export ANDROID_APPLIANCE_CONFIG=/nonexistent-android-appliance-test
}

running() {
  echo active >"$FAKE/unit"
  echo 1 >"$FAKE/boot"
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

t_start_from_stopped() {
  ctl start
  # Uses --no-block so Ctrl-C stops waiting, not the emulator start.
  called "systemctl start --no-block android-appliance-emulator.service"
  [ -e "$FAKE/state/last-activity" ]
  state_is running
}

t_start_no_wait() {
  ctl start --no-wait
  called "systemctl start --no-block android-appliance-emulator.service"
}

t_restart_waits_for_shutdown() {
  running
  ctl restart
  called "systemctl stop android-appliance-emulator.service"
  called "systemctl start --no-block android-appliance-emulator.service"
  state_is running
}

t_boot_hook_fails_when_unit_dies() {
  echo inactive >"$FAKE/unit"
  echo 0 >"$FAKE/boot"
  not ctl boot-hook
  grep -q 'stopped during boot' "$FAKE/out"
}

t_boot_hook_fails_on_unauthorized() {
  echo active >"$FAKE/unit"
  echo 0 >"$FAKE/boot"
  touch "$FAKE/unauthorized"
  not ctl boot-hook
  grep -q 'unauthorized' "$FAKE/out"
}

t_boot_timeout_reports_adb_state() {
  echo active >"$FAKE/unit"
  echo 0 >"$FAKE/boot"
  touch "$FAKE/offline"
  not ctl boot-hook
  grep -q 'adb:offline' "$FAKE/out"
}

t_stop_hook_bounded_when_pid_lives() {
  running
  sleep 30 &
  pid=$!
  ANDROID_APPLIANCE_STOP_HOOK_TIMEOUT=2 MAINPID=$pid ctl stop-hook
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

t_start_running_is_idempotent() {
  running
  ctl start
  state_is running
  not called "adb emu avd start"
}

t_tap_starts_stopped() {
  ctl tap 10 20
  called "systemctl start --no-block android-appliance-emulator.service"
  called "adb shell input tap 10 20"
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

t_ui_fails_when_dump_never_succeeds() {
  running
  touch "$FAKE/ui_broken"
  not ctl ui
  grep -q 'did not produce a hierarchy' "$FAKE/out"
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

t_idle_before_stop_keeps_running() {
  running
  idle_for 700
  ctl idle-check
  not called "adb emu avd stop"
  not grep -q '^systemctl stop' "$FAKE/calls"
}

t_idle_stops_running() {
  running
  idle_for 3700
  ctl idle-check
  called "systemctl stop --no-block android-appliance-emulator.service"
}

t_idle_display_input_keeps_running() {
  running
  idle_for 3700
  echo 3000 >"$FAKE/xidle"
  ctl idle-check
  not grep -q '^systemctl stop' "$FAKE/calls"
}

t_idle_stopped_does_nothing() {
  idle_for 99999
  ctl idle-check
  [ "$(grep -c '^systemctl' "$FAKE/calls")" = 1 ]
}

t_removed_commands_fail_without_adb() {
  local rc=0
  ctl suspend >/dev/null 2>&1 || rc=$?
  [ "$rc" = 2 ]
  rc=0
  ctl resume >/dev/null 2>&1 || rc=$?
  [ "$rc" = 2 ]
  [ ! -s "$FAKE/calls" ]
}

t_stop_hook_only_kills() {
  running
  MAINPID='' ctl stop-hook
  [ "$(grep '^adb emu' "$FAKE/calls")" = "adb emu kill" ]
}

t_start_no_wait_does_not_probe_adb() {
  running
  ctl start --no-wait
  not grep -q '^adb' "$FAKE/calls"
}

t_idle_does_not_interrupt_inflight_command() {
  running
  idle_for 3700
  exec 8>"$FAKE/state/activity.lock"
  flock -s 8
  ctl idle-check
  not grep -q '^systemctl stop' "$FAKE/calls"
  flock -u 8
  exec 8>&-
  ctl idle-check
  called "systemctl stop --no-block android-appliance-emulator.service"
}

t_boot_hook_refreshes_activity_after_long_boot() {
  running
  idle_for 3700
  ctl boot-hook
  [ "$(($(date +%s) - $(stat -c %Y "$FAKE/state/last-activity")))" -lt 5 ]
}

t_failed_screenshot_preserves_previous_file() {
  running
  printf 'previous' >"$FAKE/shot.png"
  touch "$FAKE/screenshot_broken"
  not ctl screenshot "$FAKE/shot.png"
  [ "$(cat "$FAKE/shot.png")" = previous ]
  [ -z "$(find "$FAKE" -name '.screenshot.*' -print -quit)" ]
}

t_ui_does_not_return_previous_dump_after_failed_refresh() {
  running
  printf '<hierarchy stale="true"/>' >"$FAKE/window_dump.xml"
  touch "$FAKE/ui_broken"
  not ctl ui
  not grep -q 'stale="true"' "$FAKE/out"
}

t_start_waits_for_queued_dependency() {
  touch "$FAKE/pending_start"
  echo 1 >"$FAKE/boot" # A stale guest property must not bypass the unit/job check.
  ctl start
  called "systemctl show --property=Job --value android-appliance-emulator.service"
  state_is running
}

t_unwritable_lock_explains_service_sandbox() {
  mkdir "$FAKE/state/activity.lock"
  not ctl start --no-wait
  grep -q 'ReadWritePaths' "$FAKE/out"
  grep -q 'does not prove the host filesystem is read-only' "$FAKE/out"
  not grep -q '^systemctl start' "$FAKE/calls"
}

for t in $(declare -F | awk '{print $3}' | grep '^t_'); do
  check "${t#t_}" "$t"
done

if [ "$failures" -gt 0 ]; then
  echo "$failures test(s) failed"
  exit 1
fi
echo "all tests passed"
