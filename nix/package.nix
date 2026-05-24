{ lib, python3Packages, fetchFromGitHub, ... }:

python3Packages.buildPythonApplication rec {
  pname = "commutecompass";
  version = "0.1.0";
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

  pythonImportsCheck = [ "commutecompass" ];
}