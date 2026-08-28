"""Spoločné pytest nastavenie pre testy MCP research servera."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Nasmeruje SQLite databázu prieskumov do dočasného priečinka testu."""
    db_path = tmp_path / "research_sessions.sqlite3"
    monkeypatch.setenv("RESEARCH_SESSION_DB", str(db_path))
    return db_path
