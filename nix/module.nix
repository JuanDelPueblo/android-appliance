{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.android-appliance;
  inherit (lib) mkOption types;

  prefix = "android-appliance";
  emulatorUnit = "${prefix}-emulator.service";

  # Fixed internal values. Callers use androidctl and never see them.
  apiLevel = "36";
  imageTag = "google_apis_playstore";
  consolePort = 5554;
  serial = "emulator-${toString consolePort}";
  adbServerPort = "5038";
  xDisplay = ":57";
  runDir = "/run/${prefix}";
  vncSocket = "${runDir}/vnc.sock";
  # The X screen has the same 9:16 shape as the device, so scrcpy fills it.
  screenWidth = 720;
  screenHeight = 1280;

  # Enabling the module accepts the Android SDK license for these packages.
  androidSdk =
    ((pkgs.androidenv.override { licenseAccepted = true; }).composeAndroidPackages {
      platformVersions = [ apiLevel ];
      includeEmulator = true;
      includeSystemImages = true;
      systemImageTypes = [ imageTag ];
      abiVersions = [ "x86_64" ];
      toolsVersion = null;
      buildToolsVersions = [ ];
      includeCmake = false;
      includeNDK = false;
    }).androidsdk;
  sdkRoot = "${androidSdk}/libexec/android-sdk";
  adb = "${sdkRoot}/platform-tools/adb";

  displayUrl = "http://127.0.0.1:${toString cfg.display.port}/vnc.html?autoconnect=true&resize=scale";

  androidctl = pkgs.callPackage ./package.nix {
    env = {
      ANDROID_APPLIANCE_STATE_DIR = cfg.stateDir;
      ANDROID_APPLIANCE_USER = cfg.user;
      ANDROID_APPLIANCE_UNIT = emulatorUnit;
      ANDROID_APPLIANCE_ADB = adb;
      ANDROID_APPLIANCE_SERIAL = serial;
      ANDROID_ADB_SERVER_PORT = adbServerPort;
      ANDROID_APPLIANCE_IDLE_SUSPEND = toString (cfg.idleSuspendMinutes * 60);
      ANDROID_APPLIANCE_IDLE_STOP = toString (cfg.idleHibernateMinutes * 60);
      ANDROID_APPLIANCE_XPRINTIDLE = lib.getExe pkgs.xprintidle;
    }
    // lib.optionalAttrs cfg.display.enable {
      ANDROID_APPLIANCE_DISPLAY_URL = displayUrl;
      ANDROID_APPLIANCE_X_DISPLAY = xDisplay;
    };
  };
  ctl = lib.getExe androidctl;

  # Environment shared by every unit that talks to the emulator or adb.
  androidEnv = {
    ANDROID_HOME = sdkRoot;
    ANDROID_SDK_ROOT = sdkRoot;
    ANDROID_AVD_HOME = "${cfg.stateDir}/avd";
    ANDROID_USER_HOME = "${cfg.stateDir}/home/.android";
    ANDROID_ADB_SERVER_PORT = adbServerPort;
    ANDROID_SERIAL = serial;
    HOME = "${cfg.stateDir}/home";
  };

  commonService = {
    User = cfg.user;
    Group = cfg.group;
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ReadWritePaths = [
      cfg.stateDir
      runDir
    ];
  };
in
{
  options.services.android-appliance = {
    enable = lib.mkEnableOption "the on-demand Android Emulator appliance";

    user = mkOption {
      type = types.str;
      default = prefix;
      description = "User that runs the emulator and may operate it with androidctl.";
    };

    group = mkOption {
      type = types.str;
      default = prefix;
      description = "Primary group of the appliance processes and state.";
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/${prefix}";
      description = ''
        Persistent appliance directory. It holds the AVD, userdata, the Quick
        Boot snapshot and the adb keys. Back it up to keep Android app state.
      '';
    };

    avdName = mkOption {
      type = types.str;
      default = "android";
      description = "Name of the AVD in the state directory.";
    };

    idleSuspendMinutes = mkOption {
      type = types.ints.positive;
      default = 10;
      description = "Pause the emulator after this many minutes without use.";
    };

    idleHibernateMinutes = mkOption {
      type = types.ints.positive;
      default = 60;
      description = ''
        Stop the emulator (saving Quick Boot state) after this many minutes
        without use. This counts from the last use, not from the suspend.
      '';
    };

    memoryMiB = mkOption {
      type = types.ints.positive;
      default = 4096;
      description = "Guest RAM. A change makes the next start a cold boot.";
    };

    cores = mkOption {
      type = types.ints.positive;
      default = 4;
      description = "Guest CPU cores. A change makes the next start a cold boot.";
    };

    diskSize = mkOption {
      type = types.str;
      default = "32G";
      description = "Userdata partition size. It applies only when the AVD is created.";
    };

    gpu = mkOption {
      type = types.str;
      default = "swiftshader_indirect";
      example = "host";
      description = ''
        Emulator -gpu mode. The software renderer works on every headless
        host. "host" needs a usable GPU and EGL on the host.
      '';
    };

    display = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Serve the device screen to a browser through scrcpy and noVNC.";
      };

      port = mkOption {
        type = types.port;
        default = 6090;
        description = "Loopback port of the noVNC web page.";
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = [
          {
            assertion = cfg.idleSuspendMinutes < cfg.idleHibernateMinutes;
            message = "services.android-appliance.idleSuspendMinutes must be less than idleHibernateMinutes.";
          }
        ];

        users.users = lib.mkIf (cfg.user == prefix) {
          ${prefix} = {
            isSystemUser = true;
            group = cfg.group;
            home = cfg.stateDir;
          };
        };
        users.groups = lib.mkIf (cfg.group == prefix) { ${prefix} = { }; };

        environment.systemPackages = [ androidctl ];

        systemd.tmpfiles.settings.${prefix} =
          lib.genAttrs
            [
              cfg.stateDir
              "${cfg.stateDir}/home"
              "${cfg.stateDir}/screenshots"
              runDir
            ]
            (_: {
              d = {
                inherit (cfg) user group;
                mode = "0750";
              };
            });

        # The emulator is never part of a boot target. androidctl, the display
        # socket or an operator starts it on demand.
        systemd.services."${prefix}-emulator" = {
          description = "Android Emulator appliance";
          after = [ "network.target" ];
          wants = [ "${prefix}-idle.timer" ];
          unitConfig.RequiresMountsFor = [ cfg.stateDir ];
          path = [ pkgs.coreutils ];
          environment = androidEnv // {
            AVD_NAME = cfg.avdName;
            AVD_API = apiLevel;
            AVD_TAG = imageTag;
            AVD_SYSTEM_IMAGE = "system-images/android-${apiLevel}/${imageTag}/x86_64";
            AVD_RAM_MIB = toString cfg.memoryMiB;
            AVD_CORES = toString cfg.cores;
            AVD_DISK_SIZE = cfg.diskSize;
          };
          serviceConfig = commonService // {
            Type = "simple";
            SupplementaryGroups = [
              "kvm"
            ]
            ++ lib.optionals (cfg.gpu == "host") [
              "render"
              "video"
            ];
            PrivateTmp = true;
            WorkingDirectory = cfg.stateDir;
            ExecStartPre = "${androidctl}/bin/android-avd-init";
            ExecStart = lib.escapeShellArgs [
              "${sdkRoot}/emulator/emulator"
              "-avd"
              cfg.avdName
              "-port"
              (toString consolePort)
              "-no-window"
              "-no-audio"
              "-no-boot-anim"
              "-no-metrics"
              "-gpu"
              cfg.gpu
            ];
            # Wait for Android boot completion, so "activating" means "starting".
            ExecStartPost = "${ctl} boot-hook";
            # `adb emu kill` saves the Quick Boot snapshot. systemd sends
            # SIGTERM and then SIGKILL only if that does not finish in time.
            ExecStop = "${ctl} stop-hook";
            TimeoutStartSec = "20min";
            TimeoutStopSec = "3min";
            Restart = "no";
          };
        };

        # The timer runs only while the emulator unit is active.
        systemd.timers."${prefix}-idle" = {
          description = "Idle policy for the Android appliance";
          bindsTo = [ emulatorUnit ];
          after = [ emulatorUnit ];
          timerConfig = {
            OnActiveSec = "30s";
            OnUnitActiveSec = "30s";
            AccuracySec = "5s";
          };
        };

        systemd.services."${prefix}-idle" = {
          description = "Suspend or stop the idle Android appliance";
          serviceConfig = commonService // {
            Type = "oneshot";
            ExecStart = "${ctl} idle-check";
          };
        };

        # Only the appliance user may start, stop or restart the emulator unit.
        # The display units start through socket activation and dependencies,
        # which need no permission.
        security.polkit.enable = true;
        security.polkit.extraConfig = ''
          polkit.addRule(function (action, subject) {
            if (action.id == "org.freedesktop.systemd1.manage-units" &&
                subject.user == ${builtins.toJSON cfg.user} &&
                action.lookup("unit") == ${builtins.toJSON emulatorUnit} &&
                ["start", "stop", "restart"].indexOf(action.lookup("verb")) >= 0) {
              return polkit.Result.YES;
            }
          });
        '';
      }

      # Browser display: Xvnc (X server and VNC in one process) shows scrcpy,
      # and noVNC serves it on loopback. The first connection to the socket
      # starts the chain and Android. It all stops when the emulator stops.
      (lib.mkIf cfg.display.enable {
        systemd.services."${prefix}-xvnc" = {
          description = "Virtual display for the Android appliance";
          partOf = [ emulatorUnit ];
          serviceConfig = commonService // {
            ExecStart = lib.escapeShellArgs [
              "${pkgs.tigervnc}/bin/Xvnc"
              xDisplay
              "-geometry"
              "${toString screenWidth}x${toString screenHeight}"
              "-depth"
              "24"
              "-desktop"
              "Android"
              "-SecurityTypes"
              "None"
              "-AlwaysShared"
              "-rfbport"
              "-1"
              "-rfbunixpath"
              vncSocket
              "-rfbunixmode"
              "0600"
              "-nolisten"
              "tcp"
            ];
            ExecStartPost = "${pkgs.bash}/bin/bash -c 'until [ -S ${vncSocket} ]; do ${pkgs.coreutils}/bin/sleep 0.2; done'";
          };
        };

        systemd.services."${prefix}-scrcpy" = {
          description = "Android screen mirror for the appliance display";
          bindsTo = [
            emulatorUnit
            "${prefix}-xvnc.service"
          ];
          after = [
            emulatorUnit
            "${prefix}-xvnc.service"
          ];
          startLimitIntervalSec = 0;
          environment = androidEnv // {
            ADB = adb;
            DISPLAY = xDisplay;
          };
          serviceConfig = commonService // {
            ExecStart = lib.escapeShellArgs [
              (lib.getExe pkgs.scrcpy)
              "--serial=${serial}"
              "--no-audio"
              "--window-title=Android"
              "--window-borderless"
              "--window-x=0"
              "--window-y=0"
              "--window-width=${toString screenWidth}"
              "--window-height=${toString screenHeight}"
              "--max-size=${toString screenHeight}"
              "--max-fps=30"
              "--video-bit-rate=4M"
              "--render-driver=software"
            ];
            Restart = "always";
            RestartSec = 2;
          };
        };

        systemd.sockets."${prefix}-display" = {
          description = "Browser display for the Android appliance";
          wantedBy = [ "sockets.target" ];
          listenStreams = [ "127.0.0.1:${toString cfg.display.port}" ];
        };

        systemd.services."${prefix}-display" = {
          description = "noVNC for the Android appliance";
          requires = [ "${prefix}-xvnc.service" ];
          after = [ "${prefix}-xvnc.service" ];
          wants = [ "${prefix}-scrcpy.service" ];
          partOf = [ emulatorUnit ];
          serviceConfig = commonService // {
            # Resume a suspended emulator or queue the start of a stopped one.
            ExecStartPre = "${ctl} start --no-wait";
            ExecStart = lib.escapeShellArgs [
              (lib.getExe' pkgs.python3Packages.websockify "websockify")
              "--inetd"
              "--web=${pkgs.novnc}/share/webapps/novnc"
              "--unix-target=${vncSocket}"
            ];
            StandardInput = "socket";
            StandardOutput = "journal";
          };
        };
      })
    ]
  );
}
