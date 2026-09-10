"""Guards for foundation properties that previously had no failing test.

Each was identified by mutation: the defect could be reinstated and every
existing test still passed. Each test here is paired with the mutation that
must make it fail.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

import tools.publications_search as pubs
import tools.research_session as rs
import tools.web_search as web_search
from tools.decision_capture import CollectorContext, DecisionCollector, _normalize_url

QUERY = "smart door lock controlled by a mobile application with access codes"


# --- G1: publication provider is a real value, not just a present key ---------

def test_publication_provider_is_the_actual_source_marker():
    """_extract_block_provider reads ^SOURCE:, not PROVIDER:.

    The earlier test used a PROVIDER: fixture and only checked the key existed,
    so removing the provider argument entirely still passed.
    """
    collector = DecisionCollector(context=CollectorContext(source_type="publication"))
    text = (
        "Publication title returned by Crossref: **Seismic wave classification**.\n"
        "SOURCE: crossref\n"
        "Seismic wave classification for earthquake early warning systems.\n"
    )
    pubs._filter_publication_text(QUERY, text, 5, _collector=collector)
    assert collector.events, "no event captured"
    assert collector.events[0]["provider"] == "crossref", collector.events[0]


# --- G2: collector construction is protected --------------------------------

def test_collector_setup_failure_returns_none_instead_of_raising(monkeypatch, temp_db):
    """Restoring unprotected initialisation must fail here."""
    session_id = json.loads(rs.research_session_start(QUERY))["session_id"]

    def boom(*_a, **_k):
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr("tools.decision_capture.query_fingerprint", boom)
    result = rs._new_decision_collector(session_id, "run1", "web", 1, QUERY)
    assert result is None, "capture setup must be fail-open, not raise"


def test_collector_setup_failure_logs_at_warning(monkeypatch, temp_db, caplog):
    session_id = json.loads(rs.research_session_start(QUERY))["session_id"]

    def boom(*_a, **_k):
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr("tools.decision_capture.query_fingerprint", boom)
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        rs._new_decision_collector(session_id, "run1", "web", 1, QUERY)
    assert any("capture setup failed" in r.message for r in caplog.records)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# --- G3: persistence diagnostics are visible under normal logging ------------

def test_persistence_failure_is_visible_at_warning_not_debug(monkeypatch, temp_db, caplog):
    """Downgrading this log to DEBUG must fail here.

    caplog is set to WARNING, so a DEBUG record would not be captured.
    """
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(rs, "_connect", boom)
    collector = DecisionCollector()
    collector.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        rs._persist_decision_events(collector)
    records = [r for r in caplog.records if "decision persistence failed" in r.message]
    assert records, "persistence failure invisible at normal logging level"
    assert records[0].levelno >= logging.WARNING


# --- G4: persisted provenance carries real values ---------------------------

def test_persisted_provenance_values_are_not_blank(temp_db):
    """Replacing the persisted query_variant with '' must fail here."""
    collector = DecisionCollector(
        context=CollectorContext(session_id="s", run_id="r", source_type="patent", attempt=2)
    )
    collector.record(
        decision_stage="provider_filter", decision_reason="accepted", retained=True,
        decision_query="normalized scorer input",
        query_variant="smart door lock controlled mobile application access codes",
        provider="google_patents_xhr",
    )
    rs._persist_decision_events(collector)
    with sqlite3.connect(rs._db_path()) as conn:
        row = conn.execute(
            "SELECT decision_query, query_variant, provider FROM evaluation_candidate_decisions"
        ).fetchone()
    assert row[0] == "normalized scorer input"
    assert row[1] == "smart door lock controlled mobile application access codes"
    assert row[2] == "google_patents_xhr"
    # decision_query and query_variant are distinct concepts and must not merge
    assert row[0] != row[1]


# --- G5: the four URL collisions, as committed regression tests --------------

@pytest.mark.parametrize(
    "left, right",
    [
        ("https://example.com/Article", "https://example.com/article"),
        ("https://example.com/d?id=ABC", "https://example.com/d?id=abc"),
        ("https://example.com/d?source=manual", "https://example.com/d?source=api"),
        ("https://example.com/d?ref=1", "https://example.com/d?ref=2"),
    ],
    ids=["path-case", "value-case", "source-param", "ref-param"],
)
def test_distinct_urls_do_not_collapse(left, right):
    assert _normalize_url(left) != _normalize_url(right)


def test_host_case_is_still_folded_and_tracking_still_stripped():
    assert _normalize_url("https://WWW.Example.COM/p") == _normalize_url("https://example.com/p")
    assert _normalize_url("https://e.com/d?id=7&utm_source=x&gclid=y") == "e.com/d?id=7"


# --- G6: warm cache is honoured, not bypassed -------------------------------

def test_warm_patent_cache_emits_no_new_events_and_is_not_bypassed(monkeypatch):
    """Production TTL cache behaviour is preserved.

    A warm search returns identical bytes, makes no provider calls and emits no
    fresh decision events. Capture must not bypass the cache or fabricate events
    from cached winners; 5C will expose invocation-level reuse information.
    """
    import asyncio

    import tools.patent_search as patent_search

    calls = {"n": 0}

    async def provider(_client, _variant, _max_results=10):
        calls["n"] += 1
        return [
            patent_search.PatentCandidate(
                title="Smart door lock with mobile application",
                url="https://patents.google.com/patent/US1111111B2/en",
                patent_number="US1111111B2",
                snippet="Smart door lock unlocked by a mobile application using access codes.",
                provider="google_patents_xhr",
            )
        ]

    async def empty(_client, _variant, _max_results=10):
        return []

    monkeypatch.setattr(patent_search, "_google_patents_xhr_search", provider)
    for name in ("_tavily_patent_search", "_exa_patent_search", "_wipo_patentscope_search"):
        monkeypatch.setattr(patent_search, name, empty)

    patent_search._PATENT_SEARCH_CACHE.clear()
    cold_collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    cold = asyncio.run(patent_search.patent_search(QUERY, 5, _collector=cold_collector))
    cold_calls = calls["n"]
    assert cold_collector.events, "cold run emitted no events"

    warm_collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    warm = asyncio.run(patent_search.patent_search(QUERY, 5, _collector=warm_collector))

    assert warm == cold, "warm cache changed the returned bytes"
    assert calls["n"] == cold_calls, "warm run called providers; the cache was bypassed"

    # A warm run must emit no relevance decisions -- nothing was scored -- but it
    # must emit the cache_lookup marker, so the dataset builder can tell reuse
    # from a fresh search that genuinely found no candidates.
    warm_decisions = [e for e in warm_collector.events if e["decision_stage"] != "cache_lookup"]
    assert warm_decisions == [], "warm run fabricated events from cached results"

    warm_lookups = [e for e in warm_collector.events if e["decision_stage"] == "cache_lookup"]
    assert len(warm_lookups) == 1, warm_collector.events
    assert warm_lookups[0]["decision_reason"] == "cache_hit"
    assert warm_lookups[0]["dataset_eligible"] is False, "a cache marker is not a document"

    cold_lookups = [e for e in cold_collector.events if e["decision_stage"] == "cache_lookup"]
    assert len(cold_lookups) == 1 and cold_lookups[0]["decision_reason"] == "cache_miss"
