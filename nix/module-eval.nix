{ pkgs, nixpkgs, module }:
let
  inherit (pkgs) lib;
  app = pkgs.writeShellScriptBin "commutecompass" ''exit 0'';
  openclaw = pkgs.writeShellScriptBin "openclaw" ''exit 0'';
  evaluate = extra: (nixpkgs.lib.nixosSystem {
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      module
      {
        system.stateVersion = "26.05";
        boot.loader.grub.enable = false;
        fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
        services.commutecompass = {
          enable = true;
          package = app;
          configFile = ../examples/config.toml;
          venuesFile = ../data/known_venues.yaml;
          environmentFile = "/run/secrets/commutecompass-env";
          openclaw = { package = openclaw; target = "synthetic-test-target"; };
          skill.users = [ "skill-test" ];
          alarm.enable = true;
        };
        users.users.skill-test.isNormalUser = true;
      }
      extra
    ];
  }).config;
  config = evaluate {};
  services = config.systemd.services;
  timers = config.systemd.timers;
in
assert lib.assertMsg (lib.all (item: item.assertion) config.assertions)
  (lib.concatMapStringsSep "\n" (item: item.message)
    (builtins.filter (item: !item.assertion) config.assertions));
assert services.commutecompass-poll.serviceConfig.Type == "oneshot";
assert services.commutecompass-alarm.serviceConfig.Type == "simple";
assert services.commutecompass-alarm.serviceConfig.Restart == "always";
assert timers.commutecompass-poll.timerConfig.OnUnitActiveSec == "1min";
assert timers.commutecompass-alarm-publish.timerConfig.Persistent;
assert builtins.elem "skill-test" config.users.groups.commutecompass.members;
assert builtins.any (package: package.name == "commutecompass-skill") config.environment.systemPackages;
pkgs.runCommand "commutecompass-module-eval" {} ''
  touch "$out"
''
