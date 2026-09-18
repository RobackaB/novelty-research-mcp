"""Offline fetch-chain integration and defect-restoration controls."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

import tools.patent_fetch as pf
import tools.patent_identity as identity
import tools.pdf_fetch as pdf
from tools.patent_safety import redact_patent_text

NUMBER = "US10762444B2"
URL = f"https://patents.google.com/patent/{NUMBER}/en"
PDF = "https://patentimages.storage.googleapis.com/aa/bb/US10762444.pdf"
PARAGRAPH = (
    "An irrigation controller measures moisture in garden soil and sends readings "
    "wirelessly to a valve controller that skips scheduled watering when the soil "
    "remains sufficiently wet and allows the schedule to be updated remotely. "
)
EXCERPT = f"United States Patent {NUMBER}\n" + PARAGRAPH * 7
ABSTRACT = f"United States Patent {NUMBER}\nAbstract\n" + PARAGRAPH * 7
CLAIMS = f"United States Patent {NUMBER}\nWhat is claimed is:\n1. " + PARAGRAPH * 7
BLOCK = "Verify you are human. CAPTCHA challenge. " + PARAGRAPH * 7


def html_document(kind="abstract", number=NUMBER):
    section = f"<section itemprop='{kind}'>" + ("1. " if kind == "claims" else "") + PARAGRAPH * 7 + "</section>"
    return f"<html><head><title>{number} - Irrigation controller</title></head><body>{section}</body></html>", ""


@pytest.fixture
def chain(monkeypatch):
    """Stub only external backends; retain orchestration and all evidence gates."""
    responses, calls = {}, []
    pf._PATENT_FETCH_CACHE.clear()

    async def invoke(name):
        calls.append(name)
        response = responses.get(name, ("", "") if name in {"http", "browser", "archive"} else "")
        if isinstance(response, Exception):
            raise response
        return response

    async def direct_pdf(*args, **kwargs):
        return await invoke("pdf")

    async def reader(target, **kwargs):
        return await invoke("reader_pdf" if target == PDF else "reader_html")

    async def direct(*args):
        return await invoke("http")

    async def browser(*args):
        return await invoke("browser")

    async def archive(*args):
        return await invoke("archive")

    monkeypatch.setattr(pf, "_patent_pdf_fetch", direct_pdf)
    monkeypatch.setattr(pf, "fetch_via_jina", reader)
    monkeypatch.setattr(pf, "_wayback_patent_fetch", archive)
    monkeypatch.setattr(pf, "PATENT_FETCH_PROVIDERS", (
        pf.PatentFetchProvider("google_patents_http", direct),
        pf.PatentFetchProvider("google_patents", browser),
    ))
    return responses, calls


async def run():
    return await pf.patent_fetch(URL, timeout_ms=5000, pdf_url=PDF)


def log(output):
    line = next(line for line in output.splitlines() if line.startswith("ATTEMPT_LOG_JSON:"))
    return json.loads(line.partition(":")[2])


@pytest.mark.parametrize("earlier", ["pdf", "reader_pdf", "http", "browser"])
async def test_earlier_abstract_does_not_prevent_later_claim_upgrade(chain, earlier):
    responses, calls = chain
    responses[earlier] = html_document() if earlier in {"http", "browser"} else ABSTRACT
    responses["reader_html"] = CLAIMS
    output = await run()
    assert pf._evidence_level_of(output) == "claim_verified"
    assert "What is claimed is" in output
    assert "reader_html" in calls
    assert "archive" not in calls


async def test_whole_chain_order_and_no_downgrade_to_excerpt(chain):
    responses, calls = chain
    responses.update(pdf=ABSTRACT, reader_pdf=EXCERPT, http=html_document("description"),
                     browser=html_document("description"), reader_html=EXCERPT)
    output = await run()
    assert calls == ["pdf", "reader_pdf", "http", "browser", "reader_html", "archive"]
    assert pf._evidence_level_of(output) == "abstract_verified"
    assert "PROVIDER: google_patents_pdf\n" in output
    assert len(log(output)) == 6


async def test_verified_pdf_claims_are_maximal_and_stop_chain(chain):
    responses, calls = chain
    responses["pdf"] = CLAIMS
    assert pf._evidence_level_of(await run()) == "claim_verified"
    assert calls == ["pdf"]


@pytest.mark.parametrize("failure", [pf.BotBlockedError("blocked"), httpx.ReadTimeout("timeout"), RuntimeError("failed")])
async def test_archive_is_reached_after_every_live_backend_failure(chain, failure):
    responses, calls = chain
    responses.update(http=failure, browser=failure, reader_html=failure, archive=html_document("claims"))
    output = await run()
    assert pf._evidence_level_of(output) == "claim_verified"
    assert "PROVIDER: wayback" in output
    assert calls[-1] == "archive"
    assert "irrigation controller" in output


async def test_google_blocks_and_reader_html_recovers_exact_document(chain):
    responses, _ = chain
    responses.update(http=(BLOCK, BLOCK), browser=(BLOCK, BLOCK), reader_html=CLAIMS)
    output = await run()
    assert pf._evidence_level_of(output) == "claim_verified"
    assert [entry["status"] for entry in log(output) if entry["provider"] in {"google_patents_http", "google_patents"}] == ["blocked", "blocked"]


@pytest.mark.parametrize("backend", ["pdf", "reader_pdf", "http", "browser", "reader_html", "archive"])
async def test_wrong_kind_publication_is_rejected_in_every_backend(chain, backend):
    responses, _ = chain
    responses[backend] = html_document("claims", NUMBER.replace("B2", "A1")) if backend in {"http", "browser", "archive"} else CLAIMS.replace("B2", "A1")
    output = await run()
    assert pf._evidence_level_of(output) == "fetch_failed"
    assert "COVERAGE_TOKENS" not in output


@pytest.mark.parametrize("number", ["US 10,762,444 B2", "US-10762444-B2", "us10762444b2"])
async def test_same_publication_formatting_is_accepted_from_content(chain, number):
    responses, _ = chain
    responses["reader_html"] = CLAIMS.replace(NUMBER, number)
    assert pf._evidence_level_of(await run()) == "claim_verified"


@pytest.mark.parametrize("text", [
    EXCERPT + "The applicant claims priority from an earlier application.",
    EXCERPT + "\n1. " + PARAGRAPH,  # unlabelled numbered prose
    EXCERPT + "\nClaims\nDescription\n" + PARAGRAPH * 3,
    EXCERPT + "\nAbstract\nDescription\n" + PARAGRAPH * 3,
])
async def test_unstructured_pdf_text_is_only_fetched_excerpt(chain, text):
    responses, _ = chain
    responses["pdf"] = text
    output = await run()
    assert pf._evidence_level_of(output) == "fetched_excerpt"
    assert "CONTENT:" in output
    assert "ABSTRACT: No abstract" in output
    assert "CLAIM1: No first claim" in output


async def test_reader_markdown_abstract_survives_heading_cleaning(chain):
    responses, _ = chain
    responses["reader_html"] = f"Title: Example\nURL Source: {URL}\nMarkdown Content:\n" + ABSTRACT.replace("Abstract", "## Abstract")
    output = await run()
    assert pf._evidence_level_of(output) == "abstract_verified"
    assert "ABSTRACT: An irrigation controller" in output


async def test_reader_request_url_and_citation_cannot_establish_identity(chain):
    responses, _ = chain
    responses["reader_html"] = f"Title: {NUMBER}\nURL Source: {URL}\nMarkdown Content:\n" + CLAIMS.replace(NUMBER, "US9111111B1") + f"\nReferences cited: {NUMBER}"
    assert pf._evidence_level_of(await run()) == "fetch_failed"


async def test_later_backend_error_retains_earlier_abstract_and_safe_diagnostics(chain, monkeypatch, caplog):
    responses, _ = chain
    secret = "SENTINEL_ERROR_CREDENTIAL"
    monkeypatch.setenv("JINA_API_KEY", secret)
    responses.update(pdf=ABSTRACT, http=RuntimeError(f"Bearer {secret}"),
                     browser=RuntimeError(secret), reader_html=RuntimeError(secret), archive=RuntimeError(secret))
    with caplog.at_level(logging.INFO):
        output = await run()
    assert pf._evidence_level_of(output) == "abstract_verified"
    assert secret not in output + caplog.text
    assert any(entry.get("error_type") == "RuntimeError" for entry in log(output))


async def test_authenticated_target_is_not_forwarded_to_reader_or_archive(chain):
    _, calls = chain
    output = await pf.patent_fetch(URL + "?token=SENTINEL_URL_TOKEN", 5000)
    assert "SENTINEL" not in output
    assert calls == []


@pytest.mark.parametrize("text", [
    '{"api_key": "SENTINEL_JSON_SECRET"}',
    "https://example.org/patent?auth=SENTINEL_AUTH_SECRET",
    "https://example.org/patent?sig=SENTINEL_SIGNATURE_SECRET",
    "Authorization: Bearer SENTINEL_BEARER_SECRET",
    "https://r.jina.ai/https://user:SENTINEL_PASSWORD@example.org/patent",
    "https://example.org/path?redirect=https%3A%2F%2Fuser%3ASENTINEL_PASSWORD%40elsewhere.org",
])
def test_unconfigured_credentials_are_syntax_redacted(text):
    assert "SENTINEL" not in redact_patent_text(text)


async def test_pdf_transport_exception_is_not_logged_with_url_or_secret(monkeypatch, caplog):
    secret = "SENTINEL_PDF_ERROR_SECRET"
    real_client = httpx.AsyncClient

    def handler(request):
        raise RuntimeError(f"Bearer {secret} https://user:{secret}@example.org/document")

    monkeypatch.setattr(pdf.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with caplog.at_level(logging.INFO):
        assert await pdf.pdf_fetch_text("https://example.org/document.pdf") == ""
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


async def test_negative_control_later_weak_result_would_downgrade(chain, monkeypatch):
    responses, _ = chain
    responses.update(pdf=ABSTRACT, reader_html=EXCERPT)
    monkeypatch.setattr(pf, "promote_evidence_level", lambda current, candidate: candidate)
    with pytest.raises(AssertionError):
        assert pf._evidence_level_of(await run()) == "abstract_verified"


async def test_negative_control_legacy_pdf_abstract_label_is_caught(chain, monkeypatch):
    responses, _ = chain
    responses["pdf"] = EXCERPT
    original = pf._pdf_fields
    monkeypatch.setattr(pf, "_pdf_fields", lambda *args: original(*args).replace("FETCHED_EXCERPT", "ABSTRACT_VERIFIED"))
    with pytest.raises(AssertionError):
        assert pf._evidence_level_of(await run()) == "fetched_excerpt"


async def test_negative_control_kind_code_erasure_accepts_wrong_publication(chain, monkeypatch):
    responses, _ = chain
    responses["reader_html"] = CLAIMS.replace("B2", "A1")
    exact = identity.exact_publication_identity
    monkeypatch.setattr(identity, "exact_publication_identity", lambda value: exact(value).replace("A1", "").replace("B2", ""))
    with pytest.raises(AssertionError):
        assert pf._evidence_level_of(await run()) == "fetch_failed"


@pytest.mark.parametrize("document,level", [
    (ABSTRACT, "abstract_verified"),
    (CLAIMS.replace("What is claimed is:", "Claims"), "claim_verified"),
    (EXCERPT + "\nAbstract\nDescription\n" + PARAGRAPH * 3, "fetched_excerpt"),
])
async def test_real_reader_http_path_preserves_sections(chain, monkeypatch, document, level):
    import tools.jina_reader as jina
    real_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "r.jina.ai"
        return httpx.Response(200, text=f"Title: Patent\nURL Source: {URL}\nMarkdown Content:\n{document}")

    monkeypatch.setattr(jina.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(pf, "fetch_via_jina", jina.fetch_via_jina_preserving_structure)
    output = await pf.patent_fetch(URL, timeout_ms=5000)
    assert pf._evidence_level_of(output) == level
    assert requests


async def test_negative_control_reader_cleaner_would_remove_real_abstract(chain, monkeypatch):
    import tools.jina_reader as jina
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=ABSTRACT))
    monkeypatch.setattr(jina.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(pf, "fetch_via_jina", jina.fetch_via_jina)
    with pytest.raises(AssertionError):
        assert pf._evidence_level_of(await pf.patent_fetch(URL, timeout_ms=5000)) == "abstract_verified"


async def test_dom_missing_claims_notice_cannot_stop_later_reader(chain):
    responses, calls = chain
    responses["http"] = (
        f"<title>{NUMBER}</title><section class='claims'><p>"
        "No claims are available for this document. Please consult the official publication for details."
        "</p></section>", "",
    )
    responses["reader_html"] = CLAIMS
    output = await run()
    assert "reader_html" in calls
    assert "What is claimed is" in output
    assert "No claims are available" not in output


async def test_legitimate_cookie_claim_is_not_deleted_as_boilerplate(chain):
    responses, _ = chain
    claim = "1. A browser controller comprising a memory configured to store a cookie and a processor configured to authenticate a user and retrieve a requested document from a remote network server."
    responses["http"] = (f"<title>{NUMBER}</title><section class='claims'><claim-text>{claim}</claim-text></section>", "")
    output = await run()
    assert pf._evidence_level_of(output) == "claim_verified"
    claim_line = next(line for line in output.splitlines() if line.startswith("CLAIM1:"))
    assert "store a cookie" in claim_line


async def test_legitimate_captcha_technology_claim_is_not_a_challenge_page(chain):
    responses, _ = chain
    responses["pdf"] = CLAIMS.replace("irrigation controller", "CAPTCHA access denied notification controller")
    assert pf._evidence_level_of(await run()) == "claim_verified"


@pytest.mark.parametrize("notice", [
    "Claims are currently unavailable. Please consult the official document for details.",
    "The claims of this patent could not be loaded at this time.",
    "1. Claims are currently unavailable. Please consult the official document for details.",
])
async def test_typed_claim_load_notice_does_not_stop_reader_upgrade(chain, notice):
    responses, calls = chain
    responses["http"] = (
        f"<title>{NUMBER}</title><section class='claims'><claim-text>{notice}</claim-text></section>", "",
    )
    responses["reader_html"] = CLAIMS
    output = await run()
    assert "reader_html" in calls
    assert pf._evidence_level_of(output) == "claim_verified"
    assert "What is claimed is" in output
    assert notice not in output


_SECTION_LOAD_NOTICES = (
    (
        "Claims",
        "1. Claims are currently unavailable. Please consult the official publication "
        "for details. This service could not load the claim text at this time. "
        "Reload this page or try again later.",
    ),
    (
        "Abstract",
        "The abstract of this patent could not be loaded at this time. Please "
        "consult the official publication for details. Reload this page or try "
        "again later to obtain the missing text.",
    ),
    (
        "Claims",
        "1. Claims are\ncurrently unavailable. Please consult the official publication "
        "for details. This service could not load the claim text at this time. "
        "Reload this page or try again later.",
    ),
    (
        "Abstract",
        "Abstract is\ncurrently unavailable. Please consult the official publication "
        "for details. This service could not load the abstract text at this time. "
        "Reload this page or try again later.",
    ),
)


def _assert_load_notice_did_not_replace_archive(output, calls, backend, notice):
    assert "archive" in calls, "a section-load notice prematurely stopped fallback"
    provider = {
        "pdf": "google_patents_pdf",
        "reader_pdf": "google_patents_pdf_jina",
        "reader_html": "google_patents_html_jina",
    }[backend]
    attempt = next(entry for entry in log(output) if entry["provider"] == provider)
    assert attempt["evidence_level"] == "fetched_excerpt", "a load notice was promoted as a real section"
    assert pf._evidence_level_of(output) == "abstract_verified"
    assert "PROVIDER: wayback" in output
    assert "ABSTRACT: An irrigation controller" in output
    assert notice not in output


@pytest.mark.parametrize("backend", ["pdf", "reader_pdf", "reader_html"])
@pytest.mark.parametrize("heading,notice", _SECTION_LOAD_NOTICES, ids=["claims", "abstract", "wrapped-claims", "wrapped-abstract"])
async def test_text_section_load_notice_does_not_replace_later_archive(chain, backend, heading, notice):
    responses, calls = chain
    # The exact header and genuine description satisfy identity and document
    # length. Only the section-load guard can reject the claimed strong level.
    responses[backend] = EXCERPT + f"\n{heading}\n{notice}"
    responses["archive"] = html_document("abstract")
    _assert_load_notice_did_not_replace_archive(await run(), calls, backend, notice)


@pytest.mark.parametrize("heading,notice", _SECTION_LOAD_NOTICES, ids=["claims", "abstract", "wrapped-claims", "wrapped-abstract"])
async def test_negative_control_text_load_notice_promotion_is_caught(chain, monkeypatch, heading, notice):
    responses, calls = chain
    responses["reader_html"] = EXCERPT + f"\n{heading}\n{notice}"
    responses["archive"] = html_document("abstract")
    monkeypatch.setattr(identity, "section_unavailable", lambda text, section: False)
    with pytest.raises(AssertionError, match="prematurely stopped fallback|promoted as a real section"):
        _assert_load_notice_did_not_replace_archive(await run(), calls, "reader_html", notice)
