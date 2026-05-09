{
  description = "commutecompass — NYC commute orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    git-hooks.url = "github:cachix/git-hooks.nix";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      git-hooks,
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
            mypy
          ]
        );
        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            ruff.enable = true;
            mypy = {
              enable = true;
              settings.binPath = "${pythonEnv}/bin/mypy";
            };
            pytest = {
              enable = true;
              name = "pytest";
              entry = "${pythonEnv}/bin/pytest -q";
              language = "system";
              pass_filenames = false;
              types = [ "python" ];
            };
          };
        };
      in
      {
        packages.default = commutecompass;
        packages.commutecompass = commutecompass;

        checks = {
          inherit pre-commit-check;
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            commutecompass
            pythonEnv
            ruff
            mypy
            pre-commit
          ] ++ pre-commit-check.enabledPackages;
          shellHook = pre-commit-check.shellHook;
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
