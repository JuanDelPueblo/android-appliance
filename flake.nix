{
  description = "On-demand Android Emulator appliance for NixOS, with a Hermes plugin";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      nixosModules.default = ./nix/module.nix;

      packages.${system} = {
        androidctl = pkgs.callPackage ./nix/package.nix { };
        default = self.packages.${system}.androidctl;
        # Boots the real emulator in a NixOS VM. It needs KVM with nested
        # virtualization and downloads the system image, so it is not a check.
        integration-test = import ./nix/tests/integration.nix { inherit pkgs self; };
      };

      checks.${system} = import ./nix/checks.nix { inherit pkgs self nixpkgs; };

      formatter.${system} = pkgs.nixfmt-tree;
    };
}
