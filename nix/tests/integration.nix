# End-to-end test with the real emulator in a NixOS VM.
#
#   nix build .#integration-test -L
#
# The host needs KVM with nested virtualization. The first run downloads the
# Android system image. A cold boot inside the VM takes some minutes.
{ pkgs, self }:
let
  # The Android SDK is unfree. The test gets its own package set so the
  # flake's default pkgs stay free.
  testPkgs = import pkgs.path {
    system = pkgs.stdenv.hostPlatform.system;
    config.allowUnfree = true;
  };
in
testPkgs.testers.runNixOSTest {
  name = "android-appliance";

  nodes.machine = {
    imports = [ self.nixosModules.default ];

    virtualisation = {
      memorySize = 8192;
      cores = 4;
      diskSize = 20480;
      qemu.options = [ "-cpu host" ];
    };
    boot.kernelModules = [
      "kvm-intel"
      "kvm-amd"
    ];

    users.users.tony = {
      isNormalUser = true;
      group = "users";
    };
    users.users.mallory.isNormalUser = true;

    environment.systemPackages = [
      testPkgs.curl
      testPkgs.xwininfo
    ];

    services.android-appliance = {
      enable = true;
      user = "tony";
      group = "users";
      memoryMiB = 2048;
      cores = 2;
      diskSize = "4G";
      idleSuspendMinutes = 1;
      idleHibernateMinutes = 2;
    };
  };

  testScript = ''
    import time

    state_dir = "/var/lib/android-appliance"
    emulator = "android-appliance-emulator.service"

    def tony(cmd, timeout=900):
        return machine.succeed(f"su tony -c '{cmd}'", timeout=timeout).strip()

    def status():
        return tony("androidctl status")

    def wait_state(state, timeout=300):
        machine.wait_until_succeeds(f"su tony -c 'androidctl status' | grep -q '^state={state} '", timeout=timeout)

    def set_idle(seconds):
        machine.succeed(f"su tony -c 'touch -d @$(($(date +%s) - {seconds})) {state_dir}/last-activity'")

    def uptime():
        return float(tony("androidctl shell cat /proc/uptime").split()[0])

    machine.wait_for_unit("multi-user.target")

    with subtest("android stays off after boot"):
        machine.fail(f"systemctl is-active {emulator}")
        machine.succeed("systemctl is-active android-appliance-display.socket")
        assert status().startswith("state=stopped boot_completed=0"), status()

    with subtest("permissions are narrow"):
        machine.fail(f"su mallory -c 'systemctl start {emulator}'")
        machine.fail("su tony -c 'systemctl stop nscd.service'")
        machine.fail("su tony -c 'systemctl start android-appliance-idle.service'")
        machine.fail(f"su tony -c 'systemctl mask {emulator}'")

    with subtest("fresh AVD, cold start and boot completion"):
        start = time.monotonic()
        tony("androidctl start", timeout=1500)
        print(f"cold boot took {time.monotonic() - start:.0f}s")
        assert status().startswith("state=running boot_completed=1"), status()
        machine.succeed(f"test -f {state_dir}/avd/android.ini")
        print(machine.succeed(f"cat {state_dir}/avd/android.avd/config.ini"))
        machine.succeed(f"grep -Eq '^hw.ramSize ?= ?2048' {state_dir}/avd/android.avd/config.ini")

    with subtest("adb commands and screenshot"):
        assert tony("androidctl shell getprop ro.build.version.sdk") == "36"
        tony("androidctl screenshot /tmp/shot.png")
        machine.succeed("head -c 8 /tmp/shot.png | od -An -tx1 | grep -q '89 50 4e 47'")
        assert "<hierarchy" in tony("androidctl ui")
        tony("androidctl key HOME")
        tony("androidctl tap 540 960")
        tony("androidctl swipe 540 1500 540 500 200")

    with subtest("root runs androidctl as the appliance user"):
        assert machine.succeed("androidctl status").startswith("state=running")
        machine.succeed(f"test $(stat -c %U {state_dir}/last-activity) = tony")

    with subtest("suspend and resume"):
        tony("androidctl suspend")
        assert status().startswith("state=suspended"), status()
        # status does not wake the emulator.
        assert status().startswith("state=suspended"), status()
        start = time.monotonic()
        tony("androidctl resume")
        elapsed = time.monotonic() - start
        print(f"resume took {elapsed:.2f}s")
        assert elapsed < 5, elapsed
        assert status().startswith("state=running"), status()

    with subtest("automation resumes a suspended emulator"):
        tony("androidctl suspend")
        tony("androidctl shell true")
        assert status().startswith("state=running"), status()

    with subtest("quick boot stop and restore"):
        tony("androidctl stop", timeout=300)
        machine.fail(f"systemctl is-active {emulator}")
        # The emulator waits up to 20s for a graceful shutdown. The bracketed
        # pattern stops pgrep from matching the test command itself.
        machine.wait_until_fails("pgrep -f '[q]emu-system-x86_64'", timeout=120)
        assert status().startswith("state=stopped"), status()
        machine.succeed(f"test -d {state_dir}/avd/android.avd/snapshots/default_boot")
        start = time.monotonic()
        tony("androidctl start", timeout=900)
        elapsed = time.monotonic() - start
        print(f"quick boot restore took {elapsed:.0f}s")
        # A restored guest keeps its uptime from before the stop.
        assert uptime() > elapsed + 20, (uptime(), elapsed)

    with subtest("idle suspend"):
        set_idle(70)
        wait_state("suspended", timeout=120)

    with subtest("idle hibernate"):
        set_idle(130)
        machine.wait_until_fails(f"systemctl is-active {emulator}", timeout=300)
        # The emulator waits up to 20s for a graceful shutdown. The bracketed
        # pattern stops pgrep from matching the test command itself.
        machine.wait_until_fails("pgrep -f '[q]emu-system-x86_64'", timeout=120)
        machine.fail("systemctl is-active android-appliance-idle.timer")

    with subtest("browser display starts android"):
        machine.succeed("curl -sf --max-time 60 http://127.0.0.1:6090/vnc.html | grep -q noVNC")
        machine.wait_for_unit(emulator, timeout=900)
        machine.wait_for_unit("android-appliance-scrcpy.service", timeout=120)
        machine.wait_until_succeeds("DISPLAY=:57 xwininfo -root -tree | grep -q Android", timeout=120)
        machine.succeed("systemctl is-active android-appliance-display.service")
        assert tony("androidctl display").startswith("http://127.0.0.1:6090/")

    with subtest("the display does not keep android alive"):
        set_idle(130)
        machine.wait_until_fails(f"systemctl is-active {emulator}", timeout=300)
        machine.fail("systemctl is-active android-appliance-display.service")
        machine.fail("systemctl is-active android-appliance-xvnc.service")
        machine.succeed("systemctl is-active android-appliance-display.socket")

    with subtest("host reboot saves state and leaves android off"):
        tony("androidctl start", timeout=900)
        # Power-cycle instead of machine.reboot(): the driver starts QEMU with
        # -no-reboot, so a guest reboot exits QEMU and the shell cannot reconnect.
        machine.shutdown()
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.fail(f"systemctl is-active {emulator}")
        assert status().startswith("state=stopped"), status()
        tony("androidctl start", timeout=900)
        assert status().startswith("state=running boot_completed=1"), status()
  '';
}
