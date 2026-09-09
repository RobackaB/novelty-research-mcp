"""Tests of requirement coverage from whole documents: web tokens, patent descriptions, publication PDFs."""

from __future__ import annotations

import json

import tools.patent_evidence_pack as pep
import tools.publication_evidence_pack as pub
import tools.web_evidence_pack as wep
from tools.publication_evidence_pack import PublicationCandidate, _direct_pdf_url, _resolve_pdf_url
from tools.requirement_match import unique_coverage_tokens
from tools.web_search import _format_fetch_result

ATOMS = [
    {"category": "function", "label": "mobile application", "terms": ["mobile", "application"]},
    {"category": "function", "label": "entry history", "terms": ["entry", "history"]},
]

VERIFY_OK = (
    "STATUS: OK\n"
    "Verification status for URL: https://patents.google.com/patent/US1234567B2/en returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://arxiv.org/abs/2301.00001 returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://example.com/product returned ALIVE with HTTP 200.\n"
)


# --- unique_coverage_tokens ---------------------------------------------------

def test_unique_coverage_tokens_dedupes_and_caps():
    text = "lock lock lock mobile mobile application entry history " * 10
    tokens_line = unique_coverage_tokens(text)
    assert tokens_line.split() == ["lock", "mobile", "application", "entry", "history"]
    capped = unique_coverage_tokens("a1 b2 c3 d4 e5", cap=3)
    assert len(capped.split()) == 3


def test_unique_coverage_tokens_empty():
    assert unique_coverage_tokens("") == ""
    assert unique_coverage_tokens(None) == ""


# --- Web: COVERAGE_TOKENS from the whole page --------------------------------

def test_fetch_result_coverage_tokens_include_offtopic_sentences():
    # The sentence with "entry history" has no overlap with the query, so it
    # reaches neither CONTENT nor ANALYSIS. The whole-page COVERAGE_TOKENS still
    # contain it.
    content = (
        ". ".join(f"Smart lock paragraph about the mobile application number {i}" for i in range(30))
        + ". The device also keeps entry history records."
    )
    output = _format_fetch_result(
        "https://example.com", "2024-01-01", content, "OK", query="smart lock mobile application"
    )
    coverage_line = next(line for line in output.splitlines() if line.startswith("COVERAGE_TOKENS:"))
    assert "history" in coverage_line
    analysis_lines = [line for line in output.splitlines() if line.startswith(("CONTENT:", "ANALYSIS:"))]
    assert all("history" not in line for line in analysis_lines)


async def test_web_pack_coverage_from_full_page_tokens(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Result title returned by search: **Smart lock product**.\n"
        "Source page URL for this result: https://example.com/product\n"
        "Local rerank score: 7.0/10.\n"
        "Short summary snippet from search: Smart lock with mobile application."
    )

    async def fake_search(query, max_results=5):
        return search_output

    async def fake_fetch(url, timeout_ms=14000, query=""):
        # CONTENT/ANALYSIS do not contain "entry history"; COVERAGE_TOKENS do.
        return (
            f"SOURCE_URL: {url} was the page requested.\n"
            "DATE: Unknown was the best date found.\n"
            "CONTENT: The smart lock is unlocked by a mobile application on your phone daily.\n"
            "STATUS: OK\n"
            "EVIDENCE_LEVEL: FULLTEXT_VERIFIED\n"
            "COVERAGE_TOKENS: smart lock mobile application entry history records"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(wep, "web_search", fake_search)
    monkeypatch.setattr(wep, "web_fetch", fake_fetch)
    monkeypatch.setattr(wep, "verify_sources", fake_verify)

    payload = json.loads(
        await wep.web_evidence_pack(
            "smart lock mobile application entry history", atomic_requirements=ATOMS
        )
    )
    hit = payload["hits"][0]
    assert hit["atom_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True


# --- Patenty: description sekcia ---------------------------------------------

async def test_patent_pack_coverage_from_description(monkeypatch):
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
                        "snippet": "Smart door lock.",
                        "score": 7.0,
                    }
                ],
            }
        )

    async def fake_fetch(url, timeout_ms=18000, pdf_url=""):
        # The claims cover only "mobile application"; "entry history" is in the description alone.
        return (
            "PATENT_NUMBER: US1234567B2 was identified from the page.\n"
            "FILED: 2020-01-01 was identified on the page.\n"
            "ASSIGNEE: ACME was identified on the page.\n"
            "CLAIM1: 1. A smart door lock controlled by a mobile application.\n"
            "ABSTRACT: A smart door lock for residential use.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: CLAIM_VERIFIED\n"
            "DESCRIPTION_TEXT: In one embodiment the controller stores an entry history log "
            "of every unlocking event for later review by the owner of the premises.\n"
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
            "smart door lock mobile application entry history",
            atomic_requirements=ATOMS,
        )
    )
    hit = payload["hits"][0]
    assert hit["claim_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True


def test_patent_fetch_extracts_description_field():
    from tools.patent_fetch import _extract_fields

    html = (
        "<html><head><title>US1234567B2 - Smart lock</title></head><body>"
        "<section itemprop='abstract'><div>A smart door lock unlocked by a mobile application "
        "with several additional safety functions included.</div></section>"
        "<section itemprop='claims'><claim><div class='claim-text'>1. A smart door lock system "
        "comprising a mobile application interface for remote control.</div></claim></section>"
        "<section itemprop='description'><p>" + " ".join(
            f"The described embodiment number {i} stores an entry history log." for i in range(10)
        ) + "</p></section>"
        "</body></html>"
    )
    output = _extract_fields(
        "https://patents.google.com/patent/US1234567B2/en", html, "", "google_patents", []
    )
    assert "DESCRIPTION_TEXT:" in output
    description_line = next(line for line in output.splitlines() if line.startswith("DESCRIPTION_TEXT:"))
    assert "entry history" in description_line


# --- Publications: full PDF texts ---------------------------------------------

def test_direct_pdf_url_for_candidate():
    arxiv = PublicationCandidate(title="t", url="https://arxiv.org/abs/2301.00001", doi="", summary="")
    assert _direct_pdf_url(arxiv) == "https://arxiv.org/pdf/2301.00001"
    direct = PublicationCandidate(title="t", url="https://example.com/paper.PDF?download=1", doi="", summary="")
    assert _direct_pdf_url(direct) == "https://example.com/paper.PDF?download=1"
    doi = PublicationCandidate(title="t", url="https://doi.org/10.1000/xyz", doi="10.1000/xyz", summary="")
    assert _direct_pdf_url(doi) == ""


async def test_resolve_pdf_url_prefers_direct_over_unpaywall(monkeypatch):
    async def fake_unpaywall(doi):
        raise AssertionError("Unpaywall should not be called when a direct PDF URL exists")

    monkeypatch.setattr(pub, "_unpaywall_pdf_url", fake_unpaywall)
    arxiv = PublicationCandidate(title="t", url="https://arxiv.org/abs/2301.00001", doi="", summary="")
    assert await _resolve_pdf_url(arxiv) == "https://arxiv.org/pdf/2301.00001"


async def test_resolve_pdf_url_falls_back_to_unpaywall_for_doi(monkeypatch):
    async def fake_unpaywall(doi):
        assert doi == "10.1000/xyz"
        return "https://example.com/open-access.pdf"

    monkeypatch.setattr(pub, "_unpaywall_pdf_url", fake_unpaywall)
    doi_candidate = PublicationCandidate(title="t", url="https://doi.org/10.1000/xyz", doi="10.1000/xyz", summary="")
    assert await _resolve_pdf_url(doi_candidate) == "https://example.com/open-access.pdf"


async def test_resolve_pdf_url_empty_without_doi_or_direct():
    candidate = PublicationCandidate(title="t", url="https://example.com/landing-page", doi="", summary="")
    assert await _resolve_pdf_url(candidate) == ""


async def test_unpaywall_pdf_url_closed_access(monkeypatch):
    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"is_oa": False, "best_oa_location": None}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr(pub.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    assert await pub._unpaywall_pdf_url("10.1000/closed") == ""


async def test_unpaywall_pdf_url_open_access(monkeypatch):
    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"is_oa": True, "best_oa_location": {"url_for_pdf": "https://example.com/oa.pdf"}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr(pub.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    assert await pub._unpaywall_pdf_url("10.1000/open") == "https://example.com/oa.pdf"


async def test_unpaywall_pdf_url_empty_doi_short_circuits():
    assert await pub._unpaywall_pdf_url("") == ""


async def test_unpaywall_pdf_url_network_error_returns_empty(monkeypatch):
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(pub.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    assert await pub._unpaywall_pdf_url("10.1000/err") == ""


async def test_publication_pack_fulltext_pdf_coverage(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Publication title returned by ArXiv: **Smart lock forensics**.\n"
        "Publication year returned by ArXiv: 2023.\n"
        "Authors listed for this publication result: Jane Doe.\n"
        "Abstract excerpt from this publication record: Mobile application security of smart locks.\n"
        "ArXiv URL for this publication: https://arxiv.org/abs/2301.00001.\n"
        "SOURCE: ArXiv"
    )

    async def fake_search(query, max_results=10, english_query=""):
        return search_output

    async def fake_pub_fetch(url, timeout_s=12.0):
        return "TOOL_ERROR: publication_fetch\nREASON: blocked\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"

    async def fake_pdf_text(url, timeout_s=20.0, max_pages=25, max_bytes=15 * 1024 * 1024):
        assert url == "https://arxiv.org/pdf/2301.00001"
        return (
            "The full paper analyzes smart locks controlled by a mobile application. "
            "Section 4 evaluates how the entry history log can be extracted forensically."
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pub, "publications_search", fake_search)
    monkeypatch.setattr(pub, "publication_fetch", fake_pub_fetch)
    monkeypatch.setattr(pub, "pdf_fetch_text", fake_pdf_text)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    payload = json.loads(
        await pub.publication_evidence_pack(
            "smart lock mobile application entry history",
            atomic_requirements=ATOMS,
        )
    )
    hit = payload["hits"][0]
    assert hit["fulltext_analyzed"] is True
    assert hit["fulltext_word_count"] > 10
    # A failed page fetch would give fetch_failed; the full text raises it to fetched_excerpt.
    assert hit["evidence_level"] == "fetched_excerpt"
    assert hit["atom_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True


async def test_publication_pack_no_pdf_targets_without_atoms(monkeypatch):
    """Without atomic requirements no PDF texts are downloaded, which saves a run."""
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Publication title returned by ArXiv: **Smart lock forensics**.\n"
        "Abstract excerpt from this publication record: Mobile application security of smart locks.\n"
        "ArXiv URL for this publication: https://arxiv.org/abs/2301.00001.\n"
        "SOURCE: ArXiv"
    )

    called = {"pdf": False}

    async def fake_search(query, max_results=10, english_query=""):
        return search_output

    async def fake_pub_fetch(url, timeout_s=12.0):
        return "TOOL_ERROR: publication_fetch\nREASON: blocked\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"

    async def fake_pdf_text(url, **kwargs):
        called["pdf"] = True
        return "text"

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pub, "publications_search", fake_search)
    monkeypatch.setattr(pub, "publication_fetch", fake_pub_fetch)
    monkeypatch.setattr(pub, "pdf_fetch_text", fake_pdf_text)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    await pub.publication_evidence_pack("smart lock")
    assert called["pdf"] is False
