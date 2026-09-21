"""Real fetch/pack/writer regressions with offline transport and secret sentinels."""

import importlib
import json

import httpx
import pytest

from tools import research_session as rs
from test_publication_partial_diagnostics import QUERY, _install

fetch = importlib.import_module("tools.publication_fetch")
pack = importlib.import_module("tools.publication_evidence_pack")
ABSTRACT = (
    "We evaluate smart door locks controlled by a mobile application with access codes. "
    "Experimental measurements demonstrate secure access control across multiple residential installations."
)
SECRET = "SENTINEL_publication_credential_746291"
URL = "https://publisher.example/article"


def transport(monkeypatch, *, html="", reader=None, error=None):
    real_client = httpx.AsyncClient

    def handler(request):
        if error:
            raise error
        return httpx.Response(403 if reader is not None else 200, text=html)

    monkeypatch.setattr(fetch.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))

    async def jina(*args, **kwargs):
        if isinstance(reader, Exception):
            raise reader
        return reader

    monkeypatch.setattr(fetch, "fetch_via_jina", jina)


def install_pack(monkeypatch):
    _install(monkeypatch)

    async def verify(*args, **kwargs):
        return ""

    async def no_pdf(*args, **kwargs):
        return ""

    monkeypatch.setattr(pack, "verify_sources", verify)
    monkeypatch.setattr(pack, "_resolve_pdf_url", no_pdf)


@pytest.mark.parametrize("markup", [
    '<meta name="citation_abstract" content="{}">',
    '<section class="abstract"><h2>Abstract</h2><p>{}</p></section>',
    '<div id="abstract">{}</div>',
])
async def test_identifiable_substantive_abstract(monkeypatch, markup):
    transport(monkeypatch, html=markup.format(ABSTRACT))
    output = await fetch.publication_fetch(URL)
    assert pack._fetch_level(output) == "abstract_verified"
    assert ABSTRACT in output


@pytest.mark.parametrize("markup", [
    "<title>Publisher landing page</title>",
    '<title>Study</title><meta name="citation_abstract" content="Unknown">',
    '<title>Study</title><meta name="citation_abstract" content="   ">',
    '<title>Study</title><section class="abstract">Sign in to read the full article and subscribe to our journal for more information.</section>',
    '<title>Study</title><section class="abstract"><nav>Home browse articles journals search authors contact help about support news subscribe</nav></section>',
])
async def test_landing_placeholder_and_chrome_are_metadata_only(monkeypatch, markup):
    transport(monkeypatch, html=markup)
    output = await fetch.publication_fetch(URL)
    assert pack._fetch_level(output) == "verified_metadata"
    assert "Fetched abstract:" not in pack._fetch_summary(output)
    assert "Fetched title:" in pack._fetch_summary(output)


async def test_generic_description_is_excerpt_not_abstract(monkeypatch):
    transport(monkeypatch, html=f'<meta name="description" content="{ABSTRACT}">')
    output = await fetch.publication_fetch(URL)
    assert pack._fetch_level(output) == "fetched_excerpt"
    assert ABSTRACT in pack._fetch_summary(output)


async def test_short_abstract_is_preserved_as_excerpt(monkeypatch):
    short = "Wireless sensors measure motor vibration and temperature during operation."
    transport(monkeypatch, html=f'<meta name="citation_abstract" content="{short}">')
    output = await fetch.publication_fetch(URL)
    assert pack._fetch_level(output) == "fetched_excerpt"
    assert short in pack._fetch_summary(output)


@pytest.mark.parametrize("reader,level", [
    (ABSTRACT, "fetched_excerpt"),
    ("Abstract\n" + ABSTRACT, "fetched_excerpt"),
    ("Unknown", "fetch_failed"),
    ("", "fetch_failed"),
    ("Sign in to read this article and subscribe to our journal for access to all publications.", "fetch_failed"),
])
async def test_reader_does_not_establish_abstract_structure(monkeypatch, reader, level):
    transport(monkeypatch, reader=reader)
    output = await fetch.publication_fetch(URL)
    assert pack._fetch_level(output) == level
    if level == "fetched_excerpt":
        assert ABSTRACT in pack._fetch_summary(output)


@pytest.mark.parametrize("html,level", [
    (f'<meta name="citation_abstract" content="{ABSTRACT}">', "abstract_verified"),
    ('<title>Study</title>', "verified_metadata"),
    ('<meta name="citation_abstract" content="Unknown"><title>Study</title>', "verified_metadata"),
    (f'<meta name="description" content="{ABSTRACT}">', "fetched_excerpt"),
])
async def test_real_fetch_pack_writer_persists_evidence_level(monkeypatch, temp_db, html, level):
    install_pack(monkeypatch)
    transport(monkeypatch, html=html)
    sid = json.loads(rs.research_session_start(QUERY))["session_id"]
    ack = json.loads(await rs.publication_evidence_to_session(sid, QUERY, attempt_no=1, max_fetches=6))
    with rs._connect() as conn:
        row = conn.execute("SELECT normalized_json FROM evidence_results WHERE session_id=?", (sid,)).fetchone()
        raw = conn.execute("SELECT evidence_level FROM raw_evidence_items WHERE session_id=?", (sid,)).fetchall()
    hits = json.loads(row[0])["hits"]
    assert hits and raw
    assert {hit["evidence_level"] for hit in hits} == {level}
    assert {item[0] for item in raw} == {level}
    assert ack


@pytest.mark.parametrize("stage", ["http", "reader", "fetch", "search", "verify", "writer", "timeout"])
async def test_secret_absent_from_fetch_pack_ack_and_all_sqlite_text(monkeypatch, temp_db, caplog, stage):
    install_pack(monkeypatch)
    error = httpx.ConnectError(f"Bearer {SECRET} https://provider.example/?api_key={SECRET}")
    transport(monkeypatch, html=f'<meta name="citation_abstract" content="{ABSTRACT}">',
              error=error if stage == "http" else None,
              reader=error if stage == "reader" else None)

    async def fail(*args, **kwargs):
        raise error

    if stage in {"http", "reader"}:
        text = await fetch.publication_fetch(URL + "?api_key=" + SECRET)
        assert SECRET not in text and "provider.example" not in text
        assert "ConnectError" in text
    elif stage in {"fetch", "search", "verify"}:
        monkeypatch.setattr(pack, {"fetch": "publication_fetch", "search": "publications_search", "verify": "verify_sources"}[stage], fail)
    if stage in {"writer", "timeout"}:
        if stage == "timeout":
            error = TimeoutError(SECRET)
        monkeypatch.setattr(rs, "publication_evidence_pack", fail)
    else:
        payload = await pack.publication_evidence_pack(QUERY)
        assert SECRET not in payload
        if stage != "search":
            assert json.loads(payload)["hits"], "failed verification must not discard discoveries"
    sid = json.loads(rs.research_session_start(QUERY))["session_id"]
    ack = await rs.publication_evidence_to_session(sid, QUERY, attempt_no=1, max_fetches=6)
    assert SECRET not in ack
    with rs._connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert SECRET not in dump
    assert "provider.example" not in dump
    assert SECRET not in caplog.text


async def test_reader_content_reaches_pack_and_storage_at_weaker_level(monkeypatch, temp_db):
    install_pack(monkeypatch)
    transport(monkeypatch, reader=ABSTRACT + " Additional observations establish reader provenance.")
    sid = json.loads(rs.research_session_start(QUERY))["session_id"]
    await rs.publication_evidence_to_session(sid, QUERY, attempt_no=1, max_fetches=6)
    with rs._connect() as conn:
        payload = json.loads(conn.execute("SELECT normalized_json FROM evidence_results WHERE session_id=?", (sid,)).fetchone()[0])
    assert payload["hits"]
    assert all(h["evidence_level"] == "fetched_excerpt" for h in payload["hits"])
    assert any("reader provenance" in h["summary"] for h in payload["hits"])


async def test_authenticated_fetch_url_and_echoed_credentials_are_redacted(monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", SECRET)
    transport(monkeypatch, html=f'<title>Study Bearer {SECRET}</title>')
    output = await fetch.publication_fetch(f"https://user:{SECRET}@publisher.example/article")
    assert SECRET not in output
    assert "user:" not in output
    assert "[REDACTED" in output


def test_blank_fetch_fields_do_not_consume_next_label():
    output = "ABSTRACT: \nCONTENT: Retrieved useful text\nEVIDENCE_LEVEL: FETCHED_EXCERPT"
    assert pack._field(output, "ABSTRACT") == ""
    assert "Fetched abstract:" not in pack._fetch_summary(output)
    assert "Retrieved useful text" in pack._fetch_summary(output)
