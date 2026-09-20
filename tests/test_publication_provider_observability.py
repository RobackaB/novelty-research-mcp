"""Provider failures survive real wrappers, aggregation, packing and storage."""

import asyncio
import importlib
import json

import httpx
import pytest

from tools import publications_search as ps, publication_evidence_pack as pack, research_session as rs
from tools import alphaxiv_client as alpha
from tools.result_contract import parse_status_marker, parse_completed_marker, parse_error_count

arxiv = importlib.import_module("tools.arxiv_search")
QUERY = "smart door lock controlled by a mobile application with access codes"
SECRET = "SENTINEL_PRIVATE_PROVIDER_TOKEN_123456"


def _healthy():
    return [(7.0, ps._crossref_block(
        "Smart door lock mobile access study", "Author", "2024", QUERY,
        "https://doi.org/10.1234/healthy", 7.0,
    ).replace("Crossref", "SemanticScholar"))]


def _install(monkeypatch, failed=(), *, healthy=True, delays=None, crossref_hits=False):
    """Keep real provider wrappers; stub transport and arXiv's blocking SDK I/O."""
    real_client = httpx.AsyncClient
    calls = []

    async def handler(request):
        host = request.url.host
        provider = "crossref" if "crossref" in host else "openalex" if "openalex" in host else "pubmed"
        calls.append(provider)
        if delays:
            await asyncio.sleep(delays.get(provider, 0))
        if provider in failed:
            return httpx.Response(403, text=SECRET, request=request)
        if provider == "crossref":
            items = [{"DOI": "10.1234/crossref", "title": ["Smart door lock access codes research"],
                      "abstract": QUERY, "published": {"date-parts": [[2024]]}}] if crossref_hits else []
            return httpx.Response(200, json={"message": {"items": items}})
        if provider == "openalex":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})

    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    monkeypatch.setenv("PUBMED_API_KEY", SECRET)
    monkeypatch.delenv("ALPHAXIV_API_KEY", raising=False)

    async def semantic(*args, **kwargs):
        if "semantic_scholar" in failed:
            raise RuntimeError(f"Bearer {SECRET} https://private.example/?api_key={SECRET}")
        return _healthy() if healthy else [], False, False

    monkeypatch.setattr(ps, "_semantic_scholar_blocks", semantic)
    if "alphaxiv" in failed:
        monkeypatch.setenv("ALPHAXIV_API_KEY", SECRET)

        def failing_transport():
            raise RuntimeError(f"Authorization: Bearer {SECRET}")

        monkeypatch.setattr(alpha, "_streamable_http_client", failing_transport)

    def sdk(query, max_results):
        if "arxiv_marker" in failed or "arxiv" in failed:
            raise RuntimeError(f"https://private.example/?token={SECRET}")
        return []

    monkeypatch.setattr(arxiv, "_run_search", sdk)
    if "arxiv_exception" in failed:
        async def raises(*args, **kwargs):
            raise RuntimeError(f"Bearer {SECRET}")
        monkeypatch.setattr(ps, "arxiv_search", raises)
    return calls


def _assert_partial(output, provider):
    assert parse_status_marker(output) == "partial_failure", "failure was swallowed"
    assert parse_completed_marker(output) is False
    assert parse_error_count(output) > 0
    assert provider in output
    assert "publication_provider_error" in output
    assert "Smart door lock mobile access study" in output
    assert SECRET not in output
    assert "private.example" not in output


@pytest.mark.parametrize("provider", ["crossref", "pubmed", "openalex", "alphaxiv", "arxiv_exception", "arxiv_marker"])
async def test_real_provider_failure_with_healthy_hits_is_partial(monkeypatch, provider):
    _install(monkeypatch, {provider})
    output = await ps.publications_search(QUERY, 10)
    _assert_partial(output, provider.split("_")[0])


async def test_clean_crossref_empty_is_not_failure(monkeypatch):
    calls = _install(monkeypatch)
    output = await ps.publications_search(QUERY, 10)
    assert "crossref" in calls
    assert parse_status_marker(output) == "ok"
    assert parse_completed_marker(output) is True
    assert parse_error_count(output) == 0
    assert "Smart door lock mobile access study" in output


async def test_all_clean_empty_is_reliable_no_results(monkeypatch):
    _install(monkeypatch, healthy=False)
    output = await ps.publications_search(QUERY, 10)
    assert parse_status_marker(output) == "ok"
    assert parse_completed_marker(output) is True
    assert parse_error_count(output) == 0
    assert "RELIABLE_NO_RESULTS: TRUE" in output


async def test_healthy_multi_provider_hits_remain_successful(monkeypatch):
    _install(monkeypatch, crossref_hits=True)
    output = await ps.publications_search(QUERY, 10)
    assert parse_status_marker(output) == "ok"
    assert parse_error_count(output) == 0
    assert "SOURCE: Crossref" in output
    assert "SOURCE: SemanticScholar" in output


async def test_all_failure_is_incomplete_not_negative_evidence(monkeypatch):
    _install(monkeypatch, {"semantic_scholar", "crossref", "pubmed", "openalex", "alphaxiv", "arxiv"}, healthy=False)
    output = await ps.publications_search(QUERY, 10)
    assert parse_status_marker(output) == "partial_failure"
    assert parse_completed_marker(output) is False
    assert "RELIABLE_NO_RESULTS: FALSE" in output
    assert parse_error_count(output) >= 6
    for provider in ("semantic_scholar", "crossref", "alphaxiv", "pubmed", "openalex", "arxiv"):
        assert provider in output
    assert SECRET not in output


async def test_rate_limit_no_hit_fallback_retains_other_provider_errors(monkeypatch):
    _install(monkeypatch, {"crossref", "pubmed", "openalex", "alphaxiv", "arxiv"}, healthy=False)

    async def limited(*args, **kwargs):
        return [], False, True

    monkeypatch.setattr(ps, "_semantic_scholar_blocks", limited)
    output = await ps.publications_search(QUERY, 10)
    assert parse_completed_marker(output) is False
    assert "crossref:" in output and "pubmed:" in output and "arxiv:" in output
    assert SECRET not in output


async def test_arxiv_healthy_paper_about_rate_limits_is_not_a_provider_failure(monkeypatch):
    from tools.result_contract import NormalizedResult, prepend_markers

    async def search(*args):
        body = "Publication title returned by ArXiv: **Smart door lock rate limit study**.\n" + QUERY + "\nSOURCE: ArXiv"
        return prepend_markers(\n            NormalizedResult(status="ok", completed=True, reliable_no_results=False, hits=[body]),\n            body,\n        )

    monkeypatch.setattr(ps, "arxiv_search", search)
    result = await ps._arxiv_blocks_safe([QUERY], QUERY, 10)
    assert result
    assert result.errors == []


async def test_error_order_ignores_provider_completion_order(monkeypatch):
    outputs = []
    for order in ({"crossref": 0.015}, {"openalex": 0.015}):
        with monkeypatch.context() as patch:
            _install(patch, {"crossref", "pubmed", "openalex", "alphaxiv", "arxiv"}, delays=order)
            outputs.append(await ps.publications_search(QUERY, 10))
    assert outputs[0] == outputs[1]
    assert outputs[0].index("crossref:") < outputs[0].index("alphaxiv:") < outputs[0].index("pubmed:") < outputs[0].index("openalex:") < outputs[0].index("arxiv:")


async def test_crossref_successful_variant_survives_later_failure(monkeypatch):
    count = 0

    async def records(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError(SECRET)
        return [("Smart door lock mobile access", "Author", "2024", QUERY, "https://doi.org/10.1234/a")]

    monkeypatch.setattr(ps, "_crossref_search", records)
    result = await ps._crossref_blocks_safe(["first", "second"], QUERY, 10)
    assert len(result) == 1
    assert result.errors == ["RuntimeError"]
    errors = []
    assert ps._provider_blocks(result, "crossref", errors) == result
    assert errors == ["crossref: RuntimeError"]


@pytest.mark.parametrize("mode", ["esearch_error", "efetch_error", "xml_error", "xml_malformed"])
async def test_pubmed_inner_failures_are_observable(monkeypatch, mode):
    real_client = httpx.AsyncClient
    _install(monkeypatch)

    def handler(request):
        if "esearch" in request.url.path:
            if mode == "esearch_error":
                return httpx.Response(200, json={"error": SECRET})
            return httpx.Response(200, json={"esearchresult": {"idlist": ["123"]}})
        if mode == "efetch_error":
            return httpx.Response(500, text=SECRET)
        return httpx.Response(200, text=f"<ERROR>{SECRET}</ERROR>" if mode == "xml_error" else "<broken")

    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    result = await ps._pubmed_blocks_safe([QUERY], QUERY, 10)
    assert result == []
    assert result.errors
    assert SECRET not in json.dumps(result.errors)


async def test_partial_diagnostics_and_hits_reach_session_safely(temp_db, monkeypatch, caplog):
    _install(monkeypatch, {"crossref", "alphaxiv"})

    async def fetch(*args, **kwargs):
        return f"ABSTRACT: {QUERY}\nSTATUS: OK\nEVIDENCE_LEVEL: ABSTRACT_VERIFIED"

    async def verify(*args, **kwargs):
        return ""

    monkeypatch.setattr(pack, "publication_fetch", fetch)
    monkeypatch.setattr(pack, "verify_sources", verify)
    session = json.loads(rs.research_session_start(QUERY))["session_id"]
    ack = json.loads(await rs.publication_evidence_to_session(session, QUERY, attempt_no=1))
    assert ack["retrieval_status"] == "partial_failure"
    with rs._connect() as conn:
        row = conn.execute("SELECT * FROM evidence_results WHERE session_id=?", (session,)).fetchone()
        payload = json.loads(row["normalized_json"])
        assert row["completed"] == 0
        assert row["error_count"] > 0
        assert payload["hits"]
        assert payload["errors"][0]["type"] == "publication_provider_error"
        for table in ("evidence_results", "trace_events", "raw_evidence_items", "evaluation_candidate_decisions"):
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE session_id=?", (session,))]
            assert SECRET not in json.dumps(rows)
    assert SECRET not in json.dumps(ack) + caplog.text


@pytest.mark.parametrize("provider,wrapper", [
    ("crossref", "_crossref_blocks_safe"), ("pubmed", "_pubmed_blocks_safe"),
    ("openalex", "_openalex_blocks_safe"), ("alphaxiv", "_alpha_blocks_safe"),
    ("arxiv_marker", "_arxiv_blocks_safe"),
])
async def test_restoring_swallowed_failure_fails_regression(monkeypatch, provider, wrapper):
    _install(monkeypatch, {provider})
    _assert_partial(await ps.publications_search(QUERY, 10), provider.split("_")[0])
    real = getattr(ps, wrapper)

    async def swallowed(*args, **kwargs):
        return list(await real(*args, **kwargs))  # Historical list-only contract loses errors.

    monkeypatch.setattr(ps, wrapper, swallowed)
    with pytest.raises(AssertionError, match="failure was swallowed"):
        _assert_partial(await ps.publications_search(QUERY, 10), provider.split("_")[0])
