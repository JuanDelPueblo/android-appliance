#!/usr/bin/env bash
# Tests for scripts/provision-sdk.sh. They use a staged root, a local
# file:// repository with small fake archives and a fake ldd, so they
# need no network, no root and no real SDK.
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

conf=$root/etc/android-appliance/appliance.conf
mkdir -p "$root/etc/android-appliance"
cat >"$conf" <<'EOF'
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

# The installation needs python3 to make the fake archives.
if ! command -v python3 >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then
  echo "skip installation tests (python3 or unzip is not installed)"
  echo "provision-sdk tests passed"
  exit 0
fi

# make_archive ZIP TOP REVISION FILE...: make a fake SDK archive. Each
# program has mode 0744, as sdkmanager would leave it.
repo=$root/repo
mkdir -p "$repo/sys-img"
make_archive() {
  python3 - "$@" <<'EOF'
import sys, zipfile
path, top, revision, *programs = sys.argv[1:]
with zipfile.ZipFile(path, "w") as z:
    z.writestr(f"{top}/source.properties", f"Pkg.Revision={revision}\n")
    for program in programs:
        info = zipfile.ZipInfo(f"{top}/{program}")
        info.external_attr = 0o100744 << 16
        z.writestr(info, "#!/bin/sh\n")
EOF
}
make_archive "$repo/emu-1.zip" emulator 1.2.3 emulator qemu/linux-x86_64/qemu-system-x86_64 lib64/qt/lib/libQt6Core.so.6
make_archive "$repo/pt-1.zip" platform-tools 4.5.6 adb
make_archive "$repo/pt-2.zip" platform-tools 4.5.7 adb
make_archive "$repo/sys-img/img-9.zip" x86_64 9 system.img
make_archive "$repo/tools-1.zip" cmdline-tools 7.0 bin/sdkmanager
sha() { sha256sum "$1" | cut -d' ' -f1; }

# The host lock file pins the fake archives. The first line of a
# package is its default revision.
cat >"$root/etc/android-appliance/sdk.lock" <<EOF
emulator 1.2.3 emu-1.zip $(sha "$repo/emu-1.zip")
platform-tools 4.5.6 pt-1.zip $(sha "$repo/pt-1.zip")
platform-tools 4.5.7 pt-2.zip 0000000000000000000000000000000000000000000000000000000000000000
system-images;android-35;google_apis_playstore;x86_64 9 sys-img/img-9.zip $(sha "$repo/sys-img/img-9.zip")
cmdline-tools 7.0 tools-1.zip $(sha "$repo/tools-1.zip")
EOF
echo "APPLIANCE_SDK_REPOSITORY=file://$repo" >>"$conf"

# A first installation into an empty SDK directory.
rm -rf "$sdk"
out=$(bash "$provision" --root "$root" 2>&1) || fail "the installation failed: $out"
for file in emulator/emulator platform-tools/adb system-images/android-35/google_apis_playstore/x86_64/system.img; do
  [ -e "$sdk/$file" ] || fail "missing $file after the installation"
done
grep -q '^Pkg.Revision=1.2.3$' "$sdk/emulator/source.properties" || fail "wrong emulator revision"
grep -q '^Pkg.Revision=4.5.6$' "$sdk/platform-tools/source.properties" || fail "the default revision is not the first lock line"
echo "ok   installs the pinned archives"
[ "$(stat -c %a "$sdk/emulator/emulator")" = 755 ] || fail "the emulator mode is not 0755"
[ "$(stat -c %a "$sdk/platform-tools/adb")" = 755 ] || fail "the adb mode is not 0755"
echo "ok   the appliance user can run the SDK programs"
[ ! -e "$sdk/cmdline-tools" ] || fail "the command-line tools must not be installed by default"
[ ! -e "$sdk/.provision" ] || fail "the work directory was not removed"
echo "ok   no command-line tools and no work directory"

# A second run downloads nothing: it works without the repository.
mv "$repo" "$repo.away"
out=$(bash "$provision" --root "$root" 2>&1) || fail "the second run failed: $out"
case $out in
  *downloading*) fail "the second run downloaded an archive: $out" ;;
esac
mv "$repo.away" "$repo"
echo "ok   idempotent re-run"

# A wrong SHA-256 stops the installation and keeps the installed package.
echo "APPLIANCE_SDK_PLATFORM_TOOLS_VERSION=4.5.7" >>"$conf"
if out=$(bash "$provision" --root "$root" 2>&1); then
  fail "a wrong SHA-256 must stop the installation"
fi
case $out in
  *"SHA-256 of pt-2.zip"*) ;;
  *) fail "the error must name the archive: $out" ;;
esac
grep -q '^Pkg.Revision=4.5.6$' "$sdk/platform-tools/source.properties" || fail "the old platform-tools were changed"
echo "ok   a wrong SHA-256 stops and changes nothing"

# A revision that is not in a lock file stops before a download.
sed -i 's/^APPLIANCE_SDK_PLATFORM_TOOLS_VERSION=.*/APPLIANCE_SDK_PLATFORM_TOOLS_VERSION=9.9.9/' "$conf"
if out=$(bash "$provision" --root "$root" 2>&1); then
  fail "an unknown revision must stop the installation"
fi
case $out in
  *"platform-tools 9.9.9 is not in"*) ;;
  *) fail "the error must name the unknown revision: $out" ;;
esac
sed -i '/^APPLIANCE_SDK_PLATFORM_TOOLS_VERSION=/d' "$conf"
echo "ok   an unknown revision stops"

# A pinned revision replaces a different installed revision, for
# example an SDK from sdkmanager.
sed -i 's/^Pkg.Revision=.*/Pkg.Revision=1.2.4/' "$sdk/emulator/source.properties"
touch "$sdk/emulator/package.xml"
out=$(bash "$provision" --root "$root" 2>&1) || fail "the replacement failed: $out"
grep -q '^Pkg.Revision=1.2.3$' "$sdk/emulator/source.properties" || fail "the emulator was not replaced"
[ ! -e "$sdk/emulator/package.xml" ] || fail "the old emulator files remain"
echo "ok   replaces a different revision"

# The check warns when a revision is not the pin.
sed -i 's/^Pkg.Revision=.*/Pkg.Revision=1.2.4/' "$sdk/emulator/source.properties"
out=$(bash "$provision" --root "$root" --check 2>&1) || fail "the check failed: $out"
case $out in
  *"WARNING emulator is 1.2.4, but the pin is 1.2.3"*) ;;
  *) fail "the check must report the revision difference: $out" ;;
esac
echo "ok   check reports a revision that is not the pin"

# The command-line tools are installed on request.
echo "APPLIANCE_SDK_CMDLINE_TOOLS_VERSION=7.0" >>"$conf"
out=$(bash "$provision" --root "$root" 2>&1) || fail "the tools installation failed: $out"
[ "$(stat -c %a "$sdk/cmdline-tools/latest/bin/sdkmanager")" = 755 ] || fail "the command-line tools are missing"
echo "ok   installs the command-line tools on request"

# The project lock file has a valid line for each default package.
lock=$here/../conf/sdk.lock
for package in emulator platform-tools cmdline-tools \
  "system-images;android-35;google_apis_playstore;x86_64" \
  "system-images;android-36;google_apis_playstore;x86_64"; do
  awk -v p="$package" '$1 == p && NF == 4 && $4 ~ /^[0-9a-f]{64}$/ { found = 1 } END { exit !found }' "$lock" ||
    fail "conf/sdk.lock has no valid line for $package"
done
echo "ok   conf/sdk.lock pins every default package"

echo "provision-sdk tests passed"
