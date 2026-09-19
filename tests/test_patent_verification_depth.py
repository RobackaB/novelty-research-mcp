"""Patent verification depth: the discovery score must not narrow the pool.

The discovery score is computed from title and snippet only. A low score means
thin discovery evidence, not proof that deeper candidates are unworthy of
verification -- and the candidate holding the strongest claims may sit below a
weak-scoring first result. These tests drive the real evidence pack.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import tools.patent_evidence_pack as pep
from tools.patent_evidence_pack import select_candidates_for_verification

QUERY = "smart door lock controlled by a mobile application with access codes"
VERIFY_OK = "URL_STATUS: https://patents.google.com/patent/US1B2/en -> ok\n"

# Every score below the historical 5.0 cut, so the old ceiling would be 3.
LOW = 3.2
HIGH = 7.5


def _candidate(n: int, score: float, *, url: bool = True) -> dict:
    return {
        "title": f"US{n}B2 - Irrigation and lock control {n}",
        "url": f"https://patents.google.com/patent/US{n}B2/en" if url else "",
        "patent_number": f"US{n}B2",
        "snippet": "Smart door lock unlocked by a mobile application with access codes.",
        "score": score,
    }


def _install(monkeypatch, candidates, *, strong_for: str | None = None):
    """Stub discovery/verification; the pack's own selection stays real."""
    fetched: list[str] = []

    async def fake_search(query, max_results=10, **_kw):
        return json.dumps({
            "schema_version": "patent.v1", "source_type": "patent", "status": "ok",
            "completed": True, "reliable_no_results": False, "query": query,
            "provider": "stub", "errors": [], "notes": [], "results": candidates,
        })

    async def fake_fetch(url, timeout_ms=18000, pdf_url="", *, patent_number=""):
        fetched.append(patent_number or url)
        level = "CLAIM_VERIFIED" if patent_number == strong_for else "SEARCH_SNIPPET_ONLY"
        claim = (
            "CLAIM1: 1. A smart door lock system comprising a mobile application, "
            "temporary access codes and an entry history log.\n"
            if patent_number == strong_for else
            "CLAIM1: No first claim was extracted from the page.\n"
        )
        return (
            f"PATENT_NUMBER: {patent_number} was identified from the page.\n"
            "FILED: 2020-01-01 was identified on the page.\n"
            "ASSIGNEE: ACME was identified on the page.\n"
            f"{claim}"
            "ABSTRACT: A smart door lock unlocked by a mobile application.\n"
            "STATUS: OK was returned for this extraction.\n"
            f"EVIDENCE_LEVEL: {level}\nPROVIDER: google_patents\n"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pep, "patent_search", fake_search)
    monkeypatch.setattr(pep, "patent_fetch", fake_fetch)
    monkeypatch.setattr(pep, "verify_sources", fake_verify)
    return fetched


def _run(max_fetches: int) -> dict:
    return json.loads(
        asyncio.run(pep.patent_evidence_pack(query=QUERY, max_fetches=max_fetches))
    )


# --- 1. low discovery score must not cut the pool to 3 -----------------------

def test_low_discovery_scores_still_verify_the_full_budget(monkeypatch):
    fetched = _install(monkeypatch, [_candidate(n, LOW) for n in range(1, 9)])
    _run(6)
    assert len(fetched) == 6, f"only {len(fetched)} verified; the old 3-ceiling is back"
    assert fetched == [f"US{n}B2" for n in range(1, 7)]


# --- 2. hard ceiling of 6 -----------------------------------------------------

def test_high_discovery_score_remains_bounded_at_six(monkeypatch):
    fetched = _install(monkeypatch, [_candidate(n, HIGH) for n in range(1, 13)])
    _run(10)                       # caller asks for more than the ceiling
    assert len(fetched) == 6


def test_ceiling_constant_is_six():
    assert pep.PATENT_VERIFICATION_CEILING == 6


# --- 3. caller budget is authoritative ---------------------------------------

def test_caller_budget_is_respected(monkeypatch):
    fetched = _install(monkeypatch, [_candidate(n, HIGH) for n in range(1, 9)])
    _run(2)
    assert len(fetched) == 2
    assert fetched == ["US1B2", "US2B2"]


def test_zero_budget_verifies_nothing(monkeypatch):
    fetched = _install(monkeypatch, [_candidate(n, HIGH) for n in range(1, 5)])
    _run(0)
    assert fetched == []


# --- 4. a non-fetchable candidate must not consume budget --------------------

def test_unfetchable_candidate_does_not_waste_budget(monkeypatch):
    candidates = [
        _candidate(1, LOW), _candidate(2, LOW, url=False),
        _candidate(3, LOW), _candidate(4, LOW),
    ]
    fetched = _install(monkeypatch, candidates)
    _run(3)
    assert fetched == ["US1B2", "US3B2", "US4B2"], fetched


def test_original_indices_are_preserved_across_gaps():
    candidates = [
        _candidate(1, LOW), _candidate(2, LOW, url=False),
        _candidate(3, LOW), _candidate(4, LOW),
    ]
    selected = select_candidates_for_verification(candidates, 3)
    assert [index for index, _ in selected] == [0, 2, 3]
    assert [item["patent_number"] for _, item in selected] == ["US1B2", "US3B2", "US4B2"]


# --- 5. a deep candidate's substantive evidence is recorded -------------------

def test_deep_candidate_strong_evidence_is_recorded_for_the_right_patent(monkeypatch):
    """The 5th candidate holds the claims; the old ceiling never reached it."""
    candidates = [_candidate(n, LOW) for n in range(1, 7)]
    _install(monkeypatch, candidates, strong_for="US5B2")
    payload = _run(6)
    strong = [h for h in payload["hits"] if h.get("evidence_level") == "claim_verified"]
    assert strong, f"deep candidate evidence not recorded: {payload['hits']}"
    assert strong[0]["patent_number"] == "US5B2"
    assert len(strong) == 1, "evidence attached to the wrong hit as well"


# --- 6. failure of one deeper fetch does not stop the others -----------------

def test_one_failed_deep_fetch_does_not_block_the_rest(monkeypatch):
    candidates = [_candidate(n, LOW) for n in range(1, 7)]
    _install(monkeypatch, candidates)

    async def flaky(url, timeout_ms=18000, pdf_url="", *, patent_number=""):
        if patent_number == "US4B2":
            raise RuntimeError("provider exploded")
        return (
            f"PATENT_NUMBER: {patent_number} was identified from the page.\n"
            "CLAIM1: No first claim was extracted from the page.\n"
            "ABSTRACT: A smart door lock unlocked by a mobile application.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: SEARCH_SNIPPET_ONLY\nPROVIDER: google_patents\n"
        )

    monkeypatch.setattr(pep, "patent_fetch", flaky)
    payload = _run(6)
    assert payload["status"] in {"ok", "partial_failure"}
    assert len(payload["hits"]) >= 5, payload


# --- 7. determinism -----------------------------------------------------------

def test_selection_is_deterministic_across_repeated_runs(monkeypatch):
    candidates = [_candidate(n, LOW) for n in range(1, 9)]
    runs = []
    for _ in range(3):
        fetched = _install(monkeypatch, candidates)
        _run(6)
        runs.append(list(fetched))
    assert runs[0] == runs[1] == runs[2]


def test_selection_does_not_reorder_discovery_order():
    candidates = [_candidate(1, 0.1), _candidate(2, 9.9), _candidate(3, 0.2)]
    selected = select_candidates_for_verification(candidates, 3)
    assert [item["patent_number"] for _, item in selected] == ["US1B2", "US2B2", "US3B2"]


# --- negative controls: production path, not a toy helper --------------------

def _old_score_dependent_selection(candidates, max_fetches):
    """The historical defect: score-dependent ceiling AND slice-before-filter."""
    top_score = float(candidates[0].get("score") or 0.0) if candidates else 0.0
    base_ceiling = 6 if top_score >= 5.0 else 3
    fetch_limit = max(0, min(max_fetches, base_ceiling, len(candidates)))
    return [(i, it) for i, it in enumerate(candidates[:fetch_limit]) if it.get("url")]


def test_restored_score_ceiling_breaks_the_low_score_regression(monkeypatch):
    fetched = _install(monkeypatch, [_candidate(n, LOW) for n in range(1, 9)])
    monkeypatch.setattr(pep, "select_candidates_for_verification", _old_score_dependent_selection)
    _run(6)
    assert len(fetched) == 3, (
        f"expected the historical 3-ceiling to verify only 3, saw {len(fetched)}"
    )


def test_restored_slice_before_filter_underfills_the_budget(monkeypatch):
    candidates = [
        _candidate(1, HIGH), _candidate(2, HIGH, url=False),
        _candidate(3, HIGH), _candidate(4, HIGH),
    ]
    fetched = _install(monkeypatch, candidates)
    monkeypatch.setattr(pep, "select_candidates_for_verification", _old_score_dependent_selection)
    _run(3)
    assert fetched == ["US1B2", "US3B2"], (
        f"slice-before-filter should under-fill to 2, saw {fetched}"
    )
