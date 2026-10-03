#!/usr/bin/env bash
# provision-sdk.sh: install the Android SDK without a package manager.
#
# Run as root: ./scripts/provision-sdk.sh
# Check an installed SDK and change nothing: ./scripts/provision-sdk.sh --check
# Staging mode for tests: ./scripts/provision-sdk.sh --root DIR --check
#
# It downloads the command-line tools, accepts the Android SDK license,
# and installs the emulator, platform-tools and the configured system
# image under APPLIANCE_ANDROID_HOME (default /opt/android-sdk).
# Running it again updates the SDK components.
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
url=${CMDLINE_TOOLS_URL:-https://dl.google.com/android/repository/commandlinetools-linux-16111833_latest.zip}

die() {
  echo "provision-sdk: $*" >&2
  exit 1
}

# check_sdk: make sure that the SDK can run the appliance emulator.
check_sdk() {
  [ -x "$sdk/emulator/emulator" ] || die "the emulator binary is missing in $sdk"
  [ -x "$sdk/platform-tools/adb" ] || die "the adb binary is missing in $sdk"
  [ -d "$sdk/system-images/android-$api/$image_tag/$abi" ] ||
    die "the system image android-$api;$image_tag;$abi is missing in $sdk"

  # The emulator runs as APPLIANCE_USER, not as the owner of the SDK.
  # sdkmanager installs the programs with mode 0744, which only the
  # owner can run.
  local denied
  denied=$(find "$sdk/emulator" "$sdk/platform-tools" "$sdk/system-images/android-$api/$image_tag/$abi" \
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

if [ "$check_only" = 1 ]; then
  check_sdk
  echo "provision-sdk: the SDK at $sdk is ready (API $api, $image_tag, $abi)"
  exit 0
fi

[ -z "$root" ] || die "--root works only with --check"
[ "$(id -u)" = 0 ] || die "run this script as root"
for tool in curl unzip java; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is missing; on Fedora run: dnf install $tool unzip java-25-openjdk-headless"
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "provision-sdk: downloading $url"
curl -fL "$url" -o "$work/tools.zip"
mkdir -p "$sdk/cmdline-tools"
unzip -q "$work/tools.zip" -d "$work/unpacked"
rm -rf "$sdk/cmdline-tools/latest"
mv "$work/unpacked/cmdline-tools" "$sdk/cmdline-tools/latest"
sdkmanager=$sdk/cmdline-tools/latest/bin/sdkmanager

echo "provision-sdk: accepting the Android SDK license"
yes | "$sdkmanager" --sdk_root="$sdk" --licenses >/dev/null

echo "provision-sdk: installing platform-tools, emulator and the API $api image"
"$sdkmanager" --sdk_root="$sdk" "platform-tools" "emulator" "system-images;android-$api;$image_tag;$abi"

# The appliance user runs the SDK programs; sdkmanager gives them 0744.
chmod -R a+rX "$sdk"

check_sdk

echo "provision-sdk: SDK ready at $sdk (API $api, $image_tag, $abi)"
echo "provision-sdk: set APPLIANCE_ANDROID_HOME=$sdk in /etc/android-appliance/appliance.conf"
