#!/usr/bin/env bash
# lifecycle.sh: end-to-end test of the appliance on a real host. It
# needs KVM, rootless Podman with Quadlet, a systemd user session and a
# built image (./install.sh builds one). It installs its own instance
# from that image with install.sh --no-build, drives it with androidctl,
# and removes the instance again with --uninstall --purge. The image
# stays. It prints PASS or FAIL per step and stops at the first failure.
# A run takes about 6 minutes; the first boot creates a new AVD.
#
#   tests/e2e/lifecycle.sh
#
# Settings (environment):
#   E2E_IMAGE      image to test (default localhost/android-appliance:<VERSION>-api36)
#   E2E_NAME       instance name (default aa-e2e)
#   E2E_DIR        scratch directory: configuration, state, androidctl
#                  (default /var/tmp/<E2E_NAME>; keep it off a tmpfs)
#   E2E_PORT       HTTP port on 127.0.0.1 (default 47080)
#   E2E_TURN_PORT  TURN port on 127.0.0.1 (default 47081)
#   E2E_GPU        auto, host or lavapipe (default auto)
#   E2E_MEMORY_MIB guest RAM (default 3072)
#   E2E_KEEP=1     keep the instance after the run, to look at it
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
name=${E2E_NAME:-aa-e2e}
dir=${E2E_DIR:-/var/tmp/$name}
port=${E2E_PORT:-47080}
turn_port=${E2E_TURN_PORT:-47081}
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
  echo "lifecycle: $*" >&2
  exit 1
}

[ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is missing or not usable"
command -v podman >/dev/null 2>&1 || die "podman is not installed"
systemctl --user show-environment >/dev/null 2>&1 || die "there is no systemd user session"
podman image exists "$image" || die "the image $image does not exist; build it with ./install.sh first, or set E2E_IMAGE"
api=$(podman image inspect --format '{{index .Labels "android-appliance.api-level"}}' "$image")
[[ $api =~ ^[0-9]+$ ]] || die "the image $image has no android-appliance.api-level label"

ctl() { "$dir/bin/androidctl" "$@"; }
installer() { "$repo/install.sh" --config "$conf" "$@" >>"$work/install.log" 2>&1; }

status_json() { curl -s --max-time 10 --unix-socket "$sock" http://localhost/api/v1/status; }

# state: the lifecycle state of the status object.
state() { status_json | grep -oE '"state":"[a-z]+"' | cut -d'"' -f4; }

is_state() { [ "$(state)" = "$1" ]; }

# until_ok SECONDS COMMAND...: retry COMMAND every second.
until_ok() {
  local deadline=$((SECONDS + $1))
  shift
  until "$@" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 1
  done
}

# prints PATTERN COMMAND...: COMMAND succeeds and prints a line that
# matches PATTERN (grep -E). The output is read whole: grep -q in a pipe
# can stop the command with SIGPIPE, and pipefail reports a failure.
prints() {
  local pattern=$1 out
  shift
  out=$("$@") || return 1
  grep -qE -- "$pattern" <<<"$out"
}

# top_package: the package of the activity in front. The device filters
# the long dumpsys output.
top_package() {
  ctl shell 'dumpsys activity activities 2>/dev/null | grep -m 1 topResumedActivity' | sed -E 's/.* u0 ([^ /]+)\/.*/\1/'
}

is_top() { [ "$(top_package)" = "$1" ]; }

wakefulness() { ctl shell 'dumpsys power 2>/dev/null | grep -m 1 mWakefulness=' | sed 's/^ *mWakefulness=//'; }

# The screen goes to Dozing (ambient display) or Asleep.
screen_off() {
  case $(wakefulness) in Asleep | Dozing) return 0 ;; esac
  return 1
}

screen_on() { [ "$(wakefulness)" = Awake ]; }

# first_field_focused: the first text field of the screen has the focus.
first_field_focused() {
  local xml re='<node [^>]*class="android.widget.EditText"[^>]*>'
  xml=$(ctl ui) || return 1
  [[ $xml =~ $re ]] && [[ ${BASH_REMATCH[0]} == *'focused="true"'* ]]
}

# center TEXT: the center "x y" of the first node with that text in the
# last UI dump.
center() {
  local re="text=\"$1\"[^>]*bounds=\"\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]\""
  [[ $(<"$work/ui.xml") =~ $re ]] || return 1
  echo "$(((BASH_REMATCH[1] + BASH_REMATCH[3]) / 2)) $(((BASH_REMATCH[2] + BASH_REMATCH[4]) / 2))"
}

ui_has() {
  ctl ui >"$work/ui.xml" && grep -qF -- "$1" "$work/ui.xml"
}

# The home screen shows the Phone icon; the app drawer lists Settings too.
on_home() { ui_has 'text="Phone"' && ! grep -qF -e 'text="Settings"' -e 'text="App info"' "$work/ui.xml"; }

daemon_log_count() { ctl logs daemon -n 100000 | grep -c -- "$1" || true; }

fail() {
  echo "  $*" >&2
  exit 1
}

info() { echo "  $*"; }

# evidence: what the daemon and the device say when a step fails. A
# stopped device stays stopped.
evidence() {
  status_json >"$work/fail-status.json" 2>/dev/null || true
  ctl logs daemon -n 300 >"$work/fail-daemon.log" 2>/dev/null || true
  if is_state running; then
    ctl ui >"$work/fail-ui.xml" 2>/dev/null || true
    ctl screenshot "$work/fail.png" >/dev/null 2>&1 || true
  fi
}

failed=0
# step NAME FUNCTION: run a step in a subshell with errexit, and print
# its result. The steps build on each other, so a failure ends the run.
step() {
  local started=$SECONDS rc
  set +e
  (
    set -e
    "$2"
  )
  rc=$?
  set -e
  if [ "$rc" = 0 ]; then
    echo "PASS $1 ($((SECONDS - started))s)"
  else
    echo "FAIL $1 (see $work)"
    failed=1
    evidence
    exit 1
  fi
}

cleanup() {
  local rc=$?
  # The purge deletes the logs of a failure; keep them with the work files.
  if [ "$failed" = 1 ] && [ -d "$dir/state/appliance/logs" ]; then
    cp -a "$dir/state/appliance/logs" "$work/" || true
    journalctl --user -u "$name.service" --since "$started_at" --no-pager -o cat >"$work/journal.txt" 2>&1 || true
  fi
  if [ "${E2E_KEEP:-0}" = 1 ]; then
    echo "lifecycle: kept the instance $name (ANDROID_APPLIANCE_CONFIG=$conf)"
  elif [ -f "$conf" ]; then
    "$repo/install.sh" --config "$conf" --uninstall --purge >/dev/null 2>&1 || echo "lifecycle: the clean-up of $name failed" >&2
  fi
  if [ "$rc" = 0 ] && [ "$failed" = 0 ]; then rm -rf "$work"; fi
  rmdir "$dir/bin" "$dir" 2>/dev/null || true
}

# A run that was interrupted leaves its instance behind.
if [ -f "$conf" ]; then
  "$repo/install.sh" --config "$conf" --uninstall --purge >/dev/null 2>&1 || die "cannot remove the old instance of $conf"
fi
mkdir -p "$dir" "$work"
started_at=$(date '+%Y-%m-%d %H:%M:%S')
trap cleanup EXIT

write_config() {
  cat >"$conf" <<EOF
# The instance of tests/e2e/lifecycle.sh.
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
APPLIANCE_IDLE_STOP_MINUTES=$1
EOF
  chmod 0600 "$conf"
}

install_instance() {
  write_config 60
  installer --no-build || fail "install.sh failed: $(tail -n 5 "$work/install.log")"
  systemctl --user is-active --quiet "$name.service" || fail "$name.service is not active"
  [ "$(podman container inspect --format '{{.ImageName}}' "$name")" = "$image" ] || fail "the container does not run $image"
  ctl doctor >"$work/doctor.txt" || fail "androidctl doctor failed: $(grep FAIL "$work/doctor.txt")"
  case $(ctl status) in
    "state=stopped boot_completed=0 "*) ;;
    *) fail "a new instance must not boot Android: $(ctl status)" ;;
  esac
  prints '^appliance ' ctl version || fail "androidctl version does not reach the daemon"
}

on_demand_boot() {
  local started=$SECONDS retries
  ctl screenshot "$work/first.png" >/dev/null
  info "first boot and screenshot: $((SECONDS - started))s, $(status_json | grep -oE '"gpu":"[a-z]+"')"
  [ "$(head -c 8 "$work/first.png" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ] || fail "the screenshot is not a PNG"
  [ "$(od -An -tu1 -j16 -N8 "$work/first.png" | tr -s ' ')" = " 0 0 4 56 0 0 7 128" ] || fail "the screenshot is not 1080x1920"
  case $(ctl status) in
    "state=running boot_completed=1 "*) ;;
    *) fail "Android is not running after the screenshot: $(ctl status)" ;;
  esac
  [ "$(daemon_log_count 'creating AVD')" = 1 ] || fail "the first boot did not create the AVD"
  # A first boot on the GPU can crash a few seconds after boot_completed;
  # the daemon then boots once more by itself.
  retries=$(daemon_log_count 'starting android again')
  [ "$(daemon_log_count 'emulator started')" = $((1 + retries)) ] || fail "more launches than boots"
  [ "$retries" = 0 ] || info "the first boot crashed and was retried once"
}

shell_commands() {
  local rc=0
  ctl shell 'echo out; echo err >&2; exit 42' >"$work/out" 2>"$work/err" || rc=$?
  [ "$rc" = 42 ] || fail "shell exit status $rc, not 42"
  [ "$(cat "$work/out")" = out ] && [ "$(cat "$work/err")" = err ] || fail "shell stdout or stderr is wrong"
  [ "$(ctl shell getprop ro.build.version.sdk)" = "$api" ] || fail "the device is not API $api"
}

keys_and_touch() {
  local home xy
  home=$(ctl shell 'cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME' | tail -n 1)
  home=${home%%/*}
  # Android is slow for a minute after a first boot, and the launcher
  # can come up after boot_completed: the waits are long. Android handles
  # keys later, so every key waits for its effect before the next step.
  ctl key HOME
  until_ok 120 is_top "$home" || fail "HOME did not show the launcher $home"
  ctl launch com.android.settings
  until_ok 60 is_top com.android.settings || fail "launch did not open Settings"
  ctl key BACK
  until_ok 60 is_top "$home" || fail "BACK did not leave Settings"
  ctl force-stop com.android.settings

  ctl key POWER
  until_ok 60 screen_off || fail "POWER did not turn the screen off (mWakefulness=$(wakefulness))"
  ctl key POWER
  until_ok 60 screen_on || fail "POWER did not turn the screen on (mWakefulness=$(wakefulness))"
  ctl key HOME

  # A swipe up on the home screen opens the app drawer.
  until_ok 60 on_home || fail "HOME did not show the home screen"
  # Android can drop a gesture while it is still busy after a first boot
  # (seen once in two runs): the swipe gets three tries.
  for _ in 1 2 3; do
    ctl swipe 540 1500 540 400 300
    until_ok 10 ui_has 'text="Settings"' && break
  done
  ui_has 'text="Settings"' || fail "the swipe did not open the app drawer"
  ctl key HOME

  # A long press on an icon opens its menu.
  until_ok 30 on_home || fail "HOME did not close the app drawer"
  xy=$(center Phone) || fail "no Phone icon"
  # shellcheck disable=SC2086  # "x y"
  ctl long-press $xy 800
  until_ok 30 ui_has 'text="App info"' || fail "the long press did not open the app menu"
  ctl key BACK
  until_ok 30 on_home || fail "BACK did not close the app menu"
}

# open_contact_form: start the "new contact" form and wait for it.
open_contact_form() {
  ctl shell 'am start -a android.intent.action.INSERT -t vnd.android.cursor.dir/contact' >"$work/am-start.txt" 2>&1 || return 1
  grep -q '^Starting: Intent' "$work/am-start.txt" || return 1
  until_ok 60 ui_has 'text="First name"'
}

text_entry() {
  local contacts xy
  # The intent is a test fixture, not the appliance: one more try is fine.
  open_contact_form || open_contact_form || fail "the new contact form did not open: $(cat "$work/am-start.txt")"
  contacts=$(top_package) || fail "no activity in front"
  xy=$(center 'First name') || fail "no First name field"
  # shellcheck disable=SC2086  # "x y"
  ctl tap $xy
  until_ok 30 first_field_focused || fail "the tap did not focus the First name field"
  # The first focus of a fresh AVD: Gboard's stylus sheet must not take the text.
  ctl text 'Hello Grüße 日本 😀'
  until_ok 30 ui_has 'text="Hello Grüße 日本 &#128512;"' || fail "the text did not reach the field exactly"
  ctl force-stop "$contacts"
  ctl key HOME
}

files() {
  local listing
  head -c 3000001 /dev/urandom >"$work/blob"
  ctl push "$work/blob" /data/local/tmp/e2e-blob
  listing=$(ctl ls /data/local/tmp)
  grep -qx 'file 3000001 e2e-blob' <<<"$listing" || fail "ls does not show the pushed file: $listing"
  ctl pull /data/local/tmp/e2e-blob "$work/blob.back"
  cmp -s "$work/blob" "$work/blob.back" || fail "the pulled file differs"
  if ctl pull /nonexistent "$work/missing" 2>/dev/null; then fail "pulling a missing file succeeded"; fi
}

apps() {
  local apk
  # An APK from the device itself: BasicDreams is small, and an install
  # with -r turns it into an update in /data/app.
  apk=$(ctl shell pm path com.android.dreams.basic | sed -n 's/^package://p')
  [ -n "$apk" ] || fail "com.android.dreams.basic is missing"
  ctl pull "$apk" "$work/app.apk"
  prints Success ctl install "$work/app.apk" -r || fail "install -r failed"
  prints '^package:/data/app/' ctl shell pm path com.android.dreams.basic || fail "the APK is not installed in /data/app"
  prints '^com\.android\.dreams\.basic$' ctl apps --all || fail "apps --all does not list the app"
}

logs_and_remote() {
  local token rc=0
  prints . ctl logs emulator -n 5 || fail "the emulator log is empty"
  prints 'android is running' ctl logs daemon || fail "the daemon log has no boot"
  token=$(ctl token)
  [ "${#token}" -ge 32 ] || fail "the token is too short"
  prints "^http://127\.0\.0\.1:$port/login\?code=" ctl dashboard || fail "the dashboard link is wrong"
  [ "$(ANDROID_APPLIANCE_URL=http://127.0.0.1:$port ANDROID_APPLIANCE_TOKEN=$token ctl shell echo remote)" = remote ] ||
    fail "remote mode with the token failed"
  ANDROID_APPLIANCE_URL=http://127.0.0.1:$port ctl status >/dev/null 2>&1 || rc=$?
  [ "$rc" = 1 ] || fail "TCP without the token was not refused"
}

# The MCP server over stdio: `androidctl mcp` runs it in the container
# (podman exec) against the daemon's socket. The check needs the Python
# environment of the repository for the MCP client (scripts/dev-venv.sh).
mcp_stdio() {
  local python=$repo/.venv/bin/python
  if [ ! -x "$python" ]; then
    info "no $python: the MCP check is skipped (run scripts/dev-venv.sh)"
    return 0
  fi
  "$python" - "$dir/bin/androidctl" <<'PY' >"$work/mcp.out" 2>&1 || fail "the MCP client failed: $(tail -n 20 "$work/mcp.out")"
import asyncio, json, sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    async with Client(StdioServerParameters(command=sys.argv[1], args=["mcp"])) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"android_status", "android_tap", "android_ui", "android_screenshot"} <= names, names
        r = await c.call_tool("android_status", {})
        assert not r.is_error and json.loads(r.content[0].text)["state"] == "running", r.content
        r = await c.call_tool("android_ui", {})
        assert not r.is_error and "center=" in r.content[0].text, r.content
        r = await c.call_tool("android_screenshot", {"max_size": 400})
        assert not r.is_error and r.content[0].type == "image" and r.content[1].text.startswith("device "), r.content
        r = await c.call_tool("android_launch", {"package": "no.such.app"})
        assert r.is_error and "(not_found)" in r.content[0].text, r.content
        print("mcp ok")

asyncio.run(main())
PY
  grep -q '^mcp ok$' "$work/mcp.out" || fail "the MCP check printed no result: $(tail -n 5 "$work/mcp.out")"
}

# What the container keeps from Android and what it does not (the section
# "What the guest can reach" of docs/ARCHITECTURE.md).
guest_reach() {
  local out
  # The emulator's gRPC port refuses a call without the token.
  out=$(podman exec -i "$name" /opt/appliance/venv/bin/python - 2>&1 <<'EOF'
import sys
sys.path.insert(0, "/opt/appliance")
import grpc
from appliance import _proto  # noqa: F401
import emulator_controller_pb2_grpc as services
from google.protobuf import empty_pb2
try:
    services.EmulatorControllerStub(grpc.insecure_channel("127.0.0.1:8554")).getStatus(empty_pb2.Empty(), timeout=5)
    print("answered")
except grpc.RpcError as e:
    print(e.code().name)
EOF
  ) || true
  [ "$out" = UNAUTHENTICATED ] || fail "gRPC did not refuse a call without the token: $out"
  # The guest reaches the daemon through 10.0.2.2, and the daemon asks for the token.
  out=$(ctl shell "printf 'GET /api/v1/status HTTP/1.0\\r\\n\\r\\n' | nc -w 3 10.0.2.2 8080 | head -n 1")
  case $out in *" 401 "*) ;; *) fail "the guest did not get 401 from the daemon: $out" ;; esac
  # The state is private, and /data has no setuid files or device nodes.
  [ "$(stat -c %a "$dir/state")" = 700 ] || fail "the state directory is not mode 0700"
  [ "$(podman exec "$name" stat -c %a /data)" = 700 ] || fail "/data is not mode 0700 in the container"
  out=$(podman exec "$name" grep ' /data ' /proc/mounts)
  case $out in *nosuid*) ;; *) fail "/data is not mounted nosuid: $out" ;; esac
  case $out in *nodev*) ;; *) fail "/data is not mounted nodev: $out" ;; esac
}

stop_start_keeps_data() {
  local marker=stop-$RANDOM-$SECONDS started
  ctl shell "echo $marker >/data/local/tmp/e2e-marker"
  started=$SECONDS
  ctl stop
  info "stop: $((SECONDS - started))s"
  case $(ctl status) in "state=stopped "*) ;; *) fail "Android did not stop" ;; esac
  started=$SECONDS
  ctl start
  info "cold boot: $((SECONDS - started))s"
  [ "$(ctl shell cat /data/local/tmp/e2e-marker)" = "$marker" ] || fail "the marker was lost"
  prints '^package:/data/app/' ctl shell pm path com.android.dreams.basic || fail "the installed app was lost"
  prints Success ctl uninstall com.android.dreams.basic || fail "uninstall failed"
  prints '^package:/system/' ctl shell pm path com.android.dreams.basic || fail "uninstall did not remove the update"
}

recreate_container() {
  local marker=recreate-$RANDOM-$SECONDS old
  ctl shell "echo $marker >/data/local/tmp/e2e-marker"
  old=$(podman container inspect --format '{{.Id}}' "$name")
  systemctl --user stop "$name.service"
  if podman container exists "$name"; then podman rm "$name" >/dev/null; fi
  systemctl --user start "$name.service"
  [ "$(podman container inspect --format '{{.Id}}' "$name")" != "$old" ] || fail "the container was not recreated"
  [ "$(ctl shell cat /data/local/tmp/e2e-marker)" = "$marker" ] || fail "the marker was lost"
}

crash_recovery() {
  local json
  # Android booted a moment ago (the previous step): a crash now counts
  # as a failed boot, and the daemon boots once more by itself.
  podman exec "$name" pkill -9 -f qemu-system
  until_ok 30 is_state starting || fail "the daemon did not boot again after the crash: $(status_json)"
  ctl wait >/dev/null
  prints '"last_error":null' status_json || fail "a retried crash must not be the last error: $(status_json)"
  [ "$(daemon_log_count 'starting android again')" = 1 ] || fail "the retry is not in the daemon log"
  grep -q '"retried": true' "$dir/state/appliance/state.json" || fail "the crash is not in the crash history"
  # One retry per start: the next crash is reported.
  podman exec "$name" pkill -9 -f qemu-system
  until_ok 30 is_state stopped || fail "the status did not become stopped"
  json=$(status_json)
  grep -q '"kind":"crash"' <<<"$json" || fail "last_error is not a crash: $json"
  grep -q '"exit_status":-9' <<<"$json" || fail "last_error has no signal 9: $json"
  # The stale locks of the killed emulator must not block the next boot.
  [ "$(ctl shell cat /data/local/tmp/e2e-marker | cut -d- -f1)" = recreate ] || fail "no clean boot after the crash"
  prints '"last_error":null' status_json || fail "the next boot did not clear last_error"
}

graceful_restart() {
  local since
  ctl start
  since=$(date '+%Y-%m-%d %H:%M:%S')
  systemctl --user restart "$name.service"
  journalctl --user -u "$name.service" --since "$since" --no-pager -o cat >"$work/journal.txt"
  grep -q 'qemu exited with status 0' "$work/journal.txt" || fail "qemu did not exit cleanly on the restart"
  grep -q 'daemon stopped' "$work/journal.txt" || fail "the daemon did not stop cleanly"
  case $(ctl status) in "state=stopped "*) ;; *) fail "Android runs after the restart" ;; esac
}

idle_stop() {
  local started rc=0 seen=0
  # A configuration change applies with install.sh.
  write_config 1
  installer --no-build || fail "install.sh failed: $(tail -n 5 "$work/install.log")"
  prints '"idle_stop_seconds":60' status_json || fail "the idle limit did not change"
  ctl start
  started=$SECONDS
  # A running command holds a lease: no idle stop, even past the limit.
  ctl shell 'sleep 75' &
  while kill -0 $! 2>/dev/null; do
    [ "$(state)" = running ] || fail "Android stopped while a command ran"
    prints '"idle_seconds":(6[0-9]|7[0-9])' status_json && seen=1
    sleep 5
  done
  wait $! || rc=$?
  [ "$rc" = 0 ] || fail "the long command failed ($rc)"
  [ "$seen" = 1 ] || fail "the idle time did not pass the limit during the command"
  until_ok 150 is_state stopped || fail "no idle stop"
  info "idle stop $((SECONDS - started))s after the start, $(daemon_log_count 'idle for') in the log"
}

uninstall_purge() {
  "$repo/install.sh" --config "$conf" --uninstall --purge >>"$work/install.log" 2>&1 || fail "uninstall failed"
  [ ! -e "$runtime/containers/systemd/$name.container" ] || fail "the unit file is still there"
  [ "$(systemctl --user show -p LoadState --value "$name.service")" = not-found ] || fail "the unit is still loaded"
  ! podman container exists "$name" || fail "the container is still there"
  [ ! -e "$dir/state" ] && [ ! -e "$conf" ] || fail "the state or the configuration is still there"
  podman image exists "$image" || fail "the image was removed"
}

echo "lifecycle: $name on 127.0.0.1:$port with $image (API $api, GPU $gpu)"
step "install from the built image" install_instance
step "on-demand first boot" on_demand_boot
step "shell" shell_commands
step "keys, tap, swipe, long press" keys_and_touch
step "text into a focused field" text_entry
step "push, pull, ls" files
step "install an APK" apps
step "logs, token, remote mode" logs_and_remote
step "mcp over stdio" mcp_stdio
step "what the guest reaches" guest_reach
step "stop and cold start keep userdata" stop_start_keeps_data
step "container recreation keeps userdata" recreate_container
step "crash recovery" crash_recovery
step "graceful stop on unit restart" graceful_restart
step "idle stop after 1 minute" idle_stop
step "uninstall --purge" uninstall_purge
echo "lifecycle: all steps passed"
