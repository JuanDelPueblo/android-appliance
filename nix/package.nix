{
  lib,
  stdenvNoCC,
  makeWrapper,
  coreutils,
  gnugrep,
  gnused,
  systemd,
  util-linux,
  # ANDROID_APPLIANCE_* settings baked into the androidctl wrapper.
  env ? { },
}:
let
  runtimePath = lib.makeBinPath [
    coreutils
    gnugrep
    gnused
    systemd
    util-linux
  ];
  setEnv = lib.concatStringsSep " " (
    lib.mapAttrsToList (name: value: "--set-default ${name} ${lib.escapeShellArg value}") env
  );
in
stdenvNoCC.mkDerivation {
  pname = "androidctl";
  version = "0.1.0";
  src = ../src;

  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 androidctl $out/libexec/androidctl
    install -Dm755 avd-init $out/libexec/avd-init
    makeWrapper $out/libexec/androidctl $out/bin/androidctl \
      --prefix PATH : ${runtimePath} ${setEnv}
    makeWrapper $out/libexec/avd-init $out/bin/android-avd-init \
      --prefix PATH : ${runtimePath}
    runHook postInstall
  '';

  meta = {
    description = "Control and automate the Android Emulator appliance";
    mainProgram = "androidctl";
    platforms = lib.platforms.linux;
  };
}
