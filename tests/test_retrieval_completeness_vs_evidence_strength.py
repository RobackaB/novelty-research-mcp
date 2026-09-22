"""Strong evidence must not hide incomplete retrieval.

Evidence strength and retrieval completeness are separate dimensions. A source
could hold strong surviving evidence while its search/provider retrieval was
genuinely partial, and the checklist marked it ready and can_finalize while the
report read `exact_match · high · complete` -- with a body stating that no single
source verified the combination.

Completeness is keyed on STRUCTURED errors, which every evidence pack reserves
for search/provider failures; detail-fetch failures go to warnings, so a page
that failed to fetch after a complete search does not mark the search incomplete.
"""

from __future__ import annotations

import json

import pytest

import tools.evidence_quality as eq
import tools.research_session as rs

QUERY = "smart door lock controlled by a mobile application with access codes"
PROVIDER_ERROR = {"type": "provider_error", "message": "provider down"}


def _hits(source_type: str, n: int = 4) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        hit = {
            "title": f"{source_type} strong {i}",
            "url": f"https://example.com/{source_type}/{i}",
            "canonical_id": f"{source_type}-{i}",
            "evidence_level": "claim_verified" if source_type == "patent" else "abstract_verified",
            "summary": "Smart door lock controlled by a mobile application with access codes.",
            "verified_url": True,
            "relevance": "focused",
            "relevance_score": 8.0,
        }
        if source_type == "patent":
            hit["patent_number"] = f"US{i}111111B2"
        out.append(hit)
    return out


def _pack(source_type, *, partial=False, fetch_warning=False):
    return {
        "source_type": source_type,
        "status": "partial_failure" if (partial or fetch_warning) else "ok",
        "completed": not partial,
        "reliable_no_results": False,
        "hits": _hits(source_type),
        "errors": [PROVIDER_ERROR] if partial else [],
        "warnings": ["Fetch failed for https://example.com/x: timeout"] if fetch_warning else [],
    }


def _session(target, *, partial=False, fetch_warning=False, attempts=1):
    session = json.loads(rs.research_session_start(QUERY))["session_id"]
    rs.research_session_understand_query(session, english_query=QUERY)
    for source_type in rs.SOURCE_TYPES:
        is_target = source_type == target
        for attempt in range(1, (attempts if is_target else 1) + 1):
            rs.research_session_save_evidence(
                session, source_type,
                _pack(source_type, partial=partial and is_target,
                      fetch_warning=fetch_warning and is_target),
                query=QUERY, attempt=attempt,
            )
    return session


def _check(session, source_type):
    checklist = json.loads(rs.research_session_checklist(session))
    return checklist, next(c for c in checklist["source_checks"] if c["source_type"] == source_type)


def _header(session) -> str:
    answer = rs.research_session_user_answer(session, original_query=QUERY)
    return next(line for line in answer.splitlines() if "Retrieval:" in line)


SOURCES = ["publication", "patent", "web"]


# --- strong + partial, all three writers --------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_strong_but_partial_source_stays_retryable_while_budget_remains(temp_db, source):
    checklist, check = _check(_session(source, partial=True), source)
    assert check["quality_grade"] == "strong", "evidence strength must not be downgraded"
    assert check["search_incomplete"] is True
    assert check["ready"] is False
    assert check["blocking"] is True
    assert check["needs_retry"] is True
    assert checklist["can_finalize"] is False
    assert checklist["complete"] is False


@pytest.mark.parametrize("source", SOURCES)
def test_report_cannot_claim_complete_or_high_for_partial_retrieval(temp_db, source):
    header = _header(_session(source, partial=True))
    assert "`complete`" not in header, header
    assert "`high`" not in header, header
    assert "`degraded`" in header


# --- retry budget exhausted ----------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_exhausted_budget_may_finalize_but_retrieval_stays_degraded(temp_db, source):
    # The real per-source budget (patent 4, publication 3, web 3), not the
    # global default of 2 -- exhausting the wrong one leaves budget remaining.
    budget = rs._max_attempts_for_source(source, rs.DEFAULT_MAX_ATTEMPTS_PER_SOURCE)
    session = _session(source, partial=True, attempts=budget)
    checklist, check = _check(session, source)
    assert check["quality_grade"] == "strong"
    assert check["ready"] is True, "exhausted budget must allow finalisation"
    assert check["blocking"] is False
    assert check["search_incomplete"] is True, "incompleteness must stay visible"
    assert checklist["complete"] is False, "run must not be reported complete"
    header = _header(session)
    assert "`complete`" not in header and "`high`" not in header, header


# --- existing complete behaviour unchanged ------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_strong_complete_retrieval_keeps_existing_behaviour(temp_db, source):
    checklist, check = _check(_session(source), source)
    assert check["quality_grade"] == "strong"
    assert check["search_incomplete"] is False
    assert check["ready"] is True and check["blocking"] is False
    assert checklist["complete"] is True
    assert "`complete`" in _header(_session(source))


# --- fetch-detail warning is not search incompleteness ------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_fetch_warning_after_complete_search_is_not_search_incomplete(temp_db, source):
    """A failed page fetch goes to warnings, not errors, so search stays complete."""
    grade = eq.grade_source(_pack(source, fetch_warning=True), source)
    assert grade["search_incomplete"] is False


def test_search_incomplete_requires_a_structured_error():
    assert eq.grade_source(_pack("web", partial=True), "web")["search_incomplete"] is True
    no_errors = _pack("web", partial=True) | {"errors": []}
    assert eq.grade_source(no_errors, "web")["search_incomplete"] is False


# --- strong evidence is preserved ---------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_strong_evidence_is_not_discarded(temp_db, source):
    session = _session(source, partial=True)
    with rs._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM raw_evidence_items WHERE session_id=? AND source_type=?",
            (session, source),
        ).fetchone()[0]
    assert count == 4, "strong hits must be stored, not dropped"


def test_classification_distinguishes_strength_from_completeness():
    complete = {"patent": {"quality_grade": "strong", "search_incomplete": False}}
    partial = {"patent": {"quality_grade": "strong", "search_incomplete": True}}
    assert eq._classify_retrieval(complete) == "complete"
    assert eq._classify_retrieval(partial) == "degraded"


# --- negative control ---------------------------------------------------------

def test_restored_strong_shortcut_breaks_the_partial_regression(temp_db, monkeypatch):
    """Historical behaviour: a strong grade is ready regardless of retrieval."""
    original = eq.grade_source

    def shortcut(source, source_type=""):
        graded = original(source, source_type)
        graded["search_incomplete"] = False  # completeness dimension ignored
        return graded

    session = _session("publication", partial=True)
    monkeypatch.setattr(rs, "grade_source", shortcut)
    checklist, check = _check(session, "publication")
    assert check["ready"] is True, "the old shortcut should mark it ready"
    assert checklist["complete"] is True, "the old shortcut should report complete"
