{ pkgs, nixpkgs, module }:
let
  inherit (pkgs) lib;
  app = pkgs.symlinkJoin {
    name = "commutecompass-module-test";
    paths = [
      (pkgs.writeShellScriptBin "commutecompass" ''
        if [ "''${EXPECT_ALARM_PUBLISH:-0}" = 1 ]; then
          test "$#" = 4
          test "$3" = alarm
          test "$4" = publish
        fi
        exit "''${MOCK_APP_STATUS:-0}"
      '')
      (pkgs.writeTextFile {
        name = "mock-openclaw-send";
        destination = "/share/commutecompass/openclaw-send.sh";
        executable = true;
        text = ''
          #!${pkgs.runtimeShell}
          exit "''${MOCK_SEND_STATUS:-0}"
        '';
      })
    ];
  };
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
  dataDirValid = dataDir: lib.all (item: item.assertion)
    (evaluate { services.commutecompass.dataDir = dataDir; }).assertions;
in
assert lib.all (path: !(dataDirValid path)) [
  "/var/lib/../etc" "/var/lib/." "/var/lib/.." "/var/lib//commutecompass"
  "/var/lib/commutecompass/" "/var/lib/nested/state" "/var/lib/" "/etc/commutecompass"
];
assert dataDirValid "/var/lib/commutecompass-test_1.0";
assert lib.assertMsg (lib.all (item: item.assertion) config.assertions)
  (lib.concatMapStringsSep "\n" (item: item.message)
    (builtins.filter (item: !item.assertion) config.assertions));
assert services.commutecompass-poll.serviceConfig.Type == "oneshot";
assert lib.hasSuffix "/bin/commutecompass-poll" services.commutecompass-poll.serviceConfig.ExecStart;
assert lib.hasSuffix "/bin/commutecompass-morning" services.commutecompass-morning.serviceConfig.ExecStart;
assert services.commutecompass-poll.serviceConfig.TimeoutStartSec == "2min";
assert services.commutecompass-morning.serviceConfig.TimeoutStartSec == "10min";
assert services.commutecompass-tomorrow.serviceConfig.TimeoutStartSec == "10min";
assert services.commutecompass-gtfs-refresh.serviceConfig.TimeoutStartSec == "30min";
assert services.commutecompass-alarm-publish.serviceConfig.TimeoutStartSec == "10min";
assert lib.hasSuffix "/bin/commutecompass-alarm-publish"
  services.commutecompass-alarm-publish.serviceConfig.ExecStart;
assert (evaluate { services.commutecompass.jobTimeouts.poll = "3min"; })
  .systemd.services.commutecompass-poll.serviceConfig.TimeoutStartSec == "3min";
assert services.commutecompass-alarm.serviceConfig.Type == "simple";
assert services.commutecompass-alarm.serviceConfig.Restart == "always";
assert timers.commutecompass-poll.timerConfig.OnUnitActiveSec == "1min";
assert lib.all (name: lib.hasSuffix " America/New_York" timers.${name}.timerConfig.OnCalendar)
  [ "commutecompass-morning" "commutecompass-tomorrow" "commutecompass-gtfs-refresh" "commutecompass-alarm-publish" ];
assert (evaluate { services.commutecompass.morningTime = "07:00:00 UTC"; })
  .systemd.timers.commutecompass-morning.timerConfig.OnCalendar == "07:00:00 UTC";
assert timers.commutecompass-alarm-publish.timerConfig.Persistent;
assert builtins.elem "skill-test" config.users.groups.commutecompass.members;
assert builtins.any (package: package.name == "commutecompass-skill") config.environment.systemPackages;
pkgs.runCommand "commutecompass-module-eval" {} ''
  ${services.commutecompass-poll.serviceConfig.ExecStart}
  ${services.commutecompass-morning.serviceConfig.ExecStart}
  EXPECT_ALARM_PUBLISH=1 ${services.commutecompass-alarm-publish.serviceConfig.ExecStart}
  status=0
  MOCK_APP_STATUS=17 ${services.commutecompass-poll.serviceConfig.ExecStart} || status=$?
  test "$status" = 17
  status=0
  MOCK_SEND_STATUS=23 ${services.commutecompass-poll.serviceConfig.ExecStart} || status=$?
  test "$status" = 23
  touch "$out"
''
