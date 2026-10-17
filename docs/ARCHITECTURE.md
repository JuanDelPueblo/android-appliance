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

The build also adds the `Rtc` service to the SDK's gRPC allow list
(`emulator/lib/emulator_access.json`, see WebRTC) and fails if the edit did
not take effect.

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
| `APPLIANCE_TURN_HOST` | empty | daemon | host name or IP address (no port) for TURN URLs; empty means the host name of the page |
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
`APPLIANCE_TURNSERVER`, `APPLIANCE_RENDER_NODE` (instead of the first `/dev/dri/renderD*`), and
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
| `input` | key table, input encoding and the input of a browser session (see Input and WebSocket session) |
| `supervisor` | lifecycle state machine, idle policy, leases, crashes |
| `backend` | the operations; one implementation used by every front end |
| `client` | the same operations over HTTP (unix socket or TCP), for MCP stdio and tests |
| `api` | REST routes, auth, static dashboard, WebSocket session |
| `auth` | API token, sessions, one-time login links |
| `turn` | coturn configuration and process, TURN credentials |
| `webrtc` | signaling bridge: the emulator streams of the viewers |
| `session` | the WebSocket session: status, streams, input, display hash |
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
- **Typing after a first boot**: in the first minutes after the first boot
  of a new AVD, typed keys are dropped or reordered, whoever sends them
  (measured through `POST /input/text` with the `keys` method and through
  the dashboard at 40 ms a key: `jane doe` became `jane do`, `ne doeja`,
  `oede jan` and `doejane`, also with no input method enabled; a paste was
  lost once). After a restart of the same AVD the text landed exactly in 14
  of 14 rounds, and the raw key events in the guest (`getevent`) were in
  order in every round that was measured. So the order of the key events
  that the daemon sends is not what fails.
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
- With `-grpc-use-token` the emulator also checks `lib/emulator_access.json`,
  and the file has no entry for the `Rtc` service: `RequestRtcStream` is
  refused with `The endpoint: /android.emulation.control.v2.Rtc/RequestRtcStream
  is not on the allowlist`. The image build adds `/android.emulation.control.v2.Rtc/.*`
  next to `EmulatorController` in the `allowed` list of `android-studio` (the
  issuer that the token mode uses). The token still guards every call.
- Without an `ice_server_config`, the emulator uses
  `stun:stun.l.google.com:19302` and puts the host's public address in the
  offer. Always pass `{ice_servers:[{urls:["stun:127.0.0.1:3478"]}]}`: host
  candidates only (the container's global addresses, UDP and TCP-passive),
  no relay allocations on the emulator side, and nothing in the emulator
  log about Google or `srflx`.
- The offer has VP8 (chosen by Chromium), VP9, an audio track (even with
  `-no-audio`) and four data channels (`mouse`, `keyboard`, `touch`,
  `adb`). The appliance sends input over its own WebSocket instead (one
  path for every front end, exact activity tracking, input independent
  of the media state).
- The emulator sends a frame only when the display changes. A new viewer
  on a static screen gets no frame; a static stream decodes 0 fps (a
  blinking caret, a clock or an animation give frames).
- A client `bye` is ignored. After the client's ICE goes away, the
  emulator ends the stream with `{"bye":"disconnected"}` after about 15-17 s.
- An offer that is never answered stays open for good and leaks file
  descriptors (about 20 for each stream), threads and, with emulator-side
  TURN, relay ports. Ways to make the emulator let go of it, measured by
  counting the descriptors of qemu: an answer with all media sections
  rejected, an answer without a candidate, and an answer with a candidate
  on port 9 all left the stream open for good (45-160 s watched, ICE
  stayed in `kIceConnectionChecking`). An answer that accepts the
  sections, with made-up ICE and DTLS values, and one candidate on a
  closed port of the emulator's own address (40000) makes ICE fail: the
  emulator sends `{"bye":"disconnected"}` 15.1 s later and the descriptors
  go back to the baseline.
- A viewer that vanishes before its ICE connected leaves a stream that the
  emulator keeps for good. Measured with Chromium killed (or closed with
  `browser.close()`) while a stream was connected: the emulator's
  descriptors stayed at 195 (baseline 100) for 5 minutes after 5 such streams,
  with no log line. A normal close ends the stream after 15-17 s. A killed
  browser's port is closed, so the packets of the relay come back as
  errors and the emulator's ICE never reaches `failed`. A candidate on a
  closed port of the emulator's own address (the one of the throwaway answer)
  sent to such a stream made the emulator end it 13 s later and free its
  19 descriptors.
- When qemu dies, `ReceiveJsepMessageStream` fails at once with
  `UNAVAILABLE`; the browser sees ICE failure only after about 15 s.
  Measured: the viewer's stream ended 0.35 s after `kill -9` of qemu.
- The gfxstream crash of the GPU section (`GL calls without a current
  context`, then `SIGSEGV`) happened once in about 40 minutes of driving a
  booted device: 2.5 minutes after the last stream had ended, with no
  stream open, while keys were sent. The daemon reported `crash` (the
  boot was long over, so no retry) and the next command cold booted.

Design:

- **TURN** (`turn`): coturn 4.18 in the container, started with the
  daemon and kept alive (a delay of 1 s that doubles to 30 s between
  restarts, and starts over after 30 s of uptime). The configuration is
  generated at every start into `/tmp/appliance/turnserver.conf` (mode
  0600, it holds the secret) from `ip -o addr show scope global`: listening on
  3478 (udp and tcp), `use-auth-secret` with the secret
  `appliance/secrets/turn-secret`, relay range 49152-49251 on the first
  IPv4 address, `no-tcp-relay`, `no-multicast-peers`, `user-quota=4`,
  `total-quota=80`, `max-allocate-lifetime=60` (a killed browser never frees
  its relays; at the default of 10 minutes a few dozen of them would use up
  the 100 ports and the quota, and a live browser refreshes at half of the
  lifetime), `denied-peer-ip` for everything and `allowed-peer-ip` for each
  global address of the container. coturn writes its log to
  stdout, which the daemon logs with the prefix `turnserver:` (at most 100
  lines a minute). Credentials are TURN REST credentials:
  `username=<expiry>:<random id>`,
  `credential=base64(HMAC-SHA1(secret, username))`, valid 24 h, new for
  every stream and issued only inside an authenticated session. Verified
  against the daemon's configuration: a relay to the container's address
  is permitted; to the Internet, the LAN gateway, other LAN hosts,
  `127.0.0.1`, `10.0.2.2`, `169.254.1.2` and multicast it gets 403, over UDP
  and TCP; a wrong password and an expired user name get 401. coturn 4.18
  rejects `no-dtls` and `no-cli` (both already defaults; no CLI port
  listens). Without a global address in the container the relay has no
  peer and WebRTC cannot connect; the daemon says so in its log.
- **Browser**: `RTCPeerConnection` with only the TURN server and
  `iceTransportPolicy: "relay"`, answer only, trickle ICE. The TURN URLs
  (`turn:<host>:<port>?transport=udp` and `?transport=tcp`) use
  `APPLIANCE_TURN_HOST`, or else the host name of the page (the `Host`
  header, or `X-Forwarded-Host` behind a trusted proxy; an IPv6 literal gets
  brackets), with `APPLIANCE_TURN_PORT`. Media stays on one known port
  whatever the network; local and remote access use the same path.
- **Bridge** (`webrtc`): one emulator stream per viewer, requested only
  when the browser asks for it. The bridge drops the emulator's `start`
  (it echoes the ICE configuration), sends the browser its own start with
  the TURN configuration, never forwards empty candidates, rebuilds the
  viewer's answer and candidates from their known fields (nothing else, a
  `bye` for example, reaches the emulator), and forwards `bye` and gRPC
  stream ends as `stream ended`. If a viewer goes away before it answers,
  the bridge sends the emulator the throwaway answer above (built from the
  offer, with the address of the emulator's first UDP host candidate). A
  stream that a viewer left stays in the bridge until the emulator ends it
  (the bye after 15-17 s). One that the emulator still holds after 25 s gets
  a candidate on a closed port (the stuck stream above), and the bridge
  forgets it after 30 s more. At most 4 viewers have a stream,
  and at most 8 closed streams wait for the emulator, so a client that
  reconnects in a loop cannot pile up encoders. The `stream_limit` error
  says which limit it is.
- **First frame and freezes**: the dashboard shows a gRPC screenshot as a
  poster until the first video frame arrives. The poster is passive
  (`GET /screenshot?passive=1`): it never starts Android and is not use,
  so a stop that races the request cannot boot Android again. A screenshot
  that counted as use would also keep an unused Android awake for ever,
  because a static screen sends no frame. So the dashboard fetches the poster
  when Android runs and the video has no frame, again every 4 s for 8 more
  times, and again whenever the display hash changes while there is still no
  frame; Reconnect and a tab that is shown again start the 8 refreshes over.
  While a stream runs, the daemon samples a small gRPC screenshot (54x96
  RGB, 2 ms) once per second and sends a hash of it as a `display` event.
  If the hash changes and no new frame is decoded within 3 s, the dashboard
  reconnects the stream. Measured: a stalled media path was noticed after
  4.3 s while a list scrolled. A change of only the seconds of a status bar
  clock is too small to change the hash every second, so the detection
  needs a visible change of the screen. The device settings are not changed
  (no seconds clock).
- **Reconnection**: on ICE `failed`, a stream end, or a WebSocket close,
  the dashboard reconnects with backoff while the device is `running`.
  It never starts a stopped device by itself; the Start button does.
  A hidden tab closes its stream after 60 s and reconnects when visible.
- **Latency**: the dashboard sets `receiver.jitterBufferTarget = 0`
  (playout delay hint) where supported. Measured with headless Chromium
  151 on juno (API 36, GPU, relay over UDP on the loopback), see the table
  below.

Measured with a throwaway page of the appliance origin that speaks the
session protocol (Playwright 1.62.1, headless Chromium 151; API 36, GPU
`host`, 3072 MiB guest):

| Measurement | Value |
|---|---|
| ICE and DTLS connected, after the start message | 88-220 ms (median 155 ms in 60 streams) |
| selected pair | local `relay/udp`, remote `host/udp`; VP8 1080x1920 |
| first decoded frame, when the screen changes at once | 270-330 ms |
| touch drag of a list to the first decoded frame that shows it moved (two runs of 40 drags) | median 58 ms and 86 ms (p25 54 and 69, p90 105 and 184; the second run shared the host with other builds and started slower) |
| key to the first decoded frame (8 keys) | 48-133 ms, median 66 |
| 20 start and stop cycles, 3.5 s apart | 20 of 20 connected; descriptors of qemu peaked at 172 (baseline 102) and were back at 102 after 15 s; no relay socket left. At 2.5 s apart one start of 20 was refused (`stream_limit`) |
| 14 cycles 0.3 s apart | 8 connected, the other 6 got `stream_limit`; descriptors peaked at 252 and were at the baseline 15 s after the last one |
| 4 viewers at once while the list scrolls | every viewer decoded 15-21 frames in 5 s, no packet lost, no frame dropped; the fifth stream got `stream_limit`, and connected after one viewer left; qemu used 17 descriptors for each stream |
| `kill -9` of qemu with a stream running | `stream ended` 0.29-0.35 s later; status `stopping`, `last_error` `crash`; a stream start answered `not_running`; a new start of Android (15 s) and a new stream worked |
| unit stop (SIGTERM) with a stream running | the unit stopped in 3.2 s; the viewer got `ended` ("android is not running"), status `stopped` and the close code 1012 |
| unanswered stream, throwaway answer | released 15.1 s later, descriptors back to the baseline |
| display hash of a static screen | the same for 8 s; another when a finger touches the launcher |
| small gRPC screenshot for the display hash | 2.1 ms (median) |

The thread count of qemu is not a usable signal (it moves by 30 and more
while idle); the descriptors are.

Input over the WebSocket, checked against Android (22 of 22 checks):
buttons HOME, BACK, APP_SWITCH (overview), POWER (screen off and on; a hold
of 1.3 s opens the Assistant, which this system image maps to a long press),
VOLUME_UP and VOLUME_DOWN (music volume 5 to 6 to 3), MENU (`KEY_MENU` in
`getevent`); touch tap, drag, hold (long press opens the app menu) and two
fingers (two tracked contacts in `getevent`); mouse click, drag and
right click (Back); wheel down and up scroll a list; keys into a text field
(letters, Backspace, ArrowLeft, Shift letters), `text` with
`Grüße 日本 😀`, and a key outside ASCII.

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
| `GET /screenshot` | `?format=png\|jpeg&quality=85&max_size=<px>&passive=1` | image; headers `X-Device-Width`, `X-Device-Height`, `X-Image-Scale`; `passive` only looks: it never starts Android (`conflict` while it does not run) and is not use |
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

Outside `/api/v1`, `POST /logout` ends the session of a browser (below).

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

Every HTTP response carries `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` (under
`no-referrer` a browser puts `Origin: null` on the form post of the login and
the logout, and the same-origin check would refuse it). The
API answers have `Content-Security-Policy: frame-ancestors 'none'`. The
dashboard and the login page have `default-src 'self'; img-src 'self' blob:
data:; media-src 'self' blob:; connect-src 'self'; style-src 'self';
script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action
'self'`: they load nothing but their own files, run no inline script and
apply no inline style (a script may set `element.style`). `connect-src
'self'` covers the WebSocket of the same origin. The API answers, the login
page and the logout carry `Cache-Control: no-store`. The dashboard's files carry
`Cache-Control: no-cache`: a browser asks the daemon again each time (a short
304), so a browser that signed out goes to the login page instead of showing
the dashboard from its cache, and an update takes effect at once.

Outside `/api/v1`: `GET /login` is the login form; `POST /login` (form
field `token`) and `GET /login?code=<code>` (a login link) set the
session cookie. `POST /logout` clears the cookie and answers 303 to
`/login`; with a cookie it needs the same `Origin` as any other request
that changes state. `GET /` serves the dashboard (`web/`, or a short page
when it is not installed); without a session it redirects to `/login`.
The only file that needs no session is `/app.css`, the style sheet of the
login page, so the dashboard's CSS must not pull in anything else for it.
The login page is rendered by the daemon, with the markup that the
dashboard's CSS styles: `<main class="login">` with the `h1`, a
`p.error` when there is a message, the form with `label[for=token]`,
`input#token` and a submit button, and `p.hint`.

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
- `POST /logout` clears the cookie (`Max-Age=0`) and answers 303 to
  `/login`. The signature of the cookie is not revoked: the cookie is
  dropped from the browser, and only `androidctl token --rotate` ends it
  everywhere.
- A login link is `http://<APPLIANCE_LISTEN_ADDRESS>:<APPLIANCE_PORT>/login?code=<code>`
  (`127.0.0.1` for a wildcard address). It works once, within 2 minutes.
- `androidctl token --rotate` also replaces the session key and drops the
  pending login links, so it ends every dashboard session and every link
  made before it. It also closes the WebSocket sessions that are open
  (close code 1008), which hold the old token or cookie.
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

`GET /api/v1/session` (cookie or bearer; a cookie needs our own `Origin`).
JSON text messages, at most 256 KiB. The daemon accepts 16 sessions at
once (more get the close code 1013).

On connect the server sends `{"type":"hello","device":{"width":1080,"height":1920},"version"}`
and `{"type":"status",...}`, the status object of `GET /status`. It
sends the status again at every change of `state`, `phase`, `gpu`,
`api_level`, `viewers` or `last_error` (changes in one step of the event
loop make one message), and every 15 s, so the idle time stays current.
`viewers` is the number of sessions with a stream.

Client to server:

| Message | Meaning |
|---|---|
| `{"type":"stream","action":"start"\|"stop"}` | start a stream (a start replaces the stream that runs); stop it |
| `{"type":"jsep","data":{...}}` | the answer `{"type":"answer","sdp"}` or a candidate `{"candidate","sdpMid","sdpMLineIndex"}` (the empty end-of-candidates message is dropped) |
| `{"type":"touch","touches":[{"id","x","y","pressure"}]}` | up to 10 fingers; `id` is a number or short text of the session; `pressure` is 0 (lift) or 0 to 1 like `PointerEvent.pressure` |
| `{"type":"mouse","x","y","buttons"}` | `buttons` like `PointerEvent.buttons`: the left button (bit 0) is a finger, a press of the right button (bit 1) is Back; moving with no button does nothing |
| `{"type":"wheel","x","y","dx","dy"}` | wheel deltas in CSS pixels; a drag of about 168 px per 120 units, in the opposite direction, started at the pointer |
| `{"type":"key","key","code","action":"down"\|"up"\|"press"}` | a W3C `KeyboardEvent.key` (`code` is informative) |
| `{"type":"button","name","action":"down"\|"up"\|"press"}` | `HOME`, `BACK`, `APP_SWITCH`, `POWER`, `VOLUME_UP`, `VOLUME_DOWN`, `MENU`; `down` and `up` allow holds |
| `{"type":"text","text"}` | any Unicode, pasted through the clipboard (at most 64 Ki characters) |
| `{"type":"ping","t"}` | answered with `{"type":"pong","t"}` |

Server to client: `status`, `hello`, `pong`, and

- `{"type":"stream","state":"starting"}`, then
  `{"type":"stream","state":"configured","iceServers":[...],"iceTransportPolicy":"relay"}`,
  then the emulator's messages as `{"type":"jsep","data":{offer or candidate}}`.
  `{"type":"stream","state":"ended","reason"}` follows when the emulator
  ends the stream, its gRPC stream fails, Android stops, or the client sent
  `stop`. A start that replaces a stream sends no `ended` for the old one.
- `{"type":"display","hash","t"}` once per second (`t` in milliseconds
  since the epoch) while this session has a stream: the hash of a small
  screenshot, for freeze detection.
- `{"type":"error","code","message"}`: `bad_request` (unreadable message,
  unknown type, a field out of range), `not_running` (a stream or input
  while Android is not running; a session never starts Android),
  `stream_limit`, `unavailable` (TURN is not running, or a paste failed).
  Errors are limited to a burst of 5 and one a second.

Rules:

- Coordinates are device pixels of the natural 1080x1920 frame. The
  dashboard maps from the video element, with its letterboxing and any
  rotation of the frame; the daemon clamps to the frame.
- Touch identifiers are per session; the daemon maps them to unique
  emulator identifiers (the mouse is one more finger). A lift of an
  unknown id is dropped. The daemon lifts all contacts of a session where
  they were last seen when the session closes, and forgets them when
  Android stops. Keys and buttons that are down are let go the same way.
- Keys use the table of Input. A printable ASCII `key` is one key press
  at every `down` and `press` (repeats included), nothing at `up`. A
  named key sends `keydown` at `down`, `keyup` at `up` and `keypress` at
  `press`; a `down` for a key that is down already is a repeat and is
  dropped (Android repeats a held key itself). `ContextMenu` and `Menu`
  are evdev 139, `BrowserHome` is `GoHome`; `CapsLock`, `Dead`, `Process`
  and `Unidentified` are dropped; a printable character outside ASCII is
  pasted like `text`. Keys, buttons and text run in the order of
  arrival, with 5 ms between key events, and a paste holds the text lock. A
  paste lets go of the modifiers that the session holds and presses them
  again after it: Android ignores the paste key while a modifier is down
  (measured: a browser's Ctrl+V pasted nothing), and Ctrl+V holds Control.
- A wheel notch that arrives while a drag runs adds to the next drag; a
  delta shorter than 30 px waits for more (a shorter drag would be a tap),
  a drag near the edge starts further in so that it keeps its length.
- Every input message (also a mouse that moves with no button) and a
  stream start update the activity time while Android runs. A session
  that only watches does not, and neither do `ping`, `status` or a message
  that was refused.
- A client that sends more than 1500 messages a second (3000 at once),
  or lets more than 256 key, button, text or stream messages wait, is
  closed with the code 1008. A client that does not read its messages (512
  waiting) is closed with 1013.

## Dashboard

The dashboard is the plain files in `web/`: `index.html`, `app.css`,
`js/*.js` as ES modules and `icons/*.svg` (drawn as CSS masks). It has no
build step, no framework and no external file, so the policy of the page can
say `default-src 'self'`. The daemon serves it at `/`.

**Page.** The device display (video element, poster and an overlay that
says what is going on), a toolbar (Back, Home, Recents, Power, Volume down
and up, keyboard capture), and a panel with the state of Android (Start,
Stop and Restart; Stop and Restart ask first, because they close apps), the
viewers, screen, API level, GPU and the last error with its log tail, the
state of the connection (session, video, ICE, codec, resolution, frame rate,
bit rate, lost packets, round trip, decoded frames, and the time from an
input to the next frame as a rough guide) and Sign out. Beside the display
at 900 px and wider, a drawer below that.

**Link.** `protocol.js` is the whole client side of the session protocol as
a state machine without I/O: it takes events (the socket opened, a message
arrived, a timer fired, the peer connection changed, the video froze, the
tab was hidden or shown) and returns effects (send, set a timer, open the
peer connection). The browser calls are in the other modules. The rules:

- The session socket reconnects with a delay that doubles from 0.5 s to
  10 s (with 25 % jitter) and starts over after a connection held for 8 s.
  The dashboard pings every 10 s and closes a socket that was silent for
  25 s. A socket that never opened asks `GET /api/v1/status`: a 401 goes to
  the login page.
- A stream is wanted while the socket is open, Android runs and the tab
  was not hidden for 60 s. It never starts Android: `not_running` from the
  daemon only ends the attempt, and the Start button is the only way.
- A stream that fails (ICE `failed`, ICE `disconnected` for 4 s, no
  connection in 15 s, an `ended` message, an `error` while it starts, a
  freeze of 3 s with a changed display hash) is closed and started again
  with the same backoff. A start replaces the stream on the server, so
  Reconnect and the `online` event of the browser need no stop first.
- A hidden tab keeps its socket but stops its stream after 60 s, and
  brings it back when the tab is shown.

**Input.** The position of a pointer is mapped from the video element (which
letterboxes the picture) to device pixels, clamped to the frame. A turned
guest keeps the natural 1080x1920 frame, so the picture position is the
touch position; a decoded frame in the other orientation is mapped back
(`geometry.js`). A mouse sends `mouse` messages (left button is a finger,
right button is Back), touch and pen send `touch` messages that list every
contact that is down (a lifted one once, with pressure 0), the wheel sends
`wheel` (a `deltaMode` of lines or pages becomes pixels). Moves and wheel
turns go once per animation frame, presses and releases at once and after
the moves before them. Everything that is down is let go when the tab is
hidden, the window loses the focus, Android stops or the socket drops.

The keyboard goes through a hidden text area that has the focus. A printable
ASCII key is `press`, a named key is `down` and `up` (a repeat is dropped),
and text that no key carries (a composition result, a character outside
ASCII, a paste) is a `text` message of at most 60,000 characters. The
shortcut for a paste is not a key: the browser's paste event delivers the
text. With a mouse or keyboard the capture is on and Ctrl+Alt+K turns it off,
so the keyboard is not trapped; on a touch device (`pointer: coarse`) it
starts off, so that a tap does not raise the on-screen keyboard, and the
toolbar button turns it on and asks for it. The toolbar buttons send
`button` messages as `down` at the press and `up` at the release, so a hold
of Power opens the power menu once `power_button_long_press` is set (the
stock setting of this image opens the Assistant).

**Login.** The login page is rendered by the daemon with the markup that
`app.css` styles. `POST /logout` clears the cookie.

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

`tests/web` tests the dashboard. Unit tests (`node --test`) cover the link
state machine, the backoff, the freeze detector, the geometry (letterbox and
rotation), the key and touch rules and the statistics. UI tests in Chromium
run against a fake appliance (rendering and the CSP, the lifecycle controls
with their confirmation, the reconnect backoff, input of mouse, touch, pen,
wheel, keyboard, IME, paste and buttons, a real WebRTC pair against a fake
emulator page with a freeze restart and a hidden tab, and the layouts at
1280x800 and 390x844), and against the real daemon with the fake emulator of
the Python tests (`daemon.test.mjs`: the login by link and by form, the
session, and the gRPC input that reaches the emulator). They need Playwright
(`AA_PLAYWRIGHT`, or `npm ci` in `tests/e2e`) and skip without it.

`tests/e2e/webrtc.sh` (`make e2e-webrtc`) is the real-device test of the
dashboard. It installs its own instance (`aa-e2e-rtc`, ports 47082 and 47083)
like `lifecycle.sh`, then `webrtc.mjs` drives headless Chromium through these
steps and checks every effect against Android (UI dumps, `dumpsys`,
`getevent` through `androidctl`), not against the dashboard:

- login: the form with a wrong and a right token (the post carries the page's
  `Origin`), a one-time link that works once, the cookie, sign out (the
  dashboard is not served from the cache afterwards), and an anonymous
  WebSocket that is refused;
- a stopped Android offers Start and nothing starts by itself; Start shows
  every phase, the poster and the first decoded frame (timed), also on a
  static screen;
- mouse: a click opens an app, a drag up opens the app drawer, a long press
  opens the icon menu, the wheel scrolls Settings (checked by the item
  positions of the UI dump), a right click is Back; the same with the guest
  rotated to landscape (the picture stays in the natural frame and the click
  lands on what is drawn);
- touch on a phone-sized page: a tap, a swipe, and two fingers (two contacts
  and tracking ids in `getevent`);
- keyboard: ASCII, Backspace, the arrows, Delete, Shift, text outside ASCII,
  an IME result and the paste shortcut, into the new-contact form, exact to
  the character;
- hardware buttons: Home, Back, Recents, the volume (the music volume
  changes), Power held for 1.5 s (the power menu) and pressed (the screen
  goes off and on);
- Restart, Stop (the dashboard shows Start and does not start Android) and
  Start (the video comes back by itself, without a reload);
- a crash of qemu right after a boot is retried and the video returns; a
  later crash shows "Android stopped unexpectedly" and Start recovers; a
  restart of the daemon is survived by the session;
- reconnection: the WebSocket closed from the page (three times), the browser
  offline for 20 s, a killed TURN server, a stopped TURN server while a list
  scrolls (the freeze is detected), a poster while the video cannot connect,
  and a daemon that stops answering for 30 s;
- two viewers (both decode, `viewers` is 2), a hidden tab (the stream closes
  after the 60 s grace period and returns), and a long session without a
  stream restart (`E2E_SOAK_SECONDS`, 120 by default);
- measurements: input to decoded frame over 20 taps and over 20 drags, the
  frame rate while scrolling, and the CPU of the emulator, daemon and coturn
  without a viewer, with one watching a static screen and with one scrolling;
- access under another host name (`http://localhost:<port>`): the TURN URLs
  follow the page and the media flows through the relay; a TURN client with
  the credentials of a session relays to the container's own address and
  gets 403 for the Internet, the loopback, `10.0.2.2`, link-local, multicast
  and the LAN gateway, over UDP and TCP, and 401 for a wrong credential and
  an expired user name;
- no stream is left behind: after all the reconnects of the run, the
  emulator's descriptors and the relay ports are back at the baseline;
- the idle stop with a dashboard open: input every 20 s keeps Android up past
  the limit, and without input it stops with the dashboard still open;
- rotating the API token closes the open session, the old token and cookie
  stop working, the dashboard goes to the login page, and the new token
  signs in;
- the daemon log (the journal) has no traceback.

Some checks need a setting of Android that makes the check possible, not
part of the appliance: `clock_seconds=1` (the screen changes once a second,
so frames flow on an otherwise static screen) and
`power_button_long_press=1` (the stock setting of this image opens the
Assistant on a long press). On a failure the step's message, a screenshot
of the page and of Android, the UI dump, the status, the messages of the
session, the console errors, the daemon log and the journal stay in
`/var/tmp/aa-e2e-rtc/work`.

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
