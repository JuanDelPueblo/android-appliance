#!/usr/bin/env bash
# provision-sdk.sh: install the pinned Android SDK without a package
# manager. install.sh runs it; you can also run it alone.
#
# Run as root: ./scripts/provision-sdk.sh
# Check an installed SDK and change nothing: ./scripts/provision-sdk.sh --check
# Staging mode for tests: ./scripts/provision-sdk.sh --root DIR [--check]
#
# It installs the emulator, platform-tools and the system image of
# APPLIANCE_API_LEVEL under APPLIANCE_ANDROID_HOME (default
# /opt/android-sdk). conf/sdk.lock pins the archive and the SHA-256 of
# each package revision. A package with the pinned revision stays as
# it is, so a second run downloads nothing.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)

root=
check_only=0
while [ $# -gt 0 ]; do
  case $1 in
    --root)
      root=${2:?--root needs a directory}
      shift 2
      ;;
    --check)
      check_only=1
      shift
      ;;
    *)
      echo "provision-sdk: unknown argument $1" >&2
      exit 2
      ;;
  esac
done

if [ -n "$root" ]; then
  export ANDROID_APPLIANCE_CONFIG=$root/etc/android-appliance/appliance.conf
fi
# shellcheck source=src/appliance-env
source "$here/src/appliance-env"

sdk=$root${APPLIANCE_ANDROID_HOME:-/opt/android-sdk}
api=${APPLIANCE_API_LEVEL:-36}
image_tag=${AVD_TAG:-google_apis_playstore}
abi=x86_64
image_package="system-images;android-$api;$image_tag;$abi"
image_dir=$sdk/system-images/android-$api/$image_tag/$abi
repository=${APPLIANCE_SDK_REPOSITORY:-https://dl.google.com/android/repository}
# The host lock file comes first, so a host can add revisions.
lock_files=("$root/etc/android-appliance/sdk.lock" "$here/conf/sdk.lock")

die() {
  echo "provision-sdk: $*" >&2
  exit 1
}

# lock_entry PACKAGE [REVISION]: print "revision archive sha256" of the
# first lock line that matches. Without REVISION, the first line of the
# package gives the default revision.
lock_entry() {
  local package=$1 revision=${2:-} file
  for file in "${lock_files[@]}"; do
    [ -f "$file" ] || continue
    awk -v p="$package" -v r="$revision" '
      $1 == p && (r == "" || $2 == r) { print $2, $3, $4; found = 1; exit }
      END { exit !found }' "$file" && return 0
  done
  return 1
}

# installed_revision DIR: print Pkg.Revision of the package in DIR.
installed_revision() {
  [ -f "$1/source.properties" ] || return 0
  sed -n 's/^Pkg\.Revision=//p' "$1/source.properties" | head -n 1
}

# pinned_revision PACKAGE KEY: print the revision that the APPLIANCE_*
# key selects, or the default revision of the lock.
pinned_revision() {
  local package=$1 key=$2 value entry
  value=${!key:-}
  case $value in
    '') ;;
    *[!0-9.]*) die "$key '$value' is not a revision (for example 37.2.12)" ;;
    *)
      echo "$value"
      return
      ;;
  esac
  entry=$(lock_entry "$package") || die "no revision of $package is in ${lock_files[*]}"
  echo "${entry%% *}"
}

# check_sdk: make sure that the SDK can run the appliance emulator.
check_sdk() {
  [ -x "$sdk/emulator/emulator" ] || die "the emulator binary is missing in $sdk"
  [ -x "$sdk/platform-tools/adb" ] || die "the adb binary is missing in $sdk"
  [ -d "$image_dir" ] || die "the system image $image_package is missing in $sdk"

  # The emulator runs as APPLIANCE_USER, not as the owner of the SDK.
  # sdkmanager installs the programs with mode 0744, which only the
  # owner can run.
  local denied
  denied=$(find "$sdk/emulator" "$sdk/platform-tools" "$image_dir" \
    \( -type f -perm -u+x ! -perm -o+x \) -o \( ! -perm -o+r \) -o \( -type d ! -perm -o+x \) | head -n 3)
  if [ -n "$denied" ]; then
    echo "provision-sdk: other users cannot read or run these SDK files:" >&2
    printf '%s\n' "$denied" | sed 's/^/  /' >&2
    die "run: chmod -R a+rX $sdk"
  fi

  # The emulator needs host libraries that a minimal server can lack.
  # The emulator launcher adds its bundled libraries to LD_LIBRARY_PATH
  # before it starts qemu, so ldd gets the same path here.
  local emu_libs=$sdk/emulator/lib64:$sdk/emulator/lib64/qt/lib
  local bin libs missing=
  for bin in "$sdk/emulator/emulator" "$sdk"/emulator/qemu/linux-x86_64/qemu-system-x86_64*; do
    [ -x "$bin" ] || continue
    libs=$(LD_LIBRARY_PATH=$emu_libs ldd "$bin" 2>&1 | grep 'not found' || true)
    if [ -n "$libs" ]; then
      missing=$missing$bin:$'\n'$libs$'\n'
    fi
  done
  if [ -n "$missing" ]; then
    echo "provision-sdk: the emulator misses these host libraries:" >&2
    printf '%s' "$missing" | sed 's/^/  /' >&2
    die "install them and re-run; dnf provides names their packages"
  fi
}

# report_revisions: compare the installed revisions with the pins. A
# difference is a warning, because an operator can manage the SDK.
report_revisions() {
  local package key dir want have
  while read -r package key dir; do
    want=$(pinned_revision "$package" "$key")
    have=$(installed_revision "$dir")
    if [ "$have" != "$want" ]; then
      echo "provision-sdk: WARNING $package is ${have:-unknown}, but the pin is $want" >&2
    fi
  done <<EOF
emulator APPLIANCE_SDK_EMULATOR_VERSION $sdk/emulator
platform-tools APPLIANCE_SDK_PLATFORM_TOOLS_VERSION $sdk/platform-tools
$image_package APPLIANCE_SDK_IMAGE_REVISION $image_dir
EOF
}

if [ "$check_only" = 1 ]; then
  check_sdk
  report_revisions
  echo "provision-sdk: the SDK at $sdk is ready (API $api, $image_tag, $abi)"
  exit 0
fi

[ -n "$root" ] || [ "$(id -u)" = 0 ] || die "run this script as root"
for tool in curl unzip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is missing; on Fedora run: dnf install curl unzip coreutils"
done

# Find every pin before the first download, so a bad key stops early.
emulator_revision=$(pinned_revision emulator APPLIANCE_SDK_EMULATOR_VERSION)
platform_tools_revision=$(pinned_revision platform-tools APPLIANCE_SDK_PLATFORM_TOOLS_VERSION)
image_revision=$(pinned_revision "$image_package" APPLIANCE_SDK_IMAGE_REVISION)
cmdline_tools_revision=
if [ -n "${APPLIANCE_SDK_CMDLINE_TOOLS_VERSION:-}" ]; then
  cmdline_tools_revision=$(pinned_revision cmdline-tools APPLIANCE_SDK_CMDLINE_TOOLS_VERSION)
fi

# Download and unpack in the SDK directory, not in /tmp. The archives
# are large, and a rename on the same file system keeps the SELinux
# label of the SDK directory.
umask 022
mkdir -p "$sdk"
work=$sdk/.provision
rm -rf "$work"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

# install_package PACKAGE REVISION TARGET TOP: install the pinned archive
# of PACKAGE into TARGET. TOP is the top directory in the archive.
install_package() {
  local package=$1 revision=$2 target=$3 top=$4
  local entry archive sha256 file have

  have=$(installed_revision "$target")
  if [ "$have" = "$revision" ]; then
    echo "provision-sdk: $package $revision is installed"
    return
  fi
  entry=$(lock_entry "$package" "$revision") ||
    die "$package $revision is not in ${lock_files[*]}; add a line with its archive and SHA-256"
  read -r _ archive sha256 <<<"$entry"

  file=$work/$(basename "$archive")
  echo "provision-sdk: downloading $package $revision ($archive)"
  curl -fL --retry 3 --silent --show-error -o "$file" "$repository/$archive" ||
    die "the download of $repository/$archive failed"
  echo "$sha256  $file" | sha256sum --check --status - ||
    die "the SHA-256 of $archive is not $sha256; the SDK did not change"

  rm -rf "$work/unpacked"
  unzip -q "$file" -d "$work/unpacked"
  rm -f "$file"
  [ -d "$work/unpacked/$top" ] || die "$archive has no $top directory"
  have=$(installed_revision "$work/unpacked/$top")
  [ "$have" = "$revision" ] || die "$archive contains revision ${have:-unknown}, not $revision"

  # Replace the whole package directory. This also replaces a package
  # from sdkmanager, so the pin applies to an existing SDK too.
  mkdir -p "$(dirname "$target")"
  rm -rf "$target.old"
  if [ -e "$target" ]; then
    mv "$target" "$target.old"
  fi
  mv "$work/unpacked/$top" "$target"
  rm -rf "$target.old"
  echo "provision-sdk: installed $package $revision"
}

echo "provision-sdk: the Android SDK License applies to these archives:"
echo "provision-sdk:   https://developer.android.com/studio/terms"

install_package emulator "$emulator_revision" "$sdk/emulator" emulator
install_package platform-tools "$platform_tools_revision" "$sdk/platform-tools" platform-tools
install_package "$image_package" "$image_revision" "$image_dir" "$abi"
# The appliance does not use the command-line tools (sdkmanager and
# avdmanager). Install them only on request.
if [ -n "$cmdline_tools_revision" ]; then
  install_package cmdline-tools "$cmdline_tools_revision" "$sdk/cmdline-tools/latest" cmdline-tools
fi

# The appliance user runs the SDK programs. An SDK from sdkmanager can
# still have programs with mode 0744.
chmod -R a+rX "$sdk"
if [ -z "$root" ] && command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled &&
  command -v restorecon >/dev/null 2>&1; then
  restorecon -R "$sdk"
fi

check_sdk

echo "provision-sdk: SDK ready at $sdk (API $api, $image_tag, $abi)"
