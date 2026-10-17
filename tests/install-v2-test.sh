#!/usr/bin/env bash
# Tests for install.sh (v2): staged installs with --root. They check the
# generated Quadlet unit (also with the real Quadlet generator when
# Podman is installed), the configuration, the image state, --rollback,
# --uninstall, the --purge safeguards, --print-run and the coexistence
# with a v1 appliance. They never run podman, systemctl or loginctl.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
installer=$repo/install.sh
version=$(cat "$repo/VERSION")
user=$(id -un)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# The fake user "tester" owns every staged path.
unset "${!APPLIANCE_@}" XDG_CONFIG_HOME XDG_DATA_HOME ANDROID_APPLIANCE_CONFIG
export HOME=/home/tester XDG_RUNTIME_DIR=/run/user/tester

fail() {
  echo "FAIL $*" >&2
  exit 1
}

# A fresh staging root with world-accessible devices.
new_root() {
  root=$tmp/$1
  mkdir -p "$root/dev/dri"
  touch "$root/dev/kvm" "$root/dev/dri/renderD128"
  chmod 0666 "$root/dev/kvm" "$root/dev/dri/renderD128"
  conf=$root/home/tester/.config/android-appliance/appliance.conf
  unit=$root/home/tester/.config/containers/systemd/android-appliance.container
  images=$root/home/tester/.config/android-appliance/android-appliance.images
  state=$root/home/tester/.local/share/android-appliance
}

stage() { bash "$installer" --root "$root" "$@"; }

# stage_fails ARGS...: the installer must fail with exit status 1.
stage_fails() {
  local rc=0
  stage "$@" >"$tmp/out" 2>&1 || rc=$?
  [ "$rc" = 1 ] || fail "install.sh $* exited with $rc, not 1: $(cat "$tmp/out")"
}

has() {
  grep -qxF -- "$1" "$2" || fail "missing '$1' in ${2#"$tmp"/}"
}

lacks() {
  if grep -qE -- "$1" "$2"; then fail "'$1' must not be in ${2#"$tmp"/}"; fi
}

generator=
for file in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
  if [ -x "$file" ]; then
    generator=$file
    break
  fi
done

# The default installation: the example configuration and its defaults.
new_root default
stage >"$tmp/out" 2>&1 || fail "the default staging failed: $(cat "$tmp/out")"
cmp -s "$conf" "$repo/conf/appliance.conf.example" || fail "the configuration is not the example"
grep -q "created /home/tester/.config/android-appliance/appliance.conf from the example" "$tmp/out" ||
  fail "the installer did not report the new configuration"
for line in \
  "# Managed by install.sh. Edit /home/tester/.config/android-appliance/appliance.conf and run install.sh again." \
  "ContainerName=android-appliance" \
  "Image=localhost/android-appliance:$version-api36" \
  "Pull=never" \
  "UserNS=keep-id" \
  "DropCapability=all" \
  "NoNewPrivileges=true" \
  "ReadOnly=true" \
  "Tmpfs=/tmp:rw,size=512m" \
  "Ulimit=core=0" \
  "RunInit=true" \
  "Timezone=local" \
  "AddDevice=/dev/kvm" \
  "AddDevice=/dev/dri/renderD128" \
  "Volume=/home/tester/.local/share/android-appliance:/data:Z,nosuid,nodev" \
  "Volume=%t/android-appliance:/run/appliance:Z" \
  "PublishPort=127.0.0.1:6090:8080/tcp" \
  "PublishPort=127.0.0.1:3478:3478/udp" \
  "PublishPort=127.0.0.1:3478:3478/tcp" \
  "EnvironmentFile=/home/tester/.config/android-appliance/appliance.conf" \
  "Environment=APPLIANCE_LISTEN_ADDRESS=127.0.0.1 APPLIANCE_PORT=6090 APPLIANCE_TURN_PORT=3478 APPLIANCE_GPU=auto" \
  "StopTimeout=150" \
  "HealthCmd=/opt/appliance/venv/bin/python -m appliance healthcheck" \
  "Environment=CONTAINERS_CONF_OVERRIDE=/home/tester/.config/android-appliance/android-appliance.containers.conf" \
  "ExecStartPre=/usr/bin/install -d -m 0700 %t/android-appliance" \
  "Restart=on-failure" \
  "TimeoutStartSec=300" \
  "TimeoutStopSec=180" \
  "WantedBy=default.target"; do
  has "$line" "$unit"
done
lacks '^GroupAdd=' "$unit"
echo "ok   unit with every podman option"

has "label = true" "$root/home/tester/.config/android-appliance/android-appliance.containers.conf"
[ "$(stat -c %a "$state")" = 700 ] || fail "the state directory is not mode 0700"
[ "$(stat -c %a "$root/run/user/tester/android-appliance")" = 700 ] || fail "the runtime directory is not mode 0700"
[ "$(readlink "$root/home/tester/.local/bin/androidctl")" = "$repo/bin/androidctl" ] || fail "androidctl is not linked"
has "CURRENT_IMAGE=localhost/android-appliance:$version-api36" "$images"
has "CURRENT_BUILT=1" "$images"
grep -q "loginctl enable-linger $user" "$tmp/out" || fail "no warning about lingering"
echo "ok   override file, directories, link, image state and linger warning"

if [ -n "$generator" ]; then
  QUADLET_UNIT_DIRS=$(dirname "$unit") "$generator" -dryrun -user >"$tmp/generated" 2>"$tmp/generator.err" ||
    fail "the Quadlet generator rejected the unit: $(cat "$tmp/generator.err")"
  exec_start=$(grep '^ExecStart=' "$tmp/generated")
  for option in '--name android-appliance' '--userns keep-id' '--cap-drop all' '--read-only' \
    '--security-opt=no-new-privileges' '--init' '--ulimit core=0' '--tz local' '--pull never' \
    '--stop-timeout 150' '--device /dev/kvm' '--device /dev/dri/renderD128' '--tmpfs /tmp:rw,size=512m' \
    '-v /home/tester/.local/share/android-appliance:/data:Z,nosuid,nodev' '--publish 127.0.0.1:3478:3478/udp' \
    '--env-file /home/tester/.config/android-appliance/appliance.conf' '--health-cmd'; do
    case $exec_start in
      *" $option "*) ;;
      *) fail "the generated podman run lacks '$option': $exec_start" ;;
    esac
  done
  echo "ok   the Quadlet generator accepts the unit"
else
  echo "skip Quadlet generator check (Podman is not installed)"
fi

# A second run keeps an edited configuration and applies it.
sed -i 's/^APPLIANCE_PORT=6090$/APPLIANCE_PORT=46090/' "$conf"
stage >"$tmp/out" 2>&1 || fail "the second staging failed: $(cat "$tmp/out")"
has "APPLIANCE_PORT=46090" "$conf"
has "PublishPort=127.0.0.1:46090:8080/tcp" "$unit"
if grep -q 'from the example' "$tmp/out"; then fail "the second run recreated the configuration"; fi
# The environment wins over the file.
APPLIANCE_PORT=46001 stage >/dev/null 2>&1
has "PublishPort=127.0.0.1:46001:8080/tcp" "$unit"
has "Environment=APPLIANCE_LISTEN_ADDRESS=127.0.0.1 APPLIANCE_PORT=46001 APPLIANCE_TURN_PORT=3478 APPLIANCE_GPU=auto" "$unit"
echo "ok   re-run keeps the configuration, environment wins"

# Lingering on: no warning.
mkdir -p "$root/var/lib/systemd/linger"
touch "$root/var/lib/systemd/linger/$user"
stage >"$tmp/out" 2>&1
if grep -q 'lingering' "$tmp/out"; then fail "warned about lingering although it is on"; fi
echo "ok   no linger warning with lingering on"

# A configuration file of its own, another instance name, IPv6, software
# rendering, and a device that the user reaches through a group.
new_root custom
chmod 0660 "$root/dev/kvm"
mkdir -p "$root/srv/aa"
cat >"$root/srv/aa/appliance.conf" <<'EOF'
# A second instance.
APPLIANCE_NAME=aa-test
APPLIANCE_STATE_DIR=/srv/aa/state
APPLIANCE_LISTEN_ADDRESS=::1
APPLIANCE_PORT=46090
APPLIANCE_TURN_PORT=46478
APPLIANCE_GPU=lavapipe
APPLIANCE_BIN_DIR=/srv/aa/bin
APPLIANCE_QUADLET_DIR=/srv/aa/units
EOF
stage --config /srv/aa/appliance.conf >"$tmp/out" 2>&1 || fail "the custom staging failed: $(cat "$tmp/out")"
unit=$root/srv/aa/units/aa-test.container
for line in \
  "ContainerName=aa-test" \
  "AddDevice=/dev/kvm" \
  "GroupAdd=keep-groups" \
  "Volume=/srv/aa/state:/data:Z,nosuid,nodev" \
  "Volume=%t/aa-test:/run/appliance:Z" \
  "PublishPort=[::1]:46090:8080/tcp" \
  "PublishPort=[::1]:46478:3478/udp" \
  "PublishPort=[::1]:46478:3478/tcp" \
  "EnvironmentFile=/srv/aa/appliance.conf" \
  "Environment=APPLIANCE_LISTEN_ADDRESS=::1 APPLIANCE_PORT=46090 APPLIANCE_TURN_PORT=46478 APPLIANCE_GPU=lavapipe" \
  "Environment=CONTAINERS_CONF_OVERRIDE=/srv/aa/aa-test.containers.conf" \
  "ExecStartPre=/usr/bin/install -d -m 0700 %t/aa-test"; do
  has "$line" "$unit"
done
lacks 'renderD' "$unit"
[ -d "$root/srv/aa/state" ] && [ -f "$root/srv/aa/aa-test.containers.conf" ] && [ -f "$root/srv/aa/aa-test.images" ] ||
  fail "the custom instance files are missing"
[ -L "$root/srv/aa/bin/androidctl" ] || fail "androidctl is not linked into APPLIANCE_BIN_DIR"
grep -q 'ANDROID_APPLIANCE_CONFIG=/srv/aa/appliance.conf' "$tmp/out" || fail "no hint for the configuration of androidctl"
grep -q 'dashboard: *http://\[::1\]:46090/' "$tmp/out" || fail "the dashboard URL is wrong: $(cat "$tmp/out")"
if [ -n "$generator" ]; then
  QUADLET_UNIT_DIRS=$root/srv/aa/units "$generator" -dryrun -user >/dev/null 2>&1 ||
    fail "the Quadlet generator rejected the custom unit"
fi
echo "ok   custom configuration, IPv6, lavapipe, keep-groups"

# Render node rules.
new_root render
rm "$root/dev/dri/renderD128"
APPLIANCE_GPU=host stage_fails
grep -q 'needs a render node' "$tmp/out" || fail "host without a render node: $(cat "$tmp/out")"
[ ! -e "$unit" ] || fail "a failed install wrote the unit"
stage >/dev/null 2>&1
lacks 'renderD' "$unit"
APPLIANCE_GPU_DEVICE=/dev/dri/renderD129 stage_fails
touch "$root/dev/dri/renderD129"
chmod 0666 "$root/dev/dri/renderD129"
APPLIANCE_GPU_DEVICE=/dev/dri/renderD129 stage >/dev/null 2>&1
has "AddDevice=/dev/dri/renderD129" "$unit"
if [ "$(id -u)" != 0 ]; then
  chmod 0000 "$root/dev/dri/renderD129"
  APPLIANCE_GPU_DEVICE=/dev/dri/renderD129 stage >"$tmp/out" 2>&1
  lacks 'renderD' "$unit"
  grep -q 'renders in software' "$tmp/out" || fail "no warning about an unusable render node"
  APPLIANCE_GPU=host APPLIANCE_GPU_DEVICE=/dev/dri/renderD129 stage_fails
fi
echo "ok   render node selection"

# A wrong configuration stops the installer before it changes a file.
for bad in 'APPLIANCE_PORT=70000' 'APPLIANCE_PORT=3478' 'APPLIANCE_STATE_DIR=relative/dir' \
  'APPLIANCE_STATE_DIR=/srv/with space' 'APPLIANCE_STATE_DIR=/srv/100%' 'APPLIANCE_STATE_DIR=/srv/a:b' \
  'APPLIANCE_GPU=swiftshader' 'APPLIANCE_NAME=bad/name' 'APPLIANCE_LISTEN_ADDRESS=localhost' \
  'APPLIANCE_API_LEVEL=thirty' 'PATH=/usr/bin' 'this line has no assignment' ' APPLIANCE_PORT=1'; do
  new_root bad
  mkdir -p "$(dirname "$conf")"
  printf '%s\n' "$bad" >"$conf"
  stage_fails
  [ ! -e "$root/home/tester/.config/containers" ] && [ ! -e "$state" ] ||
    fail "'$bad' stopped the installer after it changed files"
  rm -rf "$root"
done
echo "ok   a wrong configuration stops before any change"

# The state directory must be a directory of its own: the container
# relabels it.
for shared in / /var /home /home/tester /home/tester/.local/share /srv /etc /usr /tmp /root /run /var/lib; do
  new_root shared
  mkdir -p "$(dirname "$conf")"
  echo "APPLIANCE_STATE_DIR=$shared" >"$conf"
  stage_fails
  grep -q 'is not a dedicated directory' "$tmp/out" || fail "$shared: $(cat "$tmp/out")"
  [ ! -e "$root/home/tester/.config/containers" ] || fail "$shared: the unit was written"
  rm -rf "$root"
done
echo "ok   a shared state directory stops the installer"

# Image state and --rollback.
new_root rollback
APPLIANCE_IMAGE=localhost/aa:one stage >/dev/null 2>&1
APPLIANCE_IMAGE=localhost/aa:two stage >/dev/null 2>&1
has "CURRENT_IMAGE=localhost/aa:two" "$images"
has "PREVIOUS_IMAGE=localhost/aa:one" "$images"
stage --rollback >/dev/null 2>&1
has "Image=localhost/aa:one" "$unit"
has "CURRENT_IMAGE=localhost/aa:one" "$images"
has "PREVIOUS_IMAGE=localhost/aa:two" "$images"
stage --rollback >/dev/null 2>&1
has "Image=localhost/aa:two" "$unit"
stage --rollback >/dev/null 2>&1
APPLIANCE_IMAGE=localhost/aa:three stage >"$tmp/out" 2>&1
grep -qx 'install.sh: removing the image localhost/aa:two' "$tmp/out" || fail "the old image was not removed: $(cat "$tmp/out")"
has "CURRENT_IMAGE=localhost/aa:three" "$images"
has "PREVIOUS_IMAGE=localhost/aa:one" "$images"
# An image that another unit runs stays.
printf '[Container]\nImage=localhost/aa:one\n' >"$(dirname "$unit")/other.container"
APPLIANCE_IMAGE=localhost/aa:four stage >"$tmp/out" 2>&1
grep -q 'kept the image localhost/aa:one, which .*other.container uses' "$tmp/out" || fail "removed an image of another unit"
rm "$(dirname "$unit")/other.container"
# install.sh removes only the images that it built.
for tag in five six seven; do APPLIANCE_IMAGE=localhost/aa:$tag stage --no-build >"$tmp/out" 2>&1; done
if grep -q 'removing the image' "$tmp/out"; then fail "removed an image that install.sh did not build"; fi
has "CURRENT_BUILT=0" "$images"
new_root no-previous
stage >/dev/null 2>&1
stage_fails --rollback
grep -q 'no previous image' "$tmp/out" || fail "rollback without a previous image: $(cat "$tmp/out")"
echo "ok   image state and rollback"

# --uninstall removes what install.sh wrote and keeps the state and the
# configuration.
new_root uninstall
stage >/dev/null 2>&1
echo userdata >"$state/userdata.img"
stage --uninstall >"$tmp/out" 2>&1 || fail "uninstall failed: $(cat "$tmp/out")"
for path in "$unit" "$images" "$root/home/tester/.config/android-appliance/android-appliance.containers.conf" \
  "$root/home/tester/.local/bin/androidctl" "$root/run/user/tester/android-appliance"; do
  if [ -e "$path" ] || [ -L "$path" ]; then fail "--uninstall left ${path#"$root"}"; fi
done
[ -f "$state/userdata.img" ] && [ -f "$conf" ] || fail "--uninstall removed the state or the configuration"
grep -qx "install.sh: removing the image localhost/android-appliance:$version-api36" "$tmp/out" ||
  fail "--uninstall did not remove the image: $(cat "$tmp/out")"
grep -q 'kept the state directory' "$tmp/out" || fail "--uninstall did not report the kept state"
echo "ok   uninstall keeps the state and the configuration"

# Files that install.sh did not write stay.
new_root foreign
mkdir -p "$(dirname "$unit")" "$root/home/tester/.local/bin"
printf '[Container]\nImage=localhost/mine\n' >"$unit"
stage_fails
grep -q 'install.sh did not write it' "$tmp/out" || fail "overwrote a foreign unit: $(cat "$tmp/out")"
# --uninstall of a foreign unit changes nothing: not the runtime directory,
# the images, the link or the configuration.
mkdir -p "$root/run/user/tester/android-appliance"
touch "$root/run/user/tester/android-appliance/api.sock"
stage_fails --uninstall
grep -q 'install.sh did not write it' "$tmp/out" || fail "--uninstall of a foreign unit: $(cat "$tmp/out")"
[ -f "$unit" ] && [ -f "$root/run/user/tester/android-appliance/api.sock" ] && [ -f "$conf" ] ||
  fail "--uninstall changed files next to a foreign unit"
rm "$unit"
# The failed install linked androidctl; replace the link by a file of the user
# (never write through the link: it points into this checkout).
rm "$root/home/tester/.local/bin/androidctl"
echo mine >"$root/home/tester/.local/bin/androidctl"
stage >"$tmp/out" 2>&1
grep -q 'kept /home/tester/.local/bin/androidctl' "$tmp/out" || fail "no warning about a foreign androidctl"
stage --uninstall >/dev/null 2>&1
[ "$(cat "$root/home/tester/.local/bin/androidctl")" = mine ] || fail "replaced or removed a foreign androidctl"
echo "ok   foreign unit and androidctl stay"

# A link of someone else (the v1 command, a wrapper) is not replaced and not
# removed either; a link to this checkout is.
new_root foreign-link
mkdir -p "$root/home/tester/.local/bin" "$root/usr/local/bin" "$tmp/v1/bin"
printf '#!/bin/sh\n' >"$root/usr/local/bin/androidctl"
printf '#!/bin/sh\n' >"$tmp/v1/bin/androidctl"
bin=$root/home/tester/.local/bin/androidctl
for target in "$root/usr/local/bin/androidctl" "$tmp/v1/bin/androidctl" "$tmp/nowhere/bin/androidctl"; do
  ln -sfn "$target" "$bin"
  stage >"$tmp/out" 2>&1
  [ "$(readlink "$bin")" = "$target" ] || fail "replaced the link to $target"
  grep -q 'kept /home/tester/.local/bin/androidctl' "$tmp/out" || fail "no warning about the link to $target"
  stage --uninstall >/dev/null 2>&1
  [ "$(readlink "$bin")" = "$target" ] || fail "--uninstall removed the link to $target"
  stage >/dev/null 2>&1
done
ln -sfn "$repo/bin/androidctl" "$bin"
stage >"$tmp/out" 2>&1
if grep -q 'kept /home/tester/.local/bin/androidctl' "$tmp/out"; then fail "warned about the link to this checkout"; fi
stage --uninstall >/dev/null 2>&1
[ ! -e "$bin" ] && [ ! -L "$bin" ] || fail "--uninstall kept the link to this checkout"
echo "ok   links to other androidctl commands stay"

# --purge works only with --uninstall, then deletes the state and the
# configuration.
new_root purge
stage >/dev/null 2>&1
rc=0
stage --purge >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] || fail "--purge without --uninstall exited with $rc"
echo userdata >"$state/userdata.img"
stage --uninstall --purge >"$tmp/out" 2>&1 || fail "purge failed: $(cat "$tmp/out")"
[ ! -e "$state" ] && [ ! -e "$conf" ] && [ ! -e "$(dirname "$conf")" ] || fail "--purge kept the state or the configuration"
echo "ok   uninstall --purge deletes the state and the configuration"

# --purge refuses every shared directory, before it changes anything.
for shared in / /var /var/lib /home /home/tester /srv /opt /etc /usr /tmp /root /run /home/tester/.local/share; do
  new_root purge-refused
  stage >/dev/null 2>&1
  mkdir -p "$root$shared"
  touch "$root$shared/keep"
  sed -i "1i APPLIANCE_STATE_DIR=$shared" "$conf"
  stage_fails --uninstall --purge
  grep -q 'delete it yourself' "$tmp/out" || fail "$shared: $(cat "$tmp/out")"
  [ -f "$root$shared/keep" ] && [ -f "$unit" ] || fail "--purge changed files for $shared"
  rm -rf "$root"
done
# HOME itself, also outside /home.
new_root purge-home
HOME=/srv/me stage --config /srv/me/aa/appliance.conf >/dev/null 2>&1
sed -i "1i APPLIANCE_STATE_DIR=/srv/me" "$root/srv/me/aa/appliance.conf"
HOME=/srv/me stage_fails --uninstall --purge --config /srv/me/aa/appliance.conf
[ -d "$root/srv/me" ] || fail "--purge deleted HOME"
# A directory of another user.
if [ "$(id -u)" = 0 ]; then
  new_root purge-owner
  stage >/dev/null 2>&1
  chown 65534 "$state"
  stage_fails --uninstall --purge
  grep -q 'is not owned by' "$tmp/out" || fail "purged a directory of another user: $(cat "$tmp/out")"
else
  echo "skip --purge of a directory of another user (needs root to create one)"
fi
echo "ok   uninstall --purge refuses shared directories"

# --uninstall names its instance by the configuration file. A mistyped
# --config must neither remove nor purge the default instance, and neither
# may it take its unit over.
new_root typo
stage >/dev/null 2>&1
echo userdata >"$state/userdata.img"
typo=/home/tester/.config/android-appliance/applaince.conf
for mode in --uninstall "--uninstall --purge"; do
  # shellcheck disable=SC2086  # split the mode on purpose
  stage_fails $mode --config "$typo"
  grep -q 'applaince.conf does not exist' "$tmp/out" || fail "$mode with a missing configuration: $(cat "$tmp/out")"
  [ -f "$unit" ] && [ -f "$state/userdata.img" ] && [ -f "$conf" ] && [ -f "$images" ] ||
    fail "$mode with a mistyped --config changed the default instance"
done
stage_fails --config "$typo"
grep -q 'belongs to the configuration /home/tester/.config/android-appliance/appliance.conf, not to ' "$tmp/out" ||
  fail "a mistyped --config took the unit over: $(cat "$tmp/out")"
has "EnvironmentFile=/home/tester/.config/android-appliance/appliance.conf" "$unit"
new_root no-config
stage_fails --uninstall
grep -q 'appliance.conf does not exist' "$tmp/out" || fail "--uninstall without a configuration: $(cat "$tmp/out")"
echo "ok   a missing or mistyped configuration changes nothing"

# A second configuration with the same name does not take over the unit.
new_root takeover
stage >/dev/null 2>&1
printf 'APPLIANCE_STATE_DIR=/home/tester/other\nAPPLIANCE_PORT=47000\nAPPLIANCE_TURN_PORT=47001\n' >"$root/home/tester/second.conf"
stage_fails --config /home/tester/second.conf
grep -q 'belongs to the configuration .*appliance.conf, not to /home/tester/second.conf' "$tmp/out" ||
  fail "a second configuration took the unit over: $(cat "$tmp/out")"
has "PublishPort=127.0.0.1:6090:8080/tcp" "$unit"
[ ! -e "$root/home/tester/other" ] || fail "the refused install created the state directory"
echo "ok   a configuration with the name of another instance is refused"

# Two instances do not share a state directory, and purging one never
# deletes the other's.
new_root shared-state
stage >/dev/null 2>&1
# The daemon has made its directory by now.
mkdir "$state/appliance"
echo userdata >"$state/userdata.img"
printf 'APPLIANCE_NAME=second\nAPPLIANCE_PORT=47000\nAPPLIANCE_TURN_PORT=47001\n' >"$root/home/tester/second.conf"
second_unit=$(dirname "$unit")/second.container
stage_fails --config /home/tester/second.conf
grep -q "state directory .* belongs to the appliance 'android-appliance'" "$tmp/out" ||
  fail "a second instance shared the state directory: $(cat "$tmp/out")"
[ ! -e "$second_unit" ] || fail "the refused install wrote a unit"
stage_fails --uninstall --purge --config /home/tester/second.conf
grep -q "belongs to the appliance 'android-appliance'" "$tmp/out" || fail "purge of the other instance: $(cat "$tmp/out")"
[ -f "$state/userdata.img" ] && [ -f "$unit" ] || fail "--purge of a second instance removed the first one"
# Renaming an installed instance needs its uninstall first; then the
# directory can move to the new name.
sed -i '1i APPLIANCE_NAME=renamed' "$conf"
stage_fails
grep -q "belongs to the appliance 'android-appliance'" "$tmp/out" || fail "renamed instance: $(cat "$tmp/out")"
sed -i '1d' "$conf"
stage --uninstall >/dev/null 2>&1
[ ! -e "$state/.android-appliance-instance" ] || fail "--uninstall kept the marker"
sed -i '1i APPLIANCE_NAME=renamed' "$conf"
stage >/dev/null 2>&1 || fail "the renamed instance could not adopt the kept state"
[ "$(cat "$state/.android-appliance-instance")" = renamed ] && [ -f "$state/userdata.img" ] || fail "the marker is wrong"
echo "ok   a state directory belongs to one instance"

# A directory with other contents is not a state directory: no relabelling,
# no purge. An empty one, an adopted appliance state and one with a mode of
# its own are fine, and the mode becomes 0700.
new_root documents
mkdir -p "$root/home/tester/Documents" "$(dirname "$conf")"
echo thesis >"$root/home/tester/Documents/thesis.txt"
echo "APPLIANCE_STATE_DIR=/home/tester/Documents" >"$conf"
stage_fails
grep -q 'is not empty and holds no appliance state' "$tmp/out" || fail "installed on Documents: $(cat "$tmp/out")"
[ ! -e "$unit" ] || fail "the refused install wrote a unit"
stage_fails --uninstall --purge
grep -q 'holds no appliance state; delete it yourself' "$tmp/out" || fail "purged Documents: $(cat "$tmp/out")"
[ -f "$root/home/tester/Documents/thesis.txt" ] || fail "--purge deleted Documents"
mkdir -p "$root/home/tester/adopted/avd"
echo "APPLIANCE_STATE_DIR=/home/tester/adopted" >"$conf"
stage >/dev/null 2>&1 || fail "an adopted state directory was refused"
mkdir -m 0755 "$root/home/tester/empty"
echo "APPLIANCE_STATE_DIR=/home/tester/empty" >"$conf"
stage >/dev/null 2>&1 || fail "an empty directory was refused"
[ "$(stat -c %a "$root/home/tester/empty")" = 700 ] || fail "an existing state directory kept its mode"
echo "ok   only an empty directory or an appliance state is accepted"

# Paths that lead to the same directory: a linked HOME (ostree), the
# directories of the installation itself.
new_root ostree
rm -rf "${root:?}/home"
mkdir -p "$root/var/home/tester"
ln -s var/home "$root/home"
mkdir -p "$(dirname "$conf")"
echo "APPLIANCE_STATE_DIR=/var/home/tester" >"$conf"
stage_fails
grep -q 'is not a dedicated directory' "$tmp/out" || fail "state directory = HOME behind a link: $(cat "$tmp/out")"
for inside in /home/tester/.config/android-appliance /home/tester/.config/containers/systemd /home/tester/.local/bin /home/tester/.config; do
  new_root inside
  mkdir -p "$(dirname "$conf")"
  echo "APPLIANCE_STATE_DIR=$inside" >"$conf"
  stage_fails
  grep -q "contains \|is not a dedicated" "$tmp/out" || fail "$inside: $(cat "$tmp/out")"
  rm -rf "$root"
done
new_root ostree-purge
stage >/dev/null 2>&1
rm -rf "$root/home/tester/.local/share"
mv "$root/home" "$root/var-home" && mkdir -p "$root/var" && mv "$root/var-home" "$root/var/home" && ln -s var/home "$root/home"
echo "APPLIANCE_STATE_DIR=/var/home/tester" >>"$root/var/home/tester/.config/android-appliance/appliance.conf"
stage_fails --uninstall --purge
[ -d "$root/var/home/tester" ] || fail "--purge deleted HOME behind a link"
echo "ok   links and installation directories do not pass as state directories"

# An AVD keeps its API level: refuse before the image is built.
new_root api-level
mkdir -p "$state/avd/android.avd"
printf 'image.sysdir.1 = system-images/android-35/google_apis_playstore/x86_64/\n' >"$state/avd/android.avd/config.ini"
stage_fails
grep -q 'uses API level 35, but APPLIANCE_API_LEVEL is 36' "$tmp/out" || fail "API level mismatch: $(cat "$tmp/out")"
[ ! -e "$unit" ] || fail "the refused install wrote a unit"
APPLIANCE_API_LEVEL=35 stage >/dev/null 2>&1 || fail "the matching API level was refused"
echo "ok   an AVD of another API level is refused"

# A listener on the network gets a warning about plain HTTP.
new_root listen
stage >"$tmp/out" 2>&1
if grep -q 'plain HTTP' "$tmp/out"; then fail "warned about the loopback address"; fi
APPLIANCE_LISTEN_ADDRESS=::1 stage >"$tmp/out" 2>&1
if grep -q 'plain HTTP' "$tmp/out"; then fail "warned about ::1"; fi
APPLIANCE_LISTEN_ADDRESS=0.0.0.0 stage >"$tmp/out" 2>&1
grep -q 'WARNING APPLIANCE_LISTEN_ADDRESS=0.0.0.0 publishes the API on the network as plain HTTP' "$tmp/out" ||
  fail "no warning about a public address: $(cat "$tmp/out")"
echo "ok   warning for a public listen address"

# Outside staging, install.sh checks the configuration with the image and
# fails when the daemon does not come up. Fakes stand in for podman,
# systemctl, curl and journalctl.
if [ "$(id -u)" != 0 ] && [ -n "$generator" ]; then
  live=$tmp/live
  mkdir -p "$live/bin" "$live/home" "$live/run" "$live/units"
  cat >"$live/bin/podman" <<'EOF'
#!/usr/bin/env bash
echo "podman $*" >>"$LIVE/calls"
case $1 in
  build) touch "$LIVE/built" ;;
  image)
    case $2 in
      inspect) [ -e "$LIVE/built" ] && echo sha256:new || exit 1 ;;
      exists) exit 0 ;;
    esac
    ;;
  run)
    # `python -m appliance check-config` of the image: it reads the same file.
    while [ "$#" -gt 0 ]; do
      [ "$1" != --env-file ] || file=$2
      shift
    done
    if grep -q '^APPLIANCE_MEMORY_MIB=lots' "$file"; then
      echo "appliance: APPLIANCE_MEMORY_MIB must be an integer of at least 512, not 'lots'" >&2
      exit 1
    fi
    ;;
esac
EOF
  cat >"$live/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >>"$LIVE/calls"
case "$*" in "--user show -p LoadState --value "*) echo loaded ;; esac
EOF
  cat >"$live/bin/curl" <<'EOF'
#!/usr/bin/env bash
[ -e "$LIVE/daemon-up" ]
EOF
  printf '#!/usr/bin/env bash\necho "journal line one"\necho "journal line two"\n' >"$live/bin/journalctl"
  chmod +x "$live/bin/podman" "$live/bin/systemctl" "$live/bin/curl" "$live/bin/journalctl"
  live_install() {
    LIVE=$live HOME=$live/home XDG_RUNTIME_DIR=$live/run PATH=$live/bin:$PATH APPLIANCE_INSTALL_WAIT=2 \
      APPLIANCE_QUADLET_DIR=$live/units APPLIANCE_BIN_DIR=$live/binlink APPLIANCE_STATE_DIR=$live/home/state \
      APPLIANCE_PORT=46190 APPLIANCE_TURN_PORT=46191 \
      bash "$installer" --config "$live/appliance.conf"
  }
  rc=0
  live_install >/dev/null 2>&1 || true
  echo APPLIANCE_MEMORY_MIB=lots >>"$live/appliance.conf"
  : >"$live/calls"
  live_install >"$tmp/out" 2>&1 || rc=$?
  [ "$rc" = 1 ] || fail "a rejected configuration: exit status $rc"
  grep -q "rejects $live/appliance.conf:" "$tmp/out" || fail "the rejection is not reported: $(cat "$tmp/out")"
  grep -q "APPLIANCE_MEMORY_MIB must be an integer of at least 512, not 'lots'" "$tmp/out" ||
    fail "the message of the daemon is missing: $(cat "$tmp/out")"
  if grep -q 'systemctl --user restart' "$live/calls"; then fail "the unit was restarted with a rejected configuration"; fi
  sed -i '/^APPLIANCE_MEMORY_MIB=lots$/d' "$live/appliance.conf"
  # No daemon: a failure with the journal, and no word about --rollback at the first image.
  rc=0
  live_install >"$tmp/out" 2>&1 || rc=$?
  [ "$rc" = 1 ] || fail "a daemon that does not answer: exit status $rc: $(cat "$tmp/out")"
  grep -q 'did not answer within 2s' "$tmp/out" || fail "no report of the wait: $(cat "$tmp/out")"
  grep -qx 'journal line two' "$tmp/out" || fail "no journal lines: $(cat "$tmp/out")"
  grep -q 'android-appliance did not start' "$tmp/out" || fail "no failure message: $(cat "$tmp/out")"
  if grep -q -- '--rollback' "$tmp/out"; then fail "advised --rollback without a previous image"; fi
  [ -f "$live/units/android-appliance.container" ] || fail "the unit is gone after the failure"
  # A new image that does not start: the previous one is the way back.
  rc=0
  APPLIANCE_IMAGE=localhost/aa:next live_install >"$tmp/out" 2>&1 || rc=$?
  [ "$rc" = 1 ] || fail "a new image that does not start: exit status $rc"
  grep -q 'install.sh --rollback' "$tmp/out" || fail "no hint for --rollback: $(cat "$tmp/out")"
  touch "$live/daemon-up"
  live_install >"$tmp/out" 2>&1 || fail "the installation with a daemon failed: $(cat "$tmp/out")"
  echo "ok   a rejected configuration or a daemon that does not start fails the installation"
else
  echo "skip live installation checks (root, or no Quadlet generator)"
fi

# --print-run installs without a unit and prints a command that splits
# into the same podman arguments.
new_root print-run
stage --print-run >"$tmp/out" 2>"$tmp/err" || fail "--print-run failed: $(cat "$tmp/err")"
[ ! -e "$unit" ] || fail "--print-run wrote a unit"
[ -f "$images" ] && [ -d "$state" ] || fail "--print-run did not install the files"
grep -v '^install.sh:' "$tmp/out" >"$tmp/run.sh"
bash -n "$tmp/run.sh" || fail "the printed command does not parse"
mkdir -p "$tmp/fakebin"
cat >"$tmp/fakebin/podman" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$CONTAINERS_CONF_OVERRIDE" "\$@" >"$tmp/podman-args"
EOF
printf '#!/usr/bin/env bash\n' >"$tmp/fakebin/install"
chmod +x "$tmp/fakebin/podman" "$tmp/fakebin/install"
PATH=$tmp/fakebin:$PATH bash "$tmp/run.sh"
args=$tmp/podman-args
[ "$(head -n 1 "$args")" = /home/tester/.config/android-appliance/android-appliance.containers.conf ] ||
  fail "the printed command does not set CONTAINERS_CONF_OVERRIDE"
for arg in run -d --userns=keep-id --cap-drop=ALL no-new-privileges --read-only /tmp:rw,size=512m core=0 \
  --init --tz=local /dev/kvm /dev/dri/renderD128 /home/tester/.local/share/android-appliance:/data:Z,nosuid,nodev \
  /run/user/tester/android-appliance:/run/appliance:Z 127.0.0.1:6090:8080/tcp 127.0.0.1:3478:3478/udp \
  127.0.0.1:3478:3478/tcp /home/tester/.config/android-appliance/appliance.conf APPLIANCE_PORT=6090 \
  APPLIANCE_LISTEN_ADDRESS=127.0.0.1 \
  --stop-timeout --restart=on-failure "localhost/android-appliance:$version-api36"; do
  has "$arg" "$args"
done
health=$(sed -n 's/^HealthCmd=//p' "$tmp/default/home/tester/.config/containers/systemd/android-appliance.container")
[ "$(grep -A1 -x -- --health-cmd "$args" | tail -n 1)" = "$health" ] || fail "the health command differs from the unit"
[ "$(tail -n 1 "$args")" = "localhost/android-appliance:$version-api36" ] || fail "the image is not the last argument"
echo "ok   --print-run"

# A v1 appliance keeps its state directory and its display port.
v1_root() {
  new_root "$1"
  mkdir -p "$root/etc/systemd/system" "$root/etc/android-appliance" "$(dirname "$conf")"
  touch "$root/etc/systemd/system/android-appliance-emulator.service"
  printf 'APPLIANCE_STATE_DIR=/var/lib/juno/android\nAPPLIANCE_DISPLAY_PORT=6090\n' \
    >"$root/etc/android-appliance/appliance.conf"
}
v1_root v1-port
stage_fails
grep -q 'a v1 appliance uses port 6090' "$tmp/out" || fail "v1 port: $(cat "$tmp/out")"
[ ! -e "$unit" ] || fail "the v1 refusal wrote a unit"
for v1_state in /var/lib/juno/android /var/lib/juno/android/v2 /var/lib/juno; do
  v1_root v1-state
  printf 'APPLIANCE_STATE_DIR=%s\nAPPLIANCE_PORT=46090\n' "$v1_state" >"$conf"
  stage_fails
  grep -q 'a v1 appliance uses /var/lib/juno/android' "$tmp/out" || fail "v1 state $v1_state: $(cat "$tmp/out")"
  [ ! -e "$unit" ] || fail "the v1 refusal wrote a unit"
  rm -rf "$root"
done
v1_root v1-beside
printf 'APPLIANCE_STATE_DIR=/srv/v2\nAPPLIANCE_PORT=46090\n' >"$conf"
stage >"$tmp/out" 2>&1 || fail "install beside v1 failed: $(cat "$tmp/out")"
grep -q 'WARNING a v1 appliance is installed' "$tmp/out" || fail "no warning about the v1 appliance"
has "Volume=/srv/v2:/data:Z,nosuid,nodev" "$unit"
# Without a v1 configuration, the v1 defaults apply.
v1_root v1-defaults
rm "$root/etc/android-appliance/appliance.conf"
printf 'APPLIANCE_STATE_DIR=/var/lib/android-appliance\nAPPLIANCE_PORT=46090\n' >"$conf"
stage_fails
grep -q 'a v1 appliance uses /var/lib/android-appliance' "$tmp/out" || fail "v1 defaults: $(cat "$tmp/out")"
echo "ok   coexistence with a v1 appliance"

# --migrate turns a v1 appliance into a v2 instance on the same state.
migrate_root() {
  v1_root "$1"
  mkdir -p "$root/etc/systemd/system/multi-user.target.wants" "$root/etc/systemd/system/sockets.target.wants" "$root/etc/systemd/system/timers.target.wants"
  for v1u in android-appliance-display.socket android-appliance-idle.timer android-appliance-display.service android-appliance-scrcpy.service android-appliance-xvnc.service android-appliance-idle.service; do
    touch "$root/etc/systemd/system/$v1u"
  done
  ln -s ../android-appliance-emulator.service "$root/etc/systemd/system/multi-user.target.wants/android-appliance-emulator.service"
  ln -s ../android-appliance-display.socket "$root/etc/systemd/system/sockets.target.wants/android-appliance-display.socket"
  ln -s ../android-appliance-idle.timer "$root/etc/systemd/system/timers.target.wants/android-appliance-idle.timer"
  cat >"$root/etc/android-appliance/appliance.conf" <<EOF
APPLIANCE_STATE_DIR=/var/lib/juno/android
APPLIANCE_USER=$user
APPLIANCE_AVD_NAME=pixel
APPLIANCE_API_LEVEL=35
APPLIANCE_IDLE_STOP_MINUTES=30
APPLIANCE_MEMORY_MIB=6144
APPLIANCE_CORES=6
APPLIANCE_DISK_SIZE=64G
APPLIANCE_GPU=host
APPLIANCE_DISPLAY_ENABLED=1
APPLIANCE_DISPLAY_PORT=6091
EOF
  mkdir -p "$root/var/lib/juno/android/avd/pixel.avd" "$root/var/lib/juno/android/home/.android"
  printf 'avd.ini.encoding=UTF-8\npath=/var/lib/juno/android/avd/pixel.avd\n' >"$root/var/lib/juno/android/avd/pixel.ini"
  printf 'image.sysdir.1=system-images/android-35/google_apis_playstore/x86_64/\n' >"$root/var/lib/juno/android/avd/pixel.avd/config.ini"
  echo key >"$root/var/lib/juno/android/home/.android/adbkey"
  manifest=$root/home/tester/.config/android-appliance/android-appliance.migration
}
migrate_root migrate
stage --migrate --no-start >"$tmp/out" 2>&1 || fail "--migrate failed: $(cat "$tmp/out")"
# The v2 configuration is the v1 one, translated.
for line in \
  "APPLIANCE_STATE_DIR=/var/lib/juno/android" \
  "APPLIANCE_PORT=6091" \
  "APPLIANCE_TURN_PORT=3478" \
  "APPLIANCE_AVD_NAME=pixel" \
  "APPLIANCE_API_LEVEL=35" \
  "APPLIANCE_GPU=host" \
  "APPLIANCE_IDLE_STOP_MINUTES=30" \
  "APPLIANCE_MEMORY_MIB=6144" \
  "APPLIANCE_CORES=6" \
  "APPLIANCE_DISK_SIZE=64G"; do
  has "$line" "$conf"
done
[ "$(stat -c %a "$conf")" = 600 ] || fail "the migrated configuration is not private"
# The manifest records what --migrate-back needs.
has "V1_CONFIG=/etc/android-appliance/appliance.conf" "$manifest"
has "V1_STATE_DIR=/var/lib/juno/android" "$manifest"
has "V1_OWNER=$user" "$manifest"
has "V1_ENABLED=android-appliance-idle.timer android-appliance-display.socket android-appliance-emulator.service" "$manifest"
# The root steps are printed, and v2 is not installed before they are done.
grep -q '^  sudo systemctl disable --now android-appliance-idle.timer android-appliance-display.socket android-appliance-emulator.service' "$tmp/out" ||
  fail "no stop command for the v1 units: $(cat "$tmp/out")"
lacks 'chown' "$tmp/out"
[ ! -e "$unit" ] || fail "--migrate installed v2 before the v1 units were stopped"
# The v1 units stopped (the wants links are gone): the install goes through.
rm "$root"/etc/systemd/system/*.wants/*
stage --migrate --no-start >"$tmp/out" 2>&1 && fail "--migrate ran again over the configuration"
grep -q 'exists already' "$tmp/out" || fail "no refusal of a second migration: $(cat "$tmp/out")"
stage --no-start >"$tmp/out" 2>&1 || fail "the install after the migration failed: $(cat "$tmp/out")"
has "Volume=/var/lib/juno/android:/data:Z,nosuid,nodev" "$unit"
has "Image=localhost/android-appliance:$version-api35" "$unit"
has "PublishPort=127.0.0.1:6091:8080/tcp" "$unit"
[ "$(cat "$root/var/lib/juno/android/.android-appliance-instance")" = android-appliance ] || fail "the state is not marked"
lacks 'a v1 appliance uses' "$tmp/out"
# The v1 registry and keys are untouched.
[ "$(cat "$root/var/lib/juno/android/avd/pixel.ini")" = $'avd.ini.encoding=UTF-8\npath=/var/lib/juno/android/avd/pixel.avd' ] || fail "the v1 registry changed"
[ "$(cat "$root/var/lib/juno/android/home/.android/adbkey")" = key ] || fail "the adb key changed"
echo "ok   --migrate"

# A migration in one go: v1 is already stopped, and v1 ran as this user.
migrate_root migrate-stopped
rm "$root"/etc/systemd/system/*.wants/*
stage --migrate --no-start >"$tmp/out" 2>&1 || fail "--migrate (v1 stopped) failed: $(cat "$tmp/out")"
grep -q 'installing v2 on the v1 state' "$tmp/out" || fail "the one-go migration did not install: $(cat "$tmp/out")"
has "Volume=/var/lib/juno/android:/data:Z,nosuid,nodev" "$unit"
has "V1_ENABLED=" "$manifest"
echo "ok   --migrate with v1 stopped"

# --migrate-back removes the v2 unit and prints what brings v1 back.
stage --migrate-back >"$tmp/out" 2>&1 || fail "--migrate-back failed: $(cat "$tmp/out")"
[ ! -e "$unit" ] || fail "--migrate-back kept the unit"
[ -f "$conf" ] && [ -f "$manifest" ] || fail "--migrate-back removed the configuration or the manifest"
[ ! -e "$root/var/lib/juno/android/.android-appliance-instance" ] || fail "--migrate-back kept the state marker"
[ -d "$root/var/lib/juno/android/avd/pixel.avd" ] || fail "--migrate-back touched the state"
grep -q '^  sudo restorecon -R /var/lib/juno/android' "$tmp/out" || fail "no restorecon step: $(cat "$tmp/out")"
lacks 'systemctl enable' "$tmp/out"
# The migration of the first root recorded enabled units: they come back.
root=$tmp/migrate conf=$root/home/tester/.config/android-appliance/appliance.conf unit=$root/home/tester/.config/containers/systemd/android-appliance.container
stage --migrate-back >"$tmp/out" 2>&1 || fail "--migrate-back (enabled units) failed: $(cat "$tmp/out")"
grep -q '^  sudo systemctl enable --now android-appliance-idle.timer android-appliance-display.socket android-appliance-emulator.service' "$tmp/out" ||
  fail "no enable step: $(cat "$tmp/out")"
# v2 installs again on the same configuration afterwards.
stage --no-start >"$tmp/out" 2>&1 || fail "the reinstall after --migrate-back failed: $(cat "$tmp/out")"
[ -f "$unit" ] || fail "no unit after the reinstall"
echo "ok   --migrate-back"

# Refusals.
migrate_root migrate-refusals
rm "$root/etc/android-appliance/appliance.conf"
stage_fails --migrate
grep -q 'there is no v1 appliance to migrate' "$tmp/out" || fail "migrate without v1: $(cat "$tmp/out")"
new_root migrate-no-manifest
stage --no-start >/dev/null 2>&1
stage_fails --migrate-back
grep -q 'was not migrated from v1' "$tmp/out" || fail "migrate-back without a manifest: $(cat "$tmp/out")"
rc=0
stage --migrate --uninstall >"$tmp/out" 2>&1 || rc=$?
if [ "$rc" != 2 ] || ! grep -q 'does not combine' "$tmp/out"; then fail "--migrate --uninstall was not a usage error: $(cat "$tmp/out")"; fi
echo "ok   --migrate refusals"

# A state directory on a rotational disk gets a warning. The staged
# sysfs describes the disk of the staging directory.
new_root rotational
source=$(df -P -- "$tmp" | awk 'NR == 2 { print $1 }')
case $source in
  /dev/*)
    block=$root/sys/class/block/$(basename "$(readlink -f -- "$source")")
    mkdir -p "$block/queue"
    echo 1 >"$block/queue/rotational"
    stage >"$tmp/out" 2>&1
    grep -q 'is on a rotational disk' "$tmp/out" || fail "no warning about a rotational disk"
    echo 0 >"$block/queue/rotational"
    stage >"$tmp/out" 2>&1
    if grep -q 'on a rotational disk' "$tmp/out"; then fail "warned about a solid-state disk: $(cat "$tmp/out")"; fi
    echo "ok   rotational disk warning"
    ;;
  *) echo "skip rotational disk warning (the test directory is on $source)" ;;
esac

# Usage errors and the refusal of root.
for args in "--purge" "--rollback --uninstall" "--frobnicate" "--config"; do
  rc=0
  # shellcheck disable=SC2086  # split the argument list on purpose
  stage $args >/dev/null 2>&1 || rc=$?
  [ "$rc" = 2 ] || fail "install.sh $args exited with $rc, not 2"
done
mkdir -p "$tmp/rootbin"
cat >"$tmp/rootbin/id" <<'EOF'
#!/usr/bin/env bash
case $1 in -u) echo 0 ;; *) echo root ;; esac
EOF
printf '#!/usr/bin/env bash\nexit 1\n' >"$tmp/rootbin/podman"
cp "$tmp/rootbin/podman" "$tmp/rootbin/systemctl"
chmod +x "$tmp/rootbin/id" "$tmp/rootbin/podman" "$tmp/rootbin/systemctl"
rc=0
HOME=$tmp/roothome PATH=$tmp/rootbin:$PATH bash "$installer" --config "$tmp/roothome/x.conf" >"$tmp/out" 2>&1 || rc=$?
if [ "$rc" != 1 ] || ! grep -q 'not as root' "$tmp/out"; then
  fail "install.sh did not refuse root: $(cat "$tmp/out")"
fi
[ ! -e "$tmp/roothome" ] || fail "install.sh changed files as root"
echo "ok   usage errors and root refusal"

# --doctor runs androidctl doctor for the configuration.
new_root doctor
stage >/dev/null 2>&1
printf '#!/usr/bin/env bash\necho "true crun v2"\n' >"$tmp/fakebin/podman"
rc=0
ANDROID_APPLIANCE_SOCKET=$tmp/missing.sock PATH=$tmp/fakebin:$PATH \
  bash "$installer" --doctor --config "$conf" >"$tmp/out" 2>&1 || rc=$?
grep -qx 'ok   podman runs rootless' "$tmp/out" || fail "--doctor: $(cat "$tmp/out")"
grep -q '^FAIL the daemon does not answer' "$tmp/out" || fail "--doctor did not check the daemon"
[ "$rc" = 1 ] || fail "--doctor exited with $rc despite a failure"
echo "ok   --doctor"

echo "install v2 tests passed"
