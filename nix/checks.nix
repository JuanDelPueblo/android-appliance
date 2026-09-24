{
  pkgs,
  self,
  nixpkgs,
}:
let
  inherit (pkgs) lib;
  src = lib.cleanSource ../.;

  # A minimal system that enables the module, to check that it evaluates.
  apiLevel = 36;
  system = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      self.nixosModules.default
      {
        nixpkgs.config.allowUnfree = true;
        boot.loader.grub.enable = false;
        fileSystems."/" = {
          device = "/dev/null";
          fsType = "ext4";
        };
        system.stateVersion = "25.11";
        services.android-appliance = {
          enable = true;
          user = "tony";
          group = "users";
          inherit apiLevel;
          stateDir = "/var/lib/juno/android";
        };
        users.users.tony = {
          isNormalUser = true;
          group = "users";
        };
      }
    ];
  };
  units = system.config.systemd.services;
  bootUnits = system.config.systemd.targets.multi-user.wants or [ ];
  pythonEnv = pkgs.python3.withPackages (ps: [
    ps.fastapi
    ps.httpx
    ps.pyyaml
  ]);
in
{
  androidctl = pkgs.callPackage ./package.nix { };

  shellcheck = pkgs.runCommand "shellcheck" { nativeBuildInputs = [ pkgs.shellcheck ]; } ''
    cd ${src}
    shellcheck src/androidctl src/avd-init tests/*.sh tests/fakes/*
    touch $out
  '';

  lifecycle =
    pkgs.runCommand "androidctl-lifecycle-tests"
      {
        nativeBuildInputs = [
          pkgs.bash
          pkgs.coreutils
          pkgs.gnugrep
          pkgs.gnused
          pkgs.gawk
        ];
      }
      ''
        cp -r ${src}/src ${src}/tests .
        chmod -R u+w .
        patchShebangs src tests
        bash tests/androidctl-test.sh
        bash tests/avd-init-test.sh
        touch $out
      '';

  plugin =
    pkgs.runCommand "hermes-plugin-tests"
      {
        nativeBuildInputs = [
          pythonEnv
          pkgs.nodejs
        ];
      }
      ''
        cp -r ${src} source
        chmod -R u+w source
        cd source
        patchShebangs tests
        python -m unittest discover -s tests -v
        node --check dashboard/dist/index.js
        python -c 'import json; json.load(open("dashboard/manifest.json"))'
        touch $out
      '';

  # Evaluate a full system with the module. The string context is removed,
  # because a drvPath with context makes Nix realise the whole build closure
  # of the system.
  module =
    assert
      !(units.android-appliance-emulator ? wantedBy) || units.android-appliance-emulator.wantedBy == [ ];
    assert !(builtins.elem "android-appliance-emulator.service" bootUnits);
    assert units.android-appliance-emulator.serviceConfig.User == "tony";
    # apiLevel must reach both the system image path and avd-init.
    assert
      units.android-appliance-emulator.environment.AVD_API == toString apiLevel
      &&
        units.android-appliance-emulator.environment.AVD_SYSTEM_IMAGE
        == "system-images/android-${toString apiLevel}/google_apis_playstore/x86_64";
    pkgs.writeText "module-eval" (
      builtins.unsafeDiscardStringContext system.config.system.build.toplevel.drvPath
    );

  formatting = pkgs.runCommand "nixfmt" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
    cd ${src}
    nixfmt --check flake.nix nix/*.nix nix/tests/*.nix
    touch $out
  '';
}
