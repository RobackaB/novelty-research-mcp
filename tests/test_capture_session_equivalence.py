"""Exercise capture failures through real writers, packs, searches and SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

import tools.patent_evidence_pack as patent_pack
import tools.patent_search as patent_search
import tools.publication_evidence_pack as publication_pack
import tools.research_session as rs
import tools.web_evidence_pack as web_pack
from test_capture_pack_equivalence import (
    QUERY,
    CountingRaisingCollector,
    _mock_patent_io,
    _mock_publication_io,
    _mock_web_io,
)

SESSION_ID = "rs_capture_equivalence"
RUN_ID = "capture-equivalence-run"
PRODUCTION_TABLES = (
    "research_sessions", "evidence_results", "raw_evidence_items",
    "checklist_snapshots", "final_answers", "trace_events", "schema_migrations",
)
SCORING_STAGES = {
    "patent": "domain_anchor", "publication": "merged_rerank", "web": "provider_filter",
}
PACK_SEARCH_EDGES = {
    "patent": (patent_pack, "patent_search"),
    "publication": (publication_pack, "publications_search"),
    "web": (web_pack, "web_search"),
}


def _run_session(monkeypatch, db_path, mode: str, caplog) -> dict:
    """Only replace external I/O and the diagnostic failure under test."""
    collectors = []
    faults = []
    real_builder = rs._build_decision_collector
    real_connect = sqlite3.connect

    def build(*args, **kwargs):
        if mode == "setup_failure":
            faults.append("setup")
            raise RuntimeError("injected collector setup failure")
        if mode == "disabled":
            return None
        collector = (
            CountingRaisingCollector() if mode == "record_failure"
            else real_builder(*args, **kwargs)
        )
        collectors.append(collector)
        return collector

    class InsertFailureConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            if "INSERT INTO evaluation_candidate_decisions" in sql:
                faults.append("insert")
                raise sqlite3.OperationalError("injected evaluation INSERT failure")
            return super().executemany(sql, parameters)

    def fail_decision_schema(_conn):
        faults.append("ddl")
        raise sqlite3.OperationalError("injected evaluation DDL failure")

    with monkeypatch.context() as patch:
        patch.setenv("RESEARCH_SESSION_DB", str(db_path))
        patch.setattr(rs, "_now", lambda: "2026-09-11T12:00:00+00:00")
        patch.setattr(rs, "_build_decision_collector", build)
        if mode == "insert_failure":
            patch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(
                *a, **dict(k, factory=InsertFailureConnection)
            ))
        elif mode == "ddl_failure":
            patch.setattr(rs, "_ensure_decision_schema", fail_decision_schema)
        _mock_patent_io(patch)
        _mock_publication_io(patch)
        _mock_web_io(patch)
        patent_search._PATENT_SEARCH_CACHE.clear()
        caplog.clear()

        outputs = {}
        rs.research_session_start(QUERY, session_id=SESSION_ID)
        outputs["envelope"] = rs.research_session_understand_query(
            SESSION_ID, english_query=QUERY, run_id=RUN_ID,
        )
        outputs["initial_checklist"] = rs.research_session_checklist(
            SESSION_ID, run_id=RUN_ID,
        )
        for attempt in (1, 2):
            query = QUERY if attempt == 1 else QUERY + " security"
            for source in rs.SOURCE_TYPES:
                writer = getattr(rs, f"{source}_evidence_to_session")
                ack = asyncio.run(writer(
                    session_id=SESSION_ID, query=query, attempt_no=attempt,
                    run_id=RUN_ID, english_query=query,
                ))
                outputs[f"{source}_{attempt}"] = ack
                assert json.loads(ack)["status"] == "written", ack
                if attempt == 1:
                    duplicate = asyncio.run(writer(
                        session_id=SESSION_ID, query=query, attempt_no=attempt,
                        run_id=RUN_ID, english_query=query,
                    ))
                    outputs[f"{source}_duplicate"] = duplicate
                    assert json.loads(duplicate)["status"] == "duplicate_noop"
            outputs[f"checklist_{attempt}"] = rs.research_session_checklist(
                SESSION_ID, run_id=RUN_ID,
            )
        outputs["merged"] = rs.research_session_merge(SESSION_ID)
        outputs["report"] = rs.research_session_final_answer(
            SESSION_ID, original_query=QUERY, run_id=RUN_ID,
        )
        outputs["user_answer"] = rs.research_session_user_answer(
            SESSION_ID, original_query=QUERY, run_id=RUN_ID,
        )
        with real_connect(db_path) as conn:
            production = {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in PRODUCTION_TABLES
            }
            has_capture_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='evaluation_candidate_decisions'"
            ).fetchone()
            persisted = conn.execute(
                "SELECT source_type, attempt, decision_stage "
                "FROM evaluation_candidate_decisions ORDER BY id"
            ).fetchall() if has_capture_table else []
        logs = list(caplog.records)
    patent_search._PATENT_SEARCH_CACHE.clear()
    return {
        "production": (outputs, production), "collectors": collectors,
        "faults": faults, "persisted": persisted, "logs": logs,
    }


def _assert_full_capture(result: dict) -> None:
    """A matching output alone cannot prove that writer forwarding works."""
    events = [event for collector in result["collectors"] for event in collector.events]
    scored = {(event["source_type"], event["attempt"]) for event in events
              if event["decision_stage"] == SCORING_STAGES[event["source_type"]]}
    expected = {(source, attempt) for source in rs.SOURCE_TYPES for attempt in (1, 2)}
    assert expected <= scored, f"missing writer/search decisions: {expected - scored}"
    persisted = {(source, attempt) for source, attempt, stage in result["persisted"]
                 if stage == SCORING_STAGES[source]}
    assert expected <= persisted, f"missing persisted decisions: {expected - persisted}"


@pytest.mark.parametrize("mode", [
    "enabled", "record_failure", "setup_failure", "insert_failure", "ddl_failure",
])
def test_real_session_outputs_and_retries_are_capture_independent(
    monkeypatch, temp_db, caplog, mode,
):
    caplog.set_level(logging.DEBUG, logger="tools.decision_capture")
    caplog.set_level(logging.WARNING, logger="tools.research_session")
    disabled = _run_session(monkeypatch, temp_db, "disabled", caplog)
    result = _run_session(monkeypatch, temp_db.with_name(f"{mode}.sqlite3"), mode, caplog)

    # Complete serialized ACKs, both retry plans, all report bytes and every
    # production table column must match; no fields are dropped or normalized.
    assert result["production"] == disabled["production"]
    assert disabled["persisted"] == []
    if mode == "enabled":
        _assert_full_capture(result)
    elif mode == "record_failure":
        assert len(result["collectors"]) == 6
        assert all(collector.attempts > 0 for collector in result["collectors"])
        assert any("decision capture failed" in record.message for record in result["logs"])
        assert result["persisted"] == []
    elif mode == "setup_failure":
        assert result["faults"] == ["setup"] * 6
        assert any("capture setup failed" in record.message
                   and record.levelno >= logging.WARNING for record in result["logs"])
        assert result["persisted"] == []
    else:
        assert result["faults"] == ["insert" if mode == "insert_failure" else "ddl"] * 6
        assert len(result["collectors"]) == 6
        assert all(collector.events for collector in result["collectors"])
        assert any("decision persistence failed" in record.message
                   and record.levelno >= logging.WARNING for record in result["logs"])
        assert result["persisted"] == []


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
@pytest.mark.parametrize("edge", ["writer_to_pack", "pack_to_search"])
def test_forwarding_negative_control(monkeypatch, temp_db, caplog, source, edge):
    """Remove the real forwarding edge and require the enabled oracle to fail."""
    owner, name = (
        (rs, f"{source}_evidence_pack") if edge == "writer_to_pack"
        else PACK_SEARCH_EDGES[source]
    )
    real_call = getattr(owner, name)

    async def drop_forwarding(*args, **kwargs):
        kwargs.pop("_collector", None)
        return await real_call(*args, **kwargs)

    monkeypatch.setattr(owner, name, drop_forwarding)
    result = _run_session(monkeypatch, temp_db, "enabled", caplog)
    with pytest.raises(AssertionError, match="missing writer/search decisions"):
        _assert_full_capture(result)
