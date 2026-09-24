---
name: android
description: Operate the persistent Android Emulator appliance with androidctl and the android_* tools. Use it to open apps, read the screen, tap, type, install APKs and move files.
version: 0.1.0
platforms:
  - linux
metadata:
  hermes:
    tags: [android, emulator, automation, adb]
---

# Android appliance

One persistent Android device (Google Play, 1080x1920 portrait) runs on
this host on demand. `androidctl` is the only interface. Do not call `adb`,
`emulator` or `systemctl` directly, and do not look for serials or state
paths.

## Lifecycle

States: `stopped`, `starting`, `running`, `suspended`, `stopping`.

- Automation commands start or resume Android when necessary. Do not call
  `start` before them.
- `androidctl status` (tool `android_status`) never wakes Android. Use it
  to check the state.
- Android suspends after some idle time, then stops with its Quick Boot
  state saved. The next command restores it. This is normal.
- A start from `stopped` can take from some seconds (Quick Boot) to some
  minutes (cold boot). Wait for it; do not retry in a loop.

## Commands

Native tools cover the common operations: `android_status`,
`android_start`, `android_stop`, `android_screenshot`, `android_ui`,
`android_tap`, `android_swipe`, `android_text`, `android_key`.

Use the terminal for the other operations:

```text
androidctl status
androidctl start | stop | suspend | resume | restart | wait
androidctl screenshot [file]          # prints the PNG path
androidctl ui                         # UI hierarchy XML
androidctl tap <x> <y>
androidctl swipe <x1> <y1> <x2> <y2> [ms]
androidctl text <string>
androidctl key <keyevent>             # HOME, BACK, ENTER, 66, ...
androidctl shell <command...>
androidctl install <apk> [adb-args...]
androidctl push <local> <remote>
androidctl pull <remote> <local>
androidctl display                    # local browser display URL
```

## Procedure

1. Get the current screen with `android_ui`. Use `android_screenshot` and
   `vision_analyze` when the XML does not show enough.
2. Calculate the tap point from the element `bounds="[x1,y1][x2,y2]"`: use
   the center.
3. Do one action, then read the screen again before the next action.

Useful shell commands:

- Open an app: `androidctl shell monkey -p <package> -c android.intent.category.LAUNCHER 1`
- List packages: `androidctl shell pm list packages -3`
- Open a URL: `androidctl shell am start -a android.intent.action.VIEW -d <url>`
- Current activity: `androidctl shell dumpsys activity activities | grep -m1 ResumedActivity`

## Safety

- This device keeps signed-in accounts and app data. Do not wipe data,
  clear app storage, sign out, or run `pm clear` unless the user asks.
- Do not run `adb kill-server` or change the emulator configuration.
- Ask before a purchase, a payment, or a message to another person.
