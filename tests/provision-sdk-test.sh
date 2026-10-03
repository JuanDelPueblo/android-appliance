#!/usr/bin/env bash
# Tests for scripts/provision-sdk.sh. They use a staged root and a fake
# ldd, so they need no network, no root and no real SDK.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
provision=$here/../scripts/provision-sdk.sh
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
export PATH="$here/fakes:$PATH"
# The SDK check needs files that other users can read.
umask 022

fail() {
  echo "FAIL $*" >&2
  exit 1
}

mkdir -p "$root/etc/android-appliance"
cat >"$root/etc/android-appliance/appliance.conf" <<'EOF'
APPLIANCE_ANDROID_HOME=/opt/sdk
APPLIANCE_API_LEVEL=35
EOF

# A fake SDK with the layout of a real one.
sdk=$root/opt/sdk
make_sdk() {
  rm -rf "$sdk"
  mkdir -p "$sdk/emulator/qemu/linux-x86_64" "$sdk/emulator/lib64/qt/lib" \
    "$sdk/platform-tools" "$sdk/system-images/android-35/google_apis_playstore/x86_64"
  for bin in emulator/emulator emulator/qemu/linux-x86_64/qemu-system-x86_64 \
    emulator/qemu/linux-x86_64/qemu-system-x86_64-headless platform-tools/adb; do
    printf '#!/bin/sh\n' >"$sdk/$bin"
    chmod 0755 "$sdk/$bin"
  done
}

# The check gives ldd the bundled library path of the emulator, so a
# complete SDK passes.
make_sdk
out=$(bash "$provision" --root "$root" --check 2>&1) || fail "check of a complete SDK: $out"
echo "ok   check passes with the bundled library path"

# A missing host library is still reported.
if out=$(FAKE_LDD_MISSING=libpulse.so.0 bash "$provision" --root "$root" --check 2>&1); then
  fail "a missing host library must fail the check"
fi
case $out in
  *"libpulse.so.0 => not found"*) ;;
  *) fail "the check must name the missing library: $out" ;;
esac
case $out in
  *libQt6Core*) fail "the check must not report bundled libraries: $out" ;;
esac
echo "ok   check reports a missing host library"

# A program that only its owner can run fails the check: the emulator
# runs as the appliance user.
chmod 0744 "$sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64"
if out=$(bash "$provision" --root "$root" --check 2>&1); then
  fail "an emulator program with mode 0744 must fail the check"
fi
case $out in
  *"chmod -R a+rX $sdk"*) ;;
  *) fail "the check must give the chmod command: $out" ;;
esac
make_sdk
echo "ok   check reports programs that the appliance user cannot run"

# A missing system image for the configured API level fails.
rm -rf "$sdk/system-images/android-35"
if bash "$provision" --root "$root" --check >/dev/null 2>&1; then
  fail "a missing system image must fail the check"
fi
echo "ok   check reports a missing system image"

echo "provision-sdk tests passed"
