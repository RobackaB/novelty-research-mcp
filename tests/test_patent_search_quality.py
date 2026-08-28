"""Testy kvality patentových výsledkov podľa defektov z reálneho behu."""

from __future__ import annotations

from tools.patent_search import (
    MIN_RELEVANCE_FLOOR,
    PatentCandidate,
    _rank,
    _wipo_snippet,
)

# Doslovný riadok z výsledkovej tabuľky WIPO PATENTSCOPE zachytený pri reálnom behu.
WIPO_ROW = (
    "5. 20150294100 Method, system and computer program for comparing images US - 15.10.2015 "
    "Int.Class G06K 9/00 G PHYSICS 06 COMPUTING; CALCULATING OR COUNTING K GRAPHICAL DATA "
    "READING; PRESENTATION OF DATA; RECORD CARRIERS; HANDLING RECORD CARRIERS 9 Methods or "
    "arrangements for recognising patterns Appl.No 14751051 Applicant Paycasso Verify Ltd Inventor"
)


def test_wipo_snippet_strips_classification_boilerplate():
    """Rozpis medzinárodného triedenia spôsoboval falošné zhody s dotazom."""
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
    """Očistený snippet nesmie prejsť ako relevantný k dotazu o logoch."""
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
    """Pri núdzovom režime sa do reportu nesmú dostať úplne nesúvisiace patenty."""
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


def test_high_confidence_results_are_unaffected_by_the_floor():
    query = "smart door lock unlocked by a mobile application with access codes"
    candidates = [
        _candidate("US1", "Smart door lock unlocked by a mobile application with access codes"),
    ]
    ranked, _threshold = _rank(query, candidates, limit=10)
    assert [c.patent_number for c in ranked] == ["US1"]
