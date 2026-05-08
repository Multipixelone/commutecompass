{ inputs, config, pkgs, ... }:
let
  commutecompass = inputs.commutecompass.packages.${pkgs.system}.default;
in {
  imports = [ inputs.commutecompass.nixosModules.default ];

  age.secrets.commutecompass-env.file = ../secrets/commutecompass-env.age;

  services.commutecompass = {
    enable = true;
    package = commutecompass;
    configFile = ../config/commutecompass/config.toml;
    venuesFile = ../config/commutecompass/known_venues.yaml;
    environmentFile = config.age.secrets.commutecompass-env.path;
    morningTime = "0600:00";
    pollInterval = "1min";
  };
}