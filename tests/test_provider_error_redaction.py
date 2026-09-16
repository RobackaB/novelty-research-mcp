"""Provider diagnostics must not copy request credentials into evidence."""

from __future__ import annotations

import json
import logging
import sqlite3

import httpx
import pytest

import tools.research_session as rs
import tools.web_search as ws
import tools.patent_search as patents
import tools.publications_search as publications
import tools.alphaxiv_client as alphaxiv
from tools._provider_errors import provider_error_message
from tools.result_contract import parse_status_marker

SECRET = "SYNTHETIC_PROVIDER_SECRET"
QUERY = "wireless battery temperature sensor"


def _http_error(status=403):
    request = httpx.Request("GET", f"https://provider.invalid/search?key={SECRET}",
                            headers={"Authorization": f"Bearer {SECRET}"})
    response = httpx.Response(status, request=request, text=SECRET)
    return httpx.HTTPStatusError(f"{SECRET}: {request.url}", request=request, response=response)


@pytest.mark.parametrize("error,expected", [
    (_http_error(403), "HTTPStatusError (HTTP 403)"),
    (_http_error(429), "HTTPStatusError (HTTP 429)"),
    (httpx.ReadTimeout(SECRET), "ReadTimeout"),
    (httpx.ConnectError(SECRET), "ConnectError"),
    (RuntimeError(SECRET), "RuntimeError"),
    (ValueError(SECRET), "ValueError"),
    (ExceptionGroup(SECRET, [_http_error()]), "ExceptionGroup"),
])
def test_error_diagnostic_uses_only_type_and_http_status(error, expected):
    assert provider_error_message(error) == expected


def test_error_diagnostic_never_calls_exception_stringification():
    class UnrenderableError(Exception):
        def __str__(self):
            raise AssertionError("untrusted exception text was rendered")

    assert provider_error_message(UnrenderableError()) == "UnrenderableError"


def _mock_google_failure(monkeypatch, failure=403):
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.url.params["key"] == SECRET
        if failure == "timeout":
            raise httpx.ReadTimeout(f"request failed: {request.url}", request=request)
        return httpx.Response(failure, request=request, json={"error": SECRET})

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ws, "_google_cse_credentials", lambda: (SECRET, "synthetic-cx"))
    monkeypatch.setattr(ws, "SEARCH_PROVIDERS", [("google_cse", ws._google_cse_query)])
    monkeypatch.setattr(ws.httpx, "AsyncClient", client)


@pytest.mark.parametrize("failure", [403, 429, 503, "timeout"])
async def test_google_failure_does_not_return_credentials(monkeypatch, failure):
    _mock_google_failure(monkeypatch, failure)
    output = await ws.web_search(QUERY)
    assert parse_status_marker(output) == "failed"
    assert "COMPLETED: FALSE" in output
    assert "RELIABLE_NO_RESULTS: FALSE" in output
    assert "google_cse_web_search_failure" in output
    assert SECRET not in output
    assert "www.googleapis.com" not in output


@pytest.mark.parametrize("failure", [403, 429, 503, "timeout"])
async def test_google_failure_does_not_persist_credentials(monkeypatch, temp_db, failure):
    _mock_google_failure(monkeypatch, failure)
    rs.research_session_start(QUERY, "redaction-session")
    ack = await rs.web_evidence_to_session("redaction-session", QUERY, attempt_no=1)
    assert json.loads(ack)["retrieval_status"] == "failed"
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT evidence_json, normalized_json FROM evidence_results").fetchone()
    assert row is not None
    for stored in row:
        assert "google_cse_web_search_failure" in stored
        assert SECRET not in stored
        assert "www.googleapis.com" not in stored
    assert SECRET not in ack


async def test_redaction_negative_control_restores_returned_and_persisted_leak(monkeypatch, temp_db):
    _mock_google_failure(monkeypatch)
    assert SECRET not in await ws.web_search(QUERY)
    monkeypatch.setattr(ws, "provider_error_message", str)
    # Restore the exact defective rendering, not a fabricated output string.
    output = await ws.web_search(QUERY)
    with pytest.raises(AssertionError):
        assert SECRET not in output
    rs.research_session_start(QUERY, "negative-control")
    await rs.web_evidence_to_session("negative-control", QUERY, attempt_no=1)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT evidence_json, normalized_json FROM evidence_results").fetchone()
    for stored in row:
        with pytest.raises(AssertionError):
            assert SECRET not in stored


@pytest.mark.parametrize("with_hit", [False, True])
async def test_web_partial_failure_preserves_candidate_output_and_status(monkeypatch, with_hit):
    _mock_google_failure(monkeypatch)

    async def successful_provider(*args):
        return [("https://example.org/sensor", QUERY, QUERY + " monitoring measurements")] if with_hit else []

    monkeypatch.setattr(ws, "SEARCH_PROVIDERS", [
        ("google_cse", ws._google_cse_query), ("successful", successful_provider),
    ])
    safe = await ws.web_search(QUERY)
    monkeypatch.setattr(ws, "provider_error_message", str)
    raw = await ws.web_search(QUERY)
    assert SECRET not in safe and SECRET in raw
    assert parse_status_marker(safe) == parse_status_marker(raw) == "partial_failure"
    # Replacing just the diagnostic reproduces every output byte, including
    # scores, selected URLs/order, status markers and completion/error counts.
    request = httpx.Request("GET", "https://www.googleapis.com/customsearch/v1",
                            params={"key": SECRET, "cx": "synthetic-cx", "q": QUERY, "num": 10})
    response = httpx.Response(403, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        assert raw.replace(str(error), provider_error_message(error)) == safe
    assert ("https://example.org/sensor" in safe) is with_hit


async def test_redaction_preserves_writer_ack_duplicate_and_retry_plan(monkeypatch, temp_db):
    _mock_google_failure(monkeypatch)
    monkeypatch.setattr(rs, "_now", lambda: "2026-09-16T12:00:00+00:00")

    async def session(path):
        monkeypatch.setenv("RESEARCH_SESSION_DB", str(path))
        rs.research_session_start(QUERY, "equivalence-session")
        rs.research_session_understand_query("equivalence-session", original_query=QUERY)
        ack = await rs.web_evidence_to_session("equivalence-session", QUERY, attempt_no=1)
        duplicate = await rs.web_evidence_to_session("equivalence-session", QUERY, attempt_no=1)
        checklist = rs.research_session_checklist("equivalence-session")
        return ack, duplicate, checklist

    safe = await session(temp_db)
    monkeypatch.setattr(ws, "provider_error_message", str)
    raw = await session(temp_db.with_name("raw-control.sqlite3"))
    assert safe == raw
    assert json.loads(safe[1])["status"] == "duplicate_noop"
    assert json.loads(safe[0])["needs_retry"] is True


async def test_patent_error_diagnostics_and_logs_preserve_results(monkeypatch, caplog):
    async def empty(*args):
        return []

    async def failing(*args):
        raise _http_error()

    for name in ("_google_patents_xhr_search", "_exa_patent_search", "_wipo_patentscope_search"):
        monkeypatch.setattr(patents, name, empty)
    monkeypatch.setattr(patents, "_tavily_patent_search", failing)
    caplog.set_level(logging.INFO, logger="tools.patent_search")
    patents._PATENT_SEARCH_CACHE.clear()
    try:
        safe = json.loads(await patents.patent_search(QUERY))
        assert SECRET not in json.dumps(safe) + caplog.text
        assert "HTTPStatusError (HTTP 403)" in caplog.text
        patents._PATENT_SEARCH_CACHE.clear()
        monkeypatch.setattr(patents, "provider_error_message", str)
        raw = json.loads(await patents.patent_search(QUERY))
        assert SECRET in json.dumps(raw)
        for error in raw["errors"]:
            error["message"] = "HTTPStatusError (HTTP 403)"
        assert raw == safe
    finally:
        patents._PATENT_SEARCH_CACHE.clear()


PUBLICATION_PROVIDERS = (
    "_semantic_scholar_blocks", "_crossref_blocks_safe", "_alpha_blocks_safe",
    "_pubmed_blocks_safe", "_openalex_blocks_safe", "_arxiv_blocks_safe",
)


@pytest.mark.parametrize("failing_provider", PUBLICATION_PROVIDERS)
async def test_publication_parallel_provider_diagnostics_are_safe(monkeypatch, failing_provider):
    async def empty(*args):
        return []

    async def empty_primary(*args):
        return [], False, False

    async def failing(*args):
        raise _http_error()

    for name in PUBLICATION_PROVIDERS:
        monkeypatch.setattr(publications, name, empty_primary if name == PUBLICATION_PROVIDERS[0] else empty)
    monkeypatch.setattr(publications, failing_provider, failing)
    safe = await publications.publications_search(QUERY)
    monkeypatch.setattr(publications, "provider_error_message", str)
    raw = await publications.publications_search(QUERY)
    assert SECRET not in safe and SECRET in raw
    assert raw.replace(str(_http_error()), provider_error_message(_http_error())) == safe
    assert parse_status_marker(safe) == "partial_failure"


@pytest.mark.parametrize("message,expected_status", [(SECRET, "partial_failure"), (SECRET + " 429", "failed")])
async def test_publication_fallback_uses_original_error_for_decision_only(monkeypatch, message, expected_status):
    class FailingClient:
        async def __aenter__(self):
            raise RuntimeError(message)

        async def __aexit__(self, *args):
            return False

    async def empty(*args):
        return []

    async def empty_arxiv(*args):
        return ""

    monkeypatch.setattr(publications.httpx, "AsyncClient", lambda **kwargs: FailingClient())
    monkeypatch.setattr(publications, "_crossref_blocks", empty)
    monkeypatch.setattr(publications, "_alpha_search_blocks", empty)
    monkeypatch.setattr(publications, "arxiv_search", empty_arxiv)
    safe = await publications.publications_search(QUERY)
    monkeypatch.setattr(publications, "provider_error_message", str)
    raw = await publications.publications_search(QUERY)
    assert SECRET not in safe and SECRET in raw
    assert parse_status_marker(safe) == parse_status_marker(raw) == expected_status
    assert raw.replace(message, "RuntimeError") == safe


@pytest.mark.parametrize("fallback", ["crossref", "alphaxiv", "arxiv", "failed_arxiv", "raised_arxiv"])
async def test_publication_fallback_diagnostic_paths_preserve_outputs(monkeypatch, fallback):
    class FailingClient:
        async def __aenter__(self):
            raise _http_error()

        async def __aexit__(self, *args):
            return False

    block = (
        f"Publication title returned by Crossref: **{QUERY}**.\n"
        f"Abstract excerpt from this publication record: {QUERY} monitoring measurements.\n"
        "DOI URL constructed for this publication: https://doi.org/10.1234/sensor.\n"
        "SOURCE: Crossref"
    )

    async def crossref(*args):
        return [(8.0, block)] if fallback == "crossref" else []

    async def alpha(*args):
        return [(8.0, block)] if fallback == "alphaxiv" else []

    async def arxiv(*args):
        if fallback == "raised_arxiv":
            raise RuntimeError("secondary error")
        return "TOOL_ERROR: arxiv unavailable" if fallback == "failed_arxiv" else block

    monkeypatch.setattr(publications.httpx, "AsyncClient", lambda **kwargs: FailingClient())
    monkeypatch.setattr(publications, "_crossref_blocks", crossref)
    monkeypatch.setattr(publications, "_alpha_search_blocks", alpha)
    monkeypatch.setattr(publications, "arxiv_search", arxiv)
    safe = await publications.publications_search(QUERY)
    monkeypatch.setattr(publications, "provider_error_message", str)
    raw = await publications.publications_search(QUERY)
    assert SECRET not in safe and SECRET in raw
    assert raw.replace(str(_http_error()), provider_error_message(_http_error())) == safe
    if fallback in {"crossref", "alphaxiv", "arxiv"}:
        assert "https://doi.org/10.1234/sensor" in safe
        assert parse_status_marker(safe) == "partial_failure"
    elif fallback == "failed_arxiv":
        assert parse_status_marker(safe) == "failed"
    else:
        assert safe.startswith("TOOL_ERROR: publications_search")


async def test_alphaxiv_failure_log_is_safe_and_still_returns_no_records(monkeypatch, caplog):
    def failing_transport():
        raise _http_error()

    monkeypatch.setattr(alphaxiv, "alphaxiv_api_key", lambda: SECRET)
    monkeypatch.setattr(alphaxiv, "_streamable_http_client", failing_transport)
    caplog.set_level(logging.INFO, logger="tools.alphaxiv_client")
    assert await alphaxiv.discover_papers(QUERY) == []
    assert "HTTPStatusError (HTTP 403)" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("failing_stage", ["esearch", "efetch"])
async def test_pubmed_request_errors_already_return_no_raw_diagnostic(monkeypatch, failing_stage):
    real_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["api_key"] == SECRET
        if failing_stage in request.url.path:
            return httpx.Response(403, request=request, text=SECRET)
        return httpx.Response(200, request=request, json={"esearchresult": {"idlist": ["123"]}})

    monkeypatch.setenv("PUBMED_API_KEY", SECRET)
    monkeypatch.setattr(publications.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    assert await publications._pubmed_search_records(QUERY, 5) == []
    assert len(requests) == (1 if failing_stage == "esearch" else 2)
