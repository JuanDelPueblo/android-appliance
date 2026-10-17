# android-appliance

Android Appliance runs a headless Android device, the official SDK emulator with a Google Play image, in one rootless Podman container on a Linux host with KVM. It is built for a single user who wants an Android device that is always available: for apps that have no desktop version, for a Google account that stays signed in, or as a device that an AI agent can drive.

You get a dashboard in the browser that shows the live display over WebRTC, the `androidctl` command line tool, an HTTP API, and an MCP server for AI agents. Android boots on demand and stops when it is idle. Userdata, installed apps and the Google account are kept between boots.

## How it works

```text
host                                     container "android-appliance"
────                                     ───────────────────────────────────────────
androidctl ──unix socket──────────────►  appliance daemon (python3 -m appliance serve)
  ($XDG_RUNTIME_DIR/android-appliance/     ├─ supervisor ──► emulator (qemu, -no-window)
   api.sock)                               │                  └─ Xvnc :0 -rendernode (GPU GLX only)
androidctl mcp ──podman exec───────────►  python3 -m appliance mcp (MCP over stdio, uses the socket)
browser ─TCP 6090─(pasta)──────────────►   ├─ HTTP API, dashboard, WebSocket
browser media ──TURN 3478 udp/tcp───────►  ├─ coturn (relay to the container only)
                                           └─ adb server, gRPC client (emulator :8554)
/var/lib/... or ~/.local/share/... ──────► /data  (AVD, adb keys, appliance state)
```

- Everything runs in one container, started by a systemd user unit (Quadlet). A daemon is the container's main process and owns the emulator.
- The container is rootless. It runs as your user with all capabilities dropped, a read-only root file system and SELinux confinement.
- The host publishes two ports, by default on 127.0.0.1 only: the HTTP port (6090/tcp) for the dashboard, the API and the WebSocket session, and the TURN port (3478, UDP and TCP) for the video.
- The emulator console, adb, gRPC and the TURN relay ports exist only inside the container. Nothing on the host or the network reaches them.
- `androidctl` on the host talks to the daemon over a unix socket in `$XDG_RUNTIME_DIR/<name>/`. It needs only bash and curl.
- All state lives in one directory on the host, mounted at `/data`: the AVD, userdata, adb keys, the API token and the logs.
- The container image holds the emulator, platform-tools, the system image, coturn and the daemon. It is built on your machine from a Fedora 44 base.
- The guest can reach the container. The emulator maps 10.0.2.2 to the container's loopback, so an app with the `INTERNET` permission can open the adb server, which has no authentication. The other ports are protected by tokens.
- The device is as trusted as its owner: do not install apps you do not trust on a device that holds a Google account.

## Requirements

- A Linux host with KVM (`/dev/kvm`) that your user can read and write.
- Rootless Podman 5 with crun and pasta, cgroups v2, and a systemd user session. The design was measured on Fedora 44 with Podman 5.8 and SELinux enforcing.
- For GPU rendering, a DRM render node (`/dev/dri/renderD*`). Without one, Android renders in software with lavapipe, which is slower (about 22 % of a core when idle, and about 5-10 % janky frames).
- Lingering for your user (`loginctl enable-linger`), if the appliance should start at boot and run without a login.
- Disk: the image is 4.2 GB for one API level. The build cache keeps a second copy of the SDK (3.4 GB for one API level). A new AVD can grow to 32 GB of userdata by default (`APPLIANCE_DISK_SIZE`).
- RAM: the guest gets 4096 MiB and 4 cores by default. The tests ran with 3072 MiB.
- An SSD for the state directory. Android boots and runs much slower on a rotational disk.

## Install

```bash
git clone https://github.com/JuanDelPueblo/android-appliance.git
cd android-appliance
./install.sh
```

`install.sh` runs as your user, not as root. It writes the configuration file if it is missing, builds the image (the build downloads about 2.5 GB, the pinned SDK packages, and checks each archive with SHA-256), writes the Quadlet unit and the `CONTAINERS_CONF_OVERRIDE` file, links `androidctl` into `~/.local/bin`, and starts the unit. It then waits up to 60 s for the daemon. It prints a warning for each problem it finds, for example a missing `/dev/kvm`, lingering off, or a listen address other than the loopback.

Android does not start with the unit. Start it, and check the host:

```bash
androidctl start
androidctl doctor
```

The first boot of a new AVD creates the userdata and takes about 35 s on the reference host. To open the dashboard, print a one-time login link and open it in a browser:

```bash
androidctl dashboard
```

To keep the appliance running after you log out, and to start it at boot:

```bash
loginctl enable-linger "$USER"
```

### Configuration

The configuration is `${XDG_CONFIG_HOME:-~/.config}/android-appliance/appliance.conf`, created from `conf/appliance.conf.example`. It has one `APPLIANCE_*=value` line per key, with no spaces, no quotes, and no expansion of `~` or variables. A value in the environment of `install.sh` or `androidctl` wins over the file. To apply a change, edit the file and run `./install.sh` again. Guest hardware settings apply at the next boot and keep the userdata.

| Key | Default | Meaning |
|---|---|---|
| `APPLIANCE_NAME` | `android-appliance` | name of the container, its unit and its socket directory |
| `APPLIANCE_STATE_DIR` | `~/.local/share/android-appliance` | host state directory; must be new, empty or an appliance state |
| `APPLIANCE_LISTEN_ADDRESS` | `127.0.0.1` | address of the published ports |
| `APPLIANCE_PORT` | `6090` | HTTP port (dashboard, API) |
| `APPLIANCE_TURN_PORT` | `3478` | TURN port (UDP and TCP) |
| `APPLIANCE_TURN_HOST` | empty | host name or IP address in the TURN URLs; empty uses the host name of the page |
| `APPLIANCE_TRUST_PROXY` | `0` | `1` trusts `X-Forwarded-Proto`, `-Host` and `-For` from a reverse proxy |
| `APPLIANCE_AVD_NAME` | `android` | name of the AVD |
| `APPLIANCE_API_LEVEL` | `36` | Android API level of the system image the build uses |
| `APPLIANCE_GPU` | `auto` | `auto`, `host` or `lavapipe` |
| `APPLIANCE_GPU_DEVICE` | `auto` | render node; `auto` is the first `/dev/dri/renderD*` |
| `APPLIANCE_IDLE_STOP_MINUTES` | `60` | stop Android after this many idle minutes; `0` disables it |
| `APPLIANCE_MEMORY_MIB` | `4096` | guest RAM |
| `APPLIANCE_CORES` | `4` | guest CPU cores |
| `APPLIANCE_DISK_SIZE` | `32G` | userdata size of a new AVD |
| `APPLIANCE_BOOT_TIMEOUT` | `900` | seconds before a boot fails |
| `APPLIANCE_MAX_UPLOAD_MIB` | `4096` | largest push or APK install |
| `APPLIANCE_SDK_EMULATOR_VERSION`, `APPLIANCE_SDK_PLATFORM_TOOLS_VERSION`, `APPLIANCE_SDK_IMAGE_REVISION` | lock defaults | SDK revisions pinned in `conf/sdk.lock` |
| `APPLIANCE_SDK_REPOSITORY` | Google's repository | download location of the SDK archives, for example a mirror |
| `APPLIANCE_IMAGE` | `localhost/android-appliance:<version>-api<level>` | image to build and run |
| `APPLIANCE_BIN_DIR`, `APPLIANCE_QUADLET_DIR` | `~/.local/bin`, `~/.config/containers/systemd` | where `androidctl` is linked and the unit is written |

### Remote access

By default the ports listen on 127.0.0.1, so only the host reaches the dashboard. There are two ways to reach it from another machine.

- Keep `APPLIANCE_LISTEN_ADDRESS=127.0.0.1` and put a reverse proxy with TLS in front of the HTTP port. Set `APPLIANCE_TRUST_PROXY=1` so the daemon honours the forwarded headers. The proxy must add the address of its client to `X-Forwarded-For`; the daemon uses the last entry.
- Set `APPLIANCE_LISTEN_ADDRESS` to another address. This publishes plain HTTP: the token, the cookie and the screenshots travel in the clear, and `install.sh` warns about it.

In both cases the TURN port must reach the browser. It carries only the encrypted video. If the dashboard is reached under a name that does not resolve to the host for TURN, set `APPLIANCE_TURN_HOST`.

TCP requests need the API token, as `Authorization: Bearer <token>`, or a session cookie. Print the token with `androidctl token`. The dashboard login link comes from `androidctl dashboard`. Failed logins are rate limited. Do not browse to untrusted services on the host that serves the dashboard: the session cookie has no port, so a browser sends it to every port of the same host.

```bash
androidctl token --rotate
```

Rotating the token replaces the session key too. It ends every dashboard session, every pending login link, and every TURN credential issued before it.

### A second instance

A second instance needs its own configuration file with its own name, state directory and ports. `install.sh` refuses a name or a state directory that belongs to another configuration.

```bash
cp conf/appliance.conf.example ~/.config/android-appliance/second.conf
# edit APPLIANCE_NAME, APPLIANCE_STATE_DIR, APPLIANCE_PORT and APPLIANCE_TURN_PORT
./install.sh --config ~/.config/android-appliance/second.conf
ANDROID_APPLIANCE_CONFIG=~/.config/android-appliance/second.conf androidctl status
```

### Upgrade and rollback

Pull the new version of the checkout and run `./install.sh` again. It builds a new image tag, switches the unit to it and keeps the previous image. To go back to the previous image:

```bash
./install.sh --rollback
```

### Uninstall

`--uninstall` stops and removes the unit, the images that `install.sh` built and the `androidctl` link. It keeps the state and the configuration. `--purge` also deletes the state directory, which holds all apps and data of Android, and the configuration. Both need the configuration file of the instance; give `--config` for an instance other than the default.

```bash
./install.sh --uninstall
./install.sh --uninstall --purge
```

## Migrate from v1

v1 ran as system units with a display on port 6090. `install.sh --migrate` moves it to v2 and adopts the same state directory in place: the AVD, userdata, accounts and adb keys stay where they are.

```bash
./install.sh --migrate
```

- It reads `/etc/android-appliance/appliance.conf` and writes the v2 configuration from it: state directory, AVD name, API level, memory, cores, disk size, idle stop, the display port as `APPLIANCE_PORT`. `host` rendering stays `host`, anything else becomes `auto`.
- It writes a manifest `<name>.migration` next to the configuration. The manifest records the v1 state directory, its owner and which v1 units were enabled.
- The steps that need root are printed: `chown` of the state when v1 ran as another user, and `systemctl disable --now` of the v1 units. The v1 units must be stopped before v2 runs.
- On an interactive terminal, it offers to run the steps with `sudo`. Otherwise run them yourself, then run `./install.sh --config <file>` to install v2.
- The v1 units stay installed but disabled. The v1 registry file `avd/<name>.ini` is not touched, so a rollback works.

To go back to v1:

```bash
./install.sh --migrate-back
```

This stops v2 and removes its unit. It keeps the configuration, the manifest and the images, so `./install.sh` installs v2 again. It then prints the root steps that bring v1 back: `restorecon -R` on the state (the container relabelled it), `chown` back to the v1 owner, and `systemctl enable --now` of the units that were enabled.

## androidctl

`androidctl` is a bash script that sends requests to the daemon with curl. Exit status is 0 on success, 1 on error and 2 on a usage error. If the daemon does not answer and the unit exists, it starts the unit and waits up to 60 s.

| Command | Purpose |
|---|---|
| **Lifecycle** | |
| `status` | print state, boot readiness and idle time |
| `start [--no-wait]` | start Android and wait until it is usable |
| `stop` | shut down Android and free its RAM |
| `restart [--no-wait]` | stop, then start again |
| `wait` | block until the current boot completes |
| `display` | print the local URL of the browser display |
| `dashboard` | print a one-time login link for the dashboard |
| **Automation** (these start Android when necessary) | |
| `screenshot [file]` | save the screen as PNG and print the path |
| `ui` | print the UI hierarchy as XML |
| `tap <x> <y>` | tap a screen coordinate |
| `long-press <x> <y> [ms]` | touch and hold a coordinate |
| `swipe <x1> <y1> <x2> <y2> [ms]` | swipe between two points |
| `text <string>` | type text into the focused field |
| `key <keyevent>` | send a key event, for example `HOME` or `66` |
| `shell <command...>` | run a shell command on the device |
| `install <apk> [-r\|-g\|-d\|-t...]` | install an APK (replace, grant, downgrade, test) |
| `uninstall <package> [-k]` | remove an app; `-k` keeps its data |
| `apps [--all]` | list installed apps; `--all` adds system apps |
| `launch <package>` | start an app |
| `force-stop <package>` | stop an app |
| `push <local> <remote>` | copy a file to the device |
| `pull <remote> <local>` | copy a file from the device |
| `ls <remote>` | list a device directory |
| **Appliance** | |
| `logs [emulator\|daemon] [-n N]` | print the last log lines |
| `token [--rotate]` | print the API token, or replace it |
| `mcp` | serve the MCP tools over stdio |
| `doctor` | check the host and the appliance |
| `version` | print the versions |

Files are copied one at a time; directories are not copied. `shell` does not read standard input: use `push` for data. Coordinates are device pixels of the 1080x1920 frame. Key names are not case sensitive; a bare number is an Android key code.

`androidctl doctor` prints `ok`, `WARN` and `FAIL` lines and exits 1 when a check fails. It checks Podman (rootless, crun, cgroups v2), `/dev/kvm`, the render node, SELinux labelling, lingering, the unit, the image, the ports, the state disk and the daemon.

Settings come from the environment, then from the configuration file, then from the defaults.

| Variable | Use |
|---|---|
| `ANDROID_APPLIANCE_CONFIG` | configuration file to read; selects another instance |
| `ANDROID_APPLIANCE_URL` | talk to a daemon over TCP, for example on a remote host |
| `ANDROID_APPLIANCE_TOKEN` | API token for `ANDROID_APPLIANCE_URL` |

```bash
ANDROID_APPLIANCE_URL=https://android.example.org \
ANDROID_APPLIANCE_TOKEN=... androidctl screenshot
```

## Dashboard

Open the link from `androidctl dashboard`, or the address `http://127.0.0.1:6090` and sign in with the API token. The link works once, within 2 minutes. The login sets a session cookie that lasts 7 days. Sign out ends the session of that browser.

The dashboard shows the live display over WebRTC. The video goes through the TURN server in the container, on one known port. You can:

- touch, with several fingers, and use the mouse: the left button is a finger, the right button is Back, the wheel scrolls;
- type with the keyboard, and paste any Unicode text;
- press the hardware buttons (Back, Home, Recents, Power, Volume down and up), and hold them;
- Start, Stop and Restart Android; Stop and Restart ask first;
- see the state, the viewers, the API level, the GPU, the last error, and the connection statistics (ICE, codec, resolution, frame rate, bit rate, lost packets, round trip).

The layout has a side panel at 900 px and wider, and a drawer below the display on a phone.

- The emulator sends a frame only when the display changes. On a static screen the dashboard shows a screenshot as a poster until the first frame arrives.
- The dashboard never starts a stopped device by itself. Only the Start button does.
- A stream that stalls for 3 seconds while the screen changes is restarted. A hidden tab closes its stream after 60 s and reconnects when it is shown.
- The keyboard capture is on with a mouse or a keyboard. Ctrl+Alt+K turns it off. On a touch device it starts off, so a tap does not raise the on-screen keyboard.
- In the first minutes after the first boot of a new AVD, typed keys can be dropped or reordered. Pasted text is not affected, and after a restart of the AVD the typing is exact.
- Typed keys are subject to the auto-capitalization of Android. Paste is exact.
- Up to 4 viewers can have a stream at once.

## HTTP API

The API is at `/api/v1` on the HTTP port. The unix socket `$XDG_RUNTIME_DIR/<name>/api.sock` is trusted and needs no token. TCP requests need `Authorization: Bearer <token>` or the session cookie. JSON is used in and out unless stated. An error is an HTTP status with `{"error": {"code": "...", "message": "..."}}`. Automation requests start Android when needed and wait for the boot.

| Method and path | Purpose |
|---|---|
| `GET /health` | health check, no auth |
| `GET /status` | state, boot readiness, idle time, phase, GPU, API level, last error |
| `POST /start`, `/stop`, `/restart` | change the state; `{"wait":true}` waits |
| `POST /wait` | wait for the current boot |
| `GET /screenshot` | PNG or JPEG; `format`, `quality`, `max_size`, `passive` |
| `GET /ui` | UI hierarchy as XML or a compact list |
| `POST /input/tap`, `/input/long-press`, `/input/swipe` | touch input in device pixels |
| `POST /input/text`, `/input/key` | type text, send a key |
| `POST /shell` | run a shell command; exit code, stdout, stderr |
| `GET /apps` | list packages (`all=1` for system apps) |
| `POST /apps/install` | install an APK sent as the body |
| `POST /apps/{package}/launch`, `/stop` | open or force-stop an app |
| `DELETE /apps/{package}` | uninstall (`keep_data=1` keeps data) |
| `GET /files`, `PUT /files` | pull or push one file |
| `GET /files/list` | list a device directory |
| `GET /logs` | emulator or daemon log lines |
| `POST /auth/login-link`, `GET /auth/token`, `POST /auth/token/rotate` | login link and token; unix socket only |
| `GET /session` | WebSocket session of the dashboard |

For shell clients the API also speaks plain text. A request with `Accept: text/plain` gets errors as plain text. `POST /input/text` and `POST /shell` accept a `text/plain` body. `POST /shell?format=raw` returns stdout as the body, the exit status in `X-Exit-Code` and stderr base64-encoded in `X-Stderr`. `?format=text` gives line output for status, apps, file lists, the token and the login link.

```bash
curl -s -H "Authorization: Bearer $(androidctl token)" \
  "http://127.0.0.1:6090/api/v1/status?format=text"
```

The full contract, with limits and error codes, is in `docs/ARCHITECTURE.md`.

## MCP server for agents

`androidctl mcp` serves MCP tools over stdio. It runs the server inside the container with `podman exec`, so the host needs no Python and no shared filesystem. Add it to the MCP configuration of an agent:

```json
{"mcpServers": {"android": {"command": "androidctl", "args": ["mcp"]}}}
```

For Claude Code:

```bash
claude mcp add android -- androidctl mcp
```

There is no MCP over HTTP. A remote agent runs `androidctl` with `ANDROID_APPLIANCE_URL`, or uses an MCP client on the host where the appliance is installed.

| Tool | What it does |
|---|---|
| `android_status` | state of Android, boot readiness, idle time, last error |
| `android_start` | start Android and wait until it is usable |
| `android_stop` | shut down Android gracefully and free its memory |
| `android_restart` | stop, then start again |
| `android_wait` | wait for a boot in progress |
| `android_screenshot` | the screen as an image, with the device size and scale |
| `android_ui` | the UI hierarchy: compact, JSON or raw XML |
| `android_tap` | tap a point |
| `android_long_press` | touch and hold a point |
| `android_swipe` | swipe between two points |
| `android_text` | type text into the focused field |
| `android_key` | send a key |
| `android_shell` | run a shell command as the shell user |
| `android_apps` | list installed packages |
| `android_launch` | open an app by package name |
| `android_force_stop` | force-stop an app |
| `android_install` | install an APK given as base64 (up to 64 MiB) |
| `android_uninstall` | uninstall an app; `keep_data` keeps its data |
| `android_ls` | list a device directory |
| `android_pull` | read a device file (up to 8 MiB) |
| `android_push` | write a device file (up to 64 MiB) |
| `android_logs` | last lines of the emulator or daemon log |

- Screenshots come back as image content, JPEG by default and scaled to `max_size`, with the device size and the scale as text.
- The UI hierarchy is one actionable element per line with its center. JSON and the raw XML are options. The center of an element is the best place to tap.
- Coordinates are device pixels of the 1080x1920 frame.
- Any tool that needs the device starts it and waits for the boot. The lifecycle tools report progress while they wait and stop after a bound (`timeout`, 300 s by default). They return the current state with a note, so clients with short timeouts keep working.
- Files travel inline, as UTF-8 text or base64, with the size limits above.
- `android_text` pastes exact Unicode. Errors come back as tool errors in the form `message (code)`.

## Lifecycle and idle

Android is in one of these states: `stopped`, `starting`, `running`, `stopping`, and `restarting` (the phase after a retried crash). `running` means the guest reports a completed boot and the post-boot setup is done. A new container starts with Android stopped.

- **On-demand boot.** Any automation command (screenshot, ui, input, shell, apps, files) starts Android when it is stopped, and waits for the boot. Concurrent starts share one boot.
- **Idle stop.** Every 30 s the daemon checks the idle time. When no command is running and Android has been idle for `APPLIANCE_IDLE_STOP_MINUTES` (60 by default), it stops Android. `0` disables the idle stop.
- **What counts as activity.** Automation commands that succeed, `start` and `restart`, dashboard input, and the start of a video stream. `status`, `wait`, `stop`, `logs`, and an open dashboard without input do not. The poster screenshot of the dashboard is passive and does not count, so an unused Android is not kept awake.
- **Graceful stop.** The daemon syncs the guest file system, then asks the emulator to quit and waits up to 60 s (measured 1 to 3 s). Only after that does it send SIGTERM and then SIGKILL. The sync matters: without it, files written seconds before a stop can be missing after the next boot. Stopping the container stops Android first.
- **Data safety.** Userdata, apps and accounts stay in the state directory across stops, crashes and container recreation.
- **Crash retry.** If qemu crashes during the boot or within 30 s after `running`, the daemon boots once more by itself. Waiting callers keep waiting. The retry is one per start request. Any other crash is reported in `last_error` and leaves Android stopped, and the next command cold boots. A guest power-off is treated as a request and does not retry.
- **Timings** on the reference host (Intel UHD 630, API 36, GPU rendering): the first boot of a new AVD to `running` takes 34 to 39 s. A cold boot with existing userdata takes about 15 to 18 s. A graceful stop takes 1 to 2.4 s.

## Backup

The state directory (`APPLIANCE_STATE_DIR`) holds everything: `avd/` (AVD and userdata), `home/.android/` (adb keys), `screenshots/`, and `appliance/` (daemon state, logs, and `secrets/` with the API token, the session key and the TURN secret). Stop Android first, so the copy is consistent:

```bash
androidctl stop
```

Then copy the directory with a tool that keeps permissions (the directory is mode 0700). The API token is in `appliance/secrets/api-token`, so treat the backup as private. Restoring it into an empty state directory and running `./install.sh` brings the device back with its apps and account.

## Troubleshooting

Run `androidctl doctor` first. It names most of the problems below.

| Symptom | Remedy |
|---|---|
| `/dev/kvm is missing` or `cannot use /dev/kvm` | Enable virtualization in the firmware. Add your user to the group that owns `/dev/kvm`, then log in again. |
| No render node, or `cannot use /dev/dri/renderD...` | Android renders with lavapipe, which is slower. Add your user to the group of the node for GPU rendering. `APPLIANCE_GPU=host` makes it an error instead. |
| `podman is not rootless` or `cgroups ... needs cgroups v2` | Run the appliance as a normal user on a host with cgroups v2. |
| `the container does not run as container_t` | Restart the unit: `systemctl --user restart android-appliance.service`. |
| `port ... is in use by another program` | Set another `APPLIANCE_PORT` or `APPLIANCE_TURN_PORT`, and run `./install.sh` again. |
| The unit is not written, or the Quadlet check fails | Check `APPLIANCE_QUADLET_DIR` (default `~/.config/containers/systemd`). A unit there without the `# Managed by install.sh` line is never overwritten. |
| `lingering is off` | `loginctl enable-linger "$USER"`. Without it the appliance stops at logout and does not start at boot. |
| `the image ... is missing` | Run `./install.sh` to build it. A removed image shows only in the journal otherwise. |
| `install.sh` reports that the daemon does not answer | Read the journal lines it prints. If a previous image exists, run `./install.sh --rollback`. |
| The state directory is refused | It must be a directory of its own: absent, empty, or an appliance state. Set `APPLIANCE_STATE_DIR`. |
| An existing AVD has another API level | Set `APPLIANCE_API_LEVEL` to the level `install.sh` names. |
| `only N GiB are free` or the state is on a rotational disk | Free disk space, or move the state to an SSD. |
| The boot fails with `timeout` | Raise `APPLIANCE_BOOT_TIMEOUT`. Check `androidctl logs emulator`. |
| The boot fails with `device unauthorized` | The AVD trusts another adb key. Copy the old `adbkey` and `adbkey.pub` into `home/.android/` of the state directory and restart. |
| qemu crashes right after the first boot | The daemon retries once by itself. A repeated crash is in `last_error` and `androidctl logs emulator`. |
| The dashboard shows a poster and no video | The TURN port must reach the browser (UDP and TCP). Set `APPLIANCE_TURN_HOST` if the page host name is not right for TURN. |
| WebRTC cannot connect | The daemon needs a global address in the container for the relay. Read `androidctl logs daemon`. |
| `rate_limited` | After 5 failed logins in a minute, wrong tokens wait. The right token always works. |
| An app on the device can reach the adb server | This cannot be closed from inside the container. See the trust statement in "How it works". |

## Development and tests

The design document is `docs/ARCHITECTURE.md`. It states the contracts between the parts and the measurements behind them.

```bash
./run-tests.sh
```

`./run-tests.sh` runs every suite that needs no emulator: ShellCheck, the bash tests under `tests/`, the Python tests under `tests/python` (in the `.venv` of `scripts/dev-venv.sh`, which needs `uv`), the unit tests of the dashboard with `node --test`, and the dashboard UI tests in Chromium. A missing tool gives a `SKIP` line. The UI tests need Playwright: set `AA_PLAYWRIGHT` to a `node_modules` directory that has `playwright` 1.62.1 with its Chromium, or run `npm ci` in `tests/e2e` and `npx playwright install chromium`.

Two tests run against a real Android. They need KVM, rootless Podman with Quadlet, a systemd user session and an image built by `./install.sh`. Each installs its own instance, tests it, and removes it again. The image stays.

```bash
make e2e
make e2e-webrtc
```

- `make e2e` runs `tests/e2e/lifecycle.sh`: boot, input, apps, files, auth, idle stop, crash retry and uninstall. It takes about 6 minutes and uses ports 47080 and 47081.
- `make e2e-webrtc` runs `tests/e2e/webrtc.sh`: the dashboard in headless Chromium against a real device. It needs Node.js and Playwright, takes about 20 minutes, and uses ports 47082 and 47083.

Other scripts (`./install.sh` runs the image build with the right arguments):

```bash
scripts/dev-venv.sh     # create .venv with the pinned test dependencies
scripts/lock-deps.sh    # pin the Python dependencies with hashes (needs uv)
```

The `Containerfile` builds the image on Fedora 44. It installs the runtime libraries of the emulator, Mesa, an X server for GPU rendering only (`Xvnc`, with no VNC client access), coturn, and Python. It adds the pinned SDK (emulator, platform-tools and the system image, checked with SHA-256), a Python environment from `requirements.lock`, the `appliance` package, `androidctl` and the dashboard. The image is not published, because the SDK license does not allow redistribution.

## License

MIT. See `LICENSE`.
