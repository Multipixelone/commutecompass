{ lib, python3Packages, bash, fetchFromGitHub, ... }:

python3Packages.buildPythonApplication rec {
  pname = "commutecompass";
  # Single source of truth: read the version from pyproject.toml so it never
  # drifts from the Python package metadata.
  version = (lib.importTOML ../pyproject.toml).project.version;
  format = "pyproject";

  src = ./..;

  nativeBuildInputs = with python3Packages; [ hatchling ];

  propagatedBuildInputs = with python3Packages; [
    pydantic
    click
    google-api-python-client
    google-auth-oauthlib
    google-auth-httplib2
    httpx
    gtfs-realtime-bindings
    pyyaml
    rapidfuzz
    tomli
    tomlkit
  ];

  # patchShebangs silently no-ops here: strictDeps=1 puts it in --host mode,
  # which only searches HOST_PATH (buildInputs + propagatedBuildInputs) — bash
  # from stdenv isn't in there. Rewrite the shebang directly instead.
  postInstall = ''
    install -Dm755 contrib/openclaw-send.sh \
      $out/share/commutecompass/openclaw-send.sh
    substituteInPlace $out/share/commutecompass/openclaw-send.sh \
      --replace-fail '#!/usr/bin/env bash' '#!${bash}/bin/bash'
  '';

  pythonImportsCheck = [ "commutecompass" ];
}