{
  description = "commutecompass — NYC commute orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        commutecompass = pkgs.callPackage ./nix/package.nix { };
        pythonEnv = pkgs.python313.withPackages (
          ps: with ps; [
            pip
            pytest
            pydantic
            click
            pyyaml
            rapidfuzz
            httpx
            google-api-python-client
            google-auth-oauthlib
            google-auth-httplib2
            gtfs-realtime-bindings
            tomli
          ]
        );
      in
      {
        packages.default = commutecompass;
        packages.commutecompass = commutecompass;

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            commutecompass
            pythonEnv
            ruff
            mypy
          ];
        };
      }
    )
    // {
      nixosModules.default = import ./nix/module.nix;
      nixosModules.commutecompass = import ./nix/module.nix;

      overlays.default = final: prev: {
        commutecompass = final.callPackage ./nix/package.nix { };
      };
    };
}
