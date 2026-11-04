{ inputs, config, pkgs, ... }:
let
  commutecompass = inputs.commutecompass.packages.${pkgs.system}.default;
  openclaw = inputs.openclaw.packages.${pkgs.system}.default;
in {
  imports = [ inputs.commutecompass.nixosModules.default ];

  age.secrets.commutecompass-env.file = ../secrets/commutecompass-env.age;

  services.commutecompass = {
    enable = true;
    package = commutecompass;
    configFile = ../config/commutecompass/config.toml;
    venuesFile = ../config/commutecompass/known_venues.yaml;
    environmentFile = config.age.secrets.commutecompass-env.path;
    # Include a timezone in OnCalendar overrides, independent of the host zone.
    morningTime = "06:00:00 America/New_York";
    pollInterval = "1min";

    openclaw = {
      package = openclaw;
      target = "-987654321";
    };
  };
}