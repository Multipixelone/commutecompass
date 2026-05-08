{
  description = "commutecop — NYC commute orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        commutecop = pkgs.callPackage ./nix/package.nix {};
        pythonCmd = pkgs.writeShellScriptBin "python" ''
          exec ${pkgs.python312}/bin/python3 "$@"
        '';
      in {
        packages.default = commutecop;
        packages.commutecop = commutecop;

        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            pythonCmd
            python312
            python312Packages.pip
            python312Packages.pydantic
            python312Packages.click
            python312Packages.pyyaml
            python312Packages.ruff
            python312Packages.mypy
            python312Packages.pytest
          ];
        };
      })
    // {
      nixosModules.default = import ./nix/module.nix;
      nixosModules.commutecop = import ./nix/module.nix;

      overlays.default = final: prev: {
        commutecop = final.callPackage ./nix/package.nix {};
      };
    };
}
