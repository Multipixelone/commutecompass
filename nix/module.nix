{ config, lib, pkgs, ... }:
let
  cfg = config.services.commutecompass;

  configPath = "/etc/commutecompass/config.toml";
  exe = "${cfg.package}/bin/commutecompass --config ${configPath}";
  sendWrapper = "${cfg.package}/share/commutecompass/openclaw-send.sh";

  stateDirName = lib.removePrefix "/var/lib/" cfg.dataDir;

  serviceDefaults = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      User = cfg.user;
      Group = cfg.group;
      EnvironmentFile = cfg.environmentFile;
      Environment = [
        "OPENCLAW_TARGET=${cfg.openclaw.target}"
        "OPENCLAW_BIN=${cfg.openclaw.package}/bin/openclaw"
        "OPENCLAW_CHANNEL=${cfg.openclaw.channel}"
      ];
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

  # pipefail propagates wrapper failures (openclaw down, target rejected) up
  # to systemd so the unit shows as failed instead of silently dropping the
  # message.
  mkService = subcommand: description:
    lib.recursiveUpdate serviceDefaults {
      inherit description;
      serviceConfig.ExecStart =
        "${pkgs.bash}/bin/bash -o pipefail -c '${exe} ${subcommand} | ${sendWrapper}'";
    };
in {
  options.services.commutecompass = {
    enable = lib.mkEnableOption "commutecompass NYC commute orchestrator";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
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

    createUser = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether this module should declare `users.users.${"\${cfg.user}"}`.
        Set false when pointing `user` at a user that already exists (e.g. a
        login user that owns the openclaw state in its home directory).
      '';
    };

    createGroup = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether this module should declare `users.groups.${"\${cfg.group}"}`.
        Set false when reusing an existing group such as `users`.
      '';
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
      description = ''
        Path to env file (e.g. agenix-decrypted secrets).

        Always required:
          GOOGLE_MAPS_API_KEY
          GOOGLE_OAUTH_CLIENT_SECRET
          OPENCODE_GO_TOKEN

        Required only when notify.mode = "telegram" in config.toml (i.e. the
        Python notifier sends to Telegram directly instead of emitting
        delimited stdout for the openclaw wrapper):
          TELEGRAM_BOT_TOKEN
          TELEGRAM_CHAT_ID

        Required only when [home_assistant].enabled = true:
          HOME_ASSISTANT_TOKEN

        May also set any env vars openclaw itself needs (e.g.
        OPENCLAW_CONFIG) — the service sandbox blocks $HOME, so openclaw
        cannot pick up ~/.config/openclaw/ on its own.
      '';
    };

    openclaw = {
      package = lib.mkOption {
        type = lib.types.package;
        description = ''
          Package providing `bin/openclaw`. The morning/poll services pipe
          their delimited stdout through this binary, which delivers each
          message to the configured channel.
        '';
      };

      target = lib.mkOption {
        type = lib.types.str;
        example = "-987654321";
        description = ''
          Delivery target passed as `openclaw message send --target`. For
          Telegram, a numeric chat id (negative for groups/supergroups).

          Rendered into the unit's Environment= and therefore world-readable
          via /nix/store. If the target itself must stay secret, drop this
          option and instead set OPENCLAW_TARGET in environmentFile.
        '';
      };

      channel = lib.mkOption {
        type = lib.types.str;
        default = "telegram";
        description = ''
          Channel name passed as `openclaw message send --channel`. Must
          match a channel registered in your openclaw config.
        '';
      };
    };

    morningTime = lib.mkOption {
      type = lib.types.str;
      default = "06:00:00";
      description = ''
        OnCalendar spec for the morning digest timer.

        The TOML key scheduling.morning_run_time is ignored when running under
        systemd; this option is the source of truth.
      '';
    };

    pollInterval = lib.mkOption {
      type = lib.types.str;
      default = "1min";
      description = ''
        OnUnitActiveSec for the poll timer.

        The TOML key scheduling.poll_interval_seconds is ignored when running
        under systemd; this option is the source of truth.
      '';
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

    # Expose the CLI system-wide so admins can run `commutecompass morning`,
    # `commutecompass send-test`, etc. from any shell. The CLI's --config
    # default is /etc/commutecompass/config.toml — same path environment.etc
    # below writes to — so no extra wrapping is needed.
    environment.systemPackages = [ cfg.package ];

    users.users = lib.mkIf cfg.createUser {
      ${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
      };
    };
    users.groups = lib.mkIf cfg.createGroup {
      ${cfg.group} = {};
    };

    environment.etc."commutecompass/config.toml" = {
      source = cfg.configFile;
      user = cfg.user;
      group = cfg.group;
      mode = "0640";
    };
    environment.etc."commutecompass/known_venues.yaml" = {
      source = cfg.venuesFile;
      user = cfg.user;
      group = cfg.group;
      mode = "0640";
    };

    # /etc/commutecompass/ itself; environment.etc only manages the files inside.
    # The z lines also chown pre-existing dirs from earlier (mis-)deploys.
    systemd.tmpfiles.rules = [
      "d /etc/commutecompass 0750 ${cfg.user} ${cfg.group} -"
      "z /etc/commutecompass 0750 ${cfg.user} ${cfg.group} -"
      "z ${cfg.dataDir}      0750 ${cfg.user} ${cfg.group} -"
    ];

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
