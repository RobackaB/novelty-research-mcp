"""Tests of patent result quality, driven by defects found in a real run."""

from __future__ import annotations

from tools.patent_search import (
    MIN_RELEVANCE_FLOOR,
    PatentCandidate,
    _rank,
    _wipo_snippet,
)

# A verbatim row from the WIPO PATENTSCOPE result table, captured during a real run.
WIPO_ROW = (
    "5. 20150294100 Method, system and computer program for comparing images US - 15.10.2015 "
    "Int.Class G06K 9/00 G PHYSICS 06 COMPUTING; CALCULATING OR COUNTING K GRAPHICAL DATA "
    "READING; PRESENTATION OF DATA; RECORD CARRIERS; HANDLING RECORD CARRIERS 9 Methods or "
    "arrangements for recognising patterns Appl.No 14751051 Applicant Paycasso Verify Ltd Inventor"
)


def test_wipo_snippet_strips_classification_boilerplate():
    """The international classification listing caused false matches against the query."""
    out = _wipo_snippet(WIPO_ROW)
    assert "recognising patterns" not in out
    assert "COMPUTING" not in out
    assert "RECORD CARRIERS" not in out
    assert "Int.Class" not in out


def test_wipo_snippet_keeps_meaningful_content():
    out = _wipo_snippet(WIPO_ROW)
    assert "comparing images" in out
    assert "Paycasso Verify Ltd" in out


def test_wipo_snippet_drops_row_index_and_country_date():
    out = _wipo_snippet(WIPO_ROW)
    assert not out.startswith("5.")
    assert "15.10.2015" not in out


def test_wipo_snippet_empty_when_nothing_substantive_remains():
    assert _wipo_snippet("1. 20150294100 Int.Class G06K 9/00 Appl.No 123") == ""
    assert _wipo_snippet("") == ""


def test_wipo_snippet_no_longer_matches_unrelated_query():
    """A cleaned snippet must not pass as relevant to a query about logs."""
    from tools.relevance import evidence_score

    query = "detecting anomalies in application logs machine learning recognise unusual patterns"
    before = evidence_score(query, WIPO_ROW, "PATENT")
    after = evidence_score(query, _wipo_snippet(WIPO_ROW), "PATENT")
    assert after < before


def _candidate(number: str, title: str, snippet: str = "") -> PatentCandidate:
    return PatentCandidate(
        title=title,
        url=f"https://patents.google.com/patent/{number}/en",
        patent_number=number,
        snippet=snippet,
    )


def test_low_confidence_mode_drops_zero_score_candidates():
    """In fallback mode, entirely unrelated patents must not reach the report."""
    query = "detecting anomalies in application logs and notifying an administrator"
    candidates = [
        _candidate("WO1", "ANOMALY DETECTION SYSTEM AND METHOD for application logs"),
        _candidate("EP2", "PHOTOELECTRIC CONVERSION DEVICE"),
        _candidate("US3", "Content-Aware Hybrid Quantum Enhanced Optimization"),
    ]
    ranked, _threshold = _rank(query, candidates, limit=10, allow_low_confidence=True)
    numbers = [c.patent_number for c in ranked]
    assert "WO1" in numbers, "tematicky blizky patent ma zostat"
    assert all(c.score >= MIN_RELEVANCE_FLOOR for c in ranked)


def test_low_confidence_mode_can_return_nothing():
    query = "detecting anomalies in application logs"
    candidates = [_candidate("EP9", "PHOTOELECTRIC CONVERSION DEVICE")]
    ranked, _threshold = _rank(query, candidates, limit=10, allow_low_confidence=True)
    assert ranked == []


def test_domain_anchor_applies_to_the_primary_path_too():
    """A patent linked to the query by generic vocabulary alone must not pass, even above the threshold.

    Term-rarity weighting does not catch this when all candidates come from one
    patent family, because then every term has the same frequency.
    """
    query = "detecting anomalies in application logs and notifying an administrator"
    family = [
        _candidate(
            f"US{i}",
            "Method, system and computer program for comparing images",
            "A method of determining whether a user of a mobile device corresponds to a "
            "previously authenticated user by acquiring an image from an identity document.",
        )
        for i in range(1, 5)
    ]
    ranked, _threshold = _rank(query, family, limit=10)
    assert ranked == [], "obrazova identifikacia nesuvisi s anomaliami v logoch"


def test_domain_anchor_keeps_genuinely_related_patents():
    query = "detecting anomalies in application logs and notifying an administrator"
    candidates = [
        _candidate(
            "US1",
            "Automatically updating communication maps used to detect anomalies",
            "Detecting anomalies in logged events and alerting an administrator.",
        ),
        _candidate("US2", "Method and apparatus for comparing photographic images", "identity document"),
    ]
    ranked, _threshold = _rank(query, candidates, limit=10)
    assert "US1" in [c.patent_number for c in ranked]


def test_high_confidence_results_are_unaffected_by_the_floor():
    query = "smart door lock unlocked by a mobile application with access codes"
    candidates = [
        _candidate("US1", "Smart door lock unlocked by a mobile application with access codes"),
    ]
    ranked, _threshold = _rank(query, candidates, limit=10)
    assert [c.patent_number for c in ranked] == ["US1"]


def test_same_invention_under_different_numbers_is_deduplicated():
    """One application comes back under several publication numbers."""
    from tools.patent_search import _dedupe

    family = [
        _candidate("US20250141733", "Automatically updating communication maps to detect failures"),
        _candidate("US20250141734", "Automatically updating communication maps to detect failures"),
        _candidate("WO2025090784", "AUTOMATICALLY UPDATING COMMUNICATION MAPS TO DETECT FAILURES"),
        _candidate("US11556444", "Electronic system for static program code analysis"),
    ]
    out = _dedupe(family)
    assert len(out) == 2
    assert {c.patent_number for c in out} == {"US20250141733", "US11556444"}


def test_dedupe_keeps_distinct_short_titles():
    from tools.patent_search import _dedupe

    out = _dedupe([_candidate("US1", "Smart lock"), _candidate("US2", "Door bell")])
    assert len(out) == 2
