"""Integration tests of the SQLite research session workflow (no network)."""

from __future__ import annotations

import json
import os

import tools.research_session as rs


def _web_pack(hits=None, status="ok", completed=True, reliable_no_results=False):
    return {
        "source_type": "web",
        "status": status,
        "completed": completed,
        "reliable_no_results": reliable_no_results,
        "hits": hits if hits is not None else [],
        "errors": [],
        "warnings": [],
    }


def _strong_web_hit(url="https://example.com/product"):
    return {
        "title": "Smart door lock product page",
        "url": url,
        "evidence_level": "fetched_excerpt",
        "summary": "Smart door lock unlocked by mobile application with temporary access codes.",
        "verified_url": True,
        "relevance": "direct",
        "relevance_score": 7.5,
    }


def test_session_start_creates_session(temp_db):
    response = json.loads(rs.research_session_start("Over, či existuje inteligentný zámok"))
    assert response["ok"] is True
    assert response["session_id"].startswith("rs_")
    assert response["expected_sources"] == ["patent", "publication", "web"]
    assert os.path.exists(temp_db)


def test_session_id_sanitized(temp_db):
    response = json.loads(rs.research_session_start("query", session_id="weird id/../!!"))
    assert "/" not in response["session_id"]
    assert " " not in response["session_id"]


def test_understand_query_stores_envelope(temp_db):
    session_id = json.loads(rs.research_session_start("Inteligentný dverový zámok s aplikáciou"))["session_id"]
    response = json.loads(
        rs.research_session_understand_query(
            session_id,
            english_query="smart door lock unlocked by mobile application with temporary access codes",
        )
    )
    assert response["status"] == "ok"
    assert response["language"] == "non_english"
    assert response["key_term_count"] > 0
    assert all(count > 0 for count in response["query_variant_counts"].values())


def test_understand_query_unknown_session(temp_db):
    response = json.loads(rs.research_session_understand_query("rs_nonexistent"))
    assert response["status"] == "failed"
    assert response["errors"][0]["type"] == "unknown_session"


def test_save_evidence_and_duplicate_noop(temp_db):
    session_id = json.loads(rs.research_session_start("smart door lock"))["session_id"]
    first = json.loads(
        rs.research_session_save_evidence(
            session_id,
            "web",
            _web_pack([_strong_web_hit()]),
            query="smart door lock",
            attempt=1,
        )
    )
    assert first["status"] == "written"
    assert first["hit_count"] == 1
    assert first["inserted_hits"] == 1
    assert first["quality_grade"] in {"strong", "medium", "weak"}

    duplicate = json.loads(
        rs.research_session_save_evidence(
            session_id,
            "web",
            _web_pack([_strong_web_hit()]),
            query="smart door lock",
            attempt=1,
        )
    )
    assert duplicate["status"] == "duplicate_noop"
    assert duplicate["was_duplicate_call"] is True


def test_save_evidence_attempt_budget_exceeded(temp_db):
    session_id = json.loads(rs.research_session_start("query"))["session_id"]
    response = json.loads(
        rs.research_session_save_evidence(session_id, "web", _web_pack(), query="query", attempt=99)
    )
    assert response["status"] == "attempt_budget_exceeded"
    assert response["ok"] is False
    assert response["needs_retry"] is False


def test_record_failure_stores_failed_attempt(temp_db):
    session_id = json.loads(rs.research_session_start("query"))["session_id"]
    response = json.loads(
        rs.research_session_record_failure(
            session_id, "patent", "query", status="timeout", error_type="timeout", message="deadline"
        )
    )
    assert response["status"] == "timeout"
    assert response["quality_grade"] == "failed_retrieval"
    snapshot = json.loads(rs.research_session_get(session_id))
    assert snapshot["latest"]["patent"]["status"] == "timeout"


def test_english_query_diagnostics_in_ack(temp_db):
    session_id = json.loads(rs.research_session_start("dotaz po slovensky"))["session_id"]
    response = json.loads(
        rs.research_session_save_evidence(
            session_id,
            "web",
            _web_pack([_strong_web_hit()]),
            query="dotaz po slovensky",
            attempt=1,
            english_query="smart door lock",
        )
    )
    assert response["english_query_received"] is True
    assert response["english_query_used_for_search"] is True


def test_checklist_reports_missing_sources(temp_db):
    session_id = json.loads(rs.research_session_start("smart lock"))["session_id"]
    checklist = json.loads(rs.research_session_checklist(session_id))
    assert checklist["ok"] is True
    assert checklist["can_finalize"] is False
    assert checklist["needs_supervisor_loop"] is True
    missing = {check["source_type"] for check in checklist["source_checks"] if check["state"] == "missing"}
    assert missing == {"patent", "publication", "web"}
    assert len(checklist["retry_actions"]) == 3


def test_checklist_unknown_session(temp_db):
    checklist = json.loads(rs.research_session_checklist("rs_missing"))
    assert checklist["status"] == "failed"
    assert checklist["can_finalize"] is False


def test_full_workflow_to_user_answer(temp_db):
    session_id = json.loads(rs.research_session_start("smart door lock mobile application"))["session_id"]
    rs.research_session_understand_query(session_id, english_query="smart door lock mobile application")
    for source_type, pack in (
        ("patent", {"source_type": "patent", "status": "ok", "completed": True,
                    "reliable_no_results": False, "hits": [{
                        "title": "Smart lock patent",
                        "url": "https://patents.google.com/patent/US1234567B2/en",
                        "patent_number": "US1234567B2",
                        "evidence_level": "claim_verified",
                        "summary": "Smart door lock with mobile application control and access codes.",
                        "verified_url": True,
                        "relevance": "focused",
                        "relevance_score": 8.0,
                    }], "errors": [], "warnings": []}),
        ("publication", {"source_type": "publication", "status": "ok", "completed": True,
                         "reliable_no_results": True, "hits": [], "errors": [], "warnings": []}),
        ("web", _web_pack([_strong_web_hit()])),
    ):
        response = json.loads(
            rs.research_session_save_evidence(session_id, source_type, pack, query="smart door lock", attempt=1)
        )
        assert response["status"] == "written", response

    checklist = json.loads(rs.research_session_checklist(session_id))
    assert checklist["can_finalize"] is True
    assert checklist["stop_reason"] is not None

    answer = rs.research_session_user_answer(session_id)
    assert isinstance(answer, str)
    assert "## Summary" in answer or "## Zhrnutie" in answer
    assert "US1234567B2" in answer

    # After the answer, the session is closed to further writes.
    rejected = json.loads(
        rs.research_session_save_evidence(session_id, "web", _web_pack(), query="another", attempt=2)
    )
    assert rejected["status"] == "rejected_session_closed"


def test_user_answer_unknown_session(temp_db):
    answer = rs.research_session_user_answer("rs_missing")
    assert "Research session not found" in answer


def test_db_file_not_locked_after_operations(temp_db):
    """SQLite connections must be closed after every operation (Windows lock test)."""
    session_id = json.loads(rs.research_session_start("lock check"))["session_id"]
    rs.research_session_save_evidence(session_id, "web", _web_pack([_strong_web_hit()]), query="lock check", attempt=1)
    renamed = temp_db.with_name("renamed.sqlite3")
    os.rename(temp_db, renamed)  # Fails on Windows if anything still holds an open handle.
    os.rename(renamed, temp_db)


def test_query_hash_stability():
    assert rs.query_hash("  Smart   Lock ") == rs.query_hash("smart lock")
    assert rs.query_hash("a") != rs.query_hash("b")


def test_build_query_envelope_atoms_for_english_query():
    envelope = rs._build_query_envelope(
        "Verify whether a smart door lock unlocked by a mobile application, "
        "allowing time-limited access codes, logging entry history and notifying the owner already exists."
    )
    assert envelope["language"] == "en"
    assert envelope["critical_requirements_atomic"], "the atomic decomposition must not be empty"
    assert envelope["query_too_generic"] is False
    assert envelope["query_variants"]["web"], "web variants must exist"


def test_build_query_envelope_generic_query_flagged():
    envelope = rs._build_query_envelope("system")
    assert envelope["query_too_generic"] is True


async def test_evidence_to_session_wrapper_with_stub(temp_db, monkeypatch):
    """Exercise the whole async web_evidence_to_session path with a stubbed provider."""

    async def _fake_pack(**kwargs):
        return _web_pack([_strong_web_hit()])

    monkeypatch.setattr(rs, "web_evidence_pack", _fake_pack)
    session_id = json.loads(rs.research_session_start("smart lock"))["session_id"]
    response = json.loads(
        await rs.web_evidence_to_session(session_id=session_id, query="smart lock", attempt_no=1)
    )
    assert response["status"] == "written"
    assert response["evidence_source_type"] == "web"
    assert response["hit_count"] == 1


async def test_evidence_to_session_provider_exception_recorded(temp_db, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(rs, "web_evidence_pack", _boom)
    session_id = json.loads(rs.research_session_start("smart lock"))["session_id"]
    response = json.loads(
        await rs.web_evidence_to_session(session_id=session_id, query="smart lock", attempt_no=1)
    )
    assert response["status"] == "provider_error"
    assert response["needs_retry"] is True
    snapshot = json.loads(rs.research_session_get(session_id))
    assert snapshot["latest"]["web"]["status"] == "provider_error"


async def test_evidence_to_session_rejects_wrong_source_type(temp_db):
    session_id = json.loads(rs.research_session_start("smart lock"))["session_id"]
    response = json.loads(
        await rs.web_evidence_to_session(
            session_id=session_id, query="smart lock", attempt_no=1, source_type="patent"
        )
    )
    assert response["status"] == "invalid_source_type"
