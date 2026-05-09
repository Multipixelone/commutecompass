{ config, lib, pkgs, ... }:
let
  cfg = config.services.commutecompass;

  configPath = "/etc/commutecompass/config.toml";
  exe = "${cfg.package}/bin/commutecompass --config ${configPath}";

  stateDirName = lib.removePrefix "/var/lib/" cfg.dataDir;

  serviceDefaults = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      User = cfg.user;
      Group = cfg.group;
      EnvironmentFile = cfg.environmentFile;
      StateDirectory = stateDirName;
      StateDirectoryMode = "0750";
      # init-db is idempotent (CREATE TABLE IF NOT EXISTS); cheap to run every tick
      ExecStartPre = "${exe} init-db";

      # Hardening — network-only Python app, no caps, no devices
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectKernelLogs = true;
      ProtectControlGroups = true;
      ProtectClock = true;
      ProtectHostname = true;
      ProtectProc = "invisible";
      ProcSubset = "pid";
      RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
      RestrictNamespaces = true;
      RestrictRealtime = true;
      RestrictSUIDSGID = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      SystemCallArchitectures = "native";
      SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
      CapabilityBoundingSet = [ "" ];
      AmbientCapabilities = [ "" ];
      UMask = "0077";
      ReadWritePaths = [ cfg.dataDir ];
    };
  };

  mkService = subcommand: description:
    lib.recursiveUpdate serviceDefaults {
      inherit description;
      serviceConfig.ExecStart = "${exe} ${subcommand}";
    };
in {
  options.services.commutecompass = {
    enable = lib.mkEnableOption "commutecompass NYC commute orchestrator";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The commutecompass package.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "commutecompass";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "commutecompass";
    };

    configFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to commutecompass config.toml.";
    };

    venuesFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to known_venues.yaml.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to env file (e.g. agenix-decrypted secrets).";
    };

    morningTime = lib.mkOption {
      type = lib.types.str;
      default = "06:00:00";
      description = "OnCalendar spec for morning digest.";
    };

    pollInterval = lib.mkOption {
      type = lib.types.str;
      default = "1min";
      description = "OnUnitActiveSec for polling.";
    };

    pollOnBootSec = lib.mkOption {
      type = lib.types.str;
      default = "1min";
      description = "OnBootSec delay before the first poll fires after boot.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/commutecompass";
      description = ''
        State directory. Must live under /var/lib/ so systemd's
        StateDirectory= can manage ownership and permissions.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [{
      assertion = lib.hasPrefix "/var/lib/" cfg.dataDir;
      message = "services.commutecompass.dataDir must be under /var/lib/ (got: ${cfg.dataDir})";
    }];

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
    };
    users.groups.${cfg.group} = {};

    environment.etc."commutecompass/config.toml".source = cfg.configFile;
    environment.etc."commutecompass/known_venues.yaml".source = cfg.venuesFile;

    systemd.services."commutecompass-morning" = mkService "morning" "commutecompass morning digest";
    systemd.services."commutecompass-poll"    = mkService "poll"    "commutecompass poll tick";

    systemd.timers."commutecompass-morning" = {
      description = "Daily morning digest timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.morningTime;
        Persistent = true;
      };
    };

    systemd.timers."commutecompass-poll" = {
      description = "Poll timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = cfg.pollOnBootSec;
        OnUnitActiveSec = cfg.pollInterval;
      };
    };
  };
}
