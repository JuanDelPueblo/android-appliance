#!/usr/bin/env bash
# smoke.sh: end-to-end check on an installed host. It needs KVM and a
# provisioned SDK. It starts Android, runs device commands, checks the
# browser display and stops Android again. The first boot creates the
# AVD and can take several minutes.
set -euo pipefail

ctl=${ANDROIDCTL:-androidctl}
command -v "$ctl" >/dev/null 2>&1 || ctl=/usr/local/bin/androidctl
[ -x "$ctl" ] || {
  echo "smoke: androidctl is not installed" >&2
  exit 1
}
[ -c /dev/kvm ] || {
  echo "smoke: /dev/kvm is missing; the emulator needs KVM" >&2
  exit 1
}

step() {
  echo
  echo "smoke: $*"
}

step "start Android (a cold boot; the first run also creates the AVD)"
"$ctl" start

step "status reports a running, booted device"
out=$("$ctl" status)
echo "$out"
case $out in
  "state=running boot_completed=1 "*) ;;
  *)
    echo "smoke: unexpected status" >&2
    exit 1
    ;;
esac

step "run a device shell command"
"$ctl" shell getprop ro.build.version.release

step "capture a screenshot"
shot=$("$ctl" screenshot)
[ -n "$shot" ] && [ -s "$shot" ]
echo "smoke: screenshot at $shot"

step "fetch the browser display page"
url=$("$ctl" display)
echo "smoke: $url"
if command -v curl >/dev/null 2>&1; then
  page=$(curl -fsS "$url")
  case $page in
    *noVNC* | *novnc*) ;;
    *)
      echo "smoke: the display page did not load" >&2
      exit 1
      ;;
  esac
fi

step "stop Android and confirm the stopped state"
"$ctl" stop
out=$("$ctl" status)
case $out in
  "state=stopped "*) ;;
  *)
    echo "smoke: unexpected status after stop: $out" >&2
    exit 1
    ;;
  esac

echo
echo "smoke: all checks passed"
