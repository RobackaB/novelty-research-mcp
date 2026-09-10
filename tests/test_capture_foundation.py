"""Foundation corrections for Goal 5B capture, one test group per review point.

Each semantic correction is paired with a defect-restoration control in
test_decision_capture_controls.py, and by an external
mutation script run against this tree (results reported with the patch): the defective behaviour is restored and the
named assertion here is shown to fail, so a green test cannot be an artefact of
an assertion that would hold either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

import tools.publications_search as pubs
import tools.research_session as rs
import tools.web_search as web_search
from tools.decision_capture import (
    DECISION_REASONS,
    DECISION_STAGES,
    CollectorContext,
    DecisionCollector,
    _normalize_url,
    candidate_identity,
    query_envelope_hash,
    query_fingerprint,
    safe_record,
)

ORIGINAL_QUERY = "Is there a smart door lock controlled by a mobile application?"


# --- P1: the fingerprint identifies the ORIGINAL query ------------------------

def test_fingerprint_is_stable_across_cleaning_retries_and_variants():
    """Retries and variants of one original query must stay one sample."""
    base = query_fingerprint(ORIGINAL_QUERY)
    assert base == query_fingerprint("  is there a SMART   door lock "
                                     "controlled by a mobile application? ")
    # a per-attempt executed variant is NOT the original query
    assert base != query_fingerprint("smart lock mobile app access codes")


def test_distinct_original_queries_are_not_conflated():
    assert query_fingerprint(ORIGINAL_QUERY) != query_fingerprint(
        "anomaly detection in application logs"
    )


def test_session_collector_uses_the_stored_original_query(temp_db):
    """Built through the real session path, not from the per-attempt query."""
    session_id = json.loads(rs.research_session_start(ORIGINAL_QUERY))["session_id"]
    collector = rs._new_decision_collector(session_id, "run1", "web", 3, "some other text")
    assert collector.context.query_fingerprint == query_fingerprint(ORIGINAL_QUERY)


# --- P2: envelope hashing uses a real, tested path ----------------------------

def test_envelope_hash_is_computed_from_a_real_envelope(temp_db):
    """A silently-empty hash must not count as success."""
    session_id = json.loads(rs.research_session_start(ORIGINAL_QUERY))["session_id"]
    rs.research_session_understand_query(session_id, english_query=ORIGINAL_QUERY)
    collector = rs._new_decision_collector(session_id, "run1", "web", 1, ORIGINAL_QUERY)
    assert collector.context.query_envelope_hash, "envelope hash silently empty"

    with rs._connect() as conn:
        row = rs._session_row(conn, rs._clean_session_id(session_id))
    atoms = rs._safe_json_loads(row["query_envelope_json"], {}).get(
        "critical_requirements_atomic", []
    )
    assert atoms, "fixture produced no atomic requirements"
    assert collector.context.query_envelope_hash == query_envelope_hash(atoms)


def test_envelope_hash_persists_through_the_session_path(temp_db):
    session_id = json.loads(rs.research_session_start(ORIGINAL_QUERY))["session_id"]
    rs.research_session_understand_query(session_id, english_query=ORIGINAL_QUERY)
    collector = rs._new_decision_collector(session_id, "run1", "publication", 1, ORIGINAL_QUERY)
    collector.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
    rs._persist_decision_events(collector)
    with sqlite3.connect(rs._db_path()) as conn:
        stored = conn.execute(
            "SELECT query_fingerprint, query_envelope_hash FROM evaluation_candidate_decisions"
        ).fetchone()
    assert stored[0] == query_fingerprint(ORIGINAL_QUERY)
    assert stored[1] == collector.context.query_envelope_hash and stored[1]


# --- P3: collision capture describes the real outcome -------------------------

def test_merge_collision_reason_names_the_discarded_occurrence():
    """Production keeps the first result for a URL; the later one is discarded."""
    assert "discarded_duplicate_url" in DECISION_REASONS
    assert "overwritten_by_later_provider" not in DECISION_REASONS


# --- P4: overlap-gate rejection is not a threshold rejection ------------------

def test_overlap_gate_and_threshold_are_separate_reasons():
    assert "below_overlap_gate" in DECISION_REASONS
    assert "below_threshold" in DECISION_REASONS


def test_overlap_gate_event_records_no_threshold():
    collector = DecisionCollector(context=CollectorContext(source_type="web"))
    collector.record(
        decision_stage="content_gate", decision_reason="below_overlap_gate",
        score_at_decision=1.0,
    )
    assert collector.events[0]["threshold_at_decision"] is None


# --- P5: provider and executed variant survive --------------------------------

def test_provider_and_variant_are_recorded_at_publication_hooks():
    collector = DecisionCollector(context=CollectorContext(source_type="publication"))
    text = "Title: Seismic waves\nPROVIDER: openalex\nSeismic wave classification for earthquakes."
    pubs._filter_publication_text("anomaly detection application logs", text, 5, _collector=collector)
    assert collector.events, "no event captured"
    event = collector.events[0]
    assert event["query_variant"] == "anomaly detection application logs"
    assert "provider" in event


def test_provenance_is_not_overwritten_by_the_source_category():
    collector = DecisionCollector(context=CollectorContext(source_type="publication"))
    collector.record(
        decision_stage="provider_filter", decision_reason="accepted",
        provider="crossref", query_variant="smart lock mobile app", retained=True,
    )
    assert collector.events[0]["provider"] == "crossref"
    assert collector.events[0]["query_variant"] == "smart lock mobile app"
    assert collector.events[0]["source_type"] == "publication"


# --- P6: closed enums enforced at runtime -------------------------------------

def test_invalid_stage_and_reason_are_rejected_at_runtime():
    collector = DecisionCollector()
    with pytest.raises(ValueError):
        collector.record(decision_stage="not_a_stage", decision_reason="accepted")
    with pytest.raises(ValueError):
        collector.record(decision_stage="provider_filter", decision_reason="not_a_reason")
    assert collector.events == [], "an invalid event was stored"


def test_invalid_events_do_not_break_execution_through_safe_record():
    collector = DecisionCollector()
    safe_record(collector, decision_stage="bogus", decision_reason="accepted")
    assert collector.events == []


def test_valid_values_are_accepted():
    collector = DecisionCollector()
    for stage in sorted(DECISION_STAGES):
        collector.record(decision_stage=stage, decision_reason="accepted")
    assert len(collector.events) == len(DECISION_STAGES)


# --- P7: conservative URL identity --------------------------------------------

def test_meaningful_query_parameters_are_preserved():
    a = candidate_identity(url="https://example.com/doc?id=1")[0]
    b = candidate_identity(url="https://example.com/doc?id=2")[0]
    assert a != b, "distinct documents collapsed into one identity"


def test_tracking_parameters_are_removed():
    plain = _normalize_url("https://example.com/doc?id=7")
    tracked = _normalize_url("https://www.example.com/doc?id=7&utm_source=x&gclid=y")
    assert plain == tracked == "example.com/doc?id=7"


def test_repeated_meaningful_parameters_are_preserved():
    assert _normalize_url("https://example.com/d?tag=a&tag=b") == "example.com/d?tag=a&tag=b"
    assert _normalize_url("https://example.com/d?tag=a") != _normalize_url(
        "https://example.com/d?tag=a&tag=b"
    )


def test_url_only_tracking_params_collapse_to_the_path():
    assert _normalize_url("https://example.com/doc?utm_source=x") == "example.com/doc"


# --- P9: fail-open persistence is observable ----------------------------------

def test_persistence_failure_logs_a_diagnostic_and_preserves_execution(monkeypatch, temp_db, caplog):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(rs, "_connect", boom)
    collector = DecisionCollector()
    collector.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
    with caplog.at_level(logging.DEBUG, logger="tools.research_session"):
        rs._persist_decision_events(collector)
    assert any("decision persistence failed" in r.message for r in caplog.records), (
        "failure was swallowed with no diagnostic"
    )


def test_successful_persistence_actually_writes_rows(temp_db):
    """Guards against a missing helper masquerading as successful capture."""
    collector = DecisionCollector(context=CollectorContext(session_id="s", source_type="web"))
    collector.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
    rs._persist_decision_events(collector)
    with sqlite3.connect(rs._db_path()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM evaluation_candidate_decisions").fetchone()[0]
    assert count == 1, "persistence reported no error but wrote nothing"


# --- P4 (behavioural): the web hook records the gate that actually rejected ---

def _run_web_search(monkeypatch, results, collector):
    """Drive the real web_search offline with a fixed provider payload."""
    async def fake_provider(_client, _query, _max_results):
        return results

    monkeypatch.setattr(web_search, "SEARCH_PROVIDERS", (("stub_provider", fake_provider),))
    monkeypatch.setattr(web_search, "_fetch_page_text", None, raising=False)
    return asyncio.run(
        web_search.web_search("smart door lock mobile application access codes",
                              max_results=5, _collector=collector)
    )


def test_overlap_gate_rejection_is_not_recorded_as_below_threshold(monkeypatch):
    """A candidate failing query overlap must not be labelled a threshold reject."""
    collector = DecisionCollector(context=CollectorContext(source_type="web"))
    _run_web_search(
        monkeypatch,
        [("https://example.com/unrelated", "Quarterly earnings report",
          "Financial results for the fiscal year with revenue figures.")],
        collector,
    )
    gate_events = [e for e in collector.events if e["decision_stage"] == "content_gate"]
    assert gate_events, f"overlap-gate rejection not captured: {collector.events}"
    assert gate_events[0]["decision_reason"] == "below_overlap_gate"
    assert gate_events[0]["threshold_at_decision"] is None
    assert not [
        e for e in collector.events
        if e["decision_stage"] == "provider_filter" and e["title"] == "Quarterly earnings report"
    ], "overlap failure was also recorded as a threshold decision"


def test_web_merge_collision_records_the_later_occurrence_as_discarded(monkeypatch):
    """Production keeps the first result; the later one is what gets discarded."""
    collector = DecisionCollector(context=CollectorContext(source_type="web"))
    same_url = "https://example.com/lock"
    _run_web_search(
        monkeypatch,
        [
            # Both pass every gate, so this is a genuine collision. The FIRST
            # scores 6.01 and the SECOND 8.90: production keeps the first even
            # though the later one scores higher, so the recorded outcome is not
            # an artefact of equal scores.
            (same_url, "Smart door lock", "Smart door lock with access codes."),
            (same_url, "Smart door lock mobile application",
             "Smart door lock controlled by a mobile application with access codes."),
        ],
        collector,
    )
    collisions = [e for e in collector.events if e["decision_stage"] == "merge_collision"]
    assert collisions, f"no collision captured: {collector.events}"
    event = collisions[0]
    assert event["decision_reason"] == "discarded_duplicate_url"
    # the discarded occurrence is the LATER one
    assert event["title"] == "Smart door lock mobile application"
    assert event["payload"]["kept_title"] == "Smart door lock"
    # explicit: the discarded occurrence is the higher scoring one
    assert web_search._web_rerank_score(
        "smart door lock mobile application access codes",
        "Smart door lock mobile application",
        "Smart door lock controlled by a mobile application with access codes.",
    ) > web_search._web_rerank_score(
        "smart door lock mobile application access codes",
        "Smart door lock", "Smart door lock with access codes.",
    )
    assert event["retained"] is None
    assert event["provider"] == "stub_provider"




def test_overlap_pass_score_fail_records_below_threshold(monkeypatch):
    collector = DecisionCollector(context=CollectorContext(source_type="web"))
    _run_web_search(
        monkeypatch,
        [("https://example.com/borderline", "smart door lock mobile", "")],
        collector,
    )
    events = [e for e in collector.events if e["title"] == "smart door lock mobile"]
    assert events, f"no event captured: {collector.events}"
    event = events[0]
    assert event["decision_stage"] == "provider_filter", event
    assert event["decision_reason"] == "below_threshold", event
    assert event["threshold_at_decision"] == web_search.MIN_WEB_RERANK_SCORE
    assert event["retained"] is False


def test_accepted_web_candidate_is_recorded_as_accepted(monkeypatch):
    collector = DecisionCollector(context=CollectorContext(source_type="web"))
    _run_web_search(
        monkeypatch,
        [("https://example.com/ok", "Smart door lock mobile application",
          "Smart door lock controlled by a mobile application with access codes.")],
        collector,
    )
    accepted = [e for e in collector.events if e["decision_reason"] == "accepted"]
    assert accepted, f"acceptance not captured: {collector.events}"
    assert accepted[0]["retained"] is True
