"""External-review regressions for identity provenance and network boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import tools.patent_fetch as pf
import tools.patent_identity as identity
import tools.patent_safety as safety
import tools.jina_reader as jina
import tools.source_verify as verify
import tools.chromium_scraper as browser

NUMBER = "US10762444B2"
URL = f"https://patents.google.com/patent/{NUMBER}/en"
PDF = "https://patentimages.storage.googleapis.com/x/US10762444.pdf"
WORDS = "A controller measures soil moisture and transmits readings to a valve that controls scheduled watering. " * 15


@pytest.mark.parametrize("target", [
    "http://localhost/", "http://localhost./", "http://sub.localhost/",
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/",
    "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/",
    "http://[fc00::1]/", "http://[fe80::1]/", "http://0.0.0.0/", "http://[::]/",
    "http://240.0.0.1/", "http://100.64.0.1/", "http://192.0.2.1/", "http://224.0.0.1/",
    "https://r.jina.ai/http://127.0.0.1/", "https://web.archive.org/web/1/http://[::1]/",
    "https://patents.google.com/?url=http%3A%2F%2F169.254.169.254%2F",
])
def test_internal_document_targets_are_rejected(target):
    assert not safety.public_document_url(target)
    assert not safety.patent_document_url(target)


@pytest.mark.parametrize("target", [
    "https://attacker.example.org/document", "https://patents.google.com.attacker.org/",
    "https://r.jina.ai/https://attacker.example.org/document",
])
def test_patent_network_policy_rejects_untrusted_hostnames(target):
    assert not safety.patent_document_url(target)


@pytest.mark.parametrize("target", [URL, PDF, f"https://r.jina.ai/{URL}",
                                        f"https://web.archive.org/web/20260101/{URL}"])
def test_existing_document_hosts_remain_allowed(target):
    assert safety.patent_document_url(target)


def test_network_rejection_does_not_misclassify_capture_as_credential_bearing():
    # Ineligible fetch locators are still exact passive capture data. Only
    # credentials, not a network-policy rejection, justify secret redaction.
    assert not safety.public_document_url("https://a/1")
    assert safety.redact_patent_text("https://a/1") == "https://a/1"


@pytest.mark.parametrize("kind", ["claims", "abstract"])
@pytest.mark.parametrize("basis", ["canonical_url", "official_pdf_url"])
def test_family_url_cannot_promote_other_publication(kind, basis):
    heading = "What is claimed is: 1. " if kind == "claims" else "Abstract\n"
    content = f"US10762444A1\n{heading}{WORDS}"
    provenance = identity.resolve_identity(patent_number=NUMBER,
        **{basis: URL if basis == "canonical_url" else PDF}, content=content)
    assert provenance == basis
    assert identity.evidence_level_for_content(content=content, identity=provenance) == "fetched_excerpt"


@pytest.mark.parametrize("basis", ["structured_lookup", "normalised_number_in_content"])
def test_exact_identity_still_allows_structural_promotion(basis):
    content = f"{NUMBER}\nWhat is claimed is: 1. {WORDS}"
    kwargs = {"structured_lookup_number": NUMBER} if basis == "structured_lookup" else {"content": content}
    provenance = identity.resolve_identity(patent_number=NUMBER, official_pdf_url=PDF, **kwargs)
    assert provenance == basis
    assert identity.evidence_level_for_content(content=content, identity=provenance) == "claim_verified"


def test_wrong_structured_kind_does_not_establish_identity():
    assert identity.resolve_identity(patent_number=NUMBER, structured_lookup_number="US10762444A1") == "none"


@pytest.mark.parametrize("target", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/"])
async def test_private_discovery_target_never_reaches_fetch(monkeypatch, target):
    called = []

    async def backend(*args, **kwargs):
        called.append(args)
        return ""

    monkeypatch.setattr(pf, "_patent_pdf_fetch", backend)
    output = await pf.patent_fetch_for_candidate(target, pdf_url=PDF, patent_number=NUMBER)
    assert "FETCH_FAILED" in output
    assert not called


@pytest.mark.parametrize("backend", ["http", "pdf", "reader", "archive", "verification"])
@pytest.mark.parametrize("target", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/"])
async def test_redirect_is_rejected_before_internal_http_request(monkeypatch, backend, target):
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": target})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    try:
        if backend == "http":
            await pf._static_patent_fetch(URL, 5000)
        elif backend == "pdf":
            await pf._patent_pdf_fetch(PDF, 5000)
        elif backend == "reader":
            await jina.fetch_via_jina_preserving_structure(URL)
        elif backend == "archive":
            await pf._wayback_patent_fetch(URL, 5000)
        else:
            await verify.verify_patent_sources([URL])
    except ValueError as exc:
        assert str(exc) == "Unsafe patent document target rejected."
    assert len(requests) == 1
    assert target not in requests


@pytest.mark.parametrize("target", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/"])
async def test_unsafe_archive_snapshot_is_never_requested(monkeypatch, target):
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"archived_snapshots": {"closest": {"available": True, "url": target}}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    assert await pf._wayback_patent_fetch(URL, 5000) == ("", "")
    assert len(requests) == 1


@pytest.mark.parametrize("request_url,status,expected", [
    (URL, 200, "fulfilled"), (URL, 302, "aborted"),
    ("http://127.0.0.1/", 200, "aborted"),
    ("http://[::1]/", 200, "aborted"),
    ("http://169.254.169.254/", 200, "aborted"),
])
async def test_patent_browser_guards_requests_and_disallows_redirect_following(monkeypatch, request_url, status, expected):
    import playwright.async_api as api

    route = SimpleNamespace(request=SimpleNamespace(url=request_url, resource_type="document"),
        abort=AsyncMock(), continue_=AsyncMock(), fetch=AsyncMock(return_value=SimpleNamespace(status=status)), fulfill=AsyncMock())
    callbacks = []
    context = SimpleNamespace(route=AsyncMock(side_effect=lambda pattern, handler: callbacks.append(handler)), close=AsyncMock())

    async def goto(*args, **kwargs):
        await callbacks[0](route)

    page = SimpleNamespace(goto=goto, content=AsyncMock(return_value="html"),
        locator=lambda selector: SimpleNamespace(inner_text=AsyncMock(return_value="text")))
    context.new_page = AsyncMock(return_value=page)
    instance = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    manager = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=instance)))
    cm = AsyncMock()
    cm.__aenter__.return_value = manager
    monkeypatch.setattr(api, "async_playwright", lambda: cm)
    await browser.fetch_page_html_and_text(URL, timeout_ms=5000, allowed_url=safety.patent_document_url)
    assert instance.new_context.call_args.kwargs["service_workers"] == "block"
    assert not route.continue_.called
    if expected == "fulfilled":
        route.fulfill.assert_awaited_once()
        assert not route.abort.called
    else:
        route.abort.assert_awaited_once()
        assert not route.fulfill.called
    if request_url == URL:
        route.fetch.assert_awaited_once_with(max_redirects=0, timeout=5000)
    else:
        assert not route.fetch.called


async def test_redirect_guard_negative_control(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200, text="synthetic metadata response")

    async def missing_guard(request):
        pass

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(pf, "guard_patent_request", missing_guard)
    await pf._static_patent_fetch(URL, 5000)
    with pytest.raises(AssertionError):
        assert len(requests) == 1, "redirect reached internal host"
