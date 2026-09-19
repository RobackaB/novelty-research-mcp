"""Exercise patent search and real number enrichment over a mock HTTP transport."""

from __future__ import annotations

import json
import sqlite3
import io

import httpx
import pytest

import tools.patent_search as ps
import tools.patent_evidence_pack as pep
import tools.research_session as rs
import tools.patent_fetch as pf


NUMBER = "US10762444B2"
URL = f"https://patents.google.com/patent/{NUMBER}/en"
PDF_PATH = "aa/bb/US10762444.pdf"
PDF_URL = f"https://patentimages.storage.googleapis.com/{PDF_PATH}"
QUERY = "soil moisture irrigation valve schedule"
TITLE = "Soil moisture irrigation valve schedule controller"
SNIPPET = "Soil moisture irrigation valve schedule controller with wireless sensing."


@pytest.fixture
def search_transport(monkeypatch):
    """Replace HTTP I/O only; retain real providers, ranking and enrichment."""
    original_client = httpx.AsyncClient
    clients = []
    requests = []
    lookup_requests = []
    state = {
        "discovery": "tavily", "lookup_status": 200, "title": TITLE,
        "snippet": SNIPPET, "pdf_path": PDF_PATH,
        "assignee": "Fixture Irrigation Company", "url": URL,
    }

    def respond(request):
        requests.append(request)
        if request.url.host == "patentimages.storage.googleapis.com":
            return httpx.Response(200, content=state["pdf_bytes"], headers={"Content-Type": "application/pdf"})
        if request.url.host == "patents.google.com":
            if request.url.params.get("url") == f"q=({NUMBER})":
                lookup_requests.append(request)
                assert not clients[-1].is_closed
                return httpx.Response(state["lookup_status"], json={
                    "results": {"cluster": [{"result": [{"patent": {
                        "publication_number": NUMBER,
                        "pdf": state["pdf_path"],
                        "assignee": state["assignee"],
                    }}]}]},
                })
            return httpx.Response(200, json={"results": {"cluster": []}})
        if request.url.host == "api.tavily.com":
            records = [{"title": state["title"], "url": state["url"], "content": state["snippet"]}]
            return httpx.Response(200, json={
                "results": records if state["discovery"] == "tavily" else [],
            })
        if request.url.host == "api.exa.ai":
            records = [{"title": state["title"], "url": state["url"], "text": state["snippet"]}]
            return httpx.Response(200, json={
                "results": records if state["discovery"] == "exa" else [],
            })
        if request.url.host == "patentscope.wipo.int":
            if state["discovery"] == "wipo":
                return httpx.Response(200, text=(
                    "<html><body><table><tr><td>1.</td>"
                    f'<td><a href="detail.jsf?docId={NUMBER}">{NUMBER}</a></td>'
                    f"<td>{TITLE}</td><td>US - 01.01.2020</td>"
                    f"<td>{SNIPPET}</td></tr></table></body></html>"
                ))
            return httpx.Response(200, text="<html><body></body></html>")
        raise AssertionError(f"Unexpected mock request: {request.url.host}")

    def client_factory(*args, **kwargs):
        client = original_client(
            *args, transport=httpx.MockTransport(respond), **kwargs
        )
        clients.append(client)
        return client

    monkeypatch.setenv("TAVILY_API_KEY", "fixture-tavily-key")
    monkeypatch.setenv("EXA_API_KEY", "fixture-exa-key")
    monkeypatch.setattr(ps.httpx, "AsyncClient", client_factory)
    ps._PATENT_SEARCH_CACHE.clear()
    pf._PATENT_FETCH_CACHE.clear()
    yield state, clients, requests, lookup_requests
    ps._PATENT_SEARCH_CACHE.clear()
    pf._PATENT_FETCH_CACHE.clear()


def _assert_enriched(payload):
    assert len(payload["results"]) == 1
    hit = payload["results"][0]
    assert hit["patent_number"] == NUMBER
    assert hit["pdf_url"] == PDF_URL, "real enrichment did not reach search output"
    assert hit["assignee"] == "Fixture Irrigation Company"
    assert hit["evidence_level"] == "search_snippet_only"
    assert hit["needs_fetch"] is True


@pytest.mark.parametrize("discovery", ["tavily", "exa", "wipo"])
async def test_real_search_enriches_with_live_client_and_closes_it_afterward(
    search_transport, discovery
):
    state, clients, requests, lookups = search_transport
    state["discovery"] = discovery

    payload = json.loads(await ps.patent_search(QUERY, 5))

    _assert_enriched(payload)
    assert payload["status"] == "ok"
    assert len(lookups) == 1
    assert len(clients) == 1
    assert clients[0].is_closed
    assert {request.url.host for request in requests} == {
        "patents.google.com", "api.tavily.com", "api.exa.ai", "patentscope.wipo.int",
    }


async def test_enriched_search_cache_remains_byte_identical_without_new_requests(
    search_transport,
):
    _, _, requests, lookups = search_transport
    first = await ps.patent_search(QUERY, 5)
    _assert_enriched(json.loads(first))
    request_count = len(requests)

    assert await ps.patent_search(QUERY, 5) == first
    assert len(requests) == request_count
    assert len(lookups) == 1


async def test_blocked_number_lookup_preserves_search_decisions(search_transport):
    state, _, _, lookups = search_transport
    enriched = json.loads(await ps.patent_search(QUERY, 5))
    _assert_enriched(enriched)
    ps._PATENT_SEARCH_CACHE.clear()
    state["lookup_status"] = 403

    blocked = json.loads(await ps.patent_search(QUERY, 5))

    assert len(lookups) == 2
    assert blocked["results"][0]["pdf_url"] == ""
    assert blocked["results"][0]["assignee"] == ""
    enriched["results"][0]["pdf_url"] = ""
    enriched["results"][0]["assignee"] = ""
    assert blocked == enriched


async def test_negative_control_restored_closed_client_breaks_enrichment(
    monkeypatch, search_transport
):
    """Restore premature closure while still executing actual number enrichment."""
    _, clients, _, lookups = search_transport
    real_enrich = ps._enrich_ranked_candidates

    async def close_before_enrichment(client, candidates):
        await client.aclose()
        return await real_enrich(client, candidates)

    monkeypatch.setattr(ps, "_enrich_ranked_candidates", close_before_enrichment)
    payload = json.loads(await ps.patent_search(QUERY, 5))

    with pytest.raises(AssertionError, match="real enrichment did not reach"):
        _assert_enriched(payload)
    assert payload["status"] == "ok", "enrichment failure must remain fail-open"
    assert lookups == [], "httpx rejects the lookup before transport when closed"
    assert clients[0].is_closed


async def test_real_low_confidence_search_candidate_is_enriched(search_transport):
    state, clients, _, lookups = search_transport
    state["title"] = "Wireless sensor"
    state["snippet"] = "A soil sensor."

    payload = json.loads(await ps.patent_search(QUERY, 5))

    _assert_enriched(payload)
    assert payload["status"] == "partial_failure"
    assert payload["completed"] is False
    score = payload["results"][0]["score"]
    assert ps.MIN_RELEVANCE_FLOOR <= score < ps.MIN_RELEVANCE_SCORE_FALLBACK
    assert len(lookups) == 1
    assert len(clients) == 1 and clients[0].is_closed


async def test_search_serialization_contains_no_credential_metadata(
    monkeypatch, search_transport
):
    state, _, _, lookups = search_transport
    secret = "patent_metadata_SENTINEL_832daa"
    bearer = "unconfigured_bearer_SENTINEL_91ee"
    monkeypatch.setenv("PATENT_TEST_API_KEY", secret)
    state["pdf_path"] = f"{PDF_PATH}?api_key={secret}"
    state["assignee"] = f"Fixture Company Bearer {bearer}"
    state["snippet"] = f"{SNIPPET} https://user:{secret}@example.test/document"

    text = await ps.patent_search(QUERY, 5)

    assert secret not in text
    assert bearer not in text
    assert "api_key=" not in text
    assert "Bearer " not in text
    assert "@example.test" not in text
    payload = json.loads(text)
    assert payload["results"][0]["pdf_url"] == "[REDACTED_URL]"
    assert payload["results"][0]["patent_number"] == NUMBER
    assert payload["status"] == "ok"
    assert len(lookups) == 1


@pytest.mark.parametrize("logging_fails", [False, True])
async def test_secret_bearing_capture_is_omitted_without_changing_retrieval(
    monkeypatch, search_transport, temp_db, caplog, logging_fails
):
    state, _, _, _ = search_transport
    secret = "SENTINEL_CAPTURE_CREDENTIAL_8547"
    monkeypatch.setenv("PATENT_TEST_API_KEY", secret)
    state["snippet"] = SNIPPET + f" Bearer {secret}"

    async def verify(urls, **kwargs):
        return ""

    monkeypatch.setattr(pep, "verify_sources", verify)
    if logging_fails:
        def broken_logger(*args, **kwargs):
            raise RuntimeError("handler unavailable")
        monkeypatch.setattr(ps.LOGGER, "warning", broken_logger)
    rs.research_session_start(QUERY, "capture-secret")
    # Actual HTTP parsers, search, collector, pack, writer and SQLite persistence.
    output = await rs.patent_evidence_to_session("capture-secret", QUERY, max_fetches=0, attempt_no=1)
    assert json.loads(output)["status"] == "written"
    with sqlite3.connect(temp_db) as conn:
        dump = "\n".join(conn.iterdump())
        count = conn.execute("SELECT count(*) FROM evaluation_candidate_decisions").fetchone()[0]
    assert count > 0, "safe query-level cache observation must still persist"
    assert secret not in output + dump + caplog.text
    if not logging_fails:
        assert "credential-bearing event" in caplog.text


async def test_negative_control_unsanitized_capture_would_store_secret(
    monkeypatch, search_transport, temp_db
):
    state, _, _, _ = search_transport
    secret = "SENTINEL_CAPTURE_CONTROL_9357"
    monkeypatch.setenv("PATENT_TEST_API_KEY", secret)
    state["snippet"] = SNIPPET + f" Bearer {secret}"
    monkeypatch.setattr(ps, "safe_record", ps._safe_record)

    async def verify(urls, **kwargs):
        return ""

    monkeypatch.setattr(pep, "verify_sources", verify)
    rs.research_session_start(QUERY, "capture-control")
    await rs.patent_evidence_to_session("capture-control", QUERY, max_fetches=0, attempt_no=1)
    with sqlite3.connect(temp_db) as conn:
        dump = "\n".join(conn.iterdump())
    with pytest.raises(AssertionError):
        assert secret not in dump


@pytest.mark.parametrize("discovery", ["tavily", "exa"])
@pytest.mark.parametrize("pdf_kind", ["B2", "A1"])
async def test_non_google_discovery_reaches_real_enriched_pdf(monkeypatch, search_transport, discovery, pdf_kind):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    state, _, requests, lookups = search_transport
    state.update(discovery=discovery, url="https://patentscope.wipo.int/search/en/detail.jsf?docId=12345",
                 title=f"{TITLE} {NUMBER}")
    document = (f"US10762444{pdf_kind} What is claimed is: 1. " +
                "A soil moisture irrigation valve schedule controller transmits sensor measurements wirelessly and controls watering. " * 20)
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({document}) Tj ET".encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    state['pdf_bytes'] = output.getvalue()

    async def empty_reader(*args, **kwargs):
        return ""

    async def empty_archive(*args, **kwargs):
        return "", ""

    monkeypatch.setattr(pf, 'PATENT_FETCH_PROVIDERS', ())
    monkeypatch.setattr(pf, 'fetch_via_jina', empty_reader)
    monkeypatch.setattr(pf, '_wayback_patent_fetch', empty_archive)
    # Real search, number enrichment, pack, internal fetch, HTTP PDF stream and
    # pypdf extraction. Only external HTTP responses/fallbacks are replaced.
    payload = json.loads(await pep.patent_evidence_pack(QUERY, max_results=5))
    hit = payload['hits'][0]
    assert hit['url'] == state['url']
    assert hit['patent_number'] == NUMBER
    assert len(lookups) == 1
    assert any(request.url.host == 'patentimages.storage.googleapis.com' for request in requests)
    if pdf_kind == 'B2':
        assert hit['evidence_level'] == 'claim_verified'
        assert 'Claim evidence:' in hit['summary']
        assert 'sensor measurements' in hit['summary']
    else:
        assert hit['evidence_level'] == 'search_snippet_only'
        assert 'Claim evidence:' not in hit['summary']
