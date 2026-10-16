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
namespace. The host publishes two ports, by default on 127.0.0.1:

| Port | Use |
|---|---|
| `APPLIANCE_PORT` (6090/tcp) | dashboard, REST API, WebSocket session, MCP |
| `APPLIANCE_TURN_PORT` (3478/udp+tcp) | WebRTC media through TURN |

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
  -gpu <host|lavapipe> -feature -QuickbootFileBacked[,-Vulkan] -no-snapshot \
  -grpc 8554
```

`-grpc <port>` disables JWT; the port is never published. The launcher
execs `qemu-system-x86_64-headless`, so the spawned PID is the qemu PID.
The discovery file is `$ANDROID_EMULATOR_HOME/avd/running/pid_<pid>.ini`
(`grpc.port`, `port.adb`, `port.serial`).

### Podman options

```text
--userns=keep-id --cap-drop=ALL --security-opt no-new-privileges
--read-only --tmpfs /tmp:rw,size=512m --ulimit core=0 --init
--device /dev/kvm [--device /dev/dri/renderD128] [--group-add keep-groups]
--tz=local --stop-timeout 150
-v <state>:/data:Z -v $XDG_RUNTIME_DIR/android-appliance:/run/appliance:Z
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
- The SDK in the image is read-only; the emulator writes only to
  `$HOME`, `/tmp` and the AVD directory.

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
4. The `appliance` package and the dashboard (`web/`).

The SDK archives are downloaded when the user builds the image. The
image is not published, because the SDK license does not allow
redistribution.

## Persistent state

The state directory is mounted at `/data`. The layout keeps the v1
layout, so a v1 state directory is adopted as it is:

| Path | Content |
|---|---|
| `avd/<name>.ini`, `avd/<name>.avd/` | AVD configuration and userdata (v1 compatible) |
| `home/.android/` | adb keys (`adbkey`, `adbkey.pub`), emulator settings |
| `home/.cache/` | Mesa shader cache |
| `screenshots/` | screenshots saved by `androidctl` |
| `appliance/state.json` | daemon metadata (AVD origin, first-boot flag, crash history) |
| `appliance/secrets/` | API token, session key, TURN secret (mode 0600) |
| `appliance/logs/` | emulator logs of the last boots |
| `appliance/daemon.lock` | single-daemon lock (flock) |

The AVD registry `avd/<name>.ini` has an absolute `path=`. A v1 registry
holds the host path, which does not exist in the container. The daemon
does not edit it (a rollback to v1 needs it). It writes a registry with
`path=/data/avd/<name>.avd` to a tmpfs directory and points
`ANDROID_AVD_HOME` there. The v1 check that refused a registry that points
outside the state directory is therefore not needed.

The environment of the emulator: `HOME=/data/home`,
`ANDROID_USER_HOME=ANDROID_EMULATOR_HOME=/data/home/.android`,
`ANDROID_AVD_HOME=<tmpfs registry dir>`, `ANDROID_SDK_ROOT=/opt/android-sdk`,
`ANDROID_ADB_SERVER_PORT=5037`, `DISPLAY=:0` (GPU mode).

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
| `APPLIANCE_API_LEVEL` | `36` | install (build) | system image of the image build |
| `APPLIANCE_GPU` | `auto` | install, daemon | `auto`, `host` or `lavapipe` |
| `APPLIANCE_GPU_DEVICE` | `auto` | install | render node; `auto` is the first `/dev/dri/renderD*` |
| `APPLIANCE_AVD_NAME` | `android` | daemon | AVD name |
| `APPLIANCE_IDLE_STOP_MINUTES` | `60` | daemon | idle shutdown; `0` disables it |
| `APPLIANCE_MEMORY_MIB` | `4096` | daemon | guest RAM |
| `APPLIANCE_CORES` | `4` | daemon | guest CPU cores |
| `APPLIANCE_DISK_SIZE` | `32G` | daemon | userdata size of a new AVD |
| `APPLIANCE_BOOT_TIMEOUT` | `900` | daemon | seconds before a boot fails |
| `APPLIANCE_TRUST_PROXY` | `0` | daemon | honour `X-Forwarded-Proto`/`-Host` from a reverse proxy |
| `APPLIANCE_SDK_*` | lock defaults | install (build) | SDK revisions, as in v1 |
| `APPLIANCE_NAME` | `android-appliance` | install, androidctl | container and unit name (tests use another) |
| `APPLIANCE_BIN_DIR` | `~/.local/bin` | install | where `androidctl` is linked |
| `APPLIANCE_QUADLET_DIR` | `~/.config/containers/systemd` | install | where the unit is written (tests use `$XDG_RUNTIME_DIR/containers/systemd`) |
| `APPLIANCE_IMAGE` | `localhost/android-appliance:<version>-api<level>` | install | image to build and run |

`install.sh --config FILE` and `androidctl` with `ANDROID_APPLIANCE_CONFIG=FILE`
select another configuration file, and so another instance. The unix
socket of an instance is `$XDG_RUNTIME_DIR/<APPLIANCE_NAME>/api.sock`.

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
  share one boot.
- **stop**: running and starting stop gracefully (a start in progress
  fails with "stopped"); stopped returns; stopping joins.
- **restart**: stop, then start.
- **Launch**: AVD check and setup; remove stale locks; start Xvnc (GPU);
  start the emulator; wait for the discovery file, gRPC and adb; wait for
  `sys.boot_completed=1`; post-boot setup (`svc power stayon true`,
  `wm dismiss-keyguard`, `settings put secure stylus_handwriting_enabled 0`
  if it suppresses Gboard's stylus sheet); state `running`.
- **Graceful stop**: one `adb emu kill`, wait up to 60 s (measured 1-3 s),
  then SIGTERM, wait 10 s, then SIGKILL; then stop Xvnc. Never send a
  second kill while qemu tears down (Phase 0.1 saw SIGABRT cores).
- **Failure detection**: a qemu exit while starting or running is a crash.
  `Could not start renderer` or `FATAL` in the emulator log means qemu
  will hang; kill it. A boot that exceeds `APPLIANCE_BOOT_TIMEOUT` stops
  the emulator. `unauthorized` that lasts more than 30 s fails the boot
  with the v1 guidance (copy the old adb key). A crash or failure sets
  `stopped` with `last_error` (kind, message, exit status, log tail, time).
  There is no automatic restart; the next use cold boots.
- **Stale locks**: after a kill, `<avd>.avd/hardware-qemu.ini.lock` keeps
  the old qemu PID. Container PIDs are small and repeat, so the next
  emulator often sees a live process with that PID and stops with
  `Running multiple emulators with the same AVD`. Before each launch the
  daemon removes `hardware-qemu.ini.lock` and `running/pid_*.ini`. This is
  safe only when no other emulator uses the AVD: the daemon holds
  `appliance/daemon.lock` for its whole life, and checks that no other
  process (for example a v1 emulator on the host) holds a lock on the AVD
  files before it removes anything.
- **Interrupted first boot**: a first boot that is killed leaves userdata
  that answers `unauthorized` for good. `state.json` records whether the
  daemon created the AVD and whether its first boot completed. An AVD
  that the daemon created and that never completed a boot is recreated.
  An adopted AVD is never deleted.
- **Container stop** (SIGTERM): stop Android gracefully, then exit.
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
`appliance/state.json`, so it survives a daemon restart.

### Operations

| Operation | Implementation |
|---|---|
| screenshot | gRPC `getScreenshot` (PNG); scaled/JPEG on request; adb `screencap` fallback |
| ui | `uiautomator dump` with the v1 retry loop; XML, or a compact list for agents |
| tap, long-press, swipe | gRPC touch on the shared input stream |
| text | clipboard paste (exact unicode); `keys` method types ASCII keys |
| key | gRPC key from the key table; unknown names and numbers use `adb shell input keyevent` |
| shell | `adb shell` (exit status, stdout, stderr) |
| apps | `pm list packages`, `adb install`, `pm uninstall`, launch, force-stop |
| files | `adb push`, `adb pull`, `ls` |

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
  digits. A `KEYCODE_` prefix is accepted.

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
`not_found` (404), `conflict` (409), `boot_failed`/`timeout`/`unavailable`
(503), `internal` (500).

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
| `POST /apps/install` | APK body (`application/vnd.android.package-archive`), `?replace=1&grant=1&downgrade=1&test=1` | `{"output"}` |
| `POST /apps/{package}/launch` | | `{}` |
| `POST /apps/{package}/stop` | | `{}` |
| `DELETE /apps/{package}` | `?keep_data=1` | `{"output"}` |
| `GET /files` | `?path=` | file bytes |
| `PUT /files` | `?path=`, raw body | `{"bytes"}` |
| `GET /files/list` | `?path=` | `{"entries":[{"name","type","size","mtime"}]}` |
| `GET /logs` | `?source=emulator\|daemon&lines=200` | text |
| `POST /auth/login-link` | local only | `{"url"}` (single use, 2 min) |
| `GET /auth/token` | local only | `{"token"}` |
| `POST /auth/token/rotate` | local only | `{"token"}` |
| `GET /session` | WebSocket | dashboard session (below) |

The status object: `state`, `boot_completed`, `idle_seconds`,
`idle_stop_seconds`, `uptime_seconds`, `phase` (detail while starting),
`gpu` (resolved mode), `api_level`, `avd`, `viewers`, `last_error`,
`version`.

### Text formats for shell clients

`androidctl` is bash and has no JSON parser, so the API also speaks
plain text:

- A request with `Accept: text/plain` gets errors as `text/plain`: the
  message only, with the same HTTP status.
- `POST /input/text` and `POST /shell` accept a `text/plain` body (the raw
  UTF-8 text or command) as well as JSON.
- `POST /shell?format=raw` returns stdout as the body, the exit status in
  the `X-Exit-Code` header and stderr base64-encoded in `X-Stderr`
  (truncated to 16 KiB, with `X-Stderr-Truncated: 1`).
- `?format=text` gives line-oriented output for `status` (the v1 line),
  `apps` (one package per line), `files/list` (`type size name` per line),
  `auth/token` and `auth/login-link` (the value alone), and for
  `start`/`stop`/`restart`/`wait` (the status line).

### Authentication

- The unix socket is trusted: file permissions protect it (the directory
  is mode 0700). `androidctl` on the host uses it.
- TCP requests need either `Authorization: Bearer <token>` or a session
  cookie. The token is random (32 bytes), stored in
  `appliance/secrets/api-token`; `androidctl token` prints it and
  `androidctl token --rotate` replaces it.
- The dashboard's login page accepts the token, or a one-time link from
  `androidctl dashboard`, and sets an HttpOnly, SameSite=Strict session
  cookie (Secure over HTTPS). State-changing requests with a cookie must
  have a same-origin `Origin`; the WebSocket checks `Origin` too.
- Failed logins are rate limited. TURN credentials are issued only to
  authenticated sessions.
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
`launch <pkg>`, `force-stop <pkg>`, `uninstall <pkg>`, `ls <remote>`,
`logs [emulator|daemon] [-n N]`, `token [--rotate]`, `mcp` (stdio MCP
server through `podman exec -i`), `doctor`, `version`.

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
  with the v1 safeguards against shared directories.
- `--doctor` (also `androidctl doctor`) checks Podman, rootless mode,
  crun, cgroups v2, `/dev/kvm`, the render node, SELinux labelling,
  lingering, ports, free disk space, a rotational state disk, and the
  daemon health.
- `--migrate` adopts a v1 deployment (see Migration).

The unit runs `Restart=on-failure`, `TimeoutStopSec=180`, and is wanted
by `default.target`; lingering keeps it running without a login. Hosts
without systemd can run the same `podman run` command
(`install.sh --print-run`).

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
  `androidctl` against a fake API; staged installs; migration on fake v1
  trees.
- Real-device tests (KVM, Podman): image build, boot on GPU and lavapipe,
  every CLI/API/MCP operation, idle stop, crash recovery (`kill -9` qemu),
  container restart and recreation with userdata kept, concurrent
  commands, WebRTC with headless Chromium (frames, latency, reconnection,
  input checked against getevent/dumpsys/UI dumps), and migration of a v1
  AVD.
