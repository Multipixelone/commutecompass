{ inputs, config, pkgs, ... }:
let
  commutecop = inputs.commutecop.packages.${pkgs.system}.default;
in {
  imports = [ inputs.commutecop.nixosModules.default ];

  age.secrets.commutecop-env.file = ../secrets/commutecop-env.age;

  services.commutecop = {
    enable = true;
    package = commutecop;
    configFile = ../config/commutecop/config.toml;
    venuesFile = ../config/commutecop/known_venues.yaml;
    environmentFile = config.age.secrets.commutecop-env.path;
    morningTime = "06:00:00";
    pollInterval = "1min";
  };
}