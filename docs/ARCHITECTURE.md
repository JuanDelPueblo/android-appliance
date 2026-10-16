# Android Appliance v2: architecture

This document is the design of record for v2. It states what each part
does, the contracts between the parts, and the facts that the design
rests on. The facts come from measurements on a headless Fedora 44 host
(Intel UHD 630, rootless Podman 5.8, crun, pasta, SELinux enforcing)
with emulator 37.2.12 and the API 35 Google Play image.

## Goals

- One self-contained Podman container. The host needs Podman, KVM and,
  for GPU rendering, a DRM render node. Nothing else.
- Rootless. The container runs as the invoking user, with all
  capabilities dropped and SELinux confinement (`container_t`).
- The v1 lifecycle guarantees stay: Android cold boots on demand, stops
  when idle, stops gracefully, and keeps its userdata, apps and accounts.
- One control backend, used by `androidctl`, the HTTP API, the dashboard
  and the MCP server. No systemd or Hermes dependency inside it.
- Emulator-native WebRTC replaces scrcpy, Xvnc-as-display and noVNC.

## Overview

```text
host                                     container "android-appliance"
────                                     ───────────────────────────────────────────
androidctl ──unix socket──────────────►  appliance daemon (python3 -m appliance serve)
  ($XDG_RUNTIME_DIR/android-appliance/     ├─ supervisor ──► emulator (qemu, -no-window)
   api.sock)                               │                  └─ Xvnc :0 -rendernode (GPU GLX only)
browser / MCP client ─TCP 6090─(pasta)─►   ├─ HTTP API, dashboard, MCP (/mcp), WebSocket
browser media ──TURN 3478 udp/tcp───────►  ├─ coturn (relay to the container only)
                                           └─ adb server, gRPC client (emulator :8554)
/var/lib/... or ~/.local/share/... ──────► /data  (AVD, adb keys, appliance state)
```

The emulator's console (5554), adb (5555), gRPC (8554), the adb server
(5037) and the TURN relay ports exist only in the container network
namespace: nothing on the host or on the network reaches them. The host
publishes two ports, by default on 127.0.0.1:

| Port | Use |
|---|---|
| `APPLIANCE_PORT` (6090/tcp) | dashboard, REST API, WebSocket session, MCP |
| `APPLIANCE_TURN_PORT` (3478/udp+tcp) | WebRTC media through TURN |

### What the guest can reach

Android can reach the container, which the container does not stop. The
emulator's user-mode network maps the address 10.0.2.2 to the loopback
of the network namespace in which qemu runs, which is the container's.
Any app with the `INTERNET` permission (no other grant), and any page in
the guest's browser, can therefore open every port that listens on the
loopback or on `0.0.0.0` in the container. Measured on the API 36 image:
`10.0.2.2:5037` answers `host:version`, `10.0.2.2:5554` asks for the
console token, `10.0.2.2:8554` speaks gRPC, and Chrome in the guest
rendered `http://10.0.2.2:8080/api/v1/health` of the daemon. The
emulator's netsim daemon also listens on the loopback. What protects each
port:

| Port | Protection |
|---|---|
| console 5554 | the console token (`/data/home/.emulator_console_auth_token`, mode 0600) |
| gRPC 8554 | `-grpc-use-token`: a random token per start, in the discovery file only (see Emulator command) |
| daemon 8080 | the API token or a session cookie, as for any TCP client |
| adb server 5037 | none |

The adb server has no authentication: it trusts its local clients and
holds the adb key. An app that talks to it gets the `shell` user:
`input`, `screencap`, `pm`, `logcat`, installs, and the files of
`/data/local/tmp`. This cannot be closed from inside the container.
Measured: the emulator registers itself with the adb server on
`127.0.0.1:<ANDROID_ADB_SERVER_PORT>`. With the server on a unix socket
(`adb -L localfilesystem:...`, `ADB_SERVER_SOCKET` in the environment of
the emulator), the server never found the emulator in 60 s and the
emulator's own adb calls failed. adb refuses any other listen address
than `localhost` or `*` (`listening on specified hostname currently
unsupported`), so the server cannot move to `127.0.0.2`, which the guest
does not reach. The emulator offers no switch that blocks the host
loopback and keeps the internet: the only user-mode option is
`restrict=on`, which blocks both. The daemon cannot hide behind another
address either, because the guest reaches the container's own address
over the same network.

So the container does not contain an untrusted app. The device is as
trusted as its owner: do not install apps you do not trust on a device
that holds a Google account.

## Container runtime

### GPU

Measured facts (emulator 37.2.12):

- The only hardware-GPU path is `-gpu host` with an X display. Without
  one, the renderer fails (`Could not start renderer`) and qemu hangs and
  ignores SIGTERM. `ANDROID_EGL_ON_EGL=1` loads the SDK's SwiftShader EGL
  and either segfaults or renders black frames. There is no X-free
  hardware path.
- An in-container TigerVNC `Xvnc` started with `-rendernode` gives DRI3
  GLX on the GPU (Mesa iris). It is only a GLX server: no VNC client
  connects, and no TCP port is open. It booted Android 16 of 16 times
  (first boot about 30 s, later cold boots about 13 s).
  `-rfbport -1` alone is fatal; a unix VNC path is required:
  `Xvnc :0 -rendernode /dev/dri/renderD128 -geometry 1080x1920 -depth 24 -rfbport -1 -rfbunixpath /tmp/xvnc.sock -rfbunixmode 0600 -SecurityTypes None -nolisten tcp`
- Measured with v2 and the API 36 image on the GPU: 2 of 23 first boots
  of a new AVD ended with a qemu SIGSEGV in a gfxstream `RenderThread`
  about 3 s after `sys.boot_completed`. The emulator log ends with GL
  calls without a current context (`gles_v2_imp.cpp ... error null ctx`,
  the calls of a color buffer creation, then `glGetString`). No cold
  boot crashed. The crash lands after the daemon reported `running`
  (measured: `running` at 34 s, the crash 3 s later). The daemon boots
  once more by itself (see Lifecycle), with the userdata of the first
  boot; the command that triggered the boot and the commands that
  follow succeed.
- Software fallback: `-gpu lavapipe` (ANGLE on llvmpipe) needs no X and no
  DRM device and booted 10 of 10 times. It is slower (about 5-10 %
  janky frames, about 22 % of a core when idle).
- `swiftshader_indirect`, `swiftshader`, `software` and `guest` segfault
  about 10 s into boot, also without SELinux. Do not offer them.
- Vulkan off (`-feature -Vulkan`) and on both work with the GPU path. The
  default is off, as in v1.

`APPLIANCE_GPU` is `auto` (default), `host` or `lavapipe`. `auto`
resolves to `host` when a render node is passed into the container and
`glxinfo -B` on the Xvnc display reports a non-`llvmpipe` renderer.
Otherwise it resolves to `lavapipe`. The resolved mode is in the status.

### Emulator command

```text
emulator -avd <name> -port 5554 -no-window -no-audio -no-boot-anim -no-metrics \
  -crash-report-mode disabled -gpu <host|lavapipe> \
  -feature -QuickbootFileBacked[,-Vulkan] -no-snapshot -grpc 8554 -grpc-use-token
```

The port 8554 is never published, but the guest reaches it (see "What
the guest can reach"). `-grpc <port>` alone turns the authentication off
(measured: `Started GRPC server at [::]:8554, security: Insecure, auth:
none`, and a warning about missing protection). With `-grpc-use-token`
the emulator logs `Started GRPC server at 127.0.0.1:8554, security:
Local, auth: +token`, writes a new random `grpc.token` into the discovery
file at every start, and answers a call without `authorization: Bearer
<token>` with `UNAUTHENTICATED`. The daemon reads the token from the
discovery file and sends it with every call. The file is in
`/data/home/.android/`, which the guest cannot read. The launcher
execs `qemu-system-x86_64-headless`, so the spawned PID is the qemu PID.
The discovery file is `$ANDROID_EMULATOR_HOME/avd/running/pid_<pid>.ini`
(`grpc.port`, `grpc.token`, `port.adb`, `port.serial`); it counts as
written when it names both the port and the token.

Crash reporting, measured with a forced `SIGSEGV` of qemu: without
`-crash-report-mode disabled` the emulator runs `crashpad_handler` for
its whole life with `--url=https://clients2.google.com/cr/report`, and a
crash writes a 1.1 MB minidump into `/tmp/android-<user>/emu-crash-<version>.db`
(the container's 512 MB tmpfs). `-no-metrics` withholds the upload
consent: the database has `uploads_enabled=0`, the report's metadata
shows `upload_attempts=0`, and no connection left the handler in the
30 s after the crash. With `-crash-report-mode disabled` the emulator
logs "Crashreporting disabled, not reporting crashes.", runs no handler,
writes no dump, and boots as before. The option is in `emulator -help-all`.

### Podman options

```text
--userns=keep-id --cap-drop=ALL --security-opt no-new-privileges
--read-only --tmpfs /tmp:rw,size=512m --ulimit core=0 --init
--device /dev/kvm [--device /dev/dri/renderD128] [--group-add keep-groups]
--tz=local --stop-timeout 150
-v <state>:/data:Z,nosuid,nodev -v $XDG_RUNTIME_DIR/android-appliance:/run/appliance:Z
-p <listen>:<port>:8080/tcp -p <listen>:<turn>:3478/udp -p <listen>:<turn>:3478/tcp
```

- Nothing failed under real `container_t` with these options;
  `label=disable` is not needed. A host may set `label = false` in
  `containers.conf` (juno does, for Samba-labelled pool data). The unit
  therefore sets `CONTAINERS_CONF_OVERRIDE` to a file with `label = true`
  for this container only, so `:Z` relabels the state directory and the
  container gets its own MCS categories.
- `--group-add keep-groups` is needed only when the render node or
  `/dev/kvm` is not world-accessible and the user reaches it through the
  `render`/`kvm` groups.
- `--shm-size` is not needed (no `/dev/shm` use was measured).
- `--ulimit core=0` keeps a crashing qemu from writing 400 MB cores.
- `/data` is mounted `nosuid,nodev`: nothing in it needs setuid files or
  device nodes. `/tmp` stays without `noexec`: no capability and
  `no-new-privileges` already take away what an executable there could
  gain, and the emulator and Mesa write and map files in `/tmp`.
- The SDK in the image is read-only; the emulator writes only to
  `$HOME`, `/tmp` and the AVD directory.
- The unit also has `--pull=never` (the image is local), the health
  check `/opt/appliance/venv/bin/python -m appliance healthcheck` (over
  the unix socket), and `--env` for the values that `install.sh`
  resolved: `APPLIANCE_LISTEN_ADDRESS`, `APPLIANCE_PORT`,
  `APPLIANCE_TURN_PORT`, `APPLIANCE_GPU`. `--env` wins over
  `--env-file`, so an environment override at install time reaches the
  daemon as well.
- Measured with a test image on juno: the unit starts in about 1 s as
  `container_t` with its own MCS categories, uid 1000, no capabilities,
  `NoNewPrivs`, a read-only root, and the ports on 127.0.0.1 only. The
  host user reaches the relabelled socket. The `podman run` command of
  `install.sh --print-run` gives the same container.

### Image

`Containerfile` builds `localhost/android-appliance:<version>-api<level>`
on `registry.fedoraproject.org/fedora:44`:

1. Runtime libraries of the emulator (found with `ldd` and with the
   `dlopen` names `libGL.so.1`, `libEGL.so.1`, `libGLESv2.so.2`,
   `libX11.so.6`, `libX11-xcb.so.1`, `libvulkan.so.1`, `libfreetype.so.6`,
   `libpulse.so.0`), Mesa (`mesa-dri-drivers`, `mesa-libEGL`, `mesa-libGL`,
   `mesa-libgbm`, `mesa-vulkan-drivers`, glvnd), `tigervnc-x11-server`,
   `glx-utils`, `coturn`, `python3`, `iproute`, `procps-ng`.
   Fedora 44 has no `libtiff.so.5`; link it to `libtiff.so.6` (only a Qt
   plugin that is never loaded headless uses it).
2. The pinned SDK from `conf/sdk.lock` (emulator, platform-tools, and the
   system image of build argument `API_LEVEL`), each archive checked with
   SHA-256. The emulator and platform-tools are one layer; the system
   image is a second layer, so images for two API levels share the first.
3. A Python virtual environment from `requirements.lock` (pinned with
   hashes), and the gRPC stubs generated from the protos in
   `appliance/proto/` (copied from the pinned emulator, Apache 2.0).
4. The `appliance` package, `androidctl` and the dashboard (`web/`).

The SDK archives are downloaded when the user builds the image. The
image is not published, because the SDK license does not allow
redistribution.

`scripts/fetch-sdk.sh` installs the SDK in the build. It checks the
SHA-256 of each archive and that `Pkg.Revision` in its
`source.properties` is the pinned revision. Build arguments: `API_LEVEL`
(default 36), `VERSION`, `SDK_REPOSITORY` (a mirror),
`SDK_EMULATOR_VERSION`, `SDK_PLATFORM_TOOLS_VERSION` and
`SDK_IMAGE_REVISION`. An empty revision selects the first lock line of
the package; for the system image, the first line of that API level.

| Path or setting | Content |
|---|---|
| `/opt/android-sdk` | emulator, platform-tools, system image (root, readable by all) |
| `/opt/appliance/venv` | Python environment; `appliance.pth` puts `/opt/appliance` on `sys.path` |
| `/opt/appliance/appliance` | the package, with the stubs in `_proto/` |
| `/opt/appliance/bin/androidctl` | the same `androidctl` as on the host; it uses `/run/appliance/api.sock` |
| `/opt/appliance/web`, `/opt/appliance/VERSION` | dashboard (when present), version |
| `/data`, `/run/appliance` | mount points (root, mode 0755) |
| environment | `PATH` with the venv, `/opt/appliance/bin`, `emulator/` and `platform-tools/` first; `ANDROID_SDK_ROOT=ANDROID_HOME=/opt/android-sdk`, `HOME=/data/home`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1` |
| command | `/opt/appliance/venv/bin/python -m appliance serve`; no `USER` (keep-id sets it) |
| labels | `org.opencontainers.image.version`, `android-appliance.api-level` |

- The bytecode is compiled in the build with hash-based checks, so it
  stays valid on the read-only root file system whatever the file times.
- The image has no `HEALTHCHECK`. Podman builds the OCI format by
  default, which drops it. The Quadlet unit sets the health check.
- The SDK is downloaded in a build stage of its own, so a change of the
  runtime packages or of the Python code does not download it again.
  The build cache keeps that stage, which is a second copy of the SDK
  (3.4 GB for one API level). `podman image prune` frees it; the next
  build then downloads again.
- Verified: a GPU boot with a writable root file system changes nothing
  outside `/data` and `/tmp` (`podman diff`). A boot also works with
  `--read-only-tmpfs=false` (read-only `/run`, `/var/tmp` and `/dev/shm`).

Measured on juno:

| Build | Time | Image |
|---|---|---|
| API 36, empty cache | 492 s | 4.21 GB |
| API 36 again, after a change of the runtime packages | 316 s, no download | 4.21 GB |
| API 35 after API 36 | 224 s, only the system image is downloaded | 4.03 GB |
| a change in `appliance/` only | 90 s | |

Layers: Fedora 191 MB, packages 555 MB, emulator and platform-tools
926 MB, system image 2.45 GB (API 36) or 2.27 GB (API 35), Python
environment 88 MB, appliance under 1 MB. The images for API 35 and 36
share 1.67 GB and use 6.6 GB together.

## Persistent state

The state directory is mounted at `/data`. The layout keeps the v1
layout, so a v1 state directory is adopted as it is:

| Path | Content |
|---|---|
| `avd/<name>.ini`, `avd/<name>.avd/` | AVD configuration and userdata (v1 compatible) |
| `home/.android/` | adb keys (`adbkey`, `adbkey.pub`), emulator settings |
| `home/.cache/` | Mesa shader cache |
| `screenshots/` | screenshots saved by `androidctl` |
| `appliance/state.json` | daemon metadata (AVD origin, first-boot flag, activity time, crash history) |
| `appliance/secrets/` | `api-token`, `session-key`, TURN secret (files 0600, directory 0700) |
| `appliance/logs/` | `emulator-<n>.log` of the last 5 boots, `xvnc.log` |
| `appliance/tmp/` | APKs and files in transit; emptied when the daemon starts |
| `appliance/daemon.lock` | single-daemon lock (flock) |

The daemon writes `state.json` and the AVD ini files atomically (a
temporary file, then a rename). Writing `state.json` is best effort: when
the disk is full or read-only, the daemon logs the error once, keeps the
data in memory and writes it with the next save. The lifecycle (crash
handling, stop, idle stop, waiting callers) never depends on the write.

The state directory is private: `install.sh` sets its mode to 0700 (also
for a directory that existed), and the daemon does the same for `/data`
when it starts. The files inside keep the modes that the emulator gives
them; the directory keeps other users out of them. `install.sh` also
writes `.android-appliance-instance` into it, the name of the instance
that uses it (see Installation).

The AVD registry `avd/<name>.ini` has an absolute `path=`. A v1 registry
holds the host path, which does not exist in the container. The daemon
does not edit it (a rollback to v1 needs it), and does not recreate a
missing one. It writes a registry with `path=/data/avd/<name>.avd` to
`/tmp/appliance/avd/` (tmpfs) and points `ANDROID_AVD_HOME` there. The v1
check that refused a registry that points outside the state directory is
therefore not needed. For an AVD that it creates, the daemon also writes
`avd/<name>.ini` with the v1 keys.

The environment of the emulator: `HOME=/data/home` (also its working
directory), `ANDROID_USER_HOME=ANDROID_EMULATOR_HOME=/data/home/.android`,
`ANDROID_AVD_HOME=/tmp/appliance/avd`,
`ANDROID_SDK_ROOT=ANDROID_HOME=/opt/android-sdk`,
`ANDROID_ADB_SERVER_PORT=5037`, `USER` (`android` when unset; without it
the emulator logs errors about `/tmp/android-unknown`), `DISPLAY=:0` (GPU
mode). Every adb call uses the same environment, so adb and the emulator
share the keys in `home/.android`.

## Configuration

One file, `${XDG_CONFIG_HOME:-~/.config}/android-appliance/appliance.conf`,
with `KEY=value` lines as in v1. `install.sh` reads the host keys; the
container gets the file as its environment file.

| Key | Default | Used by | Meaning |
|---|---|---|---|
| `APPLIANCE_STATE_DIR` | `${XDG_DATA_HOME:-~/.local/share}/android-appliance` | install | host state directory |
| `APPLIANCE_LISTEN_ADDRESS` | `127.0.0.1` | install | address of the published ports |
| `APPLIANCE_PORT` | `6090` | install, daemon | HTTP port on the host |
| `APPLIANCE_TURN_PORT` | `3478` | install, daemon | TURN port on the host |
| `APPLIANCE_TURN_HOST` | empty | daemon | host name for TURN URLs; empty means the host name of the page |
| `APPLIANCE_API_LEVEL` | `36` | install (build), daemon | system image of the image build; the daemon checks it against the image |
| `APPLIANCE_GPU` | `auto` | install, daemon | `auto`, `host` or `lavapipe` |
| `APPLIANCE_GPU_DEVICE` | `auto` | install | render node; `auto` is the first `/dev/dri/renderD*` |
| `APPLIANCE_AVD_NAME` | `android` | daemon | AVD name |
| `APPLIANCE_IDLE_STOP_MINUTES` | `60` | daemon | idle shutdown; `0` disables it |
| `APPLIANCE_MEMORY_MIB` | `4096` | daemon | guest RAM |
| `APPLIANCE_CORES` | `4` | daemon | guest CPU cores |
| `APPLIANCE_DISK_SIZE` | `32G` | daemon | userdata size of a new AVD |
| `APPLIANCE_BOOT_TIMEOUT` | `900` | daemon | seconds before a boot fails |
| `APPLIANCE_TRUST_PROXY` | `0` | daemon | honour `X-Forwarded-Proto`/`-Host`/`-For` from a reverse proxy |
| `APPLIANCE_MAX_UPLOAD_MIB` | `4096` | daemon | largest push or APK install |
| `APPLIANCE_SDK_*` | lock defaults | install (build) | SDK revisions, as in v1 |
| `APPLIANCE_NAME` | `android-appliance` | install, androidctl | container and unit name (tests use another) |
| `APPLIANCE_BIN_DIR` | `~/.local/bin` | install | where `androidctl` is linked |
| `APPLIANCE_QUADLET_DIR` | `~/.config/containers/systemd` | install | where the unit is written (tests use `$XDG_RUNTIME_DIR/containers/systemd`) |
| `APPLIANCE_IMAGE` | `localhost/android-appliance:<version>-api<level>` | install | image to build and run |

`install.sh --config FILE` and `androidctl` with `ANDROID_APPLIANCE_CONFIG=FILE`
select another configuration file, and so another instance. The unix
socket of an instance is `$XDG_RUNTIME_DIR/<APPLIANCE_NAME>/api.sock`.

Podman reads the same file as `--env-file`, so `install.sh` accepts only
comments, empty lines and `APPLIANCE_*=value` lines, and stops on any
other line. `~` and variables are not expanded; the keys with a
home-directory default stay commented out in the example.

The daemon reads the keys from its environment; an empty value counts
as unset, and an invalid value stops it with a message that names the
key. It takes the API level from the system image in the SDK
(`system-images/android-<N>/google_apis_playstore/x86_64`); when
`APPLIANCE_API_LEVEL` is set, it must match. For tests and development
runs only, the daemon also reads `APPLIANCE_DATA_DIR` (`/data`),
`APPLIANCE_RUN_DIR` (`/run/appliance`), `APPLIANCE_TMP_DIR` (`/tmp`),
`APPLIANCE_HTTP_PORT` (`8080`), `APPLIANCE_SDK_ROOT` (`/opt/android-sdk`),
`APPLIANCE_WEB_DIR` (`web/` next to the package), the programs
`APPLIANCE_EMULATOR`, `APPLIANCE_ADB`, `APPLIANCE_XVNC`, `APPLIANCE_GLXINFO`,
`APPLIANCE_RENDER_NODE` (instead of the first `/dev/dri/renderD*`), and
`APPLIANCE_CONFIG` (a file in the format above that fills the keys the
environment does not set).

## Control daemon

`python3 -m appliance serve` is the container's main process (PID 1 is
catatonit from `--init`). It is asyncio-based (Starlette on uvicorn,
grpc.aio). The modules:

| Module | Job |
|---|---|
| `config` | read and validate `APPLIANCE_*` |
| `avd` | create, validate and adjust the AVD (port of v1 `avd-init`) |
| `emulator` | Xvnc, the emulator process, discovery, logs, locks |
| `adb` | async adb wrapper |
| `grpc_client` | emulator gRPC: screenshot, input stream, clipboard, Rtc |
| `input` | key table and input encoding (see Input) |
| `supervisor` | lifecycle state machine, idle policy, leases, crashes |
| `backend` | the operations; one implementation used by every front end |
| `client` | the same operations over HTTP (unix socket or TCP), for MCP stdio and tests |
| `api` | REST routes, auth, static dashboard, WebSocket session |
| `auth` | API token, sessions, one-time login links |
| `turn` | coturn configuration and process, TURN credentials |
| `webrtc` | signaling bridge for one stream |
| `mcp_server` | MCP tools on top of `backend` |

### Lifecycle

States, as in v1: `stopped`, `starting`, `running`, `stopping`.
`running` means `sys.boot_completed=1` and the post-boot setup is done.

- **start**: stopped launches; starting joins the current boot; running
  returns; stopping waits for the stop, then launches. Concurrent starts
  share one boot. A caller's `timeout` only bounds its wait; the boot
  goes on. A start that does not wait (`wait: false`) returns at once,
  also while a stop is running, and boots when the stop is done.
- **stop**: running and starting stop gracefully (a start in progress
  fails with `conflict`, "android was stopped during the start");
  stopped returns; stopping joins.
- **restart**: stop, then start.
- **Launch**: check that no other emulator uses the AVD and remove stale
  locks (first: an AVD that another emulator holds keeps its
  configuration); AVD check and setup; `adb start-server`
  (the first start writes the adb key, which the emulator gives to a new
  AVD); start Xvnc (GPU); start the emulator; wait for the discovery file,
  gRPC (`getStatus`) and adb (`get-state`); wait for
  `sys.boot_completed=1`; post-boot setup (`svc power stayon true`,
  `wm dismiss-keyguard`, `settings put secure stylus_handwriting_enabled 0`;
  best effort, as in v1); state `running`.
  The activity time is set at the end of the boot.
- **Stylus setting**: the emulated touchscreen reports itself as a stylus
  (`TOUCHSCREEN | STYLUS`), although the appliance sends finger touches
  only (`ABS_MT_TOOL_TYPE=0`). Measured on API 36 with two fresh AVDs:
  the first finger tap on a text field (Contacts "First name") opened
  Gboard's "Try out your stylus" sheet, and the next `text` went into the
  sheet instead of the field; later focuses showed Gboard's floating
  handwriting toolbar instead of the keyboard. With the setting applied
  before the first focus, a fresh AVD showed the normal keyboard and the
  paste reached the field exactly. (An earlier API 35 run saw no sheet;
  the spike saw it on API 35 too.)
- **Graceful stop**: `adb shell sync` (up to 15 s), then one
  `adb emu kill`, wait up to 60 s (measured 1-3 s), then SIGTERM, wait
  10 s, then SIGKILL; then stop Xvnc. When the device does not answer
  yet (early boot), the stop goes on with SIGTERM. Never send a second
  kill while qemu tears down (Phase 0.1 saw SIGABRT cores). The sync is
  needed: `emu kill` does not flush the guest page cache. Measured: files
  written 5-10 s before the stop were missing or empty after the next
  boot; with the sync (0.1 s) they were complete.
- **Failure detection**: a qemu exit while starting or running is a crash.
  `Could not start renderer`, `Could not initialize emulated framebuffer`
  or `FATAL` in the emulator log means qemu will hang; kill it
  (SIGKILL). A boot that exceeds `APPLIANCE_BOOT_TIMEOUT` stops the
  emulator gracefully (`timeout`). `unauthorized` that lasts more than
  30 s fails the boot with the v1 guidance ("device unauthorized: the AVD
  trusts another adb key; copy the old key into ... then restart"). A
  crash or failure sets `stopped` with `last_error` (kind, message, exit
  status, log tail, time) and adds it to the crash history in
  `state.json`. The kinds: `avd`, `locks`, `gpu`, `launch`, `adb`,
  `crash`, `renderer`, `timeout`, `unauthorized`, `internal`. A waiting
  start gets the error (`boot_failed`, or `timeout`) once Android is
  stopped. A crash at boot gets one retry (see Crash retry); every other
  failure leaves Android stopped, and the next use cold boots.
- **Power-off from the guest**: a qemu exit with status 0 and no `FATAL`
  line (`adb shell reboot -p`, `svc power shutdown`) is a shutdown that
  the user asked for. Android stays stopped: no retry, no `last_error`,
  no crash-history entry (the log says "android powered off"). A start
  that waits gets `conflict`, "android powered off during the start".
  Measured: `reboot -p` 3 s after `running` made qemu exit 0.
- **Crash retry**: a `crash` (qemu exits without a `FATAL` line) during
  the boot, or within 30 s after `running`, counts as a failed boot: the
  daemon tears down and boots once more (phase `restarting`), a start
  that is waiting keeps waiting, and commands that arrive join the new
  boot. The crash goes into the crash history with `retried: true`, the
  daemon log says "starting android again", and `last_error` stays
  `null`. There is one retry per start request, so a repeated crash is
  reported as before; the other kinds are never retried. A command that
  was talking to the emulator when it died gets `unavailable` (503), also
  when a gesture or a paste is cut off in the middle, and also when the
  retry has already closed its connection. A stop that arrives while the
  retry waits for the crashed boot to unwind wins: the retry never
  launches. Measured:
  the first-boot GPU crash (see GPU) lands 3 s after `running`, so the
  window covers it.
- **Stale locks**: after a kill, `<avd>.avd/hardware-qemu.ini.lock` keeps
  the old qemu PID. Container PIDs are small and repeat, so the next
  emulator often sees a live process with that PID and stops with
  `Running multiple emulators with the same AVD`. Before each launch the
  daemon removes `hardware-qemu.ini.lock` and `running/pid_*.ini`. This is
  safe only when no other emulator uses the AVD: the daemon holds
  `appliance/daemon.lock` for its whole life, and checks that no other
  process (for example a v1 emulator on the host) holds a lock on the AVD
  files before it removes anything; otherwise the start fails (`locks`).
  Measured: a running emulator holds `flock` on `multiinstance.lock`, and
  qemu holds OFD locks on the disk images (`*.img`, `*.qcow2`). The kernel
  releases both when qemu dies. `/proc/locks` lists a `flock` only when
  its owner is in the reader's PID namespace, so a container does not see
  the lock of an emulator on the host or in another container. The daemon
  therefore probes the locks themselves: a non-blocking `flock` on
  `multiinstance.lock` and `F_OFD_GETLK` on the images. The probe found
  the locks of an emulator in another container, and of a holder outside
  the PID namespace of the check.
- **Interrupted first boot**: a first boot that is killed leaves userdata
  that answers `unauthorized` for good. `state.json` records whether the
  daemon created the AVD and whether its first boot completed. A new AVD
  also gets the file `appliance-first-boot` with the random id that
  `state.json` records; both are cleared as soon as the guest reports
  `sys.boot_completed=1`, before the post-boot setup, so a crash after
  that point keeps the userdata. An AVD with a matching id and no
  completed boot is recreated (verified on the device: the new AVD
  booted normally). An adopted AVD, an AVD without the file, and an AVD
  written into a directory that already held files are never deleted.
  The `unauthorized` guidance was verified on the device: after the adb
  key of a booted AVD was replaced, the start failed with it; copying
  the old `adbkey` and `adbkey.pub` back and restarting the unit booted
  the AVD in 15 s with its data.
- **Container stop** (SIGTERM): stop Android gracefully (new starts fail
  with `unavailable`), then stop the API and exit with status 0.
- **Container start**: state `stopped`. Android is not started.

### Activity and idle stop

As in v1: automation commands (screenshot, ui, input, shell, apps, files)
start Android when it is stopped, hold a lease while they run, and update
the activity time when they succeed. `start` and `restart` update it.
`status`, `wait`, `stop` and `logs` do not. Dashboard input and the start
of a video stream update it; an open dashboard without input does not.

The idle check runs every 30 s while Android is `running`. It stops
Android when no lease is held and the idle time is at least
`APPLIANCE_IDLE_STOP_MINUTES`. The activity time is persisted in
`appliance/state.json` (at most every 10 s, and at every stop), so it
survives a daemon restart. The idle check is one of the supervisor's
periodic jobs, which run only while Android is `running`; the session
front end adds its own (for example the display hash) the same way.

### Operations

| Operation | Implementation |
|---|---|
| screenshot | gRPC `getScreenshot` (PNG); scaled/JPEG on request; adb `screencap` fallback |
| ui | `uiautomator dump` with the v1 retry loop; XML, or a compact list for agents |
| tap, long-press, swipe | gRPC touch on the shared input stream |
| text | clipboard paste (exact unicode); `keys` method types ASCII keys |
| key | gRPC key from the key table; unknown names and numbers use `adb shell input keyevent` (Android ignores a name it does not know, without an error, as in v1) |
| shell | `adb shell` (exit status, stdout, stderr; output up to 64 MiB) |
| apps | `pm list packages` (`-3` unless all), `adb install`, `pm uninstall`, launch (`cmd package resolve-activity`, then `am start -n`), `am force-stop` |
| files | `adb push`, `adb pull` (through `appliance/tmp/`), `find` and `stat` for listing |

Text entry holds a lock, because the guest clipboard is shared. Every
key sender shares the 5 ms pacing, and every touch goes through the one
input stream; touch identifiers are unique per gesture.

Do not mix gRPC input and `adb shell input`: each cancels the other's
gestures. gRPC calls take about 1 ms; `adb shell input tap` about 18 ms.

## Input

Verified against Android (getevent, dumpsys, UI dumps, screenshots):

- **Coordinates** are device pixels of the natural 1080x1920 frame. The
  emulator does not clamp; clamp to `[0,1079]x[0,1919]`. Under guest
  rotation, screenshots stay 1080x1920 with the content drawn sideways,
  and touch stays in the natural frame.
- **Touch**: `InputEvent{touch_event{touches[{x, y, identifier, pressure}]}}`
  on one long-lived `streamInputEvent`. Pressure 1-1024; lift is
  `pressure=0` for the same identifier (an empty list lifts nothing). At
  most 10 contacts. Bursts of 400 moves arrive complete and ordered.
- **Mouse**: left button as touch (a reserved identifier). Hover: drop.
  Right button: send `GoBack`. Wheel: `injectWheel` does nothing; send a
  short touch drag (about 168 px per 120 units of delta over 120 ms, with
  an 80 ms hold before the lift).
- **Keys**: printable ASCII as `KeyboardEvent{key=<char>, eventType=keypress}`;
  named keys as keydown/keyup with W3C names. Overrides: Menu is evdev 139
  (`ContextMenu` and `Menu` do nothing); Home button is `GoHome` (W3C
  `Home` moves the text cursor; `BrowserHome` and evdev 172 do nothing).
  `GoBack`, `AppSwitch`, `Power`, `AudioVolumeUp`, `AudioVolumeDown` work.
  Never forward `CapsLock` (it sticks in the guest), `Dead`, `Process` or
  `Unidentified`. Keep at least 5 ms between key events (unpaced keys are
  lost inside the emulator; touch needs no pacing).
- **Text**: `KeyboardEvent.text` loses characters after about 20, and
  every path drops non-ASCII; `adb shell input text` fails on unicode.
  Exact text is `getClipboard` (save), `setClipboard(text)`, wait 0.3 s,
  `KEY_PASTE` (evdev 135), wait 0.3 s, `setClipboard(saved)`.
- **Android key names** for the CLI, API and MCP: `HOME`→`GoHome`,
  `BACK`→`GoBack`, `APP_SWITCH`/`RECENTS`→`AppSwitch`, `POWER`→`Power`,
  `VOLUME_UP`/`VOLUME_DOWN`, `MENU`→evdev 139, `ENTER`, `TAB`, `DEL`
  (Backspace), `FORWARD_DEL` (Delete), `ESCAPE`, `DPAD_UP/DOWN/LEFT/RIGHT`
  (arrows), `MOVE_HOME`/`MOVE_END`, `PASTE`→evdev 135, `SPACE`, letters and
  digits. A `KEYCODE_` prefix is accepted, and names are not case
  sensitive. A bare number is an Android key code, as in v1 (`66` is
  `ENTER`), so the digit keys are `KEYCODE_0` to `KEYCODE_9`. `CAPS_LOCK`
  is refused. Typed keys are subject to Android's auto-capitalization
  (measured: `hello world` in a name field became `Hello World`); paste is
  exact.

## WebRTC

Measured facts:

- Signaling is `Rtc` v2 (`rtc_service_v2.proto`): `RequestRtcStream`, then
  `ReceiveJsepMessageStream` and `SendJsepMessage`. The emulator offers;
  the client answers. Messages: `{"start":{...}}`, `{"sdp","type":"offer"}`,
  flat candidates `{"candidate","sdpMid","sdpMLineIndex"}`, and
  `{"bye":"disconnected"}`. There is no end-of-candidates message. The
  emulator accepts the same shapes back (`{"type":"answer","sdp"}`).
- Without an `ice_server_config`, the emulator uses
  `stun:stun.l.google.com:19302` and puts the host's public address in the
  offer. Always pass `{ice_servers:[{urls:["stun:127.0.0.1:3478"]}]}`: host
  candidates only, no relay allocations on the emulator side.
- The offer has VP8 (chosen by Chromium), VP9, an audio track (even with
  `-no-audio`) and four data channels (`mouse`, `keyboard`, `touch`,
  `adb`). The appliance sends input over its own WebSocket instead (one
  path for every front end, exact activity tracking, input independent
  of the media state).
- The emulator sends a frame only when the display changes. A new viewer
  on a static screen gets no frame; a static stream decodes 0 fps.
- A client `bye` is ignored. After the client's ICE goes away, the
  emulator ends the stream with `{"bye":"disconnected"}` after about 17 s.
- An offer that is never answered stays open for good and leaks file
  descriptors, threads and, with emulator-side TURN, relay ports.
- When qemu dies, `ReceiveJsepMessageStream` fails at once with
  `UNAVAILABLE`; the browser sees ICE failure only after about 15 s.

Design:

- **TURN**: coturn in the container, `use-auth-secret` (TURN REST
  credentials: `username=<expiry>:<id>`,
  `credential=base64(HMAC-SHA1(secret, username))`, 24 h), relay range
  49152-49251, `no-tcp-relay`, `no-multicast-peers`, quotas, and
  `denied-peer-ip` for everything except the container's own addresses
  (`allowed-peer-ip`, computed at start with `ip -o addr show scope global`).
  Verified: relaying to any other address gets 403. coturn 4.18 rejects
  `no-dtls` and `no-cli` (both already defaults).
- **Browser**: `RTCPeerConnection` with only the TURN server and
  `iceTransportPolicy: "relay"`, answer only, trickle ICE. The TURN URLs use
  `APPLIANCE_TURN_HOST`, or else the host name of the page, with
  `APPLIANCE_TURN_PORT`, for `udp` and `tcp`. Media stays on one known
  port whatever the network; local and remote access use the same path.
- **Bridge** (`webrtc`): one emulator stream per viewer, requested only
  when the browser asks for it. The bridge drops the emulator's `start`
  (it echoes the ICE configuration), sends the browser its own start with
  the TURN configuration, never forwards empty candidates, and forwards
  `bye` and gRPC stream ends as `stream ended`. If a viewer goes away
  before it answers, the bridge sends the emulator a throwaway answer, so
  ICE fails and the emulator releases the stream. Limit: 4 streams.
- **First frame and freezes**: the dashboard shows a gRPC screenshot as a
  poster until the first video frame arrives. While a stream runs, the
  daemon samples a small gRPC screenshot (for example 54x96 RGB) once per
  second and sends its hash as a `display` event. If the hash changes and
  no new frame is decoded within 3 s, the dashboard reconnects the stream.
  The device settings are not changed (no seconds clock).
- **Reconnection**: on ICE `failed`, a stream end, or a WebSocket close,
  the dashboard reconnects with backoff while the device is `running`.
  It never starts a stopped device by itself; the Start button does.
  A hidden tab closes its stream after 60 s and reconnects when visible.
- **Latency**: measured touch-to-decoded-frame was about 140 ms (median),
  including Android's own rendering. The dashboard sets
  `receiver.jitterBufferTarget = 0` (playout delay hint) where supported.

## HTTP API

Base path `/api/v1`. JSON in and out unless stated. Errors use an HTTP
status and `{"error": {"code": "<code>", "message": "<text>"}}`. Codes:
`bad_request` (400), `unauthorized` (401), `forbidden` (403),
`not_found` (404), `conflict` (409), `too_large` (413), `rate_limited` (429),
`boot_failed`/`timeout`/`unavailable` (503), `internal` (500). Unknown
paths give `not_found`, and a wrong method gives `bad_request` with
status 405. Automation requests start Android when needed and wait for
the boot; a failed boot gives `boot_failed` (or `timeout`).

| Method and path | Body or query | Result |
|---|---|---|
| `GET /health` | | `{"ok":true,"version"}`, no auth |
| `GET /status` | `?format=text` | status object; `text` gives `state=running boot_completed=1 idle_seconds=42` |
| `POST /start` | `{"wait":true,"timeout":900}` | status |
| `POST /stop` | `{"wait":true}` | status |
| `POST /restart` | `{"wait":true}` | status |
| `POST /wait` | `{"timeout":900}` | status; 409 when stopped |
| `GET /screenshot` | `?format=png\|jpeg&quality=85&max_size=<px>` | image; headers `X-Device-Width`, `X-Device-Height`, `X-Image-Scale` |
| `GET /ui` | `?format=xml\|compact` | XML text or `{"nodes":[...]}` |
| `POST /input/tap` | `{"x","y"}` | `{}` |
| `POST /input/long-press` | `{"x","y","duration_ms":800}` | `{}` |
| `POST /input/swipe` | `{"x1","y1","x2","y2","duration_ms":300}` | `{}` |
| `POST /input/text` | `{"text","method":"paste\|keys"}` | `{}` |
| `POST /input/key` | `{"key":"HOME"}` | `{}` |
| `POST /shell` | `{"command","timeout":300}` | `{"exit_code","stdout","stderr"}` |
| `GET /apps` | `?all=1` | `{"packages":[...]}` |
| `POST /apps/install` | APK body (`application/vnd.android.package-archive`), `?replace=1&grant=1&downgrade=1&test=1` | `{"output"}`; a refused APK is `bad_request` with the adb output |
| `POST /apps/{package}/launch` | | `{}`; `not_found` without the package or a launcher activity |
| `POST /apps/{package}/stop` | | `{}` |
| `DELETE /apps/{package}` | `?keep_data=1` | `{"output"}` |
| `GET /files` | `?path=` | file bytes |
| `PUT /files` | `?path=&name=`, raw body | `{"bytes"}`; a `path` that is a directory takes the file under `name` |
| `GET /files/list` | `?path=` | `{"entries":[{"name","type","size","mtime"}]}` |
| `GET /logs` | `?source=emulator\|daemon&lines=200` | text |
| `POST /auth/login-link` | local only | `{"url"}` (single use, 2 min) |
| `GET /auth/token` | local only | `{"token"}` |
| `POST /auth/token/rotate` | local only | `{"token"}` |
| `GET /session` | WebSocket | dashboard session (below) |

The status object: `state`, `boot_completed`, `idle_seconds`,
`idle_stop_seconds`, `uptime_seconds` (since the emulator started; 0
when stopped), `phase` (detail while starting: `preparing`, `gpu`,
`launching`, `connecting`, `waiting for adb`, `booting`, `setting up`,
and `restarting` after a retried crash),
`gpu` (resolved mode, `null` before the first launch), `api_level`,
`avd`, `viewers`, `last_error` (`null`, or `kind`, `message`,
`exit_status`, `log_tail`, `time`; cleared by the next start), `version`.

Device paths in `/files` must be absolute. `GET /files` refuses a
directory (`bad_request`), and so does `PUT /files` unless the request
gives a `name`; a missing path is `not_found`. A file
list entry has `type` `dir`, `file`, `link` or `other`, and `mtime` in
seconds.

Limits and validation. A `timeout` (JSON field or query) must be more
than 0 and at most 86400 seconds: NaN and infinity are refused, because
an `asyncio` timer with NaN can stop the event loop of the daemon, and a
request that never ends would hold a lease for ever. An upload
(`PUT /files`, `POST /apps/install`) is refused with `too_large` when it
exceeds `APPLIANCE_MAX_UPLOAD_MIB` or the free space of the state disk
less 1 GiB; a declared `Content-Length` is checked before the body is
read. Strings (`command`, `text`, `key`, ...) must not contain a NUL
character or unpaired surrogates, and query integers must be ASCII
digits; the daemon answers `bad_request`.

Every HTTP response carries `X-Frame-Options: DENY`, `Content-Security-Policy:
frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`; the API answers and the login page also
carry `Cache-Control: no-store`.

Outside `/api/v1`: `GET /login` is the login form; `POST /login` (form
field `token`) and `GET /login?code=<code>` (a login link) set the
session cookie. `GET /` serves the dashboard (`web/`, or a short page
when it is not installed); without a session it redirects to `/login`.

### Text formats for shell clients

`androidctl` is bash and has no JSON parser, so the API also speaks
plain text:

- A request with `Accept: text/plain` gets errors as `text/plain`: the
  message only, with the same HTTP status.
- `POST /input/text` and `POST /shell` accept a `text/plain` body (the raw
  UTF-8 text or command) as well as JSON. With a text body, the method is
  `?method=paste|keys` and the timeout `?timeout=<seconds>`.
- `POST /shell?format=raw` returns stdout as the body, the exit status in
  the `X-Exit-Code` header and stderr base64-encoded in `X-Stderr`
  (truncated to 16 KiB, with `X-Stderr-Truncated: 1`).
- `?format=text` gives line-oriented output for `status` (the v1 line),
  `apps` (one package per line), `files/list` (`type size name` per line),
  `auth/token` and `auth/login-link` (the value alone), `apps/install`
  and `DELETE /apps/{package}` (the adb output alone), and for
  `start`/`stop`/`restart`/`wait` (the status line).

### Authentication

Read "What the guest can reach" before you trust a device with accounts.

- The unix socket is trusted: file permissions protect it (the directory
  is mode 0700). `androidctl` on the host uses it.
- TCP requests need either `Authorization: Bearer <token>` or a session
  cookie. The token is random (32 bytes), stored in
  `appliance/secrets/api-token`; `androidctl token` prints it and
  `androidctl token --rotate` replaces it.
- The dashboard's login page accepts the token, or a one-time link from
  `androidctl dashboard`, and sets an HttpOnly, SameSite=Strict session
  cookie `appliance_session` (Secure over HTTPS) for 7 days. The cookie
  is signed with `appliance/secrets/session-key`. State-changing requests
  with a cookie must have a same-origin `Origin`; the WebSocket checks
  `Origin` too. A login answers with a page that moves on to `/`, not a
  redirect: a SameSite=Strict cookie set on a navigation from another
  site (a link opened from a terminal or a chat) is not sent on that
  navigation's redirect.
- A login link is `http://<APPLIANCE_LISTEN_ADDRESS>:<APPLIANCE_PORT>/login?code=<code>`
  (`127.0.0.1` for a wildcard address). It works once, within 2 minutes.
- `androidctl token --rotate` also replaces the session key and drops the
  pending login links, so it ends every dashboard session and every link
  made before it.
- Failed logins and wrong bearer tokens are rate limited: after 5 in a
  minute from one client, further wrong tokens get `rate_limited` until
  the minute is over. The right token always works, so nobody can lock
  out a client by failing from the same address. Through the published
  port, every program on the host's loopback has the same address (the
  daemon sees the address of the container's side of the network), so
  they share one count; the cookie and the unix socket are not affected.
  With `APPLIANCE_TRUST_PROXY=1` the client is the last `X-Forwarded-For`
  entry, the one that the reverse proxy added; the entries before it come
  from the client and are ignored. TURN credentials are issued only to
  authenticated sessions.
- The session cookie has no port: a browser sends it to every port of
  the same host, so a program that listens on another port of
  `127.0.0.1` (or of the host name) and that you browse to receives it.
  `HttpOnly` keeps it from scripts, not from the request. A `__Host-`
  prefix needs HTTPS. Use the bearer token for programs, and do not
  browse to untrusted services on the host that serves the dashboard.
- `APPLIANCE_LISTEN_ADDRESS` other than the loopback publishes plain
  HTTP: token, cookie and screenshots travel in the clear. `install.sh`
  warns; put a TLS reverse proxy in front instead.
- An unauthenticated connection is not kept open for ever: the TCP
  listener closes a connection that sends no complete request within
  15 s, and answers 503 beyond 256 concurrent connections. The unix
  socket has no limit. For exposure beyond the loopback, a reverse proxy
  that limits connections and request rates is the better barrier.
- Remote access: bind to another address with `APPLIANCE_LISTEN_ADDRESS`,
  or put a TLS reverse proxy in front of the HTTP port. The TURN port
  must be reachable by the browser; it carries only DTLS-SRTP media.

### WebSocket session

`GET /api/v1/session` (cookie or bearer). JSON messages.

Client to server: `{"type":"stream","action":"start"|"stop"}`,
`{"type":"jsep","data":{answer or candidate}}`,
`{"type":"touch","touches":[{"id","x","y","pressure"}]}`,
`{"type":"mouse","x","y","buttons"}`, `{"type":"wheel","x","y","dx","dy"}`,
`{"type":"key","key","code","action":"down"|"up"|"press"}`,
`{"type":"text","text"}`, `{"type":"ping","t"}`.

Server to client: `{"type":"status",...}` (on connect and on every
change), `{"type":"stream","state":"starting"|"ended","reason"}`,
`{"type":"stream","state":"configured","iceServers":[...],"iceTransportPolicy":"relay"}`,
`{"type":"jsep","data":{offer or candidate}}`,
`{"type":"display","hash","t"}`, `{"type":"pong","t"}`,
`{"type":"error","code","message"}`.

Touch identifiers are per session; the daemon maps them to unique
emulator identifiers. Coordinates are device pixels.

## androidctl

A bash script that uses `curl` on the unix socket
(`${ANDROID_APPLIANCE_SOCKET:-$XDG_RUNTIME_DIR/android-appliance/api.sock}`),
or TCP with `ANDROID_APPLIANCE_URL` and `ANDROID_APPLIANCE_TOKEN`. The host
needs no Python. The same script works inside the container
(`/run/appliance/api.sock`). The v1 commands keep their names, arguments,
output and exit codes (0 success, 1 error, 2 usage):

```text
status | start [--no-wait] | stop | restart [--no-wait] | wait | display
screenshot [file] | ui | tap x y | swipe x1 y1 x2 y2 [ms] | text <string> | key <key>
shell <command...> | install <apk> [-r|-g|-d|-t...] | push <local> <remote> | pull <remote> <local>
```

New: `dashboard` (one-time login link), `long-press x y [ms]`, `apps [--all]`,
`launch <pkg>`, `force-stop <pkg>`, `uninstall <pkg> [-k]`, `ls <remote>`,
`logs [emulator|daemon] [-n N]`, `token [--rotate]`, `doctor`, `version`.
`mcp` (stdio MCP server through `podman exec -i <name> python -m appliance
mcp`) comes with the MCP server: until the daemon has it, `androidctl mcp`
fails with "mcp is not available in this version" and the usage does not
list it.

Differences from v1 that the API makes:

- The shell command gets no standard input: the daemon runs `adb shell`
  without one. `androidctl shell` refuses input that it would drop (a
  non-empty file, or a pipe that delivers data within 0.3 s) with exit
  status 2 and "copy the data with push"; a stdin that stays silent or
  empty is no input.
- `push` and `pull` copy one file. A device directory as the destination
  of `push` takes the file under its name (`PUT /files?name=`), as adb
  does; directories are not copied. `install` takes `-r -g -d -t`.
- Extra arguments are a usage error (exit status 2): v1 ignored them
  silently (`status x`, `tap 1 2 3`, `start --wait`). The documented
  forms are unchanged.

- The endpoint: `ANDROID_APPLIANCE_URL` (TCP), else
  `ANDROID_APPLIANCE_SOCKET`, else `/run/appliance/api.sock` when it
  exists (in the container), else `$XDG_RUNTIME_DIR/<name>/api.sock`.
  The token goes to curl in a header file, so it stays out of the
  process list.
- When nothing answers on the socket and the user unit `<name>.service`
  exists, androidctl starts it and waits up to 60 s for the daemon.
  `stop` (nothing to stop), `wait` ("android is stopped"), `version`
  and `doctor` never start it. A socket file can outlive a killed
  daemon, so these commands ask `/health` instead of testing the file.
- Requests that may boot Android use a client timeout of
  `APPLIANCE_BOOT_TIMEOUT` + 600 s; status-like requests 60 s.
- `display` prints the dashboard URL. `doctor` prints `ok`, `WARN` and
  `FAIL` lines and exits 1 when a check fails. It checks that the image
  of the unit exists (a removed image only shows in the journal
  otherwise), and accepts a container that `install.sh --print-run`
  started without a unit.

## Installation

`install.sh` (rootless, as the user who owns the appliance):

- `./install.sh` builds the image, writes the configuration (when
  missing), the Quadlet unit `~/.config/containers/systemd/<name>.container`,
  the `CONTAINERS_CONF_OVERRIDE` file and `~/.local/bin/androidctl`, and
  starts the unit. Running it again applies a configuration change or an
  update (new image tag; the previous tag is kept).
- `--rollback` switches back to the previous image.
- `--uninstall` stops and removes the unit, the images and `androidctl`,
  and keeps the state and configuration. `--purge` also deletes them,
  with the v1 safeguards against shared directories. It needs the
  configuration file of the instance: without it the built-in defaults
  would name the default instance, so a mistyped `--config` is an error
  (and nothing is removed).
- `--doctor` (also `androidctl doctor`) checks Podman, rootless mode,
  crun, cgroups v2, `/dev/kvm`, the render node, SELinux labelling,
  lingering, ports, free disk space, a rotational state disk, and the
  daemon health.
- `--migrate` adopts a v1 deployment (see Migration).

The unit runs `Restart=on-failure`, `TimeoutStartSec=300`,
`TimeoutStopSec=180`, and is wanted by `default.target`; lingering keeps
it running without a login. Hosts without systemd can run the same
`podman run` command: `install.sh --print-run` installs everything but
the unit and prints it.

Rules of `install.sh`:

- Next to the configuration it keeps `<name>.containers.conf` (the
  `CONTAINERS_CONF_OVERRIDE` file) and `<name>.images` (the current and
  the previous image, and whether `install.sh` built them). It removes
  only images that it built, and never one that another unit in the
  Quadlet directory names.
- The Quadlet generator (`-dryrun -user`) checks the unit before it is
  written. A unit without the `# Managed by install.sh` line is never
  overwritten or removed.
- The state directory must be a directory of its own, because `:Z`
  relabels it and `--purge` deletes it. The path rules apply at install
  time too: no top-level directory, `$HOME`, XDG base directory or
  directory of another user, nothing that is one of these through a link
  or a bind mount (the files are compared by device and inode), and
  nothing that contains the checkout, `$HOME`, the configuration, the
  Quadlet, `androidctl` or the runtime directory. The content rules: the
  directory is absent or empty, holds the marker of this instance, or
  holds an appliance state (`appliance/` or `avd/`), for example an
  adopted v1 directory. Any other non-empty directory (`~/Documents`) is
  refused, and so is `--purge` on it.
- The marker `.android-appliance-instance` in the state directory names
  the instance. A second configuration (another name, same directory) is
  refused at install and at `--purge`; a rename needs `--uninstall`
  first, which removes the marker. The directory becomes mode 0700.
- A unit of this name belongs to the configuration that its
  `EnvironmentFile=` names. Installing or uninstalling with another
  configuration file is refused before anything changes; a second
  instance needs its own name, state directory and ports. `--uninstall`
  of a unit that `install.sh` did not write stops and deletes nothing.
- The link `<APPLIANCE_BIN_DIR>/androidctl` is replaced, and removed by
  `--uninstall`, only when it already points at `bin/androidctl` of a
  checkout of this appliance. A file, or a link to anything else (the v1
  command, a wrapper), stays, with a warning.
- An existing AVD in the state directory must have the API level of
  `APPLIANCE_API_LEVEL`; otherwise `install.sh` stops before the build
  and says which level to set.
- Before the unit is restarted, `podman run --rm <image> python -m
  appliance check-config` validates the configuration with the rules of
  the daemon (an invalid value would stop it at once and the unit would
  crash-loop). After the restart `install.sh` waits up to 60 s for the
  daemon (`APPLIANCE_INSTALL_WAIT`); if it does not answer, the
  installation fails with exit status 1, the last journal lines, and the
  advice to roll back when a previous image exists.
- A listen address other than the loopback gets a warning (plain HTTP).
- A v1 appliance (`android-appliance-emulator.service`) may stay
  installed only when the v2 state directory neither is, contains, nor
  lies inside the v1 one, and the v2 ports differ from the v1 display
  port. Otherwise `install.sh` stops.
- `--root DIR` stages every file under `DIR` (devices, sysfs and the v1
  files are read there too) and runs no podman, systemctl or loginctl.
- The v1 installer is `legacy/install.sh`, with its example
  configuration `legacy/appliance.conf.example`.

## MCP

The MCP server uses the official Python SDK. Over HTTP it is mounted at
`/mcp` in the daemon (streamable HTTP, bearer token). Over stdio,
`androidctl mcp` runs `podman exec -i <name> python3 -m appliance mcp`,
which uses `client` against the unix socket. Both serve the same tools,
built on `backend`. Screenshots are image content (base64); the UI
hierarchy and command output are text; files are base64 content, so
remote agents need no shared filesystem. Tools that can wait for a boot
send progress notifications and stop waiting after a bound, returning
the current state, so clients with short timeouts keep working.

## Migration from v1

The migration adopts the v1 state directory in place (default) or copies
it, keeps the AVD, the adb keys and the configuration values, and leaves
the v1 registry file untouched. It stops v1 and disables its units (this
part needs root for the system units), records a manifest and a backup of
the small files, and can roll back: stop v2, restore SELinux labels with
`restorecon`, re-enable v1. Details are in the migration guide.

## Testing

- Unit tests (no KVM): supervisor state machine with a fake emulator and
  fake adb, concurrency, crashes, idle; API and auth; signaling bridge
  with a fake Rtc gRPC server; input encoding; MCP with the SDK client;
  `androidctl` against a fake API; staged installs (and installs with
  fake podman and systemctl for the checks that need them); migration on
  fake v1 trees. The lifecycle tests include a state directory that
  refuses writes (a full disk), a power-off from the guest, and a
  gesture that the end of the emulator cuts off.
- Real-device tests (KVM, Podman): image build, boot on GPU and lavapipe,
  every CLI/API/MCP operation, idle stop, crash recovery (`kill -9` qemu),
  container restart and recreation with userdata kept, concurrent
  commands, WebRTC with headless Chromium (frames, latency, reconnection,
  input checked against getevent/dumpsys/UI dumps), and migration of a v1
  AVD.

`tests/e2e/lifecycle.sh` (`make e2e`) is the re-runnable real-device
test. It installs its own instance (`aa-e2e`, ports 47080 and 47081 on
127.0.0.1, state under `/var/tmp/aa-e2e`) from the built image with
`install.sh --no-build`, and checks: a new instance does not boot; a
screenshot boots a new AVD; shell exit status and stderr; HOME, launch,
BACK, POWER (screen off and on), swipe (app drawer), long press (app
menu), each checked against dumpsys or a UI dump (a swipe gets three
tries: Android dropped one right after a first boot); unicode text into the
first focused field of the new-contact form; push, pull and ls of a 3 MB
file; install of an APK pulled from the device; logs, token, login link,
remote mode with the token and refusal without it; the emulator's gRPC
port refuses a call without its token, the guest gets 401 from the
daemon through 10.0.2.2, and the state is mode 0700 with `/data` mounted
`nosuid,nodev`; a marker and the
installed app survive a stop and a cold boot; container recreation
keeps the marker; a crash right after a boot is retried once and the
next one is reported, then the next command cold boots; a unit restart
stops qemu with status 0; the idle stop after one minute does not
interrupt a 75 s shell command; `--uninstall --purge` leaves nothing but
the image. Every wait polls a condition with a deadline. On a failure
the step's message, the status, the daemon log, a UI dump and a
screenshot (only when Android runs), the emulator logs and the journal
stay in `/var/tmp/aa-e2e/work`.

Measured on juno (API 36, GPU host, 3072 MiB guest RAM, the installed
instance driven through `androidctl`):

| Measurement | Value |
|---|---|
| image build through `install.sh`, SDK stage cached, runtime layer rebuilt | 888 s (slow mirrors); `appliance/` change only 115 s; nothing changed 52 s; image 4.21 GB |
| first boot of a new AVD, to the first screenshot | 35-44 s (GPU); 23 s (lavapipe, image role) |
| cold boot with existing userdata | 15.2-17.9 s (GPU); 19.4 s (lavapipe) |
| graceful stop | 1.1-2.4 s |
| `GET /screenshot` PNG, 20 calls | median 119 ms over the socket, 116 ms over TCP; JPEG `max_size=640` 186 ms; lavapipe 101 ms |
| `POST /input/tap`, 20 calls | median 53 ms (socket), 54 ms (TCP), 52 ms (lavapipe) |
| `androidctl tap` and `androidctl screenshot`, end to end | median 71 ms and 138 ms |
| `GET /status` | 0.6 ms |
| retried crash: `SIGSEGV` to qemu 18 s after a boot | `starting` after 0.45 s, `running` again after 17 s, one launch, `last_error` null |
| `tests/e2e/lifecycle.sh`, three runs in a row | 313 s and 315 s (API 36), 356 s (API 35); first boots 39, 40 and 35 s, none crashed |
