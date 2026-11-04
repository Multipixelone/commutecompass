{ pkgs }:
let
  policy = builtins.fromJSON (builtins.readFile ../renovate.json);
  rules = policy.packageRules;
  enabled = builtins.filter (rule: rule.automerge or false) rules;
  hasDisabled = key: value: builtins.any (rule:
    !(rule.automerge or false) && builtins.elem value (rule.${key} or [])
  ) rules;
in
assert !policy.automerge;
assert builtins.length enabled == 1;
assert (builtins.head enabled).matchUpdateTypes == [ "patch" "minor" ];
assert (builtins.head enabled).matchDepNames == [ "pytest" "pytest-cov" "ruff" "mypy" "hatchling" "pip" ];
assert hasDisabled "matchManagers" "github-actions";
assert hasDisabled "matchManagers" "nix";
assert hasDisabled "matchDepNames" "httpx";
assert hasDisabled "matchDepNames" "/^google-auth/";
assert hasDisabled "matchUpdateTypes" "major";
pkgs.runCommand "commutecompass-renovate-policy" {} ''touch "$out"''
