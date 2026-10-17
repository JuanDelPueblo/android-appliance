#!/usr/bin/env bash
# install.sh: install the Android appliance as a rootless Podman
# container that a systemd user unit (Quadlet) runs.
#
# Run it as the user who owns the appliance, not as root:
#   ./install.sh [--config FILE] [--no-build] [--no-start]
#   ./install.sh --rollback          go back to the previous image
#   ./install.sh --uninstall [--purge]
#   ./install.sh --doctor            check the host
#   ./install.sh --print-run         install for a host without systemd
# Staging mode for tests: ./install.sh --root DIR [...]
#
# The configuration file (default ~/.config/android-appliance/appliance.conf)
# is created from conf/appliance.conf.example when it is missing. Edit it
# and run the installer again to apply a change; it is idempotent. The
# v1 installer (system units, no container) is in the git history before v2.
# APPLIANCE_INSTALL_WAIT=SECONDS sets how long it waits for the daemon
# after the restart (default 60).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)

usage() {
  cat <<'EOF'
usage: install.sh [--config FILE] [--no-build] [--no-start] [--root DIR]
       install.sh --rollback [--config FILE] [--no-start] [--root DIR]
       install.sh --uninstall [--purge] [--config FILE] [--root DIR]
       install.sh --print-run [--config FILE] [--no-build] [--root DIR]
       install.sh --migrate [--config FILE] [--no-build] [--no-start] [--root DIR]
       install.sh --migrate-back [--config FILE] [--root DIR]
       install.sh --doctor [--config FILE]

  --config FILE  the configuration file of the instance
  --no-build     use the existing image instead of building it
  --no-start     write the unit, but do not (re)start it
  --rollback     switch the unit back to the previous image
  --uninstall    remove the unit, the images and androidctl; keep the
                 state and the configuration
  --purge        with --uninstall: also delete the state directory (all
                 apps and data of Android) and the configuration
  --print-run    install without systemd, and print the podman run command
  --migrate      move a v1 appliance (system units) to v2: write the v2
                 configuration from /etc/android-appliance/appliance.conf,
                 adopt its state directory in place, install v2. The v1
                 units must be stopped first; the commands are printed
  --migrate-back stop v2 and print the commands that bring v1 back on the
                 same state
  --doctor       check the host (androidctl doctor)
  --root DIR     stage every file under DIR; run no podman or systemctl
EOF
}

die() {
  echo "install.sh: $*" >&2
  exit 1
}

usage_error() {
  echo "install.sh: $*" >&2
  usage >&2
  exit 2
}

root=
config=
build=1
start=1
action=install
purge=0
while [ $# -gt 0 ]; do
  case $1 in
    --config | --root)
      [ $# -ge 2 ] && [ -n "$2" ] || usage_error "$1 needs an argument"
      if [ "$1" = --config ]; then config=$2; else root=$2; fi
      shift 2
      ;;
    --no-build)
      build=0
      shift
      ;;
    --no-start)
      start=0
      shift
      ;;
    --rollback | --uninstall | --doctor | --print-run | --migrate | --migrate-back)
      [ "$action" = install ] || usage_error "$1 does not combine with --$action"
      action=${1#--}
      shift
      ;;
    --purge)
      purge=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) usage_error "unknown argument $1" ;;
  esac
done
[ "$purge" = 0 ] || [ "$action" = uninstall ] || usage_error "--purge works only with --uninstall"

if [ -n "$root" ]; then
  root=$(realpath -m -- "$root")
  [ "$root" != / ] || die "--root / is not a staging directory"
elif [ "$(id -u)" = 0 ]; then
  die "run install.sh as the user who owns the appliance, not as root; the container runs rootless"
fi
[ -n "${HOME:-}" ] || die "HOME is not set"

user=$(id -un)
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
runtime_base=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
config_file=${config:-$config_home/android-appliance/appliance.conf}
case $config_file in /*) ;; *) config_file=$PWD/$config_file ;; esac
config_file=$(realpath -m -s -- "$config_file")
config_dir=$(dirname "$config_file")

if [ "$action" = doctor ]; then
  ANDROID_APPLIANCE_CONFIG=$root$config_file exec "$here/bin/androidctl" doctor
fi

warnings=()
warn() { warnings+=("$*"); }

# Fill unset APPLIANCE_* variables from the configuration file. The
# container reads the same file as its podman environment file, so
# refuse every line that podman would read differently.
read_config() {
  local line key n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    case $line in
      '' | '#'*) continue ;;
      APPLIANCE_*=*) ;;
      *) die "$config_file:$n: '$line' is not an APPLIANCE_*=value line" ;;
    esac
    key=${line%%=*}
    case $key in *[!A-Z0-9_]*) die "$config_file:$n: '$key' is not a valid key" ;; esac
    if [ -z "${!key:-}" ]; then
      printf -v "$key" '%s' "${line#*=}"
    fi
  done <"$root$config_file"
}

# config_value FILE KEY DEFAULT: the first value of KEY in another
# configuration file (the v1 one), without touching the environment.
config_value() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      "$2="*)
        echo "${line#*=}"
        return
        ;;
    esac
  done <"$1"
  echo "$3"
}

# Values end up in the unit, where systemd gives % and spaces a meaning,
# and in podman -v and -p arguments, where : and , separate fields.
check_path() {
  [[ $2 =~ ^/[A-Za-z0-9._/+@=~-]*$ ]] ||
    die "$1 '$2' must be an absolute path of letters, digits and ._/+@=~-"
}

check_port() {
  [[ $2 =~ ^[0-9]{1,5}$ ]] && [ "$2" -ge 1 ] && [ "$2" -le 65535 ] ||
    die "$1 '$2' is not a port number"
}

load_settings() {
  name=${APPLIANCE_NAME:-android-appliance}
  state_dir=${APPLIANCE_STATE_DIR:-$data_home/android-appliance}
  listen=${APPLIANCE_LISTEN_ADDRESS:-127.0.0.1}
  port=${APPLIANCE_PORT:-6090}
  turn_port=${APPLIANCE_TURN_PORT:-3478}
  api=${APPLIANCE_API_LEVEL:-36}
  gpu=${APPLIANCE_GPU:-auto}
  gpu_device=${APPLIANCE_GPU_DEVICE:-auto}
  bin_dir=${APPLIANCE_BIN_DIR:-$HOME/.local/bin}
  quadlet_dir=${APPLIANCE_QUADLET_DIR:-$config_home/containers/systemd}
  [ -f "$here/VERSION" ] || die "$here/VERSION is missing"
  version=$(<"$here/VERSION")
  image=${APPLIANCE_IMAGE:-localhost/android-appliance:$version-api$api}

  [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
    die "APPLIANCE_NAME '$name' must be letters, digits and ._- (a container and unit name)"
  check_path APPLIANCE_STATE_DIR "$state_dir"
  check_path APPLIANCE_BIN_DIR "$bin_dir"
  check_path APPLIANCE_QUADLET_DIR "$quadlet_dir"
  check_path "the configuration file" "$config_file"
  state_dir=$(realpath -m -s -- "$state_dir")
  bin_dir=$(realpath -m -s -- "$bin_dir")
  quadlet_dir=$(realpath -m -s -- "$quadlet_dir")
  check_port APPLIANCE_PORT "$port"
  check_port APPLIANCE_TURN_PORT "$turn_port"
  [ "$port" != "$turn_port" ] || die "APPLIANCE_PORT and APPLIANCE_TURN_PORT must differ"
  if [[ $listen =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    publish_address=$listen
  elif [[ $listen =~ ^[0-9A-Fa-f:.]+$ ]] && [[ $listen == *:* ]]; then
    publish_address="[$listen]"
  else
    die "APPLIANCE_LISTEN_ADDRESS '$listen' is not an IPv4 or IPv6 address"
  fi
  turn_publish=()
  local turn_listen=${APPLIANCE_TURN_LISTEN_ADDRESSES:-$listen} turn_address
  IFS=, read -r -a turn_addresses <<<"$turn_listen"
  [ "${#turn_addresses[@]}" -gt 0 ] || die "APPLIANCE_TURN_LISTEN_ADDRESSES is empty"
  for turn_address in "${turn_addresses[@]}"; do
    if [[ $turn_address =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      turn_publish+=("$turn_address")
    elif [[ $turn_address =~ ^[0-9A-Fa-f:.]+$ ]] && [[ $turn_address == *:* ]]; then
      turn_publish+=("[$turn_address]")
    else
      die "APPLIANCE_TURN_LISTEN_ADDRESSES entry '$turn_address' is not an IPv4 or IPv6 address"
    fi
  done
  case $listen in
    127.* | ::1) ;;
    *) warn "APPLIANCE_LISTEN_ADDRESS=$listen publishes the API on the network as plain HTTP: the token, the session cookie and the screenshots can be read on the way. Keep 127.0.0.1, or put a reverse proxy with TLS in front of the port and set APPLIANCE_TRUST_PROXY=1." ;;
  esac
  [[ $api =~ ^[0-9]+$ ]] || die "APPLIANCE_API_LEVEL '$api' is not a number"
  case $gpu in
    auto | host | lavapipe) ;;
    *) die "APPLIANCE_GPU '$gpu' must be auto, host or lavapipe" ;;
  esac
  [ "$gpu_device" = auto ] || check_path APPLIANCE_GPU_DEVICE "$gpu_device"
  [[ $image =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]] || die "APPLIANCE_IMAGE '$image' is not an image name"

  unit=$name.service
  unit_file=$quadlet_dir/$name.container
  override_file=$config_dir/$name.containers.conf
  images_file=$config_dir/$name.images
  migration_file=$config_dir/$name.migration
  runtime_dir=$runtime_base/$name
}

# overlaps A B: succeed when one directory is, or contains, the other.
overlaps() {
  case "$1/" in "$2"/*) return 0 ;; esac
  case "$2/" in "$1"/*) return 0 ;; esac
  return 1
}

# install.sh marks a state directory with the name of the instance that
# uses it. Two instances must not share one, and --purge must not delete a
# directory that is not an appliance state.
state_marker=.android-appliance-instance

# state_owner DIR: the instance that the marker of DIR names.
state_owner() {
  local line=
  if [ -f "$root$1/$state_marker" ]; then IFS= read -r line <"$root$1/$state_marker" || true; fi
  printf '%s' "$line"
}

# looks_like_state DIR: DIR has the files of an appliance (v2 or v1).
looks_like_state() {
  [ -d "$root$1/appliance" ] || [ -d "$root$1/avd" ]
}

# unused_dir DIR: DIR does not exist or is empty.
unused_dir() {
  [ ! -d "$root$1" ] || [ -z "$(ls -A -- "$root$1")" ]
}

# check_state_dir: the directory is new, empty, the state of this instance
# or an adopted appliance state, and its AVD matches the API level.
check_state_dir() {
  local owner avd level ini
  owner=$(state_owner "$state_dir")
  if [ -n "$owner" ] && [ "$owner" != "$name" ]; then
    die "the state directory $state_dir belongs to the appliance '$owner'; give '$name' a directory of its own (APPLIANCE_STATE_DIR in $config_file)"
  fi
  if [ -z "$owner" ] && ! unused_dir "$state_dir" && ! looks_like_state "$state_dir"; then
    die "the state directory $state_dir is not empty and holds no appliance state; use an empty directory of its own (APPLIANCE_STATE_DIR in $config_file)"
  fi
  # An AVD keeps its API level; the daemon refuses another image at the first boot.
  avd=${APPLIANCE_AVD_NAME:-android}
  ini=$root$state_dir/avd/$avd.avd/config.ini
  if [[ $avd =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && [ -f "$ini" ]; then
    level=$(sed -n 's|^image\.sysdir\.1 *= *system-images/android-\([0-9][0-9]*\)/.*|\1|p' "$ini" | head -n 1)
    if [ -n "$level" ] && [ "$level" != "$api" ]; then
      die "the AVD in $state_dir uses API level $level, but APPLIANCE_API_LEVEL is $api; set APPLIANCE_API_LEVEL=$level in $config_file (an AVD keeps its API level), or use another state directory"
    fi
  fi
}

# check_unit_owner: an installed unit of this name belongs to this
# configuration. Another configuration with the same name, or a mistyped
# --config, would take the unit over and leave the first one orphaned.
check_unit_owner() {
  [ -f "$root$unit_file" ] && grep -q '^# Managed by install.sh' "$root$unit_file" || return 0
  local owner
  owner=$(sed -n 's/^EnvironmentFile=//p' "$root$unit_file" | head -n 1)
  if [ "$owner" != "$config_file" ]; then
    die "$unit_file belongs to the configuration $owner, not to $config_file. For a second instance set another APPLIANCE_NAME, APPLIANCE_STATE_DIR and ports in $config_file; to replace the first one, run install.sh --uninstall --config $owner"
  fi
}

# A v1 appliance (system units) on the same host must keep its state
# directory and its display port.
v1_unit=/etc/systemd/system/android-appliance-emulator.service
v1_conf=/etc/android-appliance/appliance.conf

# v1_setting KEY DEFAULT: a value of the v1 configuration.
v1_setting() {
  if [ -f "$root$v1_conf" ]; then
    config_value "$root$v1_conf" "$1" "$2"
  else
    echo "$2"
  fi
}

check_v1() {
  [ -e "$root$v1_unit" ] || return 0
  local v1_state v1_port
  v1_state=$(realpath -m -s -- "$(v1_setting APPLIANCE_STATE_DIR /var/lib/android-appliance)")
  v1_port=$(v1_setting APPLIANCE_DISPLAY_PORT 6090)
  if [ -f "$root$migration_file" ] && [ "$(config_value "$root$migration_file" V1_STATE_DIR "")" = "$v1_state" ]; then
    # This instance took the v1 state over (--migrate). v1 stays installed
    # but disabled, for --migrate-back; it must not run at the same time.
    require_v1_stopped
    return 0
  fi
  if overlaps "$state_dir" "$v1_state"; then
    die "a v1 appliance uses $v1_state ($v1_unit); set APPLIANCE_STATE_DIR in $config_file to a separate directory"
  fi
  if [ "$port" = "$v1_port" ] || [ "$turn_port" = "$v1_port" ]; then
    die "a v1 appliance uses port $v1_port ($v1_unit); set APPLIANCE_PORT and APPLIANCE_TURN_PORT in $config_file to other ports"
  fi
  warn "a v1 appliance is installed ($v1_unit). This instance runs beside it with its own state directory and port; do not boot both emulators at once on a small host. $bin_dir/androidctl controls this instance, /usr/local/bin/androidctl the v1 appliance."
}

# Devices: /dev/kvm always, the render node unless lavapipe. The user
# reaches a device that is not world-accessible through its groups,
# which the container keeps only with keep-groups.
# The v1 appliance: the system units that the v1 installer wrote, and a
# state directory with the same layout as v2 (avd/, home/.android/).
v1_units=(android-appliance-idle.timer android-appliance-display.socket android-appliance-emulator.service
  android-appliance-display.service android-appliance-scrcpy.service android-appliance-xvnc.service android-appliance-idle.service)

v1_unit_active() {
  # In the staging root nothing runs.
  [ -z "$root" ] && command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$1" 2>/dev/null
}

v1_unit_enabled() {
  if [ -n "$root" ]; then
    compgen -G "$root/etc/systemd/system/*.wants/$1" >/dev/null
  else
    command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet "$1" 2>/dev/null
  fi
}

require_v1_stopped() {
  local unit running=()
  for unit in "${v1_units[@]}"; do
    if v1_unit_active "$unit"; then running+=("$unit"); fi
  done
  [ "${#running[@]}" = 0 ] ||
    die "the v1 appliance is running (${running[*]}); it shares the state directory with this instance. Stop it first:"$'\n'"  sudo systemctl disable --now ${v1_units[*]}"
}

# migrate: v1 to v2 on the same state directory. The v1 configuration
# becomes the v2 one, the state is adopted in place (the AVD, userdata,
# accounts and adb keys stay where they are; the v1 registry file is not
# touched), and the v1 units stay installed but disabled, so that
# --migrate-back can bring v1 back. The steps that need root (stopping
# the system units, chown when v1 ran as another user) are printed.
migrate() {
  [ -f "$root$v1_conf" ] || die "there is no v1 appliance to migrate: $v1_conf does not exist"
  [ -e "$root$v1_unit" ] || die "there is no v1 appliance to migrate: $v1_unit does not exist"
  if [ -f "$root$config_file" ]; then
    die "$config_file exists already; a migrated or configured instance is not migrated again. To migrate into it anyway, move the file away first"
  fi
  local v1_state v1_user v1_gpu gpu owner unit enabled=() steps=() stop_needed=0
  v1_state=$(realpath -m -s -- "$(v1_setting APPLIANCE_STATE_DIR /var/lib/android-appliance)")
  v1_user=$(v1_setting APPLIANCE_USER android-appliance)
  v1_gpu=$(v1_setting APPLIANCE_GPU swiftshader)
  case $v1_gpu in host) gpu=host ;; *) gpu=auto ;; esac
  [ -d "$root$v1_state" ] || die "the v1 state directory $v1_state does not exist"
  [ -d "$root$v1_state/avd" ] || die "$v1_state holds no AVD (no avd/ directory); nothing to migrate"
  for unit in "${v1_units[@]}"; do
    if v1_unit_enabled "$unit"; then enabled+=("$unit"); fi
    if v1_unit_active "$unit" || v1_unit_enabled "$unit"; then stop_needed=1; fi
  done

  mkdir -p "$root$config_dir"
  cat >"$root$config_file" <<CONF
# Configuration of the Android appliance, migrated from the v1
# configuration $v1_conf on $(date -I). conf/appliance.conf.example
# documents every key. Edit the file, then run install.sh again.
APPLIANCE_NAME=android-appliance
APPLIANCE_STATE_DIR=$v1_state
APPLIANCE_LISTEN_ADDRESS=127.0.0.1
APPLIANCE_PORT=$(v1_setting APPLIANCE_DISPLAY_PORT 6090)
APPLIANCE_TURN_PORT=3478
APPLIANCE_AVD_NAME=$(v1_setting APPLIANCE_AVD_NAME android)
APPLIANCE_API_LEVEL=$(v1_setting APPLIANCE_API_LEVEL 36)
APPLIANCE_GPU=$gpu
APPLIANCE_IDLE_STOP_MINUTES=$(v1_setting APPLIANCE_IDLE_STOP_MINUTES 60)
APPLIANCE_MEMORY_MIB=$(v1_setting APPLIANCE_MEMORY_MIB 4096)
APPLIANCE_CORES=$(v1_setting APPLIANCE_CORES 4)
APPLIANCE_DISK_SIZE=$(v1_setting APPLIANCE_DISK_SIZE 32G)
CONF
  chmod 0600 "$root$config_file"
  read_config
  load_settings
  # The manifest tells check_v1 that the shared state is intended, and
  # --migrate-back what to bring back.
  owner=$(stat -c %U "$root$v1_state" 2>/dev/null || echo "$user")
  cat >"$root$migration_file" <<MANIFEST
# Managed by install.sh --migrate on $(date -I). install.sh --migrate-back reads it.
V1_CONFIG=$v1_conf
V1_STATE_DIR=$v1_state
V1_USER=$v1_user
V1_OWNER=$owner
V1_ENABLED=${enabled[*]}
MANIFEST
  chmod 0600 "$root$migration_file"
  echo "install.sh: wrote $config_file from $v1_conf (API $api, $gpu rendering, port $port, state $state_dir)"
  echo "install.sh: wrote $migration_file"

  if [ "$owner" != "$user" ]; then
    steps+=("sudo chown -R $user: $v1_state")
  fi
  if [ "$stop_needed" = 1 ]; then steps+=("sudo systemctl disable --now ${v1_units[*]}"); fi
  if [ "${#steps[@]}" != 0 ]; then
    echo "install.sh: these steps need root; run them, then run install.sh --config $config_file to install v2:"
    printf '  %s\n' "${steps[@]}"
    if [ -z "$root" ] && [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
      local answer step
      read -r -p "install.sh: run them now with sudo? [y/N] " answer
      if [[ $answer =~ ^[Yy] ]]; then
        for step in "${steps[@]}"; do
          echo "install.sh: $step"
          eval "$step" || die "the step failed: $step"
        done
        steps=()
      fi
    fi
  fi
  if [ "${#steps[@]}" != 0 ]; then
    echo "install.sh: v1 is still in place; install.sh --config $config_file installs v2 once the steps are done"
    return 0
  fi
  echo "install.sh: installing v2 on the v1 state"
  install_appliance
  echo "install.sh: the v1 units are disabled and kept for install.sh --migrate-back; the v1 registry $v1_state/avd/*.ini is untouched"
}

# migrate_back: stop v2 and remove its unit; the state and the v2
# configuration stay (install.sh installs v2 again). The steps that
# bring v1 back need root and are printed: SELinux labels of the state
# (the container relabelled it), ownership, and the units.
migrate_back() {
  [ -f "$root$config_file" ] || die "the configuration file $config_file does not exist; give the --config of the migrated instance"
  read_config
  load_settings
  [ -f "$root$migration_file" ] || die "$migration_file does not exist: this instance was not migrated from v1 with install.sh --migrate"
  local v1_state v1_owner v1_enabled steps=()
  v1_state=$(config_value "$root$migration_file" V1_STATE_DIR "")
  v1_owner=$(config_value "$root$migration_file" V1_OWNER "$user")
  v1_enabled=$(config_value "$root$migration_file" V1_ENABLED "")
  [ "$v1_state" = "$state_dir" ] || die "the state directory moved since the migration ($v1_state, now $state_dir); nothing was changed"
  if [ -z "$root" ] && command -v systemctl >/dev/null 2>&1 && [ "$(systemctl --user show -p LoadState --value "$unit" 2>/dev/null)" = loaded ]; then
    echo "install.sh: stopping $unit"
    systemctl --user stop "$unit" || die "$unit did not stop; see: journalctl --user -u $unit"
  fi
  if [ -f "$root$unit_file" ]; then
    grep -q '^# Managed by install.sh' "$root$unit_file" || die "$unit_file exists, and install.sh did not write it; nothing was removed"
    check_unit_owner
    rm -f "$root$unit_file"
    if [ -z "$root" ] && command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload
      systemctl --user reset-failed "$unit" 2>/dev/null || true
    fi
  fi
  if [ "$(state_owner "$state_dir")" = "$name" ]; then rm -f "$root$state_dir/$state_marker"; fi
  rm -rf "${root}${runtime_dir:?}"
  echo "install.sh: removed the v2 unit $unit; kept $config_file, $migration_file and the images (install.sh --config $config_file installs v2 again)"
  if [ -n "$root" ] || command -v restorecon >/dev/null 2>&1; then
    steps+=("sudo restorecon -R $v1_state")
  fi
  if [ "$v1_owner" != "$user" ]; then
    steps+=("sudo chown -R $v1_owner: $v1_state")
  fi
  if [ -n "$v1_enabled" ]; then
    steps+=("sudo systemctl enable --now $v1_enabled")
  fi
  if [ "${#steps[@]}" != 0 ]; then
    echo "install.sh: these steps bring v1 back and need root:"
    printf '  %s\n' "${steps[@]}"
  fi
}

detect_devices() {
  local node mode
  render=
  group_add=0
  if [ ! -e "$root/dev/kvm" ]; then
    warn "/dev/kvm is missing; the emulator needs KVM"
  elif [ ! -r "$root/dev/kvm" ] || [ ! -w "$root/dev/kvm" ]; then
    warn "$user cannot use /dev/kvm; add the user to the group $(stat -c %G -- "$root/dev/kvm"), then log in again"
  fi
  if [ "$gpu" != lavapipe ]; then
    if [ "$gpu_device" = auto ]; then
      for node in "$root"/dev/dri/renderD*; do
        if [ -e "$node" ]; then
          render=${node#"$root"}
          break
        fi
      done
    elif [ -e "$root$gpu_device" ]; then
      render=$gpu_device
    else
      die "APPLIANCE_GPU_DEVICE $gpu_device does not exist"
    fi
    if [ -z "$render" ] && [ "$gpu" = host ]; then
      die "APPLIANCE_GPU=host needs a render node, and /dev/dri has none"
    fi
    if [ -n "$render" ] && { [ ! -r "$root$render" ] || [ ! -w "$root$render" ]; }; then
      local group
      group=$(stat -c %G -- "$root$render")
      [ "$gpu" != host ] || die "$user cannot use $render; add the user to the group $group, then log in again"
      warn "$user cannot use $render, so Android renders in software; add the user to the group $group for the GPU"
      render=
    fi
  fi
  for node in /dev/kvm $render; do
    [ -e "$root$node" ] || continue
    mode=$(stat -c %a -- "$root$node")
    if [ $((8#$mode & 6)) != 6 ]; then group_add=1; fi
  done
}

# rotational DIR: succeed when DIR is on a rotational disk.
rotational() {
  local source dev
  source=$(df -P -- "$1" 2>/dev/null | awk 'NR == 2 { print $1 }')
  case $source in /dev/*) ;; *) return 1 ;; esac
  dev=$root/sys/class/block/$(basename "$(readlink -f -- "$source")")
  # A partition reports through its disk.
  if [ -e "$dev/partition" ]; then dev=$dev/..; fi
  [ "$(cat "$dev/queue/rotational" 2>/dev/null)" = 1 ]
}

# The image state file keeps the current and the previous image of this
# instance, and whether install.sh built them (only those are removed).
read_images() {
  current_image='' current_built=0 previous_image='' previous_built=0
  [ -f "$root$images_file" ] || return 0
  local key value
  while IFS='=' read -r key value; do
    case $key in
      CURRENT_IMAGE) current_image=$value ;;
      CURRENT_BUILT) current_built=$value ;;
      PREVIOUS_IMAGE) previous_image=$value ;;
      PREVIOUS_BUILT) previous_built=$value ;;
    esac
  done <"$root$images_file"
}

write_images() {
  mkdir -p "$root$config_dir"
  cat >"$root$images_file.tmp" <<EOF
# Managed by install.sh: the images of $name. --rollback switches to
# the previous image.
CURRENT_IMAGE=$current_image
CURRENT_BUILT=$current_built
PREVIOUS_IMAGE=$previous_image
PREVIOUS_BUILT=$previous_built
EOF
  mv "$root$images_file.tmp" "$root$images_file"
}

# The new image becomes current, the current one previous. The image
# that drops out of the two goes to dropped_image when install.sh built
# it.
rotate_images() {
  local new=$1 built=$2
  dropped_image=
  if [ "$new" = "$current_image" ]; then
    current_built=$built
    return
  fi
  if [ "$new" != "$previous_image" ] && [ -n "$previous_image" ] && [ "$previous_built" = 1 ]; then
    dropped_image=$previous_image
  fi
  previous_image=$current_image
  previous_built=$current_built
  current_image=$new
  current_built=$built
}

# Remove an image of this instance unless another unit runs it. Podman
# refuses to remove an image that a container uses.
remove_image() {
  local image=$1 file
  for file in "$root$quadlet_dir"/*.container; do
    [ -f "$file" ] && [ "$file" != "$root$unit_file" ] || continue
    if grep -qxF "Image=$image" "$file"; then
      echo "install.sh: kept the image $image, which $file uses"
      return
    fi
  done
  echo "install.sh: removing the image $image"
  if [ -z "$root" ] && ! podman rmi "$image" >/dev/null 2>&1; then
    echo "install.sh: could not remove the image $image; a container may still use it" >&2
  fi
}

build_image() {
  [ -f "$here/Containerfile" ] || die "$here/Containerfile is missing"
  local args=(--build-arg "API_LEVEL=$api" --build-arg "VERSION=$version") key variable
  for key in EMULATOR_VERSION PLATFORM_TOOLS_VERSION IMAGE_REVISION REPOSITORY; do
    variable=APPLIANCE_SDK_$key
    if [ -n "${!variable:-}" ]; then args+=(--build-arg "SDK_$key=${!variable}"); fi
  done
  old_image_id=$(podman image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
  echo "install.sh: building the image $image; the first build downloads about 2.5 GB of SDK"
  podman build --tag "$image" --file "$here/Containerfile" "${args[@]}" "$here" ||
    die "the image build failed"
}

# The daemon answers on its unix socket, which androidctl uses.
health_cmd="/opt/appliance/venv/bin/python -m appliance healthcheck"

write_unit() {
  cat <<EOF
# Managed by install.sh. Edit $config_file and run install.sh again.
[Unit]
Description=Android appliance ($name)

[Container]
ContainerName=$name
Image=$image
Pull=never
UserNS=keep-id
DropCapability=all
NoNewPrivileges=true
ReadOnly=true
Tmpfs=/tmp:rw,size=512m
Ulimit=core=0
RunInit=true
Timezone=local
AddDevice=/dev/kvm
EOF
  if [ -n "$render" ]; then echo "AddDevice=$render"; fi
  if [ "$group_add" = 1 ]; then echo "GroupAdd=keep-groups"; fi
  cat <<EOF
Volume=$state_dir:/data:Z,nosuid,nodev
Volume=%t/$name:/run/appliance:Z
PublishPort=$publish_address:$port:8080/tcp
EOF
  local address
  for address in "${turn_publish[@]}"; do
    echo "PublishPort=$address:$turn_port:3478/udp"
    echo "PublishPort=$address:$turn_port:3478/tcp"
  done
  cat <<EOF
EnvironmentFile=$config_file
Environment=APPLIANCE_LISTEN_ADDRESS=$listen APPLIANCE_PORT=$port APPLIANCE_TURN_PORT=$turn_port APPLIANCE_GPU=$gpu
StopTimeout=150
HealthCmd=$health_cmd

[Service]
Environment=CONTAINERS_CONF_OVERRIDE=$override_file
ExecStartPre=/usr/bin/install -d -m 0700 %t/$name
Restart=on-failure
TimeoutStartSec=300
TimeoutStopSec=180

[Install]
WantedBy=default.target
EOF
}

quote() {
  case $1 in
    '' | *[!A-Za-z0-9_./:=@%+,-]*) printf "'%s'" "${1//\'/\'\\\'\'}" ;;
    *) printf '%s' "$1" ;;
  esac
}

# The podman run command of the unit, for hosts without systemd.
print_run() {
  local options=(
    "--name $(quote "$name") --replace --pull=never --restart=on-failure"
    "--userns=keep-id --cap-drop=ALL --security-opt no-new-privileges"
    "--read-only --tmpfs /tmp:rw,size=512m --ulimit core=0 --init --tz=local"
    "--device /dev/kvm"
  )
  if [ -n "$render" ]; then options+=("--device $(quote "$render")"); fi
  if [ "$group_add" = 1 ]; then options+=("--group-add keep-groups"); fi
  options+=(
    "-v $(quote "$state_dir:/data:Z,nosuid,nodev") -v $(quote "$runtime_dir:/run/appliance:Z")"
    "-p $(quote "$publish_address:$port:8080/tcp")"
    "--env-file $(quote "$config_file")"
    "--env APPLIANCE_LISTEN_ADDRESS=$(quote "$listen") --env APPLIANCE_PORT=$port"
    "--env APPLIANCE_TURN_PORT=$turn_port --env APPLIANCE_GPU=$gpu"
    "--stop-timeout 150 --health-cmd $(quote "$health_cmd")"
  )
  echo "# Run $name without systemd. Stop it with: podman stop -t 150 $name"
  echo "install -d -m 0700 $(quote "$runtime_dir")"
  echo "CONTAINERS_CONF_OVERRIDE=$(quote "$override_file") podman run -d \\"
  local option address
  for address in "${turn_publish[@]}"; do
    options+=("-p $(quote "$address:$turn_port:3478/udp") -p $(quote "$address:$turn_port:3478/tcp")")
  done
  for option in "${options[@]}"; do
    echo "  $option \\"
  done
  echo "  $(quote "$image")"
}

quadlet_generator() {
  local file
  for file in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
    if [ -x "$file" ]; then
      echo "$file"
      return
    fi
  done
  return 1
}

# Write the unit only after the Quadlet generator accepts it.
install_unit() {
  local work generator errors
  if [ -f "$root$unit_file" ] && ! grep -q '^# Managed by install.sh' "$root$unit_file"; then
    die "$unit_file exists, and install.sh did not write it; remove it or choose another APPLIANCE_NAME"
  fi
  work=$(mktemp -d)
  write_unit >"$work/$name.container"
  if generator=$(quadlet_generator); then
    if ! errors=$(QUADLET_UNIT_DIRS=$work "$generator" -dryrun -user 2>&1 >/dev/null); then
      rm -rf "$work"
      die "the Quadlet generator rejected the unit:"$'\n'"$(grep -v 'Loading source unit file' <<<"$errors")"
    fi
  elif [ -z "$root" ]; then
    rm -rf "$work"
    die "the Quadlet generator of Podman is missing; install Podman 4.4 or later"
  else
    echo "install.sh: the Quadlet generator is missing; the staged unit is not validated" >&2
  fi
  mkdir -p "$root$quadlet_dir"
  cp "$work/$name.container" "$root$unit_file.tmp"
  mv "$root$unit_file.tmp" "$root$unit_file"
  rm -rf "$work"
}

# ours_androidctl LINK: LINK points at bin/androidctl in a checkout of this
# appliance. Any other link is somebody's wrapper or the v1 command.
ours_androidctl() {
  local target top
  target=$(readlink -f -- "$1") || return 1
  top=$(dirname "$(dirname "$target")")
  [ "$(basename "$target")" = androidctl ] && [ "$(basename "$(dirname "$target")")" = bin ] &&
    [ -f "$top/install.sh" ] && [ -f "$top/VERSION" ] && [ -f "$top/Containerfile" ]
}

link_androidctl() {
  local link=$root$bin_dir/androidctl
  mkdir -p "$root$bin_dir"
  if { [ -e "$link" ] || [ -L "$link" ]; } && { [ ! -L "$link" ] || ! ours_androidctl "$link"; }; then
    warn "kept $bin_dir/androidctl, which is not a link to the androidctl of this appliance; it may not control this instance"
    return
  fi
  ln -sfn "$here/bin/androidctl" "$link"
}

# Wait for the daemon, so that androidctl works when install.sh returns.
# A daemon that does not come up is a failed installation: the unit starts
# even when the daemon exits at once (a bad value, an image that cannot run).
wait_daemon() {
  local wait=${APPLIANCE_INSTALL_WAIT:-60} hint=
  local deadline=$((SECONDS + wait))
  until curl -sf --max-time 5 -o /dev/null --unix-socket "$runtime_dir/api.sock" http://localhost/api/v1/health; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "install.sh: the daemon did not answer within ${wait}s. The last lines of the journal of $unit:" >&2
      if command -v journalctl >/dev/null 2>&1; then journalctl --user -u "$unit" -n 20 --no-pager >&2 || true; fi
      if [ -n "$previous_image" ]; then hint=" To go back to the previous image: install.sh --rollback"; fi
      # The image that dropped out is not the way back; do not leave it behind untracked.
      if [ -n "$dropped_image" ]; then remove_image "$dropped_image"; fi
      die "$name did not start. Fix the cause and run install.sh again.$hint"
    fi
    sleep 1
  done
}

# The daemon refuses an invalid value at its start. Ask the image first, so
# that the unit is not restarted into a crash loop.
check_config() {
  [ -z "$root" ] || return 0
  local errors
  errors=$(podman run --rm --pull=never --env-file "$config_file" \
    --env "APPLIANCE_LISTEN_ADDRESS=$listen" --env "APPLIANCE_PORT=$port" --env "APPLIANCE_TURN_PORT=$turn_port" \
    --env "APPLIANCE_GPU=$gpu" "$image" /opt/appliance/venv/bin/python -m appliance check-config 2>&1) ||
    die "the appliance in $image rejects $config_file:"$'\n'"$errors"
}

install_appliance() {
  if [ ! -f "$root$config_file" ]; then
    mkdir -p "$root$config_dir"
    cp "$here/conf/appliance.conf.example" "$root$config_file"
    chmod 0600 "$root$config_file"
    echo "install.sh: created $config_file from the example; edit it and run install.sh again to customize"
  fi
  read_config
  load_settings
  dedicated_dir "$state_dir" "use a directory of its own, such as ~/.local/share/android-appliance" >/dev/null
  check_state_dir
  check_unit_owner
  check_v1
  detect_devices
  read_images

  local built=0 new_image_id=''
  old_image_id=''
  if [ "$action" = rollback ]; then
    [ -n "$previous_image" ] || die "there is no previous image to roll back to ($images_file)"
    image=$previous_image
    built=$previous_built
    if [ -z "$root" ] && ! podman image exists "$image"; then
      die "the previous image $image does not exist any more"
    fi
  elif [ "$build" = 1 ]; then
    if [ -z "$root" ]; then build_image; fi
    built=1
  else
    if [ -z "$root" ] && ! podman image exists "$image"; then
      die "the image $image does not exist; run install.sh without --no-build"
    fi
    if [ "$image" = "$current_image" ]; then built=$current_built; fi
    if [ "$image" = "$previous_image" ]; then built=$previous_built; fi
  fi
  check_config
  rotate_images "$image" "$built"

  if [ ! -d "$root$state_dir" ]; then
    mkdir -p "$(dirname "$root$state_dir")"
    mkdir -m 0700 "$root$state_dir"
  fi
  [ -w "$root$state_dir" ] || die "$user cannot write the state directory $state_dir"
  # The AVD holds the accounts of the device: the directory is private
  # even when the user made it with another mode.
  chmod 0700 "$root$state_dir" 2>/dev/null || warn "cannot make $state_dir private (mode 0700); other users of this host may read the Android data"
  printf '%s\n' "$name" >"$root$state_dir/$state_marker"
  install -d -m 0700 "$root$runtime_dir"
  mkdir -p "$root$config_dir"
  cat >"$root$override_file" <<EOF
# Managed by install.sh. SELinux separation for the container $name,
# also when a containers.conf of the host sets label = false.
[containers]
label = true
EOF
  link_androidctl

  if [ "$action" = print-run ]; then
    write_images
    print_run
  else
    install_unit
    write_images
    if [ -z "$root" ]; then
      systemctl --user daemon-reload
      if [ "$(systemctl --user show -p LoadState --value "$unit")" != loaded ]; then
        die "systemd did not load $unit; APPLIANCE_QUADLET_DIR must be a directory that the Quadlet generator reads, such as ~/.config/containers/systemd"
      fi
      if [ "$start" = 1 ]; then
        echo "install.sh: (re)starting $unit"
        systemctl --user restart "$unit" || die "$unit did not start; see: journalctl --user -u $unit"
        wait_daemon
      fi
    fi
    if [ ! -e "$root/var/lib/systemd/linger/$user" ]; then
      warn "lingering is off, so the appliance stops at logout and does not start at boot; turn it on with: loginctl enable-linger $user"
    fi
  fi

  # Remove old images after the restart, when no container uses them.
  if [ -n "$dropped_image" ]; then remove_image "$dropped_image"; fi
  # A rebuild under the same tag leaves the old image without a tag. An
  # image that still has a tag (another version) stays.
  if [ -z "$root" ] && [ "$built" = 1 ] && [ -n "$old_image_id" ]; then
    new_image_id=$(podman image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
    if [ "$new_image_id" != "$old_image_id" ] &&
      [ "$(podman image inspect --format '{{len .RepoTags}}' "$old_image_id" 2>/dev/null)" = 0 ]; then
      podman rmi "$old_image_id" >/dev/null 2>&1 || true
    fi
  fi

  if rotational "$root$state_dir"; then
    warn "the state directory $state_dir is on a rotational disk; Android boots and runs much slower there. Set APPLIANCE_STATE_DIR to a directory on an SSD."
  fi

  echo "install.sh: installed the Android appliance $name"
  echo "install.sh:   configuration: $config_file"
  echo "install.sh:   state:         $state_dir"
  echo "install.sh:   image:         $image"
  if [ "$action" != print-run ]; then
    echo "install.sh:   unit:          $unit_file ($unit)"
  fi
  echo "install.sh:   androidctl:    $bin_dir/androidctl"
  echo "install.sh:   dashboard:     $(ANDROID_APPLIANCE_CONFIG=$root$config_file "$here/bin/androidctl" display) (androidctl dashboard prints a login link)"
  if [ "$config_file" != "$config_home/android-appliance/appliance.conf" ]; then
    echo "install.sh: this instance has its own configuration; run androidctl with ANDROID_APPLIANCE_CONFIG=$config_file"
  fi
  print_warnings
  if [ -n "$root" ]; then
    echo "install.sh: staged the appliance under $root"
  elif [ "$action" != print-run ]; then
    echo "install.sh: start Android with: androidctl start"
  fi
}

print_warnings() {
  local warning
  for warning in "${warnings[@]}"; do
    echo "install.sh: WARNING $warning" >&2
  done
}

# dedicated_dir DIR ADVICE: print DIR with its links resolved, or refuse
# a shared directory. The container relabels the state directory for
# SELinux, and --purge deletes it.
dedicated_dir() {
  local dir=$1 resolved shared refused=0
  resolved=$(realpath -m -- "$root$dir")
  if [ -n "$root" ]; then
    case $resolved in
      "$root") resolved=/ ;;
      "$root"/*) resolved=${resolved#"$root"} ;;
      *) die "APPLIANCE_STATE_DIR '$dir' is outside the staging root" ;;
    esac
  fi
  case $resolved in
    /home/*/*) ;;
    /home/*) refused=1 ;;
    /*/*) ;;
    # / and every top-level directory: /var, /home, /srv, /opt, /etc,
    # /usr, /tmp, /root, /run, ...
    *) refused=1 ;;
  esac
  # Compare the files themselves: HOME or a data directory may be reached
  # through a link or a bind mount.
  for shared in /var/lib /var/tmp /var/home /usr/local /run/user \
    "$HOME" "$HOME/.local" "$data_home" "$config_home" "$runtime_base"; do
    if [ "$resolved" = "$(realpath -m -s -- "$shared")" ] || [ "$root$resolved" -ef "$root$shared" ]; then refused=1; fi
  done
  if [ "$refused" = 1 ]; then
    die "APPLIANCE_STATE_DIR '$dir' is not a dedicated directory; $2"
  fi
  # The directory must not hold what install.sh and androidctl keep elsewhere.
  case "$here/" in
    "$resolved"/*) die "APPLIANCE_STATE_DIR '$dir' contains this installer; $2" ;;
  esac
  for shared in "$HOME" "$data_home" "$config_home" "$runtime_base" "$config_dir" "$quadlet_dir" "$bin_dir" "$runtime_dir"; do
    case "$(realpath -m -- "$root$shared")/" in
      "$root$resolved"/*) die "APPLIANCE_STATE_DIR '$dir' contains $shared; $2" ;;
    esac
  done
  if [ -e "$root$resolved" ] && [ ! -O "$root$resolved" ]; then
    die "APPLIANCE_STATE_DIR '$dir' is not owned by $user; $2"
  fi
  echo "$resolved"
}

uninstall() {
  # Without the file the defaults would name the default instance, which a
  # mistyped --config must not remove.
  [ -f "$root$config_file" ] ||
    die "the configuration file $config_file does not exist, so install.sh cannot tell which appliance to remove; give the --config of the instance"
  read_config
  load_settings
  read_images
  local target='' container_image owner
  # Everything that is not this instance's is refused before anything is
  # stopped or deleted.
  if [ -f "$root$unit_file" ]; then
    grep -q '^# Managed by install.sh' "$root$unit_file" ||
      die "$unit_file exists, and install.sh did not write it; nothing was removed"
    check_unit_owner
  fi
  if [ "$purge" = 1 ]; then
    target=$(dedicated_dir "$state_dir" "delete it yourself")
    if [ -e "$root$v1_unit" ] &&
      overlaps "$target" "$(realpath -m -s -- "$(v1_setting APPLIANCE_STATE_DIR /var/lib/android-appliance)")"; then
      die "APPLIANCE_STATE_DIR '$state_dir' holds the state of the v1 appliance; delete it yourself"
    fi
    owner=$(state_owner "$target")
    if [ -n "$owner" ] && [ "$owner" != "$name" ]; then
      die "the state directory $state_dir belongs to the appliance '$owner'; delete it yourself"
    fi
    if [ -z "$owner" ] && ! unused_dir "$target" && ! looks_like_state "$target"; then
      die "the state directory $state_dir is not empty and holds no appliance state; delete it yourself"
    fi
  fi

  if [ -z "$root" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop "$unit" 2>/dev/null || true
  fi
  rm -f "$root$unit_file"
  if [ -z "$root" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user reset-failed "$unit" 2>/dev/null || true
  fi
  # A container from the --print-run command runs without the unit.
  if [ -z "$root" ] && command -v podman >/dev/null 2>&1 && podman container exists "$name"; then
    container_image=$(podman container inspect --format '{{.ImageName}}' "$name")
    if [ -n "$container_image" ] && { [ "$container_image" = "$current_image" ] || [ "$container_image" = "$previous_image" ]; }; then
      podman stop -t 150 "$name" >/dev/null
      podman rm -f "$name" >/dev/null
    fi
  fi
  if [ -n "$current_image" ] && [ "$current_built" = 1 ]; then remove_image "$current_image"; fi
  if [ -n "$previous_image" ] && [ "$previous_built" = 1 ]; then remove_image "$previous_image"; fi
  rm -f "$root$override_file" "$root$images_file" "$root$migration_file"
  rm -rf "${root}${runtime_dir:?}"
  local link=$root$bin_dir/androidctl
  if [ -L "$link" ] && ours_androidctl "$link"; then rm -f "$link"; fi
  echo "install.sh: removed the Android appliance $name"

  if [ "$purge" = 1 ]; then
    if ! rm -rf -- "${root}${target:?}" 2>/dev/null; then
      # Files that another container user created belong to a
      # subordinate uid of this user.
      if [ -n "$root" ] || ! podman unshare rm -rf -- "$target"; then die "could not delete $target"; fi
    fi
    rm -f "$root$config_file"
    rmdir "$root$config_dir" 2>/dev/null || true
    echo "install.sh: deleted the state directory $target and the configuration $config_file"
  else
    # Another appliance may adopt the directory now.
    if [ "$(state_owner "$state_dir")" = "$name" ]; then rm -f "$root$state_dir/$state_marker"; fi
    echo "install.sh: kept the state directory $state_dir and the configuration $config_file"
    echo "install.sh: --uninstall --purge deletes them, with all apps and data of Android"
  fi
}

case $action in
  install | rollback | print-run) install_appliance ;;
  uninstall) uninstall ;;
  migrate) migrate ;;
  migrate-back) migrate_back ;;
esac
