"""Testy hĺbkovej analýzy zdrojov: PDF dôkazy, atom coverage a exact flag."""

from __future__ import annotations

import json

import tools.patent_evidence_pack as pep
import tools.publication_evidence_pack as pub
import tools.research_session as rs
import tools.web_evidence_pack as wep
from tools.web_search import _format_fetch_result

ATOMS = [
    {"category": "function", "label": "mobile application", "terms": ["mobile", "application"]},
    {"category": "function", "label": "access codes", "terms": ["access", "codes"]},
]

VERIFY_OK = (
    "STATUS: OK\n"
    "Verification status for URL: https://patents.google.com/patent/US1234567B2/en returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://doi.org/10.1000/xyz returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://example.com/product returned ALIVE with HTTP 200.\n"
    "Verification status for URL: https://example.com/manual.pdf returned ALIVE with HTTP 200.\n"
)


def test_web_fetch_result_includes_analysis_field():
    content = ". ".join(
        [f"Sentence about smart lock feature number {i} with mobile application" for i in range(30)]
    )
    output = _format_fetch_result("https://example.com", "2024-01-01", content, "OK", query="smart lock mobile application")
    assert "CONTENT:" in output
    assert "ANALYSIS:" in output
    analysis_line = next(line for line in output.splitlines() if line.startswith("ANALYSIS:"))
    content_line = next(line for line in output.splitlines() if line.startswith("CONTENT:"))
    assert len(analysis_line) > len(content_line)


async def test_web_pack_pdf_candidate_becomes_fetched_excerpt(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Result title returned by search: **Smart lock manual PDF**.\n"
        "Source page URL for this result: https://example.com/manual.pdf\n"
        "Local rerank score: 7.0/10.\n"
        "Short summary snippet from search: Manual describing the smart lock mobile application."
    )

    async def fake_search(query, max_results=5):
        return search_output

    async def fake_pdf_text(url, timeout_s=20.0, max_pages=25, max_bytes=15 * 1024 * 1024):
        return (
            "The smart lock is unlocked by a mobile application. "
            "Users generate temporary access codes for guests. "
            "The system stores entry history and notifies the owner."
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(wep, "web_search", fake_search)
    monkeypatch.setattr(wep, "pdf_fetch_text", fake_pdf_text)
    monkeypatch.setattr(wep, "verify_sources", fake_verify)

    payload = json.loads(
        await wep.web_evidence_pack(
            "smart lock mobile application access codes",
            atomic_requirements=ATOMS,
        )
    )
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["evidence_level"] == "fetched_excerpt"
    assert hit["atom_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True
    assert hit["relevance"] == "direct"


async def test_web_pack_pdf_extraction_failure_falls_back_to_snippet(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Result title returned by search: **Smart lock manual PDF**.\n"
        "Source page URL for this result: https://example.com/manual.pdf\n"
        "Local rerank score: 7.0/10.\n"
        "Short summary snippet from search: Manual describing the smart lock mobile application access codes."
    )

    async def fake_search(query, max_results=5):
        return search_output

    async def fake_pdf_text(url, **kwargs):
        return ""

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(wep, "web_search", fake_search)
    monkeypatch.setattr(wep, "pdf_fetch_text", fake_pdf_text)
    monkeypatch.setattr(wep, "verify_sources", fake_verify)

    payload = json.loads(await wep.web_evidence_pack("smart lock mobile application access codes"))
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["evidence_level"] == "search_snippet_only"
    assert any("snippet-backed" in warning for warning in payload["warnings"])


async def test_patent_pack_exact_candidate_from_full_claims(monkeypatch):
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
                        "snippet": "Smart door lock with app control.",
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
            "CLAIM1: 1. A smart door lock system with wireless communication.\n"
            "ABSTRACT: A smart door lock for residential use.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: CLAIM_VERIFIED\n"
            "CLAIMS_TEXT: 1. A smart door lock system with wireless communication. "
            "2. The system of claim 1 controlled by a mobile application. "
            "3. The system of claim 2 generating temporary access codes.\n"
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
            "smart door lock mobile application access codes",
            atomic_requirements=ATOMS,
        )
    )
    hit = payload["hits"][0]
    # Claim 1 samotný atomy nepokrýva; plný text nárokov áno.
    assert hit["claim_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True


async def test_publication_pack_atom_coverage_upgrades_relevance(monkeypatch):
    search_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Publication title returned by Semantic Scholar: **Access control study**.\n"
        "Publication year returned by Semantic Scholar: 2022.\n"
        "Authors listed for this publication result: Jane Doe.\n"
        "Abstract excerpt from this publication record: The lock uses a mobile application to "
        "generate temporary access codes for visitors.\n"
        "DOI URL constructed for this publication: https://doi.org/10.1000/xyz.\n"
    )

    async def fake_search(query, max_results=10, english_query=""):
        return search_output

    async def fake_fetch(url, timeout_s=12.0):
        return (
            f"SOURCE_URL: {url} was the publication page requested.\n"
            "TITLE: Access control study was extracted from the publication page.\n"
            "YEAR: 2022 was extracted from the publication page.\n"
            "ABSTRACT: The lock uses a mobile application to generate temporary access codes.\n"
            "STATUS: OK was returned for this publication fetch.\n"
            "EVIDENCE_LEVEL: ABSTRACT_VERIFIED"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pub, "publications_search", fake_search)
    monkeypatch.setattr(pub, "publication_fetch", fake_fetch)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)

    payload = json.loads(
        await pub.publication_evidence_pack(
            "smart lock mobile application access codes",
            atomic_requirements=ATOMS,
        )
    )
    hit = payload["hits"][0]
    assert hit["atom_coverage"] == 1.0
    assert hit["relevance"] == "focused"
    assert hit["exact_combination_candidate_found"] is True


async def test_writers_pass_atomic_requirements(temp_db, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_web_pack(**kwargs):
        captured.update(kwargs)
        return {
            "source_type": "web",
            "status": "ok",
            "completed": True,
            "reliable_no_results": True,
            "hits": [],
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(rs, "web_evidence_pack", fake_web_pack)
    session_id = json.loads(
        rs.research_session_start("smart door lock unlocked by a mobile application with temporary access codes")
    )["session_id"]
    rs.research_session_understand_query(session_id)
    await rs.web_evidence_to_session(session_id=session_id, query="smart door lock", attempt_no=1)
    atoms = captured.get("atomic_requirements")
    assert isinstance(atoms, list) and atoms, "writer má odovzdať atomické požiadavky z envelope"
    assert all(isinstance(atom, dict) for atom in atoms)


def test_exact_candidate_reaches_verdict_and_checklist(temp_db):
    """Exact flag z packu sa má preniesť cez SQLite až do checklistu a verdiktu."""
    session_id = json.loads(rs.research_session_start("smart lock"))["session_id"]
    pack = {
        "source_type": "web",
        "status": "ok",
        "completed": True,
        "reliable_no_results": False,
        "hits": [
            {
                "title": "Smart lock product",
                "url": "https://example.com/product",
                "evidence_level": "fetched_excerpt",
                "summary": "Covers every requested element in one document.",
                "verified_url": True,
                "relevance": "direct",
                "relevance_score": 8.0,
                "atom_coverage": 1.0,
                "atom_match_count": 2,
                "exact_combination_candidate_found": True,
            }
        ],
        "errors": [],
        "warnings": [],
    }
    response = json.loads(
        rs.research_session_save_evidence(session_id, "web", pack, query="smart lock", attempt=1)
    )
    assert response["exact_combination_candidate_found"] is True

    for source_type in ("patent", "publication"):
        rs.research_session_save_evidence(
            session_id,
            source_type,
            {
                "source_type": source_type,
                "status": "ok",
                "completed": True,
                "reliable_no_results": True,
                "hits": [],
                "errors": [],
                "warnings": [],
            },
            query="smart lock",
            attempt=1,
        )

    checklist = json.loads(rs.research_session_checklist(session_id))
    assert checklist["can_finalize"] is True
    assert checklist["stop_reason"] == "exact_combination_found"

    payload_answer = rs.research_session_user_answer(session_id, debug_mode=True)
    payload = json.loads(payload_answer)
    assert payload["verdict"] == "exact_match"
    assert payload["single_source_contains_all_critical_elements"] is True
