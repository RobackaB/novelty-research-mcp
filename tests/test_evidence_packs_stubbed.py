"""Tests of the evidence packs with the network functions stubbed out."""

from __future__ import annotations

import json

import tools.patent_evidence_pack as pep
import tools.publication_evidence_pack as pub
import tools.web_evidence_pack as wep

VERIFY_OK = (
    "STATUS: OK\n"
    "Verification status for URL: https://patents.google.com/patent/US1234567B2/en returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://doi.org/10.1000/xyz returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://example.com/product returned ALIVE with HTTP 200.\n"
)


async def test_patent_evidence_pack_stubbed(monkeypatch):
    async def fake_search(query, max_results=10):
        return json.dumps(
            {
                "status": "ok",
                "completed": True,
                "reliable_no_results": False,
                "query": query,
                "provider": "stub",
                "errors": [],
                "notes": [],
                "results": [
                    {
                        "title": "US1234567B2 - Smart door lock",
                        "url": "https://patents.google.com/patent/US1234567B2/en",
                        "patent_number": "US1234567B2",
                        "snippet": "Smart door lock unlocked by a mobile application with temporary access codes.",
                        "score": 7.0,
                    }
                ],
            }
        )

    async def fake_fetch(url, timeout_ms=18000, pdf_url=""):
        return (
            "PATENT_NUMBER: US1234567B2 was identified from the page.\n"
            "FILED: 2020-01-01 was identified on the page.\n"
            "ASSIGNEE: ACME was identified on the page.\n"
            "CLAIM1: 1. A smart door lock system comprising a mobile application, "
            "temporary access codes and an entry history log.\n"
            "ABSTRACT: A smart door lock unlocked by a mobile application.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: CLAIM_VERIFIED\n"
            "PROVIDER: google_patents\n"
            'ATTEMPT_LOG_JSON: [{"provider":"google_patents","attempt":1,"status":"ok","elapsed_ms":10}]'
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pep, "patent_search", fake_search)
    monkeypatch.setattr(pep, "patent_fetch", fake_fetch)
    monkeypatch.setattr(pep, "verify_sources", fake_verify)

    payload = json.loads(
        await pep.patent_evidence_pack(
            "smart door lock mobile application temporary access codes",
            atomic_requirements=[
                {"category": "function", "label": "mobile application", "terms": ["mobile", "application"]},
                {"category": "function", "label": "access codes", "terms": ["access", "codes"]},
            ],
        )
    )
    assert payload["source_type"] == "patent"
    assert payload["status"] == "ok"
    assert payload["completed"] is True
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["patent_number"] == "US1234567B2"
    assert hit["evidence_level"] == "claim_verified"
    assert hit["verified_url"] is True
    assert hit["claim_coverage"] == 1.0
    assert hit["relevance"] == "focused"


async def test_patent_evidence_pack_search_failure(monkeypatch):
    async def fake_search(query, max_results=10):
        raise RuntimeError("network down")

    monkeypatch.setattr(pep, "patent_search", fake_search)
    payload = json.loads(await pep.patent_evidence_pack("query"))
    assert payload["status"] == "failed"
    assert payload["errors"][0]["type"] == "patent_search_failed"


async def test_publication_evidence_pack_stubbed(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Publication title returned by Semantic Scholar: **Smart door locks with mobile applications**.\n"
        "Publication year returned by Semantic Scholar: 2022.\n"
        "Authors listed for this publication result: Jane Doe.\n"
        "Abstract excerpt from this publication record: A smart door lock system using a mobile "
        "application with temporary access codes and entry history logging.\n"
        "DOI URL constructed for this publication: https://doi.org/10.1000/xyz.\n"
        "Semantic Scholar URL for this publication: https://www.semanticscholar.org/paper/abc.\n"
    )

    async def fake_search(query, max_results=10, english_query=""):
        return search_output

    async def fake_fetch(url, timeout_s=12.0):
        return (
            f"SOURCE_URL: {url} was the publication page requested.\n"
            "TITLE: Smart door locks with mobile applications was extracted from the publication page.\n"
            "YEAR: 2022 was extracted from the publication page.\n"
            "ABSTRACT: A smart door lock with app control and codes.\n"
            "STATUS: OK was returned for this publication fetch.\n"
            "EVIDENCE_LEVEL: ABSTRACT_VERIFIED"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pub, "publications_search", fake_search)
    monkeypatch.setattr(pub, "publication_fetch", fake_fetch)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    payload = json.loads(
        await pub.publication_evidence_pack("smart door lock mobile application access codes")
    )
    assert payload["source_type"] == "publication"
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["evidence_level"] == "abstract_verified"
    assert hit["doi"] == "10.1000/xyz"
    assert hit["verified_url"] is True
    assert payload["status"] in {"ok", "partial_failure"}


async def test_web_evidence_pack_stubbed(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "WEB_SEARCH_PROVIDER: stub\n\n"
        "Result title returned by search: **Smart door lock with mobile app**.\n"
        "Source page URL for this result: https://example.com/product\n"
        "Local rerank score: 7.5/10.\n"
        "Short summary snippet from search: Smart door lock unlocked by mobile application with "
        "temporary access codes, entry history and owner notification, model number SL-100."
    )

    async def fake_search(query, max_results=5):
        return search_output

    async def fake_fetch(url, timeout_ms=14000, query=""):
        return (
            f"SOURCE_URL: {url} was the page requested.\n"
            "DATE: 2023-01-01 was the best date found.\n"
            "CONTENT: The SL-100 smart door lock is unlocked by a mobile application, supports "
            "temporary access codes, logs entry history and notifies the owner on opening. "
            "Released 2023 with firmware version v2.1 documented in the manual.\n"
            "STATUS: OK\n"
            "EVIDENCE_LEVEL: FULLTEXT_VERIFIED"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(wep, "web_search", fake_search)
    monkeypatch.setattr(wep, "web_fetch", fake_fetch)
    monkeypatch.setattr(wep, "verify_sources", fake_verify)

    payload = json.loads(
        await wep.web_evidence_pack("smart door lock mobile application temporary access codes entry history")
    )
    assert payload["source_type"] == "web"
    assert payload["status"] == "ok"
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["evidence_level"] == "fetched_excerpt"
    assert hit["verified_url"] is True
    assert hit["relevance"] in {"direct", "adjacent"}


async def test_web_evidence_pack_failed_search(monkeypatch):
    async def fake_search(query, max_results=5):
        return "STATUS: FAILED\nCOMPLETED: FALSE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 1\nERROR: x - y"

    monkeypatch.setattr(wep, "web_search", fake_search)
    payload = json.loads(await wep.web_evidence_pack("query"))
    assert payload["status"] == "failed"
    assert payload["hits"] == []
