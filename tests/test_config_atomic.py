"""Config edits must replace files atomically and preserve permissions."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from commutecompass.config import delete_config_field, update_config_field


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_atomic_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete: bool, fail: bool) -> None:
    path = tmp_path / "config.toml"
    path.write_text('''calendars = []
[origin]
address = "Example venue"
lat = 0.0
lon = 0.0
[prep]
prep_minutes = 20
[scheduling]
poll_interval_seconds = 60
[paths]
venues_file = "venues.yaml"
db_path = "state.db"
oauth_token_path = "token.json"
[opencode_go]
endpoint = "https://example.invalid"
model = "example"
[mta]
subway_alerts_url = "https://example.invalid/subway"
lirr_alerts_url = "https://example.invalid/lirr"
bus_alerts_url = "https://example.invalid/bus"
''')
    path.chmod(0o640)
    before = path.read_bytes()
    inode = path.stat().st_ino

    def broken_fsync(fd: int) -> None:
        raise OSError("simulated disk failure")

    def edit() -> None:
        if delete:
            delete_config_field(path, "prep.prep_minutes")
        else:
            update_config_field(path, "prep.prep_minutes", "30")

    if fail:
        monkeypatch.setattr(os, "fsync", broken_fsync)
        with pytest.raises(OSError, match="disk failure"):
            edit()
        assert path.read_bytes() == before
    else:
        edit()
        assert path.stat().st_ino != inode
        assert path.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [path]
