{ config, lib, pkgs, ... }:
let
  cfg = config.services.commutecompass;
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

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/commutecompass";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.dataDir;
      createHome = true;
    };
    users.groups.${cfg.group} = {};

    environment.etc."commutecompass/config.toml".source = cfg.configFile;
    environment.etc."commutecompass/known_venues.yaml".source = cfg.venuesFile;

    systemd.services."commutecompass-morning" = {
      description = "commutecompass morning digest";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        StateDirectory = "commutecompass";
        ExecStartPre = "${cfg.package}/bin/commutecompass --config /etc/commutecompass/config.toml init-db";
        ExecStart = "${cfg.package}/bin/commutecompass --config /etc/commutecompass/config.toml morning";
        # Hardening: restrict filesystem access; commutecompass reads /etc/commutecompass/*
        # and writes to dataDir (StateDirectory= lands under dataDir)
        NoNewPrivileges = true;
        ProtectSystem = "strict";   # ro /usr/lib, /nix, /bin, /sbin, /etc; rw /var
        ProtectHome = "read-only";   # /home/commutecompass ro; still allows home creation
        PrivateTmp = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    systemd.timers."commutecompass-morning" = {
      description = "Daily morning digest timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.morningTime;
        Persistent = true;
      };
    };

    systemd.services."commutecompass-poll" = {
      description = "commutecompass poll loop";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        StateDirectory = "commutecompass";
        ExecStartPre = "${cfg.package}/bin/commutecompass --config /etc/commutecompass/config.toml init-db";
        ExecStart = "${cfg.package}/bin/commutecompass --config /etc/commutecompass/config.toml poll";
        # Hardening: same policy as morning service
        NoNewPrivileges = true;
        ProtectSystem = "strict";   # ro /usr/lib, /nix, /bin, /sbin, /etc; rw /var
        ProtectHome = "read-only";   # /home/commutecompass ro; still allows home creation
        PrivateTmp = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    systemd.timers."commutecompass-poll" = {
      description = "Poll timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1min";
        OnUnitActiveSec = cfg.pollInterval;
      };
    };
  };
}