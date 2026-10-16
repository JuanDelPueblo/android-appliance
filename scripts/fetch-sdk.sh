#!/usr/bin/env bash
# fetch-sdk.sh: install the pinned Android SDK packages of conf/sdk.lock.
# The Containerfile runs it during the image build.
#
# Usage: scripts/fetch-sdk.sh [--sdk DIR] [--lock FILE] PART...
#
# PART is "tools" (the emulator and platform-tools) or "image" (the
# system image of API_LEVEL). The Containerfile installs them in two
# steps, so images for two API levels share the tools layer. DIR
# defaults to /opt/android-sdk, FILE to conf/sdk.lock.
#
# The environment selects the revisions, like the build arguments of the
# same names. An empty value selects the first lock line of the package.
#   API_LEVEL                    system image API level (default 36)
#   SDK_REPOSITORY               mirror (default https://dl.google.com/android/repository)
#   SDK_EMULATOR_VERSION         emulator revision, for example 37.2.12
#   SDK_PLATFORM_TOOLS_VERSION   platform-tools revision
#   SDK_IMAGE_REVISION           system image revision
#
# Each archive must match the SHA-256 of its lock line, and its
# source.properties must have the pinned Pkg.Revision.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
sdk=/opt/android-sdk
lock=$here/conf/sdk.lock
parts=()
while [ $# -gt 0 ]; do
  case $1 in
    --sdk)
      sdk=${2:?--sdk needs a directory}
      shift 2
      ;;
    --lock)
      lock=${2:?--lock needs a file}
      shift 2
      ;;
    tools | image)
      parts+=("$1")
      shift
      ;;
    *)
      echo "usage: fetch-sdk.sh [--sdk DIR] [--lock FILE] tools|image..." >&2
      exit 2
      ;;
  esac
done
if [ ${#parts[@]} = 0 ]; then
  echo "usage: fetch-sdk.sh [--sdk DIR] [--lock FILE] tools|image..." >&2
  exit 2
fi

api=${API_LEVEL:-36}
image_tag=google_apis_playstore
abi=x86_64
repository=${SDK_REPOSITORY:-https://dl.google.com/android/repository}

die() {
  echo "fetch-sdk: $*" >&2
  exit 1
}

case $api in
  '' | *[!0-9]*) die "API_LEVEL '$api' is not a number" ;;
esac
[ -f "$lock" ] || die "the lock file $lock is missing"
for tool in curl unzip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is missing"
done

# lock_entry PACKAGE [REVISION]: print "revision archive sha256" of the
# first lock line that matches. Without REVISION, the first line of the
# package gives the default revision.
lock_entry() {
  awk -v p="$1" -v r="${2:-}" '
    $1 == p && (r == "" || $2 == r) { print $2, $3, $4; found = 1; exit }
    END { exit !found }' "$lock"
}

# pinned_revision PACKAGE VARIABLE: print the revision that VARIABLE
# selects, or the default revision of the lock.
pinned_revision() {
  local package=$1 variable=$2 value entry
  value=${!variable:-}
  case $value in
    '') ;;
    *[!0-9.]*) die "$variable '$value' is not a revision (for example 37.2.12)" ;;
    *)
      echo "$value"
      return
      ;;
  esac
  entry=$(lock_entry "$package") || die "no revision of $package is in $lock"
  echo "${entry%% *}"
}

# installed_revision DIR: print Pkg.Revision of the package in DIR.
installed_revision() {
  [ -f "$1/source.properties" ] || return 0
  sed -n 's/^Pkg\.Revision=//p' "$1/source.properties" | head -n 1
}

# Each line: package, revision, directory in the SDK, top directory in
# the archive. Every pin is checked before the first download, so a bad
# value stops early.
packages=()
for part in "${parts[@]}"; do
  case $part in
    tools)
      revision=$(pinned_revision emulator SDK_EMULATOR_VERSION)
      packages+=("emulator $revision emulator emulator")
      revision=$(pinned_revision platform-tools SDK_PLATFORM_TOOLS_VERSION)
      packages+=("platform-tools $revision platform-tools platform-tools")
      ;;
    image)
      package="system-images;android-$api;$image_tag;$abi"
      revision=$(pinned_revision "$package" SDK_IMAGE_REVISION)
      packages+=("$package $revision system-images/android-$api/$image_tag/$abi $abi")
      ;;
  esac
done
for line in "${packages[@]}"; do
  read -r package revision _ <<<"$line"
  lock_entry "$package" "$revision" >/dev/null ||
    die "$package $revision is not in $lock; add a line with its archive and SHA-256"
done

# Download and unpack next to the SDK, not in /tmp: the archives are
# large, and the final move stays on one file system.
umask 022
mkdir -p "$sdk"
work=$sdk/.fetch
rm -rf "$work"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

# install_package PACKAGE REVISION TARGET TOP: install the pinned archive
# of PACKAGE into SDK/TARGET. TOP is the top directory in the archive.
install_package() {
  local package=$1 revision=$2 target=$sdk/$3 top=$4
  local archive sha256 file have

  have=$(installed_revision "$target")
  if [ "$have" = "$revision" ]; then
    echo "fetch-sdk: $package $revision is installed"
    return
  fi
  read -r _ archive sha256 <<<"$(lock_entry "$package" "$revision")"

  file=$work/$(basename "$archive")
  echo "fetch-sdk: downloading $package $revision ($archive)"
  curl -fL --retry 3 --silent --show-error -o "$file" "$repository/$archive" ||
    die "the download of $repository/$archive failed"
  echo "$sha256  $file" | sha256sum --check --status - ||
    die "the SHA-256 of $archive is not $sha256"

  rm -rf "$work/unpacked"
  unzip -q "$file" -d "$work/unpacked"
  rm -f "$file"
  [ -d "$work/unpacked/$top" ] || die "$archive has no $top directory"
  have=$(installed_revision "$work/unpacked/$top")
  [ "$have" = "$revision" ] || die "$archive contains revision ${have:-unknown}, not $revision"

  # The appliance user runs the SDK programs; the archives can have
  # programs that only their owner can run.
  chmod -R a+rX "$work/unpacked/$top"
  mkdir -p "$(dirname "$target")"
  rm -rf "$target"
  mv "$work/unpacked/$top" "$target"
  echo "fetch-sdk: installed $package $revision"
}

echo "fetch-sdk: the Android SDK License applies to these archives:"
echo "fetch-sdk:   https://developer.android.com/studio/terms"
for line in "${packages[@]}"; do
  read -r package revision target top <<<"$line"
  install_package "$package" "$revision" "$target" "$top"
done
