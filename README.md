# android-appliance

A NixOS module, a standalone installer for systemd hosts (Fedora
Server and similar), and a Hermes plugin for one persistent Android
Emulator device. Android cold boots on demand and shuts down when idle, keeping
its apps, accounts and userdata. Suspend/resume and Quick Boot are removed.
`androidctl` is the stable interface for humans and agents. A browser shows the device screen through
scrcpy and noVNC.

## Architecture

```text
             androidctl  (CLI for humans, agents, Hermes tools, dashboard)
                 │
   ┌─────────────┼──────────────────────────────┐
   │ systemctl start/stop     adb shell ...
   ▼                              ▼
android-appliance-emulator.service ──► Android Emulator (headless, -no-window)
   │ ExecStartPost: wait for boot       ▲
   │ ExecStop: adb emu kill             │ adb
   │                                    │
   ├─► android-appliance-idle.timer ──► androidctl idle-check (every 30 s)
   │
   └─(stops)─► display units
                                        │
127.0.0.1:6090 ─► android-appliance-display.socket
                    └► websockify + noVNC ─► Xvnc :57 ◄─ scrcpy (device screen)
```

The design uses native parts only:

- **systemd** is the lifecycle manager. There is no custom daemon.
- The emulator unit is in the `activating` state until Android reports
  `sys.boot_completed=1`. Thus the systemd state is the lifecycle state.
  `androidctl start` queues the unit with `systemctl start --no-block`
  and then waits independently, so Ctrl-C stops waiting without tearing
  down an otherwise valid start. The unit keeps its `ExecStartPost`
  boot wait, so `activating` still means starting.
- **Start** always uses `-no-snapshot`. Existing apps, accounts and userdata
  survive shutdown, but guest RAM and running processes do not.
- **Stop** uses `adb emu kill`, the normal emulator shutdown path, without
  snapshot saving. `stop-hook` waits up to 90 seconds, then systemd can
  send SIGTERM; the outer `TimeoutStopSec` is 3 minutes.
- **Guest RAM** stays anonymous with `-feature -QuickbootFileBacked`,
  including when adopting an AVD that previously used snapshots.
- **Idle policy** is a timestamp file and a systemd timer. The timer is
  bound to the emulator unit, so nothing runs while Android is stopped.
  The emulator unit wants the timer. The timer is not enabled at boot:
  because of `BindsTo=`, an enabled timer would start Android at boot.
- **Display** is Xvnc at 1080x1920 (the device native size), scrcpy, and
  noVNC (websockify in inetd mode). A systemd socket on loopback starts
  the chain at the first browser connection. The chain stops when the
  emulator stops.

## Ownership boundary

```text
NixOS:
  Android runtime (SDK, emulator, system image)
  systemd units
  androidctl (module implementation detail)
  permissions (polkit rule)
  persistent appliance directory

Android:
  AVD/userdata

Hermes:
  plugin enablement
  skill/tool state
  dashboard/plugin configuration
```

The NixOS module does not write Hermes files. Hermes does not manage the
emulator. The Hermes plugin only runs `androidctl`.

## Install the NixOS module

Add the flake input and import the module:

```nix
{
  inputs.android-appliance.url = "github:JuanDelPueblo/android-appliance";

  outputs = { nixpkgs, android-appliance, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        android-appliance.nixosModules.default
        {
          services.android-appliance = {
            enable = true;
            user = "tony";
            group = "users";
            stateDir = "/var/lib/juno/android";
          };
        }
      ];
    };
  };
}
```

Requirements:

- An x86_64 host with KVM (`/dev/kvm`).
- Unfree packages must be allowed for the Android SDK, for example with
  `nixpkgs.config.allowUnfree = true` or an `allowUnfreePredicate` that
  accepts `android-sdk-*` packages.
- Enabling the module accepts the Android SDK license.

### Options

| Option | Default | Description |
|---|---|---|
| `enable` | `false` | Enable the appliance. |
| `user` | `android-appliance` | User that runs the emulator and operates it. The module creates the default user. |
| `group` | `android-appliance` | Primary group of the appliance processes and state. |
| `stateDir` | `/var/lib/android-appliance` | Persistent appliance directory. |
| `avdName` | `android` | AVD name. |
| `apiLevel` | `36` | Android API level of the system image. |
| `idleStopMinutes` | `60` | Shut Android down after this idle time. The next use cold boots. |
| `memoryMiB` | `4096` | Guest RAM. |
| `cores` | `4` | Guest CPU cores. |
| `diskSize` | `32G` | Userdata size. It applies only when the AVD is created. |
| `quickBoot` | `false` | Compatibility option: it must remain `false`. Snapshot load/save is always disabled. |
| `gpu` | `swiftshader` | Emulator `-gpu` mode. The default software renderer works on every headless host. `host` needs a usable GPU and EGL; it uses the appliance X server (`:57`), adds `render`/`video` groups, binds only `/tmp/.X11-unix`, and passes `-feature -Vulkan` to match the known-good Juno setup. |
| `display.enable` | `true` | Serve the browser display at 1080x1920. Host GPU mode keeps its X server even when browser viewing is disabled. |
| `display.port` | `6090` | Loopback port of the noVNC page. |

Every start is a cold boot. Hardware setting changes preserve userdata.

For existing configurations, remove `idleSuspendMinutes` and rename
`idleHibernateMinutes` to `idleStopMinutes` (the old name remains an alias).
Remove `quickBoot` or keep it `false`; `true` fails module validation.

The system image is Android 16 (API 36), Google APIs with Play Store,
x86_64. Set `apiLevel` to pick another level for a new or adopted AVD.
The screen is 1080x1920 at 420 dpi.

`apiLevel` does not upgrade an existing AVD. If `stateDir/avd` already
holds an AVD, `avd-init` checks its system image against the configured
level. A mismatch stops the start with an explicit error, and does not
touch the AVD. To change the level, either set `apiLevel` (NixOS) or
`APPLIANCE_API_LEVEL` (Fedora) to the level of the existing AVD, or
delete the AVD and let `avd-init` create a new one.

```text
avd-init: existing AVD 'android' uses 'system-images/android-36/...', but
avd-init: APPLIANCE_API_LEVEL in /etc/android-appliance/appliance.conf is 35 (...).
avd-init: APPLIANCE_API_LEVEL does not upgrade existing AVDs; use the matching level or recreate the AVD.
```

`avd-init` also refuses a stale registry path. The emulator follows
`path=` in `stateDir/avd/<name>.ini`; if that points outside the expected
`stateDir/avd/<name>.avd` (for example after moving `stateDir`), the start
stops instead of inspecting one AVD while launching another. Point the
state directory at the live tree. A missing registry file is recreated.

```text
avd-init: registry '/var/lib/juno/android/avd/android.ini' points to '/srv/pool/vms/android/avd/android.avd',
avd-init: but APPLIANCE_STATE_DIR in /etc/android-appliance/appliance.conf expects '/var/lib/juno/android/avd/android.avd'.
avd-init: refusing to inspect one AVD while the emulator would launch another.
```

## Install on Fedora Server (or another systemd distribution)

The appliance also installs without Nix, with its own installer.
`install.sh` is the single entry point: it installs the pinned Android
SDK, the scripts, the units and the rules.

1. Install the operating-system packages, as root. Fedora 44 ships no
   scrcpy; the zeno/scrcpy COPR provides it:

   ```bash
   dnf install tigervnc-x11-server xdpyinfo python3-websockify novnc \
     libXScrnSaver polkit dnf-plugins-core curl unzip
   dnf copr enable zeno/scrcpy
   dnf install scrcpy
   ```

2. Copy the configuration, as root. The keys match the options above:
   `APPLIANCE_STATE_DIR` is `stateDir`, `APPLIANCE_API_LEVEL` is
   `apiLevel`, `APPLIANCE_IDLE_STOP_MINUTES` is `idleStopMinutes`, and
   so on. Then edit `/etc/android-appliance/appliance.conf`.

   ```bash
   install -Dm644 conf/appliance.conf.example /etc/android-appliance/appliance.conf
   ```

3. Install and start, as root. The installer also creates the
   configuration file from the example when it is missing, then applies
   it. The first run downloads about 2.5 GB for the SDK.

   ```bash
   ./install.sh
   androidctl start
   ```

`install.sh` is idempotent: run it again after a configuration change.
It installs the scripts under `/usr/local`, the unit files under
`/etc/systemd/system` with a deployment drop-in, the tmpfiles rule and
the polkit rule. It enables and starts the display socket when the
display is on. It does not enable the emulator or the idle timer, so
Android stays off after a host boot. On an SELinux host, it gives the
installed files their default labels with `restorecon`.
`tests/smoke.sh` runs an end-to-end check on the installed host. The
idle display input uses `display-idle`, a small Python helper that needs
`libXScrnSaver`, because Fedora does not package `xprintidle`.

### The Android SDK

`install.sh` runs `scripts/provision-sdk.sh`, which installs the SDK in
`APPLIANCE_ANDROID_HOME` (default `/opt/android-sdk`). You can also run
the script alone, as root. The emulator and the system image are not in
the Fedora repositories.

The script downloads the emulator, platform-tools and the system image
of `APPLIANCE_API_LEVEL` from Google. `conf/sdk.lock` pins the archive
and the SHA-256 of each package revision, so two installs give the same
SDK. A package that has the pinned revision stays as it is, so a second
run downloads nothing. A package with a different revision, for example
from `sdkmanager`, is replaced. A wrong SHA-256 stops the script before
it changes the SDK. The archives are under the
[Android SDK License](https://developer.android.com/studio/terms).

The script needs `curl` and `unzip`; it does not need Java. The
`APPLIANCE_SDK_*` keys in `appliance.conf` select other revisions from
the lock. To use a revision that is not in `conf/sdk.lock`, add a line
to `/etc/android-appliance/sdk.lock`. The script reads that file first.

| Package | Default revision |
|---|---|
| `emulator` | 37.2.12 |
| `platform-tools` | 37.0.1 |
| `system-images;android-36;google_apis_playstore;x86_64` | 7 |
| `system-images;android-35;google_apis_playstore;x86_64` | 9 |
| `cmdline-tools` (only when `APPLIANCE_SDK_CMDLINE_TOOLS_VERSION` is set) | 23.0 |

To manage the SDK yourself, set `APPLIANCE_SDK_MANAGED=0`. Then
`install.sh` does not change the SDK. It only runs the check below.

`./scripts/provision-sdk.sh --check` checks an installed SDK and changes
nothing. It finds the emulator, adb and the system image, and runs
`ldd` on the emulator and the x86_64 qemu binaries. `ldd` gets the
bundled library path of the emulator (`emulator/lib64` and
`emulator/lib64/qt/lib`), as the `emulator` launcher does at run time.
Thus the check reports only missing host libraries. The check also makes
sure that other users can read and run the SDK files, because the
emulator runs as `APPLIANCE_USER`. `sdkmanager` installs the programs
with mode `0744`, so `provision-sdk.sh` runs `chmod -R a+rX` on the SDK
after the install. The check also warns when an installed revision is
not the pin.

## Adopting an AVD from another appliance

An AVD trusts the adb host key that created it. The old Juno service put
that key in `ANDROID_USER_HOME=/var/lib/juno/android/user-home`, while
this module uses `stateDir/home/.android`. To adopt old userdata, copy
the key before the first start:

1. Stop the old service.
2. Copy the key files:
   `cp /var/lib/juno/android/user-home/.android/adbkey* <stateDir>/home/.android/`
3. Set `apiLevel` to the level of the old AVD. The start then adopts the
   AVD or refuses on a mismatch.
4. Start Android: `androidctl start`.

A missing key makes the guest answer `device unauthorized` on first boot.

## Initial AVD creation

There is no manual step. The first `androidctl start` runs `avd-init`,
which writes the AVD files in `stateDir/avd`. The emulator then creates
userdata and does a cold boot. This first boot takes some minutes.

To start again from a clean device:

1. Stop Android: `androidctl stop`.
2. Delete the AVD: `rm -rf <stateDir>/avd`.
3. Start Android: `androidctl start`.

WARNING: Step 2 deletes all apps, accounts and data on the device.

## androidctl

```text
androidctl status                        state, boot readiness, idle time
androidctl start [--no-wait]             cold boot, then wait until usable
androidctl stop                          shut down, keeping apps and userdata
androidctl restart [--no-wait]           stop, then start
androidctl wait                          block until the current boot completes
androidctl display                       print the local browser display URL

androidctl screenshot [file]             save a PNG and print its path
androidctl ui                            print the UI hierarchy XML
androidctl tap <x> <y>
androidctl swipe <x1> <y1> <x2> <y2> [ms]
androidctl text <string>
androidctl key <keyevent>
androidctl shell <command...>
androidctl install <apk> [adb-args...]
androidctl push <local> <remote>
androidctl pull <remote> <local>
```

The automation commands start Android when necessary and update
the activity time. `status`, `wait`, `stop` and `display` do
not update the activity time. `status` never starts or wakes Android.

Exit status: `0` for success, `1` for an error, `2` for a usage error.

Run `androidctl` as the configured user. When root runs it, it runs again
as the configured user.

## Lifecycle states

| State | Meaning |
|---|---|
| `stopped` | The emulator unit is inactive. No emulator process and no guest RAM. |
| `starting` | The unit is active or activating, but Android has not finished its boot. |
| `running` | Android reports `sys.boot_completed=1`. |
| `stopping` | The unit shuts down Android and exits. |

Example:

```text
$ androidctl status
state=running boot_completed=1 idle_seconds=42
```

## Idle behavior

```text
running
  │  idleStopMinutes without use
  ▼
stopped (apps and userdata preserved)
```

"Use" is an automation command, Start or Restart, browser display input,
or a new display connection. Status polling and an open tab without input
do not keep Android running. The idle timer skips shutdown while a CLI
operation is in progress, and boot completion refreshes the activity time.
A new command or display connection cold boots a stopped Android.

The activity time is the modification time of `stateDir/last-activity`.

## Remote display

The display serves only the device screen at 1080x1920. There is no
emulator window and no emulator tool panel.

- URL: `http://127.0.0.1:6090/vnc.html?autoconnect=true&resize=scale`
  (`androidctl display` prints it).
- The page listens only on loopback. Put a reverse proxy with
  authentication in front of it to use it from another host.
- The first connection starts Android. The screen is black until
  Android is ready.
- When Android stops for idle time, the page shows "Disconnected". Reload
  the page to start Android again.

## Hermes plugin

The repository is also a native Hermes plugin. Install it with Hermes, not
with Nix:

```bash
hermes plugins install JuanDelPueblo/android-appliance
hermes plugins enable android-appliance
```

Restart the Hermes gateway and dashboard after you enable the plugin.

The plugin provides:

- The skill `android-appliance:android`, with the androidctl procedures.
- The tools `android_status`, `android_start`, `android_stop`,
  `android_screenshot`, `android_ui`, `android_tap`, `android_swipe`,
  `android_text` and `android_key`. Each tool runs one `androidctl`
  command.
- An **Android** dashboard tab with the state, the boot readiness, and the
  buttons Start, Stop, Restart and Show Display. The device view embeds
  inside the tab, with Reload Display and Hide Display controls.

The plugin finds `androidctl` on `PATH`, then at
`/usr/local/bin/androidctl`. Optional plugin settings are in
the Hermes configuration:

```bash
# Optional: use an existing authenticated reverse proxy for the embedded view.
hermes config set plugins.entries.android-appliance.settings.display_url 'https://android.example.net/vnc.html?autoconnect=true&resize=scale'
# Use a different androidctl.
hermes config set plugins.entries.android-appliance.settings.androidctl /path/to/androidctl
```

With no `display_url` override, a dashboard with native cookie authentication
serves noVNC and its WebSocket through the dashboard's own URL. Remote
browsers never receive the host's loopback address. The bridge connects only
to the loopback URL from `androidctl display`, forwards no Hermes credentials,
and requires a short-lived, single-use ticket for the WebSocket upgrade.

Legacy token-only dashboards can use the direct display when browsing on
localhost. Remote token-only dashboards need an explicit authenticated
`display_url`. An HTTPS dashboard needs an HTTPS display. An external URL
must allow iframe embedding (its CSP/frame headers and login cookies must
permit it); the default same-origin view avoids that cross-site constraint.

Show Display starts Android on demand. After an idle shutdown, use Reload
Display to start it again. Automatic reconnect is off so an idle tab does
not repeatedly start Android.

The Hermes process must run as the appliance user (or as root) to operate
Android. The dashboard and display HTTP routes use Hermes authentication;
the reverse proxy must support WebSocket upgrades.

## Permissions

- The emulator and the display processes run as `user`, with the `kvm`
  supplementary group.
- A polkit rule lets `user` start, stop and restart only
  `android-appliance-emulator.service`. The user gets no other systemd
  permission. androidctl sends these requests to systemd over D-Bus, so
  it also works from a hardened service such as the Hermes gateway.
- The display chain starts through socket activation and unit dependencies,
  so it needs no permission.
- The noVNC page and the VNC socket are local only. The VNC socket has mode
  `0600`.
- The Xvnc unit of the standalone install has no `NoNewPrivileges=true`.
  On an SELinux host such as Fedora, `/usr/bin/Xvnc` has the type
  `xserver_exec_t`, and systemd starts it in the X server domain
  (`xserver_t`). `NoNewPrivileges=` blocks that domain change
  (`nnp_transition`), and Xvnc cannot start. The unit has an empty
  `CapabilityBoundingSet=` instead, so a setuid program cannot give
  capabilities to Xvnc. The other units keep `NoNewPrivileges=true`.
  The trade-off: the X server domain is unconfined in the Fedora
  policy, so Xvnc gets no extra SELinux confinement. A host change, such
  as the `bin_t` label for `/usr/bin/Xvnc`, is not necessary.

## Backup

Back up `stateDir` (default `/var/lib/android-appliance`) to keep Android
app state:

| Path | Content |
|---|---|
| `stateDir/avd/` | AVD configuration and userdata. This is the important data. |
| `stateDir/home/` | adb keys and emulator settings. |
| `stateDir/screenshots/` | Default screenshot location. You can skip it. |

Stop Android (`androidctl stop`) before a backup, because the emulator
changes the userdata image while it runs. Old `snapshots/` directories
are ignored and do not need to be backed up.

## Troubleshooting

Look at the unit logs first:

```bash
journalctl -u android-appliance-emulator -b
journalctl -u android-appliance-idle -b
journalctl -u android-appliance-scrcpy -u android-appliance-display -b
```

| Symptom | Cause and action |
|---|---|
| `start` fails at once | Look for KVM errors in the log. Make sure that `/dev/kvm` exists. |
| `start` waits a long time on the first run | The first boot is a cold boot that creates userdata. Wait up to 20 minutes on slow hosts. |
| Every start is a cold boot | Expected: snapshot load/save is disabled. Apps and accounts remain in userdata. |
| "System UI isn't responding", ANRs, `system_server` restarts | The guest does not get CPU or disk time. Make sure that the log shows "Feature 'QuickbootFileBacked' ... overridden to 'disabled'". Keep `stateDir` on a fast local disk. |
| `state=starting` does not change | Android did not finish its boot. `start` waits with `--no-block`, so Ctrl-C only stops waiting. If the unit died, `start` reports it; otherwise run `androidctl restart`. Look at the emulator log. |
| `device unauthorized` | The AVD trusts another adb key. `start` fails fast with this diagnostic. Copy the old key into `stateDir/home/.android/` (see adopting an AVD), then restart. The appliance uses its own adb server on port 5038; do not start another server with the same port. |
| `device offline` for 120s | The guest is stuck. `start` reports it; look at the emulator log and restart. A brief offline during early boot is normal. |
| `refusing to inspect one AVD` | `stateDir/avd/<name>.ini` points outside `stateDir`. Point `stateDir` at the live tree; the AVD is untouched. |
| `Interactive authentication required` | The command ran as a user other than `user`. Run it as `user` or as root. |
| `activity.lock: Read-only file system` inside Hermes | With `ProtectSystem=strict`, grant the appliance `stateDir` in `ReadWritePaths` for both `hermes-agent` and `hermes-backend`. Also preserve normal user ownership of the directory. A service-local read-only mount does not establish that the host root filesystem is read-only. |
| Black browser display | Android is starting, or scrcpy restarts. Look at the scrcpy log. |
| Xvnc does not start, and the audit log shows `nnp_transition` | A local drop-in or an old unit adds `NoNewPrivileges=true` (or an option that implies it) to `android-appliance-xvnc.service`. Remove it; see Permissions. |
| `-gpu host` fails with EGL/display errors | Host mode needs the appliance X server (`:57`), `render`/`video` groups, and `-feature -Vulkan`. The module sets this; do not override with plain `-gpu host` and no display. Software `swiftshader` needs no display. |

To run adb directly for debugging, run it as `user` with the environment
of the emulator unit (`HOME`, `ANDROID_ADB_SERVER_PORT`, `ANDROID_HOME`):

```bash
systemctl show -p Environment android-appliance-emulator
```

For device commands, use `androidctl shell <command>`.

## Development and tests

```bash
nix flake check                    # lint, unit tests, module evaluation
nix build .#integration-test -L    # real emulator in a NixOS VM (KVM, nested)
./run-tests.sh                     # no KVM: lint, bash tests, unit checks,
                                   # staged install, plugin and dashboard tests
./tests/smoke.sh                   # installed host with KVM: real emulator
hermes plugins validate .          # Hermes admission checks
hermes plugins doctor . --ci       # Hermes runtime contract checks
```

`run-tests.sh` runs ShellCheck, the `androidctl` lifecycle tests with
fake `systemctl`, `adb` and idle commands, the `avd-init` tests, the
configuration-loader tests, the unit-file checks, the SDK check tests
with a fake `ldd`, a staged installation test, the Hermes plugin tests
and the dashboard tests. It needs no KVM. It reports a SKIP when a tool such as shellcheck, node or a Python
module is not installed.

`nix flake check` runs:

- ShellCheck on the scripts.
- Lifecycle tests for `androidctl` with fake `systemctl`, `adb` and
  `xprintidle` commands, including `--no-block` start, unit-dies,
  unauthorized, offline-timeout, bounded stop-hook, stale UI dump, failed
  screenshot preservation and in-flight idle protection cases. They need
  no KVM.
- `avd-init` tests, including stale registry-path refusal, registry
  recreation, and native-resolution enforcement.
- Python tests for the Hermes tools and dashboard backend, including real
  loopback HTTP/WebSocket proxy traffic and ticket rejection/replay checks.
- Dashboard JavaScript tests for serial polling, persistent action errors,
  embedded display URLs and removed controls.
- An evaluation of NixOS systems with the module, for both software and
  host GPU. It asserts that the emulator is not part of a boot target,
  that host mode has `DISPLAY`, Xvnc ordering, the X socket bind,
  `render`/`video` groups and `-feature -Vulkan`, that software mode has
  none of those, and that the display is 1080x1920.
- nixfmt.

The integration test boots Android in a VM and checks these items: Android
off after boot, narrow permissions, fresh AVD creation, cold start, boot
completion, adb commands, screenshot, shutdown and cold boot, idle shutdown, the browser display,
and a host reboot with Android off.

### Manual validation

Do these steps on a real host after the module is deployed:

1. **Fresh AVD creation**: Make sure that `stateDir/avd` does not exist.
   Run `androidctl start`. Make sure that `stateDir/avd/android.ini`
   exists.
2. **Cold start and boot completion**: Make sure that `androidctl start`
   returns and `androidctl status` shows `state=running boot_completed=1`.
3. **ADB command**: Run `androidctl shell getprop ro.build.version.release`.
4. **Screenshot**: Run `androidctl screenshot` and open the PNG.
5. **Shutdown and persistent data**: Keep a harmless file in Android's
   `/data/local/tmp`, stop Android, then start it. Confirm a cold boot,
   `state=running`, and that the file still exists.
6. **Idle shutdown**: Leave Android unused for `idleStopMinutes`. Confirm
   `state=stopped` and that the emulator process has exited.
7. **Browser display**: Open the display through an SSH tunnel or an
   authenticated reverse proxy. Check screen rendering, taps and keys.
8. **Hermes plugin**: Install and enable the plugin, then restart Hermes.
   Ask it for the Android status and a screenshot.
9. **Dashboard**: Open the Android tab. Test Start, Stop, Restart and
   Show Display. Confirm the embedded view loads on a remote browser
   through HTTPS, including taps and Reload Display after shutdown.
10. **Host reboot**: Confirm Android remains stopped after reboot.

## License

MIT
