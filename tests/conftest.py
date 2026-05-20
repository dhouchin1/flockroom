"""Shared fixtures — redirect DB to a temp file so tests don't touch ~/.config."""

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test_hive.db"
    monkeypatch.setenv("FLOCKROOM_DB", str(db))
    monkeypatch.setenv("FLOCKROOM_VAULT_DIR", str(tmp_path / "transcripts"))
    yield db
