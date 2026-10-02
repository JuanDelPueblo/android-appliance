#!/usr/bin/env bash
# provision-sdk.sh: install the Android SDK without a package manager.
#
# Run as root: ./scripts/provision-sdk.sh
#
# It downloads the command-line tools, accepts the Android SDK license,
# and installs the emulator, platform-tools and the configured system
# image under APPLIANCE_ANDROID_HOME (default /opt/android-sdk).
# Running it again updates the SDK components.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=src/appliance-env
source "$here/src/appliance-env"

sdk=${APPLIANCE_ANDROID_HOME:-/opt/android-sdk}
api=${APPLIANCE_API_LEVEL:-36}
image_tag=${AVD_TAG:-google_apis_playstore}
abi=x86_64
url=${CMDLINE_TOOLS_URL:-https://dl.google.com/android/repository/commandlinetools-linux-16111833_latest.zip}

die() {
  echo "provision-sdk: $*" >&2
  exit 1
}

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

[ -x "$sdk/emulator/emulator" ] || die "the emulator binary is missing after the install"
[ -x "$sdk/platform-tools/adb" ] || die "the adb binary is missing after the install"

# The emulator needs host libraries that a minimal server can lack.
for bin in "$sdk/emulator/emulator" "$sdk"/emulator/qemu/*/qemu-system-*; do
  if [ -x "$bin" ]; then
    libs=$(ldd "$bin" 2>&1 | grep 'not found' || true)
    if [ -n "$libs" ]; then
      echo "provision-sdk: $bin misses these host libraries:" >&2
      printf '%s\n' "$libs" | sed 's/^/  /' >&2
      die "install them and re-run; dnf provides names their packages"
    fi
  fi
done

echo "provision-sdk: SDK ready at $sdk (API $api, $image_tag, $abi)"
echo "provision-sdk: set APPLIANCE_ANDROID_HOME=$sdk in /etc/android-appliance/appliance.conf"
