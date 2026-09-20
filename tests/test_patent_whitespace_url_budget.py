"""A whitespace-only patent URL must not consume a verification slot.

select_candidates_for_verification filtered on `item.get("url")`, and "   " is
truthy. Such a candidate took a slot in the bounded verification budget and then
failed to fetch, silently costing a genuinely fetchable candidate its place.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import tools.patent_evidence_pack as pep
from tools.patent_evidence_pack import (
    PATENT_VERIFICATION_CEILING,
    select_candidates_for_verification,
)

QUERY = "smart door lock controlled by a mobile application with access codes"


def _candidate(n: int, url: str | None = None) -> dict:
    return {
        "title": f"US{n}B2 - Smart lock control {n}",
        "url": f"https://patents.google.com/patent/US{n}B2/en" if url is None else url,
        "patent_number": f"US{n}B2",
        "snippet": "Smart door lock unlocked by a mobile application with access codes.",
        "score": 3.2,
    }


def _numbers(selected):
    return [item["patent_number"] for _index, item in selected]


# --- unusable URLs must be skipped -------------------------------------------

@pytest.mark.parametrize(
    "url",
    ["   ", "\t", "\n", " \t\n ", "\u00a0", ""],
    ids=["spaces", "tab", "newline", "mixed", "nbsp", "empty"],
)
def test_unusable_url_does_not_consume_a_slot(url):
    candidates = [_candidate(1, url), _candidate(2)]
    selected = select_candidates_for_verification(candidates, 1)
    assert _numbers(selected) == ["US2B2"], f"{url!r} consumed the only slot"


def test_missing_url_key_is_skipped():
    candidates = [{"patent_number": "US1B2", "title": "no url key"}, _candidate(2)]
    assert _numbers(select_candidates_for_verification(candidates, 1)) == ["US2B2"]


def test_none_url_is_skipped():
    candidates = [_candidate(1, None) | {"url": None}, _candidate(2)]
    assert _numbers(select_candidates_for_verification(candidates, 1)) == ["US2B2"]


# --- the budget still fills from valid candidates ----------------------------

def test_valid_candidates_after_invalid_entries_fill_the_budget():
    candidates = [
        _candidate(1, "   "), _candidate(2, ""), _candidate(3),
        _candidate(4, "\t"), _candidate(5), _candidate(6),
    ]
    assert _numbers(select_candidates_for_verification(candidates, 3)) == [
        "US3B2", "US5B2", "US6B2",
    ]


def test_original_indices_are_preserved_across_skipped_entries():
    candidates = [
        _candidate(1, "   "), _candidate(2), _candidate(3, ""), _candidate(4),
    ]
    selected = select_candidates_for_verification(candidates, 2)
    assert [index for index, _item in selected] == [1, 3]
    assert _numbers(selected) == ["US2B2", "US4B2"]


# --- budget and ceiling unchanged --------------------------------------------

def test_caller_budget_is_still_respected():
    candidates = [_candidate(n) for n in range(1, 9)]
    assert len(select_candidates_for_verification(candidates, 2)) == 2


def test_hard_ceiling_is_still_six():
    candidates = [_candidate(n) for n in range(1, 13)]
    assert len(select_candidates_for_verification(candidates, 10)) == 6
    assert PATENT_VERIFICATION_CEILING == 6


def test_zero_budget_selects_nothing():
    assert select_candidates_for_verification([_candidate(1)], 0) == []


def test_all_urls_unusable_selects_nothing():
    candidates = [_candidate(1, "   "), _candidate(2, ""), _candidate(3, "\n")]
    assert select_candidates_for_verification(candidates, 6) == []


def test_discovery_order_is_preserved():
    candidates = [_candidate(3), _candidate(1), _candidate(2)]
    assert _numbers(select_candidates_for_verification(candidates, 3)) == [
        "US3B2", "US1B2", "US2B2",
    ]


# --- real fetch semantics unchanged ------------------------------------------

def test_failed_real_fetch_semantics_are_unchanged(monkeypatch):
    """A whitespace URL is skipped; a genuine fetch failure still behaves as before."""
    candidates = [_candidate(1, "   ")] + [_candidate(n) for n in range(2, 5)]
    fetched: list[str] = []

    async def fake_search(query, max_results=10, **_kw):
        return json.dumps({
            "schema_version": "patent.v1", "source_type": "patent", "status": "ok",
            "completed": True, "reliable_no_results": False, "query": query,
            "provider": "stub", "errors": [], "notes": [], "results": candidates,
        })

    async def fake_fetch(url, timeout_ms=18000, pdf_url="", *, patent_number=""):
        fetched.append(patent_number)
        if patent_number == "US3B2":
            raise RuntimeError("provider exploded")
        return (
            f"PATENT_NUMBER: {patent_number} was identified from the page.\n"
            "CLAIM1: No first claim was extracted from the page.\n"
            "ABSTRACT: A smart door lock unlocked by a mobile application.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: SEARCH_SNIPPET_ONLY\nPROVIDER: google_patents\n"
        )

    async def fake_verify(urls, max_urls=20):
        return "URL_STATUS: https://patents.google.com/patent/US2B2/en -> ok\n"

    monkeypatch.setattr(pep, "patent_search", fake_search)
    monkeypatch.setattr(pep, "patent_fetch", fake_fetch)
    monkeypatch.setattr(pep, "verify_sources", fake_verify)

    payload = json.loads(asyncio.run(pep.patent_evidence_pack(query=QUERY, max_fetches=3)))
    assert "US1B2" not in fetched, "the whitespace URL was fetched"
    assert fetched == ["US2B2", "US3B2", "US4B2"], fetched
    assert payload["status"] in {"ok", "partial_failure"}
    assert payload["hits"], "one failed fetch must not discard the other results"


# --- negative control ---------------------------------------------------------

def _truthy_only_selection(candidates, max_fetches):
    """The historical predicate: plain truthiness on item.get("url")."""
    budget = max(0, min(int(max_fetches), PATENT_VERIFICATION_CEILING))
    selected: list[tuple[int, dict]] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        selected.append((index, item))
        if len(selected) >= budget:
            break
    return selected


def test_restored_truthy_only_check_wastes_the_budget():
    candidates = [_candidate(1, "   "), _candidate(2, "\t"), _candidate(3)]
    historical = _truthy_only_selection(candidates, 2)
    assert _numbers(historical) == ["US1B2", "US2B2"], "the old check should waste both slots"
    fixed = select_candidates_for_verification(candidates, 2)
    assert _numbers(fixed) == ["US3B2"], "the fix must disagree with the old check"
