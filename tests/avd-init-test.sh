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

# The emulator rewrites config.ini as "key = value". An equal value stays.
sed -i 's/^hw.ramSize=.*/hw.ramSize = 2048M/' "$config"
sed -i 's/^hw.cpu.ncore=.*/hw.cpu.ncore = 2/' "$config"
touch "$ANDROID_AVD_HOME/android.avd/userdata-qemu.img"
"$here/../src/avd-init" | grep -q "keeping userdata"
grep -qx "hw.ramSize = 2048M" "$config"
[ "$(grep -c '^hw.ramSize' "$config")" = 1 ]
[ -e "$ANDROID_AVD_HOME/android.avd/userdata-qemu.img" ]

# A changed option replaces the spaced form and adds no duplicate.
AVD_RAM_MIB=3072 AVD_CORES=4 "$here/../src/avd-init" >/dev/null
grep -qx "hw.ramSize=3072" "$config"
[ "$(grep -c '^hw.ramSize' "$config")" = 1 ]
grep -qx "hw.cpu.ncore=4" "$config"
[ "$(grep -c '^hw.cpu.ncore' "$config")" = 1 ]
echo "avd-init tests passed"
