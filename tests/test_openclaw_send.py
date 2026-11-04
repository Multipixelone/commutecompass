"""Integration tests for the stdout-to-OpenClaw bridge."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "contrib/openclaw-send.sh"
START = "===COMMUTECOMPASS-MSG==="
END = "===COMMUTECOMPASS-END==="
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def run_bridge(tmp_path: Path, text: str, *, fail: bool = False) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    fake = tmp_path / "openclaw"
    record = tmp_path / "argv.jsonl"
    fake.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['ARGV_RECORD'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ['SEND_FAIL']))\n"
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "OPENCLAW_BIN": "openclaw", "OPENCLAW_TARGET": "test-chat",
           "ARGV_RECORD": str(record), "SEND_FAIL": str(int(fail))}
    result = subprocess.run(["bash", str(SCRIPT)], input=text, text=True, capture_output=True, env=env, check=False)
    calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return result, calls


@pytest.mark.parametrize(("bad", "warning"), [
    ("unframed private text\n", "unframed output"),
    (f"{START}\nabandoned\n", "nested START"),
    (f"{END}\n", "stray END"),
])
def test_framing_errors_warn_and_preserve_valid_messages(tmp_path: Path, bad: str, warning: str) -> None:
    result, calls = run_bridge(tmp_path, bad + f"{START}\ngood\n{END}\n")
    assert result.returncode == 1
    assert warning in result.stderr
    assert "private text" not in result.stderr
    assert [call[-1] for call in calls] == ["good"]


def test_send_failure_logs_metadata_not_message(tmp_path: Path) -> None:
    body = "Private event 🚆\nPrivate location"
    result, _ = run_bridge(tmp_path, f"{START}\n{body}\n{END}\n", fail=True)
    assert result.returncode == 1
    assert "Private" not in result.stderr
    assert f"message 1 ({len(body.encode())} bytes)" in result.stderr
