"""Načítanie patentovej stránky a extrakcia základných patentových údajov."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

from .chromium_scraper import PlaywrightTimeoutError, fetch_page_html_and_text
from .output_cleaner import USER_AGENT, clean_output, first_match, format_error, soup_text, trim_words

FETCH_CACHE_TTL_SECONDS = 3600
MAX_FETCH_ATTEMPTS = 2
_PATENT_FETCH_CACHE: dict[str, tuple[float, str]] = {}

_STATIC_PATENT_COUNTRY_RE = re.compile(
    r"/patent/(?P<cc>CN|JP|KR|RU|IN|TW|HK|SG|BR|MX)\d", flags=re.IGNORECASE
)


def _patent_country_prefers_static(url: str) -> bool:
    """Zistí, či sa stránka patentu má načítať priamo cez statické HTTP."""
    return bool(_STATIC_PATENT_COUNTRY_RE.search(url or ""))


async def _static_patent_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Načíta patentovú stránku cez HTTP bez použitia prehliadača."""
    timeout_s = max(2.0, min(timeout_ms / 1000.0, 15.0))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=timeout_s,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        html = response.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True) if soup else ""
    return html, text


@dataclass(frozen=True)
class PatentFetchProvider:
    name: str
    fetch: Callable[[str, int], Awaitable[tuple[str, str]]]


async def _google_patents_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Načíta Google Patents stránku staticky alebo cez Chromium fallback."""
    if _patent_country_prefers_static(url):
        try:
            html, text = await _static_patent_fetch(url, timeout_ms)
            if html:
                return html, text
        except Exception:
            pass
    return await fetch_page_html_and_text(url, timeout_ms=max(5000, min(timeout_ms, 60000)))


PATENT_FETCH_PROVIDERS = (PatentFetchProvider("google_patents", _google_patents_fetch),)


def _extract_first_claim_from_html(soup: BeautifulSoup) -> str:
    """Pokúsi sa vytiahnuť prvý patentový nárok zo štruktúry HTML stránky."""
    claim_container_selectors = (
        "section.claims",
        "[itemprop='claims']",
        ".claims",
        "#claims",
    )
    child_selectors = (
        "claim claim-text",
        "claim .claim-text",
        "claim-text",
        ".claim-text",
        "[itemprop='claimText']",
        ".claim",
        "li",
        "p",
    )
    for container_selector in claim_container_selectors:
        container = soup.select_one(container_selector)
        if container is None:
            continue
        for child_selector in child_selectors:
            for child in container.select(child_selector):
                text = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
                if not text:
                    continue
                text = re.sub(r"^\s*1\s*[.)]\s*", "", text)
                if len(text.split()) >= 8:
                    return trim_words(text, 400)
    return ""


def _extract_claim1(claim_source: str) -> str:
    """Pokúsi sa vytiahnuť prvý patentový nárok z textového obsahu stránky."""
    start_patterns = [
        r"(?:I claim:|What is claimed(?: is)?:)\s*(?:1\.)?",
        r"^\s*1\.\s*",
    ]
    for pattern in start_patterns:
        match = re.search(pattern, claim_source, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            continue
        window = claim_source[match.end(): match.end() + 6000]
        boundary = re.search(
            r"(?:^\s*|(?<=[.;:])\s+)2\s*[.)]\s+|^\s*(?:description|abstract|background|summary)\s*$",
            window,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        claim = window[:boundary.start()] if boundary else window
        claim = re.sub(r"\s+", " ", claim).strip(" .\n\t")
        if len(claim.split()) >= 8:
            claim = claim if claim.startswith("1.") else f"1. {claim}"
            return trim_words(claim, 400)
    return "No first claim was extracted from this page."


def _with_fetch_diagnostics(body: str, provider: str, attempt_log: list[dict[str, object]]) -> str:
    """Doplní k výstupu fetch nástroja providera a diagnostiku pokusov."""
    return clean_output(
        "\n".join(
            [
                body,
                f"PROVIDER: {provider}",
                f"ATTEMPT_LOG_JSON: {json.dumps(attempt_log, ensure_ascii=False, separators=(',', ':'))}",
            ]
        )
    )


def _meta_content(soup: BeautifulSoup, *names_or_props: str) -> str:
    """Vráti obsah prvého HTML meta tagu so zadaným názvom alebo vlastnosťou."""
    for token in names_or_props:
        tag = (
            soup.select_one(f'meta[name="{token}"]')
            or soup.select_one(f'meta[property="{token}"]')
            or soup.select_one(f'meta[itemprop="{token}"]')
        )
        if tag and tag.get("content"):
            return tag.get("content").strip()
    return ""


def _extract_fields(url: str, html: str, rendered_text: str, provider: str, attempt_log: list[dict[str, object]]) -> str:
    """Vytiahne patentové polia z HTML a textu načítanej stránky."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("nav, header, footer, aside, script, style, img, svg, .related, .citations"):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    patent_no = first_match([r"\b([A-Z]{2,}\d+[A-Z0-9]*)\b"], f"{title} {url}")
    all_text = f"{rendered_text}\n{soup.get_text(' ', strip=True)}"
    filed = first_match([r"((?:19|20)\d{2}-\d{2}-\d{2})\s+Application filed", r"fil(?:ed|ing)\s*(?:date)?[:\s]+([A-Za-z0-9, -]+)"], all_text)
    assignee = first_match([r"Current Assignee\s+([^\n]+)", r"assignee(?: original)?[:\s]+([^.;\n]+)"], all_text)
    abstract = soup_text(soup.select_one(".abstract, [itemprop='abstract'], abstract"))
    if not abstract:
        abstract = first_match([r"3 Claims\.\s*(.{80,1200}?)(?=\nIn prior|\nThe present invention|\nIn the accompanying drawing|$)"], rendered_text, "")
    if not abstract or len(abstract.split()) < 10:
        meta_abstract = _meta_content(
            soup,
            "citation_abstract",
            "DC.Description",
            "DC.description",
            "description",
            "og:description",
            "twitter:description",
        )
        if meta_abstract and len(meta_abstract.split()) >= 10:
            abstract = meta_abstract
    if not abstract:
        abstract = "No abstract section was found."
    claims = soup_text(soup.select_one(".claims, #claims, section[itemprop='claims'], claim-text"))
    claim1 = _extract_first_claim_from_html(soup)
    if not claim1:
        claim1 = _extract_claim1(f"{claims}\n{rendered_text}")
    has_claim = "No first claim" not in claim1
    has_abstract = bool(abstract and "No abstract section" not in abstract)
    status = "OK" if has_claim else "PARTIAL" if has_abstract else "FAILED"
    evidence_level = "CLAIM_VERIFIED" if has_claim else "ABSTRACT_VERIFIED" if has_abstract else "FETCH_FAILED"
    return _with_fetch_diagnostics(
        "\n".join(
            [
                f"PATENT_NUMBER: {patent_no} was identified from the page.",
                f"FILED: {filed} was identified on the page.",
                f"ASSIGNEE: {assignee} was identified on the page.",
                f"CLAIM1: {trim_words(claim1, 80)}",
                f"ABSTRACT: {trim_words(abstract or 'No abstract section was found.', 50)}",
                f"STATUS: {status} was returned for this extraction.",
                f"EVIDENCE_LEVEL: {evidence_level}",
            ]
        ),
        provider,
        attempt_log,
    )


async def patent_fetch(url: str, timeout_ms: int = 30000) -> str:
    """Načíta patentovú stránku a vytiahne z nej základné údaje."""
    cache_key = f"{url}|{max(5000, min(timeout_ms, 60000))}"
    cached = _PATENT_FETCH_CACHE.get(cache_key)
    if cached and time.time() - cached[0] <= FETCH_CACHE_TTL_SECONDS:
        return cached[1]
    attempt_log: list[dict[str, object]] = []
    last_error = ""
    provider = PATENT_FETCH_PROVIDERS[0]
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            html, rendered_text = await provider.fetch(url, timeout_ms)
            attempt_log.append(
                {
                    "provider": provider.name,
                    "attempt": attempt,
                    "status": "ok",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            result = _extract_fields(url, html, rendered_text, provider.name, attempt_log)
            _PATENT_FETCH_CACHE[cache_key] = (time.time(), result)
            return result
        except PlaywrightTimeoutError:
            last_error = "timeout"
            attempt_log.append(
                {
                    "provider": provider.name,
                    "attempt": attempt,
                    "status": "timeout",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except Exception as exc:
            last_error = str(exc)
            attempt_log.append(
                {
                    "provider": provider.name,
                    "attempt": attempt,
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        if attempt < MAX_FETCH_ATTEMPTS:
            await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))

    if last_error == "timeout":
        return _with_fetch_diagnostics(
            "\n".join(
                [
                    "PATENT_NUMBER: Unknown was identified from the page.",
                    "FILED: Unknown was identified on the page.",
                    "ASSIGNEE: Unknown was identified on the page.",
                    "CLAIM1: No claim excerpt could be extracted because the request timed out.",
                    "ABSTRACT: No abstract excerpt could be extracted because the request timed out.",
                    "STATUS: TIMEOUT was returned for this extraction.",
                    "EVIDENCE_LEVEL: FETCH_TIMEOUT",
                ]
            ),
            provider.name,
            attempt_log,
        )
    return _with_fetch_diagnostics(
        format_error("patent_fetch", last_error or "fetch failed", url=url) + "\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED",
        provider.name,
        attempt_log,
    )
