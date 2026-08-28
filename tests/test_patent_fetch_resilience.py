"""Testy odolnosti patent_fetch voči blokovaniu zo strany Google Patents."""

from __future__ import annotations

import asyncio

import tools.patent_fetch as pf

BLOCK_HTML = (
    "<html><head><title>Sorry...</title></head><body>"
    "but your computer or network may be sending automated queries."
    "</body></html>"
)
REAL_HTML = (
    "<html><head><title>US10831585B2 - Smart lock</title></head><body>"
    "<section itemprop='abstract'><div>A smart lock system with a mobile application.</div></section>"
    "<section itemprop='claims'><claim><div class='claim-text'>1. A smart lock system comprising "
    "a mobile application interface for remote control of the lock mechanism.</div></claim></section>"
    "</body></html>"
)


def test_is_bot_block_page_detects_known_markers():
    assert pf._is_bot_block_page(BLOCK_HTML) is True
    assert pf._is_bot_block_page(REAL_HTML) is False
    assert pf._is_bot_block_page("") is False


async def test_google_patents_fetch_raises_when_blocked_and_wayback_unavailable(monkeypatch):
    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        return BLOCK_HTML, "blocked text"

    async def fake_wayback(url, timeout_ms):
        return "", ""

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    monkeypatch.setattr(pf, "_wayback_patent_fetch", fake_wayback)

    import pytest

    with pytest.raises(pf.BotBlockedError):
        await pf._google_patents_fetch("https://patents.google.com/patent/US10831585B2/en", 30000)


async def test_google_patents_fetch_recovers_via_wayback(monkeypatch):
    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        return BLOCK_HTML, "blocked text"

    async def fake_wayback(url, timeout_ms):
        return REAL_HTML, "real text"

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    monkeypatch.setattr(pf, "_wayback_patent_fetch", fake_wayback)

    html, text = await pf._google_patents_fetch(
        "https://patents.google.com/patent/US10831585B2/en", 30000
    )
    assert html == REAL_HTML
    assert text == "real text"


async def test_google_patents_fetch_passthrough_when_not_blocked(monkeypatch):
    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        return REAL_HTML, "real text"

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    html, text = await pf._google_patents_fetch(
        "https://patents.google.com/patent/US10831585B2/en", 30000
    )
    assert html == REAL_HTML


async def test_google_patents_fetch_respects_concurrency_semaphore(monkeypatch):
    active = 0
    peak = 0

    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return REAL_HTML, "real text"

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    await asyncio.gather(
        *[
            pf._google_patents_fetch(f"https://patents.google.com/patent/US{i}/en", 30000)
            for i in range(6)
        ]
    )
    assert peak <= pf._GOOGLE_PATENTS_CONCURRENCY


async def test_patent_fetch_end_to_end_reports_blocked_status(monkeypatch):
    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        return BLOCK_HTML, "blocked text"

    async def fake_wayback(url, timeout_ms):
        return "", ""

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    monkeypatch.setattr(pf, "_wayback_patent_fetch", fake_wayback)

    result = await pf.patent_fetch("https://patents.google.com/patent/US99999999B2/en", timeout_ms=5000)
    assert "STATUS: BLOCKED was returned for this extraction." in result
    assert "EVIDENCE_LEVEL: FETCH_BLOCKED" in result


async def test_patent_fetch_end_to_end_succeeds_when_not_blocked(monkeypatch):
    async def fake_fetch_page_html_and_text(url, timeout_ms=60000):
        return REAL_HTML, "real text"

    monkeypatch.setattr(pf, "fetch_page_html_and_text", fake_fetch_page_html_and_text)
    result = await pf.patent_fetch("https://patents.google.com/patent/US10831585B2/en", timeout_ms=5000)
    assert "EVIDENCE_LEVEL: CLAIM_VERIFIED" in result
    assert "mobile application" in result
