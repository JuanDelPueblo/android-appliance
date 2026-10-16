#!/usr/bin/env bash
# Tests for bin/androidctl (v2). They run it against
# tests/fakes/fake-api.py, a fake appliance API on a unix socket, with
# fake systemctl and podman commands, so they need no container, no KVM
# and no emulator.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
androidctl=$here/../bin/androidctl
fake_api=$here/fakes/fake-api.py
real_curl=$(command -v curl)
failures=0

# Every test gets its own directory T, a fresh fake API and an
# environment that ignores the configuration and units of the host.
setup() {
  T=$(mktemp -d)
  export T
  mkdir -p "$T/api" "$T/bin" "$T/home" "$T/run"
  unset "${!APPLIANCE_@}" ANDROID_APPLIANCE_URL ANDROID_APPLIANCE_TOKEN
  export HOME=$T/home XDG_RUNTIME_DIR=$T/run XDG_CONFIG_HOME=$T/config XDG_DATA_HOME=$T/data
  export ANDROID_APPLIANCE_CONFIG=$T/appliance.conf
  export ANDROID_APPLIANCE_SOCKET=$T/api.sock
  export FAKE_API=$fake_api FAKE_SOCKET=$T/api.sock
  export PATH="$T/bin:$PATH"

  # The unit exists when $T/unit-exists does; starting it starts the API.
  cat >"$T/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >>"$T/systemctl.calls"
case "$*" in
  "--user show -p LoadState --value "*)
    if [ -e "$T/unit-exists" ]; then echo loaded; else echo not-found; fi
    ;;
  "--user is-active --quiet "*) [ -e "$T/unit-active" ] ;;
  "--user start "*)
    touch "$T/unit-active"
    python3 "$FAKE_API" --state "$T/api" --socket "$FAKE_SOCKET" >/dev/null 2>&1 &
    echo $! >"$T/unit-api.pid"
    ;;
  *) exit 1 ;;
esac
EOF
  cat >"$T/bin/podman" <<'EOF'
#!/usr/bin/env bash
echo "podman $*" >>"$T/podman.calls"
case $1 in
  info) echo "true crun v2" ;;
  image) [ -e "$T/image-exists" ] ;;
  container)
    case "$*" in
      *State.Running*) if [ -e "$T/container-running" ]; then echo true; else exit 1; fi ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
EOF
  chmod +x "$T/bin/systemctl" "$T/bin/podman"
  start_api
}

start_api() {
  rm -f "$T/api/ready"
  python3 "$fake_api" --state "$T/api" --socket "${1:-$T/api.sock}" "${@:2}" >>"$T/api.log" 2>&1 &
  echo $! >>"$T/api.pid"
  wait_ready
}

# wait_ready [STATE]: wait until the fake API of STATE listens.
wait_ready() {
  local deadline=$((SECONDS + 5))
  until [ -e "${1:-$T/api}/ready" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "the fake API did not start" >&2
      cat "$T/api.log" >&2
      return 1
    fi
    sleep 0.05
  done
}

stop_api() {
  local pid pids=()
  read -r -d '' -a pids < <(cat "$T"/*.pid 2>/dev/null) || true
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
    # Reap our own children quietly; the unit's API is not one.
    wait "$pid" 2>/dev/null || true
  done
  rm -f "$T"/*.pid "$T/api.sock"
}

# Record the curl arguments, to check timeouts and the token handling.
wrap_curl() {
  cat >"$T/bin/curl" <<EOF
#!/usr/bin/env bash
echo "curl \$*" >>"\$T/curl.calls"
exec "$real_curl" "\$@"
EOF
  chmod +x "$T/bin/curl"
}

ctl() { "$androidctl" "$@"; }

called() { grep -qxF -- "$1" "$T/api/calls"; }

# `! cmd` does not trip errexit, so negate explicitly.
not() { if "$@"; then return 1; fi; }

# status_of CMD...: run a command and print its exit status.
status_of() {
  local rc=0
  "$@" >"$T/stdout" 2>"$T/stderr" || rc=$?
  echo "$rc"
}

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
  ) >"$T/out" 2>&1
  rc=$?
  set -e
  if [ "$rc" = 0 ]; then
    echo "ok   $name"
  else
    echo "FAIL $name"
    sed 's/^/     /' "$T/out"
    [ ! -f "$T/api/calls" ] || sed 's/^/     api: /' "$T/api/calls"
    failures=$((failures + 1))
  fi
  stop_api
  rm -rf "$T"
}

t_status_prints_v1_line() {
  [[ "$(ctl status)" =~ ^state=stopped\ boot_completed=0\ idle_seconds=[0-9]+$ ]]
  called "GET /api/v1/status?format=text"
  grep -q '"accept": "text/plain"' "$T/api/headers"
}

t_start_waits_and_prints_nothing() {
  [ -z "$(ctl start)" ]
  called 'POST /api/v1/start?format=text application/json {"wait":true}'
  [[ "$(ctl status)" == "state=running boot_completed=1 "* ]]
}

t_start_no_wait() {
  ctl start --no-wait
  called 'POST /api/v1/start?format=text application/json {"wait":false}'
}

t_start_rejects_unknown_option() {
  [ "$(status_of ctl start --now)" = 2 ]
  not grep -q '^POST' "$T/api/calls"
}

t_restart() {
  ctl restart
  called 'POST /api/v1/restart?format=text application/json {"wait":true}'
  ctl restart --no-wait
  called 'POST /api/v1/restart?format=text application/json {"wait":false}'
}

t_stop() {
  ctl start
  ctl stop
  called 'POST /api/v1/stop?format=text application/json {"wait":true}'
  [[ "$(ctl status)" == "state=stopped "* ]]
}

t_stop_without_daemon_succeeds() {
  stop_api
  touch "$T/unit-exists"
  ctl stop
  [ ! -e "$T/systemctl.calls" ]
}

t_stop_with_a_stale_socket_succeeds() {
  # A killed daemon leaves its socket file behind.
  stop_api
  python3 "$fake_api" --state "$T/stale" --socket "$T/api.sock" >/dev/null 2>&1 &
  local pid=$!
  wait_ready "$T/stale"
  kill -9 "$pid"
  wait "$pid" 2>/dev/null || true
  [ -S "$T/api.sock" ]
  touch "$T/unit-exists"
  ctl stop
  [ ! -e "$T/systemctl.calls" ]
  [ "$(status_of ctl wait)" = 1 ]
  grep -q 'android is stopped' "$T/stderr"
}

t_wait_stopped_fails_with_api_message() {
  [ "$(status_of ctl wait)" = 1 ]
  [ "$(cat "$T/stderr")" = "androidctl: android is stopped" ]
}

t_wait_running() {
  ctl start
  ctl wait
  called 'POST /api/v1/wait?format=text application/json {}'
}

t_wait_without_daemon_fails() {
  stop_api
  [ "$(status_of ctl wait)" = 1 ]
  grep -q 'android is stopped' "$T/stderr"
}

t_display_url() {
  [ "$(ctl display)" = "http://127.0.0.1:6090/" ]
  printf 'APPLIANCE_LISTEN_ADDRESS=::1\nAPPLIANCE_PORT=46090\n' >"$T/appliance.conf"
  [ "$(ctl display)" = "http://[::1]:46090/" ]
}

t_dashboard_prints_login_link() {
  [ "$(ctl dashboard)" = "http://127.0.0.1:6090/login?code=fake-code" ]
  called 'POST /api/v1/auth/login-link?format=text application/json {}'
}

t_screenshot_to_file() {
  [ "$(ctl screenshot "$T/shot.png")" = "$T/shot.png" ]
  [ "$(head -c 8 "$T/shot.png" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ]
  called "GET /api/v1/screenshot?format=png"
}

t_screenshot_default_dir_is_the_state_dir() {
  mkdir "$T/state"
  echo "APPLIANCE_STATE_DIR=$T/state" >"$T/appliance.conf"
  out=$(ctl screenshot)
  [[ $out == "$T/state/screenshots/"*.png ]]
  [ -s "$out" ]
}

t_screenshot_falls_back_to_the_data_dir() {
  echo "APPLIANCE_STATE_DIR=$T/missing" >"$T/appliance.conf"
  out=$(ctl screenshot)
  [[ $out == "$T/data/android-appliance/screenshots/"*.png ]]
  [ -s "$out" ]
}

t_failed_screenshot_keeps_previous_file() {
  printf previous >"$T/shot.png"
  touch "$T/api/screenshot_fail"
  [ "$(status_of ctl screenshot "$T/shot.png")" = 1 ]
  [ "$(cat "$T/stderr")" = "androidctl: the screenshot failed" ]
  [ "$(cat "$T/shot.png")" = previous ]
  [ -z "$(find "$T" -name '.androidctl.*' -print -quit)" ]
}

t_ui_prints_xml() {
  [[ "$(ctl ui)" == *"<hierarchy"* ]]
  called "GET /api/v1/ui?format=xml"
}

t_tap_sends_json_numbers() {
  ctl tap 10 020
  called 'POST /api/v1/input/tap application/json {"x":10,"y":20}'
}

t_tap_rejects_bad_arguments() {
  [ "$(status_of ctl tap 1)" = 2 ]
  [ "$(status_of ctl tap a 2)" = 2 ]
  [ "$(status_of ctl tap -1 2)" = 2 ]
  [ "$(status_of ctl tap 1 2 3)" = 2 ]
  not grep -q input "$T/api/calls"
}

t_long_press() {
  ctl long-press 5 6 900
  called 'POST /api/v1/input/long-press application/json {"x":5,"y":6,"duration_ms":900}'
  ctl long-press 5 6
  called 'POST /api/v1/input/long-press application/json {"x":5,"y":6}'
}

t_swipe() {
  ctl swipe 1 2 3 4 300
  called 'POST /api/v1/input/swipe application/json {"x1":1,"y1":2,"x2":3,"y2":4,"duration_ms":300}'
  ctl swipe 1 2 3 4
  called 'POST /api/v1/input/swipe application/json {"x1":1,"y1":2,"x2":3,"y2":4}'
  [ "$(status_of ctl swipe 1 2 3)" = 2 ]
}

t_text_is_sent_exactly() {
  ctl text "it's" "@a" "tést  x"
  called "POST /api/v1/input/text text/plain; charset=utf-8 \"it's @a tést  x\""
  [ "$(status_of ctl text)" = 2 ]
}

t_key() {
  ctl key HOME
  called 'POST /api/v1/input/key application/json {"key":"HOME"}'
  ctl key 66
  called 'POST /api/v1/input/key application/json {"key":"66"}'
  [ "$(status_of ctl key 'a"b')" = 2 ]
}

t_shell_splits_streams_and_returns_the_exit_code() {
  [ "$(status_of ctl shell 'echo out; echo err >&2; exit 3')" = 3 ]
  [ "$(cat "$T/stdout")" = out ]
  [ "$(cat "$T/stderr")" = err ]
  called "POST /api/v1/shell?format=raw text/plain; charset=utf-8 \"echo out; echo err >&2; exit 3\""
}

t_shell_joins_arguments_like_adb() {
  [ "$(ctl shell echo a b)" = "a b" ]
  called 'POST /api/v1/shell?format=raw text/plain; charset=utf-8 "echo a b"'
  [ "$(status_of ctl shell)" = 2 ]
}

t_shell_refuses_input_that_it_would_drop() {
  # A pipe with data, a file with data: v1 passed them to the device command.
  [ "$(echo data | status_of ctl shell 'cat > /sdcard/x')" = 2 ]
  grep -q 'does not pass standard input to the device; copy the data with push' "$T/stderr"
  echo data >"$T/in.txt"
  [ "$(status_of ctl shell cat <"$T/in.txt")" = 2 ]
  not grep -q 'POST /api/v1/shell' "$T/api/calls"
  # No input: /dev/null, an empty file, a pipe that stays silent or ends at once.
  [ "$(ctl shell echo ok </dev/null)" = ok ]
  : >"$T/empty"
  [ "$(ctl shell echo ok <"$T/empty")" = ok ]
  [ "$(true | ctl shell echo ok)" = ok ]
  [ "$(sleep 2 | ctl shell echo ok)" = ok ]
}

t_shell_reports_truncated_stderr() {
  ctl shell 'head -c 20000 /dev/zero | tr "\0" x >&2' 2>"$T/err"
  [ "$(head -n 1 "$T/err" | tr -d x | wc -c)" = 1 ]
  [ "$(head -n 1 "$T/err" | wc -c)" = 16385 ]
  [ "$(sed -n 2p "$T/err")" = "androidctl: the appliance truncated stderr" ]
}

t_install_maps_flags_to_the_query() {
  printf 'PK\003\004apk' >"$T/app.apk"
  out=$(ctl install "$T/app.apk" -r -g -d -t)
  called 'POST /api/v1/apps/install?replace=1&grant=1&downgrade=1&test=1 application/vnd.android.package-archive <7 bytes>'
  [ "$out" = $'Performing Streamed Install\nSuccess' ]
  ctl install "$T/app.apk" >/dev/null
  called 'POST /api/v1/apps/install application/vnd.android.package-archive <7 bytes>'
}

t_install_rejects_other_flags() {
  printf 'PK' >"$T/app.apk"
  [ "$(status_of ctl install "$T/app.apk" -s)" = 2 ]
  [ "$(status_of ctl install)" = 2 ]
  not grep -q install "$T/api/calls"
  [ "$(status_of ctl install "$T/missing.apk")" = 1 ]
  grep -q 'cannot read' "$T/stderr"
}

t_uninstall() {
  [ "$(ctl uninstall com.example.app -k)" = Success ]
  called "DELETE /api/v1/apps/com.example.app?keep_data=1"
  [ "$(status_of ctl uninstall com.example.app)" = 1 ]
  grep -qx 'androidctl: package com.example.app is not installed' "$T/stderr"
  [ "$(status_of ctl uninstall ../x)" = 2 ]
}

t_apps() {
  [ "$(ctl apps)" = com.example.app ]
  called "GET /api/v1/apps?format=text"
  ctl apps --all | grep -qx com.android.settings
  called "GET /api/v1/apps?format=text&all=1"
  [ "$(status_of ctl apps -a)" = 2 ]
}

t_launch_and_force_stop() {
  ctl launch com.example.app
  called 'POST /api/v1/apps/com.example.app/launch application/json {}'
  ctl force-stop com.example.app
  called 'POST /api/v1/apps/com.example.app/stop application/json {}'
  [ "$(status_of ctl launch com.missing)" = 1 ]
  [ "$(cat "$T/stderr")" = "androidctl: package com.missing is not installed" ]
  [ "$(status_of ctl launch 'a/b')" = 2 ]
}

t_push_and_pull() {
  printf 'hello\n' >"$T/a.txt"
  ctl push "$T/a.txt" "/sdcard/my file+.txt"
  called "PUT /api/v1/files?path=/sdcard/my%20file%2B.txt&name=a.txt application/octet-stream <6 bytes>"
  ctl pull "/sdcard/my file+.txt" "$T/b.txt"
  cmp "$T/a.txt" "$T/b.txt"
  # A directory target takes the file name, like adb.
  ctl push "$T/a.txt" /sdcard/
  called "PUT /api/v1/files?path=/sdcard/a.txt&name=a.txt application/octet-stream <6 bytes>"
  # So does an existing directory without the slash.
  mkdir -p "$T/api/device/sdcard/Download"
  ctl push "$T/a.txt" /sdcard/Download
  called "PUT /api/v1/files?path=/sdcard/Download&name=a.txt application/octet-stream <6 bytes>"
  cmp "$T/a.txt" "$T/api/device/sdcard/Download/a.txt"
  mkdir "$T/dir"
  ctl pull /sdcard/a.txt "$T/dir"
  cmp "$T/a.txt" "$T/dir/a.txt"
}

t_pull_failure_keeps_the_target() {
  printf previous >"$T/b.txt"
  [ "$(status_of ctl pull /sdcard/missing "$T/b.txt")" = 1 ]
  grep -q 'No such file or directory' "$T/stderr"
  [ "$(cat "$T/b.txt")" = previous ]
  [ "$(status_of ctl push "$T/missing" /sdcard/x)" = 1 ]
  [ "$(status_of ctl push "$T/a")" = 2 ]
}

t_ls() {
  printf 'hello\n' >"$T/a.txt"
  ctl push "$T/a.txt" /sdcard/a.txt
  [ "$(ctl ls /sdcard)" = "file 6 a.txt" ]
  called "GET /api/v1/files/list?path=/sdcard&format=text"
}

t_logs() {
  [ "$(ctl logs | head -n 1)" = "emulator line 1" ]
  called "GET /api/v1/logs"
  ctl logs daemon -n 2 >/dev/null
  called "GET /api/v1/logs?source=daemon&lines=2"
  [ "$(status_of ctl logs -n many)" = 2 ]
  [ "$(status_of ctl logs kernel)" = 2 ]
}

t_token() {
  [ "$(ctl token)" = fake-token ]
  called "GET /api/v1/auth/token?format=text"
  [ "$(ctl token --rotate)" = rotated-token ]
  called 'POST /api/v1/auth/token/rotate?format=text application/json {}'
  [ "$(status_of ctl token --show)" = 2 ]
}

t_version() {
  [ "$(ctl version)" = "androidctl $(cat "$here/../VERSION")"$'\n'"appliance 2.0.0-fake" ]
}

t_version_without_daemon_does_not_start_it() {
  stop_api
  touch "$T/unit-exists"
  [ "$(ctl version)" = "androidctl $(cat "$here/../VERSION")" ]
  [ ! -e "$T/systemctl.calls" ]
}

t_api_errors_print_the_message() {
  touch "$T/api/boot_fail"
  [ "$(status_of ctl tap 1 2)" = 1 ]
  [ "$(cat "$T/stderr")" = "androidctl: android did not finish booting in 900s (adb:offline)" ]
}

t_long_waits_exceed_the_boot_timeout() {
  wrap_curl
  echo APPLIANCE_BOOT_TIMEOUT=1000 >"$T/appliance.conf"
  ctl start
  grep -q -- '--max-time 1600 .*/start' "$T/curl.calls"
  ctl status >/dev/null
  grep -q -- '--max-time 60 .*/status' "$T/curl.calls"
}

t_remote_mode_uses_the_token() {
  stop_api
  start_api "$T/unused.sock" --tcp 127.0.0.1:0 --token secret
  wrap_curl
  ANDROID_APPLIANCE_URL=http://127.0.0.1:$(cat "$T/api/port")/
  export ANDROID_APPLIANCE_URL ANDROID_APPLIANCE_TOKEN=secret
  [[ "$(ctl status)" == "state=stopped "* ]]
  grep -q '"authorization": "Bearer secret"' "$T/api/headers"
  # The token stays out of the process list.
  not grep -q secret "$T/curl.calls"
  [ "$(ctl display)" = "$ANDROID_APPLIANCE_URL" ]
  [ "$(status_of ctl dashboard)" = 1 ]
  grep -q 'forbidden' "$T/stderr"
  ANDROID_APPLIANCE_TOKEN=wrong
  [ "$(status_of ctl status)" = 1 ]
  [ "$(cat "$T/stderr")" = "androidctl: unauthorized: a valid API token is required" ]
}

t_missing_socket_without_unit_explains() {
  stop_api
  [ "$(status_of ctl status)" = 1 ]
  grep -q 'the appliance is not running' "$T/stderr"
  grep -q 'install.sh' "$T/stderr"
  not grep -q -- '--user start' "$T/systemctl.calls"
}

t_missing_socket_starts_the_unit() {
  stop_api
  unset ANDROID_APPLIANCE_SOCKET
  echo APPLIANCE_NAME=aa-test >"$T/appliance.conf"
  mkdir -p "$T/run/aa-test"
  export FAKE_SOCKET=$T/run/aa-test/api.sock
  touch "$T/unit-exists"
  [[ "$(ctl status 2>"$T/err")" == "state=stopped "* ]]
  grep -qx 'systemctl --user start aa-test.service' "$T/systemctl.calls"
  grep -q 'starting aa-test.service' "$T/err"
}

t_config_name_selects_the_socket() {
  stop_api
  unset ANDROID_APPLIANCE_SOCKET
  printf '# comment\nAPPLIANCE_NAME=aa-other\n' >"$T/appliance.conf"
  mkdir -p "$T/run/aa-other"
  start_api "$T/run/aa-other/api.sock"
  [[ "$(ctl status)" == "state=stopped "* ]]
}

t_environment_wins_over_the_config() {
  echo APPLIANCE_PORT=1111 >"$T/appliance.conf"
  [ "$(APPLIANCE_PORT=2222 ctl display)" = "http://127.0.0.1:2222/" ]
}

t_config_sets_only_appliance_keys() {
  printf 'PATH=/nowhere\nthis line has no assignment\nAPPLIANCE_PORT=1234\n' >"$T/appliance.conf"
  [ "$(ctl display)" = "http://127.0.0.1:1234/" ]
  [[ "$(ctl status)" == "state=stopped "* ]]
}

t_usage_errors() {
  [ "$(status_of ctl)" = 2 ]
  [ "$(status_of ctl frobnicate)" = 2 ]
  grep -q '^usage: androidctl' "$T/stderr"
  [ "$(status_of ctl status now)" = 2 ]
  ctl help | grep -q '^usage: androidctl'
}

t_v1_internal_commands_are_gone() {
  local command
  for command in boot-hook stop-hook idle-check suspend resume; do
    [ "$(status_of ctl "$command")" = 2 ]
  done
  [ ! -e "$T/api/calls" ]
}

t_mcp_is_not_available_yet() {
  # The daemon has no MCP server yet: say so, and do not advertise it.
  [ "$(status_of ctl mcp)" = 1 ]
  grep -qx 'androidctl: mcp is not available in this version: the daemon has no MCP server yet' "$T/stderr"
  not grep -q mcp <(ctl help)
  [ ! -e "$T/podman.calls" ]
}

t_doctor_checks_the_host_and_the_daemon() {
  local rc
  rc=$(status_of ctl doctor)
  # Every line is a check result; any FAIL makes the exit status 1.
  not grep -vqE '^(ok   |WARN |FAIL )' "$T/stdout"
  grep -qx 'ok   podman runs rootless' "$T/stdout"
  grep -qx 'ok   the OCI runtime is crun' "$T/stdout"
  grep -qx 'ok   cgroups v2' "$T/stdout"
  grep -q '^FAIL there is no unit ' "$T/stdout"
  grep -q '^FAIL .*android-appliance.containers.conf is missing' "$T/stdout"
  grep -qx "ok   the daemon answers at $T/api.sock (version 2.0.0-fake)" "$T/stdout"
  [ "$rc" = 1 ]
}

t_doctor_accepts_an_installed_unit() {
  mkdir -p "$T/config/containers/systemd"
  touch "$T/config/containers/systemd/android-appliance.container" "$T/unit-active"
  printf '[containers]\nlabel = true\n' >"$T/android-appliance.containers.conf"
  ctl doctor >"$T/stdout" || true
  grep -q "^ok   the unit $T/config/containers/systemd/android-appliance.container exists" "$T/stdout"
  grep -qx 'ok   android-appliance.service is active' "$T/stdout"
  grep -q '^ok   SELinux labelling is on' "$T/stdout"
}

t_doctor_checks_the_image_of_the_unit() {
  mkdir -p "$T/config/containers/systemd"
  printf '[Container]\nImage=localhost/aa:one\n' >"$T/config/containers/systemd/android-appliance.container"
  ctl doctor >"$T/stdout" || true
  grep -qx 'FAIL the image localhost/aa:one is missing; run install.sh to build it' "$T/stdout"
  touch "$T/image-exists"
  ctl doctor >"$T/stdout" || true
  grep -qx 'ok   the image localhost/aa:one exists' "$T/stdout"
}

t_doctor_accepts_a_container_without_a_unit() {
  # install.sh --print-run starts the container itself.
  touch "$T/container-running"
  ctl doctor >"$T/stdout" || true
  grep -qx 'ok   the container android-appliance runs without a unit (install.sh --print-run)' "$T/stdout"
  not grep -q '^FAIL there is no unit' "$T/stdout"
}

t_doctor_remote_checks_only_the_daemon() {
  stop_api
  start_api "$T/unused.sock" --tcp 127.0.0.1:0 --token secret
  ANDROID_APPLIANCE_URL=http://127.0.0.1:$(cat "$T/api/port") ctl doctor >"$T/stdout"
  [ "$(wc -l <"$T/stdout")" = 1 ]
  grep -q '^ok   the daemon answers' "$T/stdout"
  [ ! -e "$T/podman.calls" ]
}

for t in $(declare -F | awk '{print $3}' | grep '^t_'); do
  check "${t#t_}" "$t"
done

if [ "$failures" -gt 0 ]; then
  echo "$failures test(s) failed"
  exit 1
fi
echo "all androidctl v2 tests passed"
