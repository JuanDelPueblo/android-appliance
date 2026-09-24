#!/usr/bin/env bash
# Tests for avd-init: AVD creation, kept userdata and managed keys.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export HOME=$tmp/home ANDROID_AVD_HOME=$tmp/avd AVD_NAME=android AVD_API=36
export AVD_SYSTEM_IMAGE=system-images/android-36/google_apis_playstore/x86_64
export AVD_TAG=google_apis_playstore AVD_RAM_MIB=2048 AVD_CORES=2 AVD_DISK_SIZE=4G
config=$ANDROID_AVD_HOME/android.avd/config.ini

"$here/../src/avd-init" | grep -q "creating AVD"
grep -qx "path=$ANDROID_AVD_HOME/android.avd" "$ANDROID_AVD_HOME/android.ini"
grep -qx "image.sysdir.1=$AVD_SYSTEM_IMAGE/" "$config"
grep -qx "hw.ramSize=2048" "$config"
grep -qx "saveOnExit=true" "$ANDROID_AVD_HOME/android.avd/quickbootChoice.ini"

# The emulator writes the size with a unit. An equal value stays as it is.
sed -i 's/^hw.ramSize=.*/hw.ramSize=2048M/' "$config"
touch "$ANDROID_AVD_HOME/android.avd/userdata-qemu.img"
"$here/../src/avd-init" | grep -q "keeping userdata"
grep -qx "hw.ramSize=2048M" "$config"
[ -e "$ANDROID_AVD_HOME/android.avd/userdata-qemu.img" ]

# A changed option is applied.
AVD_CORES=4 "$here/../src/avd-init" >/dev/null
grep -qx "hw.cpu.ncore=4" "$config"
[ "$(grep -c '^hw.cpu.ncore=' "$config")" = 1 ]
echo "avd-init tests passed"
