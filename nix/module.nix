{ config, lib, pkgs, ... }:
let
  cfg = config.services.commutecop;
in {
  options.services.commutecop = {
    enable = lib.mkEnableOption "commutecop NYC commute orchestrator";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The commutecop package.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "commutecop";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "commutecop";
    };

    configFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to commutecop config.toml.";
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
      default = "/var/lib/commutecop";
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

    environment.etc."commutecop/config.toml".source = cfg.configFile;
    environment.etc."commutecop/known_venues.yaml".source = cfg.venuesFile;

    systemd.services."commutecop-morning" = {
      description = "commutecop morning digest";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        StateDirectory = "commutecop";
        # Hardening: restrict filesystem access; commutecop reads /etc/commutecop/*
        # and writes to dataDir (StateDirectory= lands under dataDir)
        NoNewPrivileges = true;
        ProtectSystem = "strict";   # ro /usr/lib, /nix, /bin, /sbin, /etc; rw /var
        ProtectHome = "read-only";   # /home/commutecop ro; still allows home creation
        PrivateTmp = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    systemd.timers."commutecop-morning" = {
      description = "Daily morning digest timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.morningTime;
        Persistent = true;
      };
    };

    systemd.services."commutecop-poll" = {
      description = "commutecop poll loop";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        StateDirectory = "commutecop";
        ExecStart = "${cfg.package}/bin/commutecop --config /etc/commutecop/config.toml poll";
        # Hardening: same policy as morning service
        NoNewPrivileges = true;
        ProtectSystem = "strict";   # ro /usr/lib, /nix, /bin, /sbin, /etc; rw /var
        ProtectHome = "read-only";   # /home/commutecop ro; still allows home creation
        PrivateTmp = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    systemd.timers."commutecop-poll" = {
      description = "Poll timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1min";
        OnUnitActiveSec = cfg.pollInterval;
      };
    };
  };
}