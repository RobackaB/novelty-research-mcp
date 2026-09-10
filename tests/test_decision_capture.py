"""Goal 5B: the decision collector must capture without affecting retrieval.

Every test here answers one of two questions: does capture change behaviour
(it must not), and does capture actually record what the dataset builder needs
(it must). Several are negative-controlled in test_decision_capture_controls.py
by removing the hook and asserting the test then fails.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import tools.patent_search as patent_search
import tools.publications_search as pubs
import tools.research_session as rs
from tools.decision_capture import (
    DECISION_REASONS,
    DECISION_STAGES,
    CollectorContext,
    DecisionCollector,
    candidate_identity,
    example_id,
    query_fingerprint,
    safe_record,
)


def _collector(source_type: str = "publication") -> DecisionCollector:
    return DecisionCollector(
        context=CollectorContext(
            session_id="s1", run_id="r1", source_type=source_type, attempt=2,
            query_fingerprint=query_fingerprint("anomaly detection in application logs"),
        )
    )


class RaisingCollector:
    """A collector that fails on every call, to prove capture is fail-open."""

    events: list = []

    def record(self, **_fields):
        raise RuntimeError("capture exploded")


# --- identity -----------------------------------------------------------------

def test_query_fingerprint_ignores_provenance_and_whitespace():
    a = query_fingerprint("  Anomaly   Detection in LOGS ")
    b = query_fingerprint("anomaly detection in logs")
    assert a == b


def test_same_document_under_different_queries_gets_different_example_ids():
    identity, kind = candidate_identity(canonical_id="10.1000/x")
    assert kind == "canonical_id"
    one = example_id(query_fingerprint("smart door lock"), "publication", identity)
    two = example_id(query_fingerprint("anomaly detection logs"), "publication", identity)
    assert one != two


def test_candidate_identity_falls_back_in_the_agreed_order():
    assert candidate_identity(canonical_id="US1B2")[1] == "canonical_id"
    assert candidate_identity(url="https://www.Example.com/a/")[1] == "url"
    assert candidate_identity(url="https://www.example.com/a")[0] == "example.com/a"
    assert candidate_identity(title="t", score_text="x")[1] == "text_hash"


# --- collector safety ---------------------------------------------------------

def test_retained_is_null_for_structural_events():
    c = _collector()
    c.record(decision_stage="dedupe", decision_reason="duplicate_of", retained=True)
    c.record(decision_stage="merge_collision", decision_reason="discarded_duplicate_url")
    c.record(decision_stage="truncation", decision_reason="beyond_limit", retained=False)
    assert [e["retained"] for e in c.events] == [None, None, None]


def test_retained_is_set_for_real_relevance_decisions():
    c = _collector()
    c.record(decision_stage="provider_filter", decision_reason="below_threshold", retained=False)
    c.record(decision_stage="domain_anchor", decision_reason="accepted", retained=True)
    assert [e["retained"] for e in c.events] == [False, True]


def test_score_text_is_preserved_exactly():
    c = _collector()
    text = "x" * 9000 + "\u00e9\n\ttail"
    c.record(decision_stage="provider_filter", decision_reason="accepted", score_text=text)
    assert c.events[0]["score_text"] == text


def test_safe_record_swallows_a_raising_collector():
    safe_record(RaisingCollector(), decision_stage="provider_filter", decision_reason="accepted")


def test_safe_record_ignores_none():
    safe_record(None, decision_stage="provider_filter", decision_reason="accepted")


def test_all_recorded_stages_and_reasons_are_in_the_closed_enums():
    c = _collector()
    c.record(decision_stage="provider_filter", decision_reason="below_threshold", retained=False)
    for event in c.events:
        assert event["decision_stage"] in DECISION_STAGES
        assert event["decision_reason"] in DECISION_REASONS


# --- capture at the real hooks ------------------------------------------------

def test_publication_threshold_rejection_is_captured_with_exact_fields():
    c = _collector()
    query = "anomaly detection in application logs"
    text = "Seismic wave classification using supervised learning for earthquakes."
    pubs._filter_publication_text(query, text, 5, _collector=c)
    rejects = [e for e in c.events if e["retained"] is False]
    assert rejects, f"no rejection captured: {c.events}"
    event = rejects[0]
    assert event["decision_stage"] == "provider_filter"
    assert event["decision_reason"] == "below_threshold"
    assert event["score_text"] == text
    assert event["decision_query"] == query
    assert event["threshold_at_decision"] == pubs.MIN_PUBLICATION_RELEVANCE_SCORE
    assert isinstance(event["score_at_decision"], float)
    assert event["attempt"] == 2 and event["session_id"] == "s1"


def test_patent_domain_anchor_rejection_does_not_claim_below_threshold():
    """A non-numeric gate must record its own reason."""
    c = _collector("patent")
    candidates = [
        patent_search.PatentCandidate(
            patent_number="US1111111B2", title="Method and system for comparing images",
            url="https://patents.google.com/patent/US1111111B2/en",
            snippet="A method, system and computer program for comparing images.",
        )
    ]
    patent_search._rank("anomaly detection in application logs", candidates, 6, _collector=c)
    anchors = [e for e in c.events if e["decision_stage"] == "domain_anchor"]
    assert anchors, f"no anchor decision captured: {c.events}"
    rejected = [e for e in anchors if e["retained"] is False]
    assert rejected, "the off-topic patent should fail the anchor"
    assert rejected[0]["decision_reason"] == "no_discriminative_term"
    assert rejected[0]["threshold_at_decision"] is None


def test_patent_dedupe_is_structural_not_a_relevance_rejection():
    c = _collector("patent")
    dup = dict(
        title="Smart door lock with mobile application",
        snippet="A smart door lock controlled from a mobile application.",
    )
    candidates = [
        patent_search.PatentCandidate(patent_number="US2222222B2", url="https://a/1", **dup),
        patent_search.PatentCandidate(patent_number="US2222222B2", url="https://a/2", **dup),
    ]
    patent_search._dedupe(candidates, _collector=c)
    dedupes = [e for e in c.events if e["decision_stage"] == "dedupe"]
    assert dedupes, f"no dedupe event captured: {c.events}"
    assert dedupes[0]["retained"] is None
    assert dedupes[0]["decision_reason"] == "duplicate_of"


# --- persistence --------------------------------------------------------------

def test_events_persist_untruncated_beyond_the_4000_char_diagnostic_limit(temp_db):
    """score_text has its own column precisely because _safe_payload truncates."""
    c = _collector()
    long_text = "lorem ipsum " * 900          # ~10800 chars, far beyond 4000
    assert len(long_text) > 4000
    c.record(decision_stage="provider_filter", decision_reason="below_threshold",
             score_text=long_text, retained=False, score_at_decision=1.5,
             threshold_at_decision=3.5)
    rs._persist_decision_events(c)
    with sqlite3.connect(rs._db_path()) as conn:
        rows = conn.execute(
            "SELECT score_text, score_at_decision, threshold_at_decision, retained "
            "FROM evaluation_candidate_decisions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == long_text, "score_text was truncated"
    assert len(rows[0][0]) > 4000
    assert rows[0][1] == 1.5 and rows[0][2] == 3.5 and rows[0][3] == 0


def test_persistence_failure_is_fail_open(monkeypatch, temp_db):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(rs, "_connect", boom)
    c = _collector()
    c.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
    rs._persist_decision_events(c)   # must not raise


def test_persisting_an_empty_or_none_collector_is_a_no_op(temp_db):
    rs._persist_decision_events(None)
    rs._persist_decision_events(_collector())


def test_structural_and_relevance_events_are_distinguishable_after_persistence(temp_db):
    c = _collector("web")
    c.record(decision_stage="merge_collision", decision_reason="discarded_duplicate_url")
    c.record(decision_stage="provider_filter", decision_reason="below_threshold", retained=False)
    rs._persist_decision_events(c)
    with sqlite3.connect(rs._db_path()) as conn:
        rows = dict(
            conn.execute(
                "SELECT decision_stage, retained FROM evaluation_candidate_decisions"
            ).fetchall()
        )
    # group 2 (removed before scoring) vs group 1 (reached a scorer)
    assert rows["merge_collision"] is None
    assert rows["provider_filter"] == 0
