"""Keep skill dispatch documentation and shell wrappers aligned with Click."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import click

from commutecompass.cli import cli
from commutecompass.config import CONFIG_SET_ALLOWLIST

SKILL = Path(__file__).resolve().parents[1] / "skills/commutecompass"


def test_documented_scripts_exist_and_are_executable() -> None:
    references = set(re.findall(r"scripts/([a-z-]+\.sh)", (SKILL / "SKILL.md").read_text()))
    assert references
    for name in references:
        script = SKILL / "scripts" / name
        assert script.is_file(), name
        assert os.access(script, os.X_OK), name


def test_config_allowlist_documentation_matches_code() -> None:
    document = (SKILL / "references/config-allowlist.md").read_text()
    documented = set(re.findall(r"^\|\s*`([a-z_]+(?:\.[a-z_]+)+)`\s*\|", document, re.MULTILINE))
    assert documented == set(CONFIG_SET_ALLOWLIST)


def test_wrapper_subcommands_exist() -> None:
    for script in sorted((SKILL / "scripts").glob("*.sh")):
        invocation = re.search(r"^exec commutecompass-skill (.+)$", script.read_text(), re.MULTILINE)
        assert invocation, script.name
        words = shlex.split(invocation.group(1))
        command: click.Command = cli
        consumed = 0
        while isinstance(command, click.Group):
            assert consumed < len(words), script.name
            subcommand = command.commands.get(words[consumed])
            assert subcommand is not None, f"{script.name}: unknown command {words[consumed]}"
            command = subcommand
            consumed += 1
        assert consumed > 0, script.name
