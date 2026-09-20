"""A partial publication retrieval must always carry a structured diagnostic.

The search could report partial_failure with ERROR_COUNT 0 and no ERROR marker,
because the degradation was described only in free-text notes. Downstream code
keys off parse_error_count, so a genuinely degraded search was indistinguishable
from a clean one. Partial results still keep their usable hits.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import tools.publication_evidence_pack as pub
import tools.publications_search as pubs
from tools.result_contract import (
    parse_completed_marker,
    parse_error_count,
    parse_status_marker,
)

QUERY = "smart door lock controlled by a mobile application with access codes"
SECRET = "SECRET_KEY_do_not_leak_7b31"
AUTHED_URL = f"https://api.example.com/v1/works?apiKey={SECRET}"


def _block(title: str, doi: str, abstract: str, score: float) -> str:
    return pubs._crossref_block(title, "A. Author", "2023", abstract,
                                f"https://doi.org/{doi}", score)


def _usable_blocks():
    return [
        (7.0, _block("Smart lock access control study", "10.1000/a",
                     "Smart door lock controlled by a mobile application using access codes.", 7.0)),
        (6.0, _block("Mobile credential door access", "10.1000/b",
                     "Mobile application credentials for door access with temporary codes.", 6.0)),
        (5.5, _block("Wireless valve scheduling", "10.1000/c",
                     "Wireless control unit operating a valve on a stored schedule.", 5.5)),
    ]


def _install(monkeypatch, *, crossref=None, semantic=None, openalex=None):
    """Stub the provider runners; publications_search's own logic stays real."""
    async def default_crossref(_sq, _rq, _mr, *_a, **_k):
        return _usable_blocks()

    async def default_semantic(*_a, **_k):
        return [], False, False

    async def empty(*_a, **_k):
        return []

    monkeypatch.setattr(pubs, "_crossref_blocks_safe", crossref or default_crossref)
    monkeypatch.setattr(pubs, "_semantic_scholar_blocks", semantic or default_semantic)
    monkeypatch.setattr(pubs, "_openalex_blocks_safe", openalex or empty)
    for name in ("_pubmed_blocks_safe", "_alpha_blocks_safe", "_arxiv_blocks_safe"):
        monkeypatch.setattr(pubs, name, empty)


def _search() -> str:
    return asyncio.run(pubs.publications_search(QUERY, 5))


# --- 1. provider failure with usable hits ------------------------------------

def test_provider_failure_emits_structured_error_and_keeps_hits(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("openalex transport failure")

    _install(monkeypatch, openalex=boom)
    out = _search()
    assert parse_status_marker(out) == "partial_failure"
    assert parse_completed_marker(out) is False
    assert (parse_error_count(out) or 0) >= 1, out[:300]
    assert "ERROR:" in out
    assert "Smart lock access control study" in out, "usable hits were dropped"


# --- 2. downstream propagation ------------------------------------------------

def test_evidence_pack_preserves_the_partial_diagnostic(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("openalex transport failure")

    _install(monkeypatch, openalex=boom)

    async def fake_fetch(url, timeout_s=12.0):
        return ("TITLE: Smart lock access control study\n"
                "ABSTRACT: Smart door locks controlled by a mobile application.\n"
                "STATUS: OK\nEVIDENCE_LEVEL: ABSTRACT_VERIFIED\n")

    async def fake_verify(urls, max_urls=20):
        return "URL_STATUS: https://doi.org/10.1000/a -> ok\n"

    monkeypatch.setattr(pub, "publication_fetch", fake_fetch)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    payload = json.loads(asyncio.run(pub.publication_evidence_pack(query=QUERY)))
    assert payload["status"] == "partial_failure"
    assert payload["completed"] is False
    assert payload["errors"], "partial state reached the pack with no diagnostic"
    assert payload["hits"], "usable hits were dropped downstream"
    assert payload["warnings"], "caller was not told retrieval was incomplete"


# --- 3. rate limit with fallback results --------------------------------------

def test_rate_limited_semantic_scholar_emits_a_safe_diagnostic(monkeypatch):
    async def rate_limited(*_a, **_k):
        return [], False, True

    _install(monkeypatch, semantic=rate_limited)
    out = _search()
    assert parse_status_marker(out) == "partial_failure"
    assert (parse_error_count(out) or 0) >= 1
    assert "semantic_scholar_rate_limited" in out
    assert "Smart lock access control study" in out


# --- 4. healthy search -------------------------------------------------------

def test_healthy_search_introduces_no_spurious_errors(monkeypatch):
    _install(monkeypatch)
    out = _search()
    assert parse_status_marker(out) == "ok"
    assert parse_completed_marker(out) is True
    assert (parse_error_count(out) or 0) == 0
    assert "ERROR:" not in out


# --- 5. complete failure keeps its semantics ----------------------------------

def test_total_failure_semantics_are_unchanged(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("provider down")

    async def empty(*_a, **_k):
        return []

    async def empty_semantic(*_a, **_k):
        return [], False, False

    _install(monkeypatch, crossref=boom, semantic=empty_semantic, openalex=empty)
    out = _search()
    assert parse_status_marker(out) in {"failed", "partial_failure"}
    assert parse_completed_marker(out) is False


# --- 6. legacy / malformed partial markers -------------------------------------

def test_legacy_partial_without_errors_gets_a_generic_diagnostic(monkeypatch):
    """A partial marker with ERROR_COUNT 0 must not stay unexplained."""
    legacy = (
        "STATUS: PARTIAL_FAILURE\nCOMPLETED: FALSE\nELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\nQUERY: " + QUERY + "\n\n"
        + _block("Smart lock access control study", "10.1000/a",
                "Smart door lock controlled by a mobile application using access codes.", 7.0)
    )

    async def legacy_search(*_a, **_k):
        return legacy

    async def fake_fetch(url, timeout_s=12.0):
        return ("TITLE: Smart lock access control study\n"
                "ABSTRACT: Smart door locks controlled by a mobile application.\n"
                "STATUS: OK\nEVIDENCE_LEVEL: ABSTRACT_VERIFIED\n")

    async def fake_verify(urls, max_urls=20):
        return "URL_STATUS: https://doi.org/10.1000/a -> ok\n"

    monkeypatch.setattr(pub, "publications_search", legacy_search)
    monkeypatch.setattr(pub, "publication_fetch", fake_fetch)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    payload = json.loads(asyncio.run(pub.publication_evidence_pack(query=QUERY)))
    assert payload["status"] == "partial_failure"
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["type"] == "publication_search_partial"
    assert payload["hits"], "hits must be preserved"


def test_generic_fallback_does_not_invent_a_provider_failure(monkeypatch):
    from tools.publications_search import _partial_retrieval_errors

    assert _partial_retrieval_errors([], False) == []
    only_rate = _partial_retrieval_errors([], True)
    assert [e["type"] for e in only_rate] == ["semantic_scholar_rate_limited"]


# --- 7. secret safety ---------------------------------------------------------

def test_credentials_do_not_leak_through_the_new_diagnostics(monkeypatch):
    async def leaky(*_a, **_k):
        raise RuntimeError(f"openalex request failed: {AUTHED_URL}")

    _install(monkeypatch, openalex=leaky)
    out = _search()
    assert parse_status_marker(out) == "partial_failure"
    assert (parse_error_count(out) or 0) >= 1
    assert SECRET not in out, "the credential leaked through the structured diagnostic"
    assert "apiKey" not in out


# --- 8. determinism ---------------------------------------------------------

def test_identical_outcomes_produce_identical_diagnostics(monkeypatch):
    from tools.publications_search import _partial_retrieval_errors

    provider_errors = ["openalex: transport failure", "pubmed: timeout"]
    first = _partial_retrieval_errors(provider_errors, True)
    second = _partial_retrieval_errors(list(provider_errors), True)
    assert first == second
    assert [e["type"] for e in first] == [
        "semantic_scholar_rate_limited",
        "publication_provider_error",
        "publication_provider_error",
    ]


# --- negative control ----------------------------------------------------------

def test_restored_notes_only_behaviour_fails_the_structured_assertion(monkeypatch):
    """Historical behaviour: partial status carried by notes alone."""
    async def boom(*_a, **_k):
        raise RuntimeError("openalex transport failure")

    _install(monkeypatch, openalex=boom)
    monkeypatch.setattr(pubs, "_partial_retrieval_errors", lambda *_a, **_k: [])
    out = _search()
    assert parse_status_marker(out) == "partial_failure"
    assert (parse_error_count(out) or 0) == 0, (
        "notes-only behaviour should emit no structured error"
    )
    assert "ERROR:" not in out
