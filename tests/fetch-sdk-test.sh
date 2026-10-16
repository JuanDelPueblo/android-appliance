#!/usr/bin/env bash
# Tests for scripts/fetch-sdk.sh. They use a local file:// repository
# with tiny fake archives, so they need no network and no real SDK.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
fetch=$here/../scripts/fetch-sdk.sh
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 022
# The caller's build settings must not leak into the tests.
unset API_LEVEL SDK_REPOSITORY SDK_EMULATOR_VERSION SDK_PLATFORM_TOOLS_VERSION SDK_IMAGE_REVISION

fail() {
  echo "FAIL $*" >&2
  exit 1
}

for tool in python3 curl unzip sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "skip fetch-sdk tests ($tool is not installed)"
    exit 0
  fi
done

# make_archive ZIP TOP REVISION FILE...: make a fake SDK archive. Each
# file has mode 0744, which only its owner can run.
repo=$work/repo
mkdir -p "$repo/sys-img"
make_archive() {
  python3 - "$@" <<'EOF'
import sys, zipfile
path, top, revision, *files = sys.argv[1:]
with zipfile.ZipFile(path, "w") as z:
    z.writestr(f"{top}/source.properties", f"Pkg.Desc=fake\nPkg.Revision={revision}\n")
    for name in files:
        info = zipfile.ZipInfo(f"{top}/{name}")
        info.external_attr = 0o100744 << 16
        z.writestr(info, "#!/bin/sh\n")
EOF
}
make_archive "$repo/emu-1.zip" emulator 1.2.3 emulator qemu/linux-x86_64/qemu-system-x86_64-headless
make_archive "$repo/emu-2.zip" emulator 1.2.4 emulator
make_archive "$repo/pt-1.zip" platform-tools 4.5.6 adb
make_archive "$repo/pt-2.zip" platform-tools 4.5.7 adb
make_archive "$repo/sys-img/img36-7.zip" x86_64 7 system.img
make_archive "$repo/sys-img/img36-5.zip" x86_64 6 system.img
make_archive "$repo/sys-img/img35-9.zip" x86_64 9 system.img
make_archive "$repo/sys-img/img35-8.zip" x86_64 8 system.img
sha() { sha256sum "$1" | cut -d' ' -f1; }

# The first line of a package gives its default revision. pt-2.zip has a
# wrong SHA-256, and img36-5.zip contains revision 6.
lock=$work/sdk.lock
cat >"$lock" <<EOF
# Fake pins for the tests.
emulator 1.2.3 emu-1.zip $(sha "$repo/emu-1.zip")
emulator 1.2.4 emu-2.zip $(sha "$repo/emu-2.zip")
platform-tools 4.5.6 pt-1.zip $(sha "$repo/pt-1.zip")
platform-tools 4.5.7 pt-2.zip 0000000000000000000000000000000000000000000000000000000000000000
system-images;android-36;google_apis_playstore;x86_64 7 sys-img/img36-7.zip $(sha "$repo/sys-img/img36-7.zip")
system-images;android-36;google_apis_playstore;x86_64 5 sys-img/img36-5.zip $(sha "$repo/sys-img/img36-5.zip")
system-images;android-35;google_apis_playstore;x86_64 9 sys-img/img35-9.zip $(sha "$repo/sys-img/img35-9.zip")
system-images;android-35;google_apis_playstore;x86_64 8 sys-img/img35-8.zip $(sha "$repo/sys-img/img35-8.zip")
EOF

sdk=$work/sdk
image36=$sdk/system-images/android-36/google_apis_playstore/x86_64
image35=$sdk/system-images/android-35/google_apis_playstore/x86_64
run() {
  SDK_REPOSITORY=file://$repo bash "$fetch" --sdk "$sdk" --lock "$lock" "$@"
}
revision() {
  sed -n 's/^Pkg\.Revision=//p' "$1/source.properties"
}

# Usage errors exit with 2.
status=0
bash "$fetch" >/dev/null 2>&1 || status=$?
[ "$status" = 2 ] || fail "no part must exit with 2, not $status"
status=0
bash "$fetch" --sdk "$sdk" everything >/dev/null 2>&1 || status=$?
[ "$status" = 2 ] || fail "an unknown part must exit with 2, not $status"
echo "ok   usage errors exit with 2"

# The tools part installs the default revisions and nothing else.
out=$(run tools 2>&1) || fail "the tools installation failed: $out"
[ "$(revision "$sdk/emulator")" = 1.2.3 ] || fail "the default emulator is not the first lock line"
[ "$(revision "$sdk/platform-tools")" = 4.5.6 ] || fail "the default platform-tools are not the first lock line"
[ -e "$sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless" ] || fail "the emulator files are missing"
[ ! -e "$sdk/system-images" ] || fail "the tools part must not install a system image"
[ ! -e "$sdk/.fetch" ] || fail "the work directory was not removed"
echo "ok   tools installs the default emulator and platform-tools"

# Everybody can read and run the SDK, whatever the archive modes.
[ "$(stat -c %a "$sdk/emulator/emulator")" = 755 ] || fail "the emulator mode is not 0755"
[ "$(stat -c %a "$sdk/platform-tools/adb")" = 755 ] || fail "the adb mode is not 0755"
[ "$(stat -c %a "$sdk/emulator/source.properties")" = 644 ] || fail "source.properties is not 0644"
[ "$(stat -c %a "$sdk/emulator/qemu/linux-x86_64")" = 755 ] || fail "a directory is not 0755"
echo "ok   the SDK is readable by all"

# The image part takes the first lock line of the API level.
out=$(run image 2>&1) || fail "the image installation failed: $out"
[ "$(revision "$image36")" = 7 ] || fail "the default API level is not 36 revision 7"
out=$(API_LEVEL=35 run image 2>&1) || fail "the API 35 installation failed: $out"
[ "$(revision "$image35")" = 9 ] || fail "the API 35 default is not its first lock line"
echo "ok   image installs the first lock line of API_LEVEL"

# The variables select other revisions of the lock.
out=$(SDK_EMULATOR_VERSION=1.2.4 run tools 2>&1) || fail "the emulator 1.2.4 installation failed: $out"
[ "$(revision "$sdk/emulator")" = 1.2.4 ] || fail "SDK_EMULATOR_VERSION was not installed"
[ ! -e "$sdk/emulator/qemu" ] || fail "files of the old emulator remain"
out=$(API_LEVEL=35 SDK_IMAGE_REVISION=8 run image 2>&1) || fail "the image revision 8 installation failed: $out"
[ "$(revision "$image35")" = 8 ] || fail "SDK_IMAGE_REVISION was not installed"
echo "ok   the variables select a revision"

# An installed pinned revision is kept; nothing is downloaded.
mv "$repo" "$repo.away"
out=$(SDK_EMULATOR_VERSION=1.2.4 run tools 2>&1) || fail "the re-run failed: $out"
case $out in
  *downloading*) fail "the re-run downloaded an archive: $out" ;;
esac
mv "$repo.away" "$repo"
echo "ok   an installed revision is kept"

# A wrong SHA-256 stops and keeps the installed package.
if out=$(SDK_PLATFORM_TOOLS_VERSION=4.5.7 run tools 2>&1); then
  fail "a wrong SHA-256 must stop the installation"
fi
case $out in
  *"the SHA-256 of pt-2.zip is not 0000"*) ;;
  *) fail "the error must name the archive: $out" ;;
esac
[ "$(revision "$sdk/platform-tools")" = 4.5.6 ] || fail "the installed platform-tools were changed"
[ ! -e "$sdk/.fetch" ] || fail "the work directory was not removed after the error"
echo "ok   a wrong SHA-256 stops"

# An archive with another revision than its lock line stops.
if out=$(SDK_IMAGE_REVISION=5 run image 2>&1); then
  fail "a revision mismatch must stop the installation"
fi
case $out in
  *"img36-5.zip contains revision 6, not 5"*) ;;
  *) fail "the error must name both revisions: $out" ;;
esac
[ "$(revision "$image36")" = 7 ] || fail "the installed system image was changed"
echo "ok   a revision mismatch stops"

# Every pin is checked before the first download.
rm -rf "$sdk"
if out=$(SDK_IMAGE_REVISION=99 run tools image 2>&1); then
  fail "an unknown revision must stop the installation"
fi
case $out in
  *"system-images;android-36;google_apis_playstore;x86_64 99 is not in $lock"*) ;;
  *) fail "the error must name the unknown revision: $out" ;;
esac
[ ! -e "$sdk/emulator" ] || fail "a bad image pin must stop before the tools download"
echo "ok   an unknown revision stops before any download"

# Bad values stop with a clear message.
check_error() {
  local expected=$1 out
  shift
  if out=$(env "$@" SDK_REPOSITORY="file://$repo" bash "$fetch" --sdk "$sdk" --lock "$lock" tools image 2>&1); then
    fail "$* must fail"
  fi
  case $out in
    *"$expected"*) ;;
    *) fail "$* must report '$expected': $out" ;;
  esac
}
check_error "SDK_EMULATOR_VERSION 'latest' is not a revision" SDK_EMULATOR_VERSION=latest
check_error "API_LEVEL 'x' is not a number" API_LEVEL=x
check_error "no revision of system-images;android-34;google_apis_playstore;x86_64 is in" API_LEVEL=34
if out=$(bash "$fetch" --sdk "$sdk" --lock "$work/none" tools 2>&1); then
  fail "a missing lock file must fail"
fi
case $out in
  *"the lock file $work/none is missing"*) ;;
  *) fail "the error must name the lock file: $out" ;;
esac
echo "ok   bad values stop"

# The project lock pins the defaults of the image builds. An empty
# repository shows which archive each one selects, with no download.
mkdir -p "$work/empty"
expect_download() {
  local expected=$1 part=$2 out
  shift 2
  if out=$(env "$@" SDK_REPOSITORY="file://$work/empty" bash "$fetch" --sdk "$sdk" "$part" 2>&1); then
    fail "a download from an empty repository must fail"
  fi
  case $out in
    *"the download of file://$work/empty/$expected failed"*) ;;
    *) fail "$part $* must select $expected: $out" ;;
  esac
}
expect_download emulator-linux_x64-16428233.zip tools
expect_download sys-img/google_apis_playstore/x86_64-36_r07.zip image
expect_download sys-img/google_apis_playstore/x86_64-35_r09.zip image API_LEVEL=35
echo "ok   conf/sdk.lock pins the emulator and the API 35 and 36 images"

echo "fetch-sdk tests passed"
