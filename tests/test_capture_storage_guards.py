"""Capture storage and context failures must never enter production decisions."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextvars import ContextVar

import pytest

import tools.decision_capture as dc
import tools.research_session as rs


def _collector() -> dc.DecisionCollector:
    collector = dc.DecisionCollector()
    collector.record(decision_stage="provider_filter", decision_reason="accepted", title="first")
    return collector


def _assert_session_operates() -> str:
    started = json.loads(rs.research_session_start("smart door lock", session_id="storage-test"))
    session_id = started["session_id"]
    with rs._connect() as conn:
        assert rs._session_row(conn, session_id)["original_query"] == "smart door lock"
    return session_id


def _create_incomplete_evaluation_table(db_path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE evaluation_candidate_decisions (id INTEGER PRIMARY KEY)")


def test_production_schema_does_not_create_evaluation_storage(temp_db):
    _assert_session_operates()
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='evaluation_candidate_decisions'"
        ).fetchone() is None


def test_incomplete_evaluation_schema_does_not_break_production(temp_db, caplog):
    _create_incomplete_evaluation_table(temp_db)
    _assert_session_operates()
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        rs._persist_decision_events(_collector())
    assert "decision persistence failed" in caplog.text
    _assert_session_operates()
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_candidate_decisions").fetchone()[0] == 0


def test_negative_control_mandatory_evaluation_schema_breaks_production(temp_db, monkeypatch):
    """Reintroducing v5's coupling makes the same production operation fail."""
    _create_incomplete_evaluation_table(temp_db)
    production_schema = rs._ensure_schema

    def coupled_schema(conn):
        production_schema(conn)
        rs._ensure_decision_schema(conn)

    monkeypatch.setattr(rs, "_ensure_schema", coupled_schema)
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        _assert_session_operates()


def test_evaluation_ddl_failure_rolls_back_new_table(temp_db, monkeypatch, caplog):
    _assert_session_operates()
    ensure_decision_schema = rs._ensure_decision_schema

    def fail_after_ddl(conn):
        ensure_decision_schema(conn)
        raise sqlite3.OperationalError("injected schema failure")

    monkeypatch.setattr(rs, "_ensure_decision_schema", fail_after_ddl)
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        rs._persist_decision_events(_collector())
    assert "decision persistence failed" in caplog.text
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='evaluation_candidate_decisions'"
        ).fetchone() is None
    _assert_session_operates()


def test_evaluation_insert_failure_rolls_back_entire_batch(temp_db, caplog):
    _assert_session_operates()
    with rs._connect() as conn:
        rs._ensure_decision_schema(conn)
        conn.execute(
            "CREATE TRIGGER reject_second BEFORE INSERT ON evaluation_candidate_decisions "
            "WHEN NEW.title='reject' BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
        )
    collector = _collector()
    collector.record(decision_stage="provider_filter", decision_reason="accepted", title="reject")
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        rs._persist_decision_events(collector)
    assert "decision persistence failed" in caplog.text
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_candidate_decisions").fetchone()[0] == 0
    _assert_session_operates()


def test_collector_uses_the_canonical_stored_session_id(temp_db):
    supplied_id = "  session / with spaces  "
    started = json.loads(rs.research_session_start("smart door lock", session_id=supplied_id))
    collector = rs._new_decision_collector(supplied_id, "run", "web", 2, "retry query")
    assert collector is not None
    assert collector.context.session_id == started["session_id"] == "session_with_spaces"
    collector.record(decision_stage="provider_filter", decision_reason="accepted")
    rs._persist_decision_events(collector)
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT d.session_id, d.attempt FROM evaluation_candidate_decisions d "
            "JOIN research_sessions s ON d.session_id=s.session_id"
        ).fetchall() == [(started["session_id"], 2)]


def test_nested_payload_is_a_detached_primitive_snapshot(temp_db):
    payload = {"origin": {"queries": ["original"], "score": 1.5}, "optional": None}
    collector = _collector()
    collector.record(decision_stage="dedupe", decision_reason="duplicate_of", payload=payload)
    payload["origin"]["queries"].append("later mutation")
    payload["origin"]["score"] = 99
    expected = {"origin": {"queries": ["original"], "score": 1.5}, "optional": None}
    assert collector.events[-1]["payload"] == expected
    rs._persist_decision_events(collector)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM evaluation_candidate_decisions WHERE decision_stage='dedupe'"
        ).fetchone()
    assert json.loads(row[0]) == expected


def test_nonprimitive_payload_fails_open_with_warning(caplog):
    collector = dc.DecisionCollector()
    with caplog.at_level(logging.WARNING, logger="tools.decision_capture"):
        dc.safe_record(
            collector, decision_stage="dedupe", decision_reason="duplicate_of",
            payload={"live_candidate": object()},
        )
    assert collector.events == []
    assert "decision capture failed" in caplog.text


def test_capture_warning_does_not_log_request_data(caplog):
    class BrokenCollector:
        def record(self, **_fields):
            raise RuntimeError("https://private.example/?api_key=secret")

    with caplog.at_level(logging.WARNING, logger="tools.decision_capture"):
        dc.safe_record(BrokenCollector())
    assert "RuntimeError" in caplog.text
    assert "private.example" not in caplog.text
    assert "secret" not in caplog.text


def test_broken_log_handler_cannot_break_capture(monkeypatch):
    def broken_warning(*_args, **_kwargs):
        raise RuntimeError("broken logging handler")

    monkeypatch.setattr(dc.LOGGER, "warning", broken_warning)
    dc.safe_record(dc.DecisionCollector(), decision_stage="invalid", decision_reason="accepted")


def test_capture_scope_restores_nested_context():
    variable = ContextVar("test_capture_context", default=None)
    with dc.capture_scope(variable, "outer"):
        assert variable.get() == "outer"
        with dc.capture_scope(variable, "inner"):
            assert variable.get() == "inner"
        assert variable.get() == "outer"
    assert variable.get() is None


def test_capture_scope_restores_context_and_preserves_body_exception():
    variable = ContextVar("test_capture_error_context", default=None)
    error = TimeoutError("provider timeout")
    with pytest.raises(TimeoutError) as caught:
        with dc.capture_scope(variable, "active"):
            raise error
    assert caught.value is error
    assert variable.get() is None


@pytest.mark.parametrize("fail_at", ["set", "reset"])
@pytest.mark.parametrize("body_fails", [False, True])
def test_capture_scope_setup_and_reset_fail_open(fail_at, body_fails, caplog):
    class BrokenContext:
        def set(self, _value):
            if fail_at == "set":
                raise RuntimeError("private request data")
            return object()

        def reset(self, _token):
            raise RuntimeError("private request data")

    calls = []
    error = TimeoutError("provider timeout")

    def run():
        with dc.capture_scope(BrokenContext(), "active"):
            calls.append("provider call")
            if body_fails:
                raise error

    with caplog.at_level(logging.WARNING, logger="tools.decision_capture"):
        if body_fails:
            with pytest.raises(TimeoutError) as caught:
                run()
            assert caught.value is error
        else:
            run()
    assert calls == ["provider call"]
    assert f"context {'setup' if fail_at == 'set' else 'reset'} failed" in caplog.text
    assert "private request data" not in caplog.text
