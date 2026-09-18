"""Patent evidence survives the real pack/writer without credential leakage."""

from __future__ import annotations

import json
import sqlite3

import pytest

import tools.patent_evidence_pack as pep
import tools.patent_fetch as pf
import tools.research_session as rs


NUMBER = "US10762444B2"
URL = f"https://patents.google.com/patent/{NUMBER}/en"
QUERY = "wireless soil moisture irrigation controller"
API_SECRET = "SENTINEL_JINA_API_SECRET_9137"
BEARER_SECRET = "SENTINEL_UNCONFIGURED_BEARER_6819"
URL_SECRET = "SENTINEL_URL_PASSWORD_5418"
AUTH_URL = f"https://reader:{URL_SECRET}@example.org/patent?api_key={API_SECRET}"
SECRET_TEXT = f"{API_SECRET} Authorization: Bearer {BEARER_SECRET} {AUTH_URL}"
DOCUMENT_TEXT = (
    f"US 10,762,444 B2\nA document describes moisture telemetry calibration. "
    + ("A wireless soil moisture irrigation controller records measurements and controls the garden valve. " * 14)
)


@pytest.fixture(autouse=True)
def isolated_patent_cache():
    pf._PATENT_FETCH_CACHE.clear()
    yield
    pf._PATENT_FETCH_CACHE.clear()


def _candidate(**overrides):
    return {
        "title": QUERY, "url": URL, "patent_number": NUMBER,
        "snippet": "A wireless soil moisture irrigation controller uses measurements to control garden watering.",
        "provider": "synthetic", "score": 9.0, **overrides,
    }


def _search_and_verify(monkeypatch, *, candidates=None, errors=None, notes=None):
    async def search(**kwargs):
        return json.dumps({
            "status": "ok", "completed": True, "reliable_no_results": False,
            "results": [_candidate()] if candidates is None else candidates,
            "errors": errors or [], "notes": notes or [],
        })

    async def verify(urls, **kwargs):
        return "\n".join(f"Verification status for URL: {url} returned ALIVE" for url in urls)

    monkeypatch.setattr(pep, "patent_search", search)
    monkeypatch.setattr(pep, "verify_sources", verify)


def _reader_backends(monkeypatch, text=DOCUMENT_TEXT):
    async def reader(*args, **kwargs):
        return text

    async def blocked(*args, **kwargs):
        raise pf.BotBlockedError("synthetic blocked page")

    monkeypatch.setattr(pf, "fetch_via_jina", reader)
    monkeypatch.setattr(pf, "PATENT_FETCH_PROVIDERS", (pf.PatentFetchProvider("google_patents", blocked),))
    monkeypatch.setattr(pf, "_wayback_patent_fetch", blocked)


def _assert_secret_free(value):
    for secret in (API_SECRET, BEARER_SECRET, URL_SECRET, AUTH_URL):
        assert secret not in value


def _stored(temp_db):
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT evidence_json, normalized_json FROM evidence_results").fetchone()
    assert row is not None
    return row


async def test_actual_reader_content_and_level_reach_pack_and_persisted_evidence(monkeypatch, temp_db):
    _search_and_verify(monkeypatch)
    _reader_backends(monkeypatch)
    # Actual patent_fetch -> pack -> session writer; only external providers are stubbed.
    result = json.loads(await pep.patent_evidence_pack(QUERY))
    hit = result["hits"][0]
    assert hit["evidence_level"] == "fetched_excerpt"
    assert "moisture telemetry calibration" in hit["summary"]
    assert "No abstract" not in hit["summary"]
    assert "No first claim" not in hit["summary"]
    assert hit["exact_combination_candidate_found"] is False
    rs.research_session_start(QUERY, "reader-content")
    ack = json.loads(await rs.patent_evidence_to_session("reader-content", QUERY, attempt_no=1))
    assert ack["status"] == "written"
    for stored in _stored(temp_db):
        assert json.loads(stored)["hits"][0]["evidence_level"] == "fetched_excerpt"
        assert "moisture telemetry calibration" in stored


async def test_reader_content_secret_sentinels_do_not_reach_pack_or_storage(monkeypatch, temp_db):
    monkeypatch.setenv("JINA_API_KEY", API_SECRET)
    _search_and_verify(monkeypatch)
    _reader_backends(monkeypatch, DOCUMENT_TEXT[:30] + SECRET_TEXT + "\n" + DOCUMENT_TEXT)
    output = await pep.patent_evidence_pack(QUERY)
    _assert_secret_free(output)
    assert json.loads(output)["hits"][0]["evidence_level"] == "fetched_excerpt"
    rs.research_session_start(QUERY, "reader-secrets")
    ack = await rs.patent_evidence_to_session("reader-secrets", QUERY, attempt_no=1)
    _assert_secret_free(ack)
    for stored in _stored(temp_db):
        _assert_secret_free(stored)


async def test_search_and_attempt_metadata_are_sanitized_recursively_before_persistence(monkeypatch, temp_db):
    monkeypatch.setenv("JINA_API_KEY", API_SECRET)
    _search_and_verify(
        monkeypatch,
        candidates=[_candidate(title=QUERY + " " + SECRET_TEXT, provider=SECRET_TEXT,
                               snippet=SECRET_TEXT + " " + QUERY)],
        errors=[{"type": "synthetic", "message": SECRET_TEXT, "detail": {"url": AUTH_URL}}],
        notes=[SECRET_TEXT],
    )

    async def fetch(**kwargs):
        return (
            f"CONTENT: {DOCUMENT_TEXT}\nEVIDENCE_LEVEL: FETCHED_EXCERPT\n"
            f"PROVIDER: {SECRET_TEXT}\nATTEMPT_LOG_JSON: "
            + json.dumps([{"provider": SECRET_TEXT, "nested": {"error": SECRET_TEXT, "url": AUTH_URL}}])
        )

    monkeypatch.setattr(pep, "patent_fetch", fetch)
    output = await pep.patent_evidence_pack(QUERY)
    _assert_secret_free(output)
    assert "[REDACTED" in output
    rs.research_session_start(QUERY, "metadata-secrets")
    ack = await rs.patent_evidence_to_session("metadata-secrets", QUERY, attempt_no=1)
    _assert_secret_free(ack)
    for stored in _stored(temp_db):
        _assert_secret_free(stored)


@pytest.mark.parametrize("stage", ["search", "fetch", "verification"])
async def test_provider_exceptions_are_safe_in_returned_and_persisted_evidence(monkeypatch, temp_db, stage):
    _search_and_verify(monkeypatch)

    async def fetch(**kwargs):
        return f"CONTENT: {DOCUMENT_TEXT}\nEVIDENCE_LEVEL: FETCHED_EXCERPT"

    async def fail(*args, **kwargs):
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr(pep, "patent_fetch", fetch)
    monkeypatch.setattr(pep, {"search": "patent_search", "fetch": "patent_fetch",
                              "verification": "verify_sources"}[stage], fail)
    output = await pep.patent_evidence_pack(QUERY)
    _assert_secret_free(output)
    assert "RuntimeError" in output
    if stage == "fetch":
        assert json.loads(output)["hits"][0]["evidence_level"] == "search_snippet_only"
    rs.research_session_start(QUERY, f"exception-{stage}")
    ack = await rs.patent_evidence_to_session(f"exception-{stage}", QUERY, attempt_no=1)
    _assert_secret_free(ack)
    for stored in _stored(temp_db):
        _assert_secret_free(stored)
        assert "RuntimeError" in stored


@pytest.mark.parametrize("error", [RuntimeError(SECRET_TEXT), TimeoutError(SECRET_TEXT)])
async def test_unexpected_pack_exception_is_safe_in_session_failure(monkeypatch, temp_db, error):
    async def fail(**kwargs):
        raise error

    monkeypatch.setattr(rs, "patent_evidence_pack", fail)
    rs.research_session_start(QUERY, "unexpected-pack-error")
    ack = await rs.patent_evidence_to_session("unexpected-pack-error", QUERY, attempt_no=1)
    _assert_secret_free(ack)
    for stored in _stored(temp_db):
        _assert_secret_free(stored)
        assert type(error).__name__ in stored


async def test_missing_candidate_url_cannot_assign_next_publications_fetched_evidence(monkeypatch):
    _search_and_verify(monkeypatch, candidates=[
        _candidate(url="", patent_number="US9999999B2", title="Missing URL publication"),
        _candidate(),
    ])
    _reader_backends(monkeypatch)
    output = json.loads(await pep.patent_evidence_pack(QUERY))
    hits = {hit["patent_number"]: hit for hit in output["hits"]}
    assert hits["US9999999B2"]["evidence_level"] == "search_snippet_only"
    assert "moisture telemetry calibration" not in hits["US9999999B2"]["summary"]
    assert hits[NUMBER]["evidence_level"] == "fetched_excerpt"
    assert "moisture telemetry calibration" in hits[NUMBER]["summary"]


async def test_pack_redaction_negative_control_detects_original_exception_serialization(monkeypatch, temp_db):
    async def fail(**kwargs):
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr(pep, "patent_search", fail)
    _assert_secret_free(await pep.patent_evidence_pack(QUERY))
    # Restore the exact former str(exc) behaviour at the production boundary.
    monkeypatch.setattr(pep, "provider_error_message", str)
    with pytest.raises(AssertionError):
        _assert_secret_free(await pep.patent_evidence_pack(QUERY))
    rs.research_session_start(QUERY, "pack-defect-restoration")
    await rs.patent_evidence_to_session("pack-defect-restoration", QUERY, attempt_no=1)
    for stored in _stored(temp_db):
        with pytest.raises(AssertionError):
            _assert_secret_free(stored)


@pytest.mark.parametrize("defect", ["level", "content"])
async def test_reader_downstream_negative_control_detects_defect_restoration(monkeypatch, defect):
    _search_and_verify(monkeypatch)
    _reader_backends(monkeypatch)

    def assert_retained(hit):
        assert hit["evidence_level"] == "fetched_excerpt"
        assert "moisture telemetry calibration" in hit["summary"]

    assert_retained(json.loads(await pep.patent_evidence_pack(QUERY))["hits"][0])
    if defect == "level":
        fixed = pep._fetch_level
        # Restore the old unknown-level default for the previously missing mapping.
        monkeypatch.setattr(pep, "_fetch_level", lambda text: (
            "search_snippet_only" if pep._field(text, "EVIDENCE_LEVEL") == "FETCHED_EXCERPT" else fixed(text)
        ))
    else:
        fixed = pep._fetch_summary
        # Restore the omission of CONTENT while keeping every other fetched field.
        monkeypatch.setattr(pep, "_fetch_summary", lambda text: fixed(
            "\n".join(line for line in text.splitlines() if not line.startswith("CONTENT:"))
        ))
    with pytest.raises(AssertionError):
        assert_retained(json.loads(await pep.patent_evidence_pack(QUERY))["hits"][0])


async def test_writer_redaction_negative_control_detects_original_failure_serialization(monkeypatch, temp_db):
    async def fail(**kwargs):
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr(rs, "patent_evidence_pack", fail)
    monkeypatch.setattr(rs, "provider_error_message", str)
    rs.research_session_start(QUERY, "writer-defect-restoration")
    await rs.patent_evidence_to_session("writer-defect-restoration", QUERY, attempt_no=1)
    for stored in _stored(temp_db):
        with pytest.raises(AssertionError):
            _assert_secret_free(stored)
