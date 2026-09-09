"""Tests of PDF-first patent fetching through the official Google storage PDF."""

from __future__ import annotations

import json

import tools.patent_evidence_pack as pep
import tools.patent_fetch as pf
from tools.patent_search import PATENT_PDF_BASE_URL, PatentCandidate, _google_patents_xhr_search

PDF_TEXT_WITH_CLAIMS = (
    "United States Patent US10831585B2 anomaly pattern recognition distributed systems. "
    "I claim : 1. A system for anomaly pattern recognition and root cause analysis in "
    "distributed systems comprising unsupervised online learning of event patterns and "
    "real time alert generation for an administrator. " + ("filler word " * 200)
)
PDF_TEXT_NO_CLAIMS = "Some patent front page text without any claim section marker. " + ("filler " * 250)


def test_pdf_claims_present_detection():
    assert pf._pdf_claims_present("I claim : 1. A system") is True
    assert pf._pdf_claims_present("What is claimed is: 1. A method") is True
    assert pf._pdf_claims_present("The invention claimed is: 1.") is True
    assert pf._pdf_claims_present("random front page text only") is False


def test_pdf_fields_emit_coverage_tokens_and_claim_level():
    out = pf._pdf_fields(
        "https://patents.google.com/patent/US10831585B2/en",
        "https://patentimages.storage.googleapis.com/x/US10831585.pdf",
        PDF_TEXT_WITH_CLAIMS,
        [],
    )
    assert "EVIDENCE_LEVEL: CLAIM_VERIFIED" in out
    assert "PDF_CLAIMS_SECTION: present" in out
    assert "COVERAGE_TOKENS:" in out
    assert "PROVIDER: google_patents_pdf" in out
    coverage_line = next(l for l in out.splitlines() if l.startswith("COVERAGE_TOKENS:"))
    assert "unsupervised" in coverage_line
    assert "administrator" in coverage_line


def test_pdf_fields_without_claims_is_abstract_level():
    out = pf._pdf_fields("https://x/patent", "https://y.pdf", PDF_TEXT_NO_CLAIMS, [])
    assert "EVIDENCE_LEVEL: ABSTRACT_VERIFIED" in out
    assert "PDF_CLAIMS_SECTION: absent" in out


def test_pdf_fields_use_sentinels_so_meta_text_never_pollutes_coverage():
    """Meta sentences about the PDF must enter neither the coverage computation nor the summary."""
    out = pf._pdf_fields("https://x/patent", "https://y.pdf", PDF_TEXT_WITH_CLAIMS, [])
    claim_line = next(l for l in out.splitlines() if l.startswith("CLAIM1:"))
    abstract_line = next(l for l in out.splitlines() if l.startswith("ABSTRACT:"))
    # The existing filters in patent_evidence_pack exclude exactly these sentinels.
    assert "No first claim" in claim_line
    assert "No abstract section" in abstract_line
    # And they genuinely do not reach the displayed summary.
    assert pep._fetch_summary(out) == ""


async def test_patent_fetch_prefers_pdf_and_skips_html(monkeypatch):
    html_called = False

    async def fake_html(url, timeout_ms=60000):
        nonlocal html_called
        html_called = True
        return "<html></html>", ""

    async def fake_pdf(url, timeout_s=20.0, max_pages=25, max_bytes=15 * 1024 * 1024):
        return PDF_TEXT_WITH_CLAIMS

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_html)
    monkeypatch.setattr(pf, "pdf_fetch_text", fake_pdf)
    pf._PATENT_FETCH_CACHE.clear()

    out = await pf.patent_fetch(
        "https://patents.google.com/patent/US10831585B2/en",
        timeout_ms=5000,
        pdf_url="https://patentimages.storage.googleapis.com/x/US10831585.pdf",
    )
    assert "EVIDENCE_LEVEL: CLAIM_VERIFIED" in out
    assert html_called is False, "when a PDF is available the blocked HTML page must not be fetched"


async def test_patent_fetch_falls_back_to_html_when_pdf_empty(monkeypatch):
    async def fake_html(url, timeout_ms=60000):
        return (
            "<html><head><title>US1 - X</title></head><body>"
            "<section itemprop='claims'><claim><div class='claim-text'>1. A system comprising "
            "an anomaly detector and an administrator alert unit for logs.</div></claim></section>"
            "</body></html>"
        ), "rendered"

    async def fake_pdf(url, **kwargs):
        return ""

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_html)
    monkeypatch.setattr(pf, "pdf_fetch_text", fake_pdf)
    pf._PATENT_FETCH_CACHE.clear()

    out = await pf.patent_fetch("https://patents.google.com/patent/US1/en", timeout_ms=5000, pdf_url="https://x.pdf")
    assert "EVIDENCE_LEVEL: CLAIM_VERIFIED" in out
    assert "anomaly detector" in out
    log = json.loads(next(l for l in out.splitlines() if l.startswith("ATTEMPT_LOG_JSON")).split(": ", 1)[1])
    assert log[0]["provider"] == "google_patents_pdf"
    assert log[0]["status"] == "empty"


async def test_patent_fetch_falls_back_to_html_when_pdf_raises(monkeypatch):
    async def fake_html(url, timeout_ms=60000):
        return "<html><head><title>US2 - Y</title></head><body><p>text</p></body></html>", "rendered"

    async def fake_pdf(url, **kwargs):
        raise RuntimeError("pdf host unreachable")

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_html)
    monkeypatch.setattr(pf, "pdf_fetch_text", fake_pdf)
    pf._PATENT_FETCH_CACHE.clear()

    out = await pf.patent_fetch("https://patents.google.com/patent/US2/en", timeout_ms=5000, pdf_url="https://x.pdf")
    log = json.loads(next(l for l in out.splitlines() if l.startswith("ATTEMPT_LOG_JSON")).split(": ", 1)[1])
    assert log[0]["status"] == "failed"


async def test_patent_fetch_rejects_too_short_pdf_text(monkeypatch):
    """Short text, a cover page for instance, does not count as the full document."""

    async def fake_html(url, timeout_ms=60000):
        return "<html><head><title>US3</title></head><body><p>x</p></body></html>", ""

    async def fake_pdf(url, **kwargs):
        return "only a handful of words here"

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_html)
    monkeypatch.setattr(pf, "pdf_fetch_text", fake_pdf)
    pf._PATENT_FETCH_CACHE.clear()

    out = await pf.patent_fetch("https://patents.google.com/patent/US3/en", timeout_ms=5000, pdf_url="https://x.pdf")
    assert "PDF_CLAIMS_SECTION" not in out


# --- patent_search: capturing the PDF path and metadata ----------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, params=None, headers=None):
        return _FakeResponse(self._payload)


async def test_xhr_search_captures_pdf_path_and_metadata():
    payload = {
        "results": {
            "cluster": [
                {
                    "result": [
                        {
                            "patent": {
                                "publication_number": "US10831585B2",
                                "title": "Online unsupervised event pattern learning",
                                "snippet": "anomaly detection in distributed systems",
                                "pdf": "30/12/f3/1b616ac6c5a32a/US10831585.pdf",
                                "assignee": "Xiaohui Gu",
                                "filing_date": "2018-03-27",
                                "grant_date": "2020-11-10",
                            }
                        }
                    ]
                }
            ]
        }
    }
    candidates = await _google_patents_xhr_search(_FakeClient(payload), "anomaly logs", 10)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.pdf_url == PATENT_PDF_BASE_URL + "30/12/f3/1b616ac6c5a32a/US10831585.pdf"
    assert c.assignee == "Xiaohui Gu"
    assert c.filing_date == "2018-03-27"
    assert c.grant_date == "2020-11-10"


async def test_xhr_search_without_pdf_field_yields_empty_pdf_url():
    payload = {"results": {"cluster": [{"result": [{"patent": {"publication_number": "US1", "title": "t"}}]}]}}
    candidates = await _google_patents_xhr_search(_FakeClient(payload), "q", 10)
    assert candidates[0].pdf_url == ""


def test_non_xhr_providers_default_to_empty_pdf_url():
    candidate = PatentCandidate(title="t", url="https://x", patent_number="US1", snippet="s")
    assert candidate.pdf_url == ""
    assert candidate.assignee == ""


# --- evidence pack: pdf_url passes through and coverage tokens are used ------

async def test_evidence_pack_passes_pdf_url_and_uses_coverage_tokens(monkeypatch):
    seen_pdf_urls = []

    async def fake_search(query, max_results=10):
        return json.dumps(
            {
                "status": "ok",
                "completed": True,
                "reliable_no_results": False,
                "query": query,
                "provider": "google_patents_xhr",
                "errors": [],
                "notes": [],
                "results": [
                    {
                        "title": "Anomaly detection patent",
                        "url": "https://patents.google.com/patent/US10831585B2/en",
                        "patent_number": "US10831585B2",
                        "snippet": "anomaly detection",
                        "score": 7.0,
                        "pdf_url": "https://patentimages.storage.googleapis.com/x/US10831585.pdf",
                    }
                ],
            }
        )

    async def fake_fetch(url, timeout_ms=18000, pdf_url=""):
        seen_pdf_urls.append(pdf_url)
        return pf._pdf_fields(url, pdf_url, PDF_TEXT_WITH_CLAIMS, [])

    async def fake_verify(urls, max_urls=20):
        return (
            "STATUS: OK\nVerification status for URL: "
            "https://patents.google.com/patent/US10831585B2/en returned ALIVE with HTTP 200.\n"
        )

    monkeypatch.setattr(pep, "patent_search", fake_search)
    monkeypatch.setattr(pep, "patent_fetch", fake_fetch)
    monkeypatch.setattr(pep, "verify_sources", fake_verify)

    atoms = [
        {"category": "function", "label": "unsupervised learning", "terms": ["unsupervised", "learning"]},
        {"category": "function", "label": "administrator alert", "terms": ["administrator", "alert"]},
    ]
    payload = json.loads(
        await pep.patent_evidence_pack("anomaly detection logs", atomic_requirements=atoms)
    )
    assert seen_pdf_urls == ["https://patentimages.storage.googleapis.com/x/US10831585.pdf"]
    hit = payload["hits"][0]
    assert hit["evidence_level"] == "claim_verified"
    # Coverage is computed from the whole PDF's tokens, not from the snippet.
    assert hit["claim_coverage"] == 1.0
    assert hit["exact_combination_candidate_found"] is True
