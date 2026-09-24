# android-appliance

A NixOS module and a Hermes plugin for one persistent Android Emulator
device. Android starts on demand, suspends when idle, and stops with its
Quick Boot state saved after longer idle time. `androidctl` is the stable
interface for humans and agents. A browser shows the device screen through
scrcpy and noVNC.

## Architecture

```text
             androidctl  (CLI for humans, agents, Hermes tools, dashboard)
                 │
   ┌─────────────┼──────────────────────────────┐
   │ systemctl start/stop     adb emu avd stop/start, adb shell ...
   ▼                              ▼
android-appliance-emulator.service ──► Android Emulator (headless, -no-window)
   │ ExecStartPost: wait for boot       ▲
   │ ExecStop: adb emu kill (saves      │ adb
   │           Quick Boot state)        │
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
- **Suspend** uses the emulator console (`adb emu avd stop`). The guest
  RAM stays in memory and the vCPUs stop. **Resume** uses
  `adb emu avd start`.
- **Stop** uses `adb emu kill`, the normal emulator shutdown path. It saves
  the Quick Boot snapshot. systemd kills the process only if the stop does
  not finish in 3 minutes.
- **Idle policy** is a timestamp file and a systemd timer. The timer is
  bound to the emulator unit, so nothing runs while Android is stopped.
- **Display** is Xvnc (an X server with a VNC server), scrcpy, and noVNC
  (websockify in inetd mode). A systemd socket on loopback starts the chain
  at the first browser connection. The chain stops when the emulator stops.

## Ownership boundary

```text
NixOS:
  Android runtime (SDK, emulator, system image)
  systemd units
  packages (androidctl)
  permissions (polkit rule)
  persistent appliance directory

Android:
  AVD/userdata
  Quick Boot state

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
| `idleSuspendMinutes` | `10` | Pause Android after this idle time. |
| `idleHibernateMinutes` | `60` | Stop Android after this idle time, counted from the last use. |
| `memoryMiB` | `4096` | Guest RAM. |
| `cores` | `4` | Guest CPU cores. |
| `diskSize` | `32G` | Userdata size. It applies only when the AVD is created. |
| `gpu` | `swiftshader_indirect` | Emulator `-gpu` mode. Use `host` only with a usable host GPU and EGL. |
| `display.enable` | `true` | Serve the browser display. |
| `display.port` | `6090` | Loopback port of the noVNC page. |

A change to `memoryMiB` or `cores` makes the next start a cold boot. The
userdata stays.

The system image is Android 16 (API 36), Google APIs with Play Store,
x86_64. The screen is 1080x1920 at 420 dpi.

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
androidctl start [--no-wait]             start or resume, then wait until usable
androidctl stop                          shut down and save Quick Boot state
androidctl suspend                       pause and keep RAM
androidctl resume                        continue after suspend
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

The automation commands start or resume Android when necessary and update
the activity time. `status`, `wait`, `stop`, `suspend` and `display` do
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
| `suspended` | The emulator is paused. The guest RAM stays in host memory. |
| `stopping` | The unit is saving Quick Boot state and exits. |

Example:

```text
$ androidctl status
state=running boot_completed=1 idle_seconds=42
```

## Idle behavior

```text
running
  │  idleSuspendMinutes without use
  ▼
suspended
  │  idleHibernateMinutes without use (total, from the last use)
  ▼
stopped with Quick Boot state saved
```

"Use" is one of these:

- An `androidctl` automation or lifecycle command (not `status`).
- Keyboard or mouse input in the browser display.
- A new connection to the browser display.

Display input also resumes an Android that is suspended (in 30 seconds or
less). An open browser tab without input does not keep Android active. The
system never starts a stopped Android because of idle policy. A new command
or a new browser connection starts it.

The activity time is the modification time of `stateDir/last-activity`.

## Remote display

The display serves only the device screen. There is no emulator window
and no emulator tool panel.

- URL: `http://127.0.0.1:6090/vnc.html?autoconnect=true&resize=scale`
  (`androidctl display` prints it).
- The page listens only on loopback. Put a reverse proxy with
  authentication in front of it to use it from another host.
- The first connection starts or resumes Android. The screen is black until
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
  buttons Start, Suspend, Resume, Stop, Restart and Open Display.

The plugin finds `androidctl` on `PATH`, then at
`/run/current-system/sw/bin/androidctl`. Optional plugin settings are in
the Hermes configuration:

```bash
# Use this URL for "Open Display", for example a reverse proxy URL.
hermes config set plugins.entries.android-appliance.settings.display_url https://android.example.net/vnc.html?autoconnect=true&resize=scale
# Use a different androidctl.
hermes config set plugins.entries.android-appliance.settings.androidctl /path/to/androidctl
```

The Hermes process must run as the appliance user (or as root) to operate
Android.

## Permissions

- The emulator and the display processes run as `user`, with the `kvm`
  supplementary group.
- A polkit rule lets `user` start, stop and restart only
  `android-appliance-emulator.service`. The user gets no other systemd
  permission and no sudo rule. This works in services with
  `NoNewPrivileges=yes`, because polkit uses D-Bus and not setuid.
- The display chain starts through socket activation and unit dependencies,
  so it needs no permission.
- The noVNC page and the VNC socket are local only. The VNC socket has mode
  `0600`.

## Backup

Back up `stateDir` (default `/var/lib/android-appliance`) to keep Android
app state:

| Path | Content |
|---|---|
| `stateDir/avd/` | AVD configuration, userdata and the Quick Boot snapshot. This is the important data. |
| `stateDir/home/` | adb keys and emulator settings. |
| `stateDir/screenshots/` | Default screenshot location. You can skip it. |

Stop Android (`androidctl stop`) before a backup, because the emulator
changes the userdata image while it runs. You can skip
`stateDir/avd/*.avd/snapshots/`: without it, the next start is a cold boot.

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
| Every start is a cold boot | The snapshot did not save. Look for errors near `emu kill` in the log. A `memoryMiB` or `cores` change also causes one cold boot. |
| `state=starting` does not change | Android did not finish its boot. Run `androidctl restart`. If that does not help, look at the emulator log. |
| `device unauthorized` or `device offline` | The appliance uses its own adb server on port 5038 and its keys in `stateDir/home/.android`. Do not start a different adb server with the same port. Run `androidctl restart`. |
| `Interactive authentication required` | The command ran as a user other than `user`. Run it as `user` or as root. |
| Black browser display | Android is starting, or scrcpy restarts. Look at the scrcpy log. |
| `-gpu host` fails | Use the default `swiftshader_indirect`, or give the host a working GPU and EGL. |

To run adb directly for debugging, use the same environment as
androidctl:

```bash
# Show ANDROID_HOME and the other values of the unit environment.
systemctl show -p Environment android-appliance-emulator
sudo -u <user> env HOME=<stateDir>/home ANDROID_ADB_SERVER_PORT=5038 \
  <ANDROID_HOME>/platform-tools/adb devices
```

For device commands, use `androidctl shell <command>`.

## Development and tests

```bash
nix flake check                    # lint, unit tests, module evaluation
nix build .#integration-test -L    # real emulator in a NixOS VM (KVM, nested)
hermes plugins validate .          # Hermes admission checks
hermes plugins doctor . --ci       # Hermes runtime contract checks
```

`nix flake check` runs:

- ShellCheck on the scripts.
- Lifecycle tests for `androidctl` with fake `systemctl`, `adb` and
  `xprintidle` commands. They need no KVM.
- Python tests for the Hermes tools and the dashboard backend.
- An evaluation of a NixOS system with the module. It asserts that the
  emulator is not part of a boot target.
- nixfmt.

The integration test boots Android in a VM and checks these items: Android
off after boot, narrow permissions, fresh AVD creation, cold start, boot
completion, adb commands, screenshot, suspend, near-instant resume, Quick
Boot stop and restore, idle suspend, idle hibernate, the browser display,
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
5. **Suspend**: Run `androidctl suspend`. Make sure that the status is
   `suspended` and that the emulator CPU use is near zero (`top`).
6. **Resume**: Run `time androidctl resume`. It must take less than one
   second.
7. **Quick Boot stop**: Run `androidctl stop`. Make sure that no
   `qemu-system` process exists and that
   `stateDir/avd/android.avd/snapshots/default_boot` exists.
8. **Quick Boot restore**: Run `time androidctl start`. It must take
   seconds, not minutes. `journalctl -u android-appliance-emulator` must
   not show a cold boot.
9. **Idle suspend**: Do not use Android for `idleSuspendMinutes`. Make sure
   that the status is `suspended`.
10. **Idle hibernate**: Do not use Android for `idleHibernateMinutes`. Make
    sure that the status is `stopped` and that the RAM is free.
11. **Browser display**: Open the display URL through an SSH tunnel or a
    reverse proxy. Make sure that only the device screen shows and that
    taps and keys work.
12. **Hermes plugin install**: Run `hermes plugins install
    JuanDelPueblo/android-appliance` and `hermes plugins enable
    android-appliance`. Restart Hermes.
13. **Hermes tool invocation**: Ask Hermes for the Android status and a
    screenshot. Make sure that it uses `android_status` and
    `android_screenshot`.
14. **Dashboard controls**: Open the **Android** tab. Use each button and
    make sure that the state changes.
15. **Host reboot**: Reboot the host. Make sure that `androidctl status`
    shows `state=stopped` after the boot.

## License

MIT
