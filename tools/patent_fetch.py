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

from ._ttl_cache import TTLCache
from .chromium_scraper import PlaywrightTimeoutError, fetch_page_html_and_text
from .output_cleaner import USER_AGENT, clean_output, first_match, format_error, soup_text, trim_words
from .pdf_fetch import pdf_fetch_text
from .requirement_match import unique_coverage_tokens

FETCH_CACHE_TTL_SECONDS = 3600
MAX_FETCH_ATTEMPTS = 2
_PATENT_FETCH_CACHE: TTLCache[str, str] = TTLCache(
    ttl_seconds=FETCH_CACHE_TTL_SECONDS, max_entries=256
)

_STATIC_PATENT_COUNTRY_RE = re.compile(
    r"/patent/(?P<cc>CN|JP|KR|RU|IN|TW|HK|SG|BR|MX)\d", flags=re.IGNORECASE
)

# Google patents.google.com aktívne blokuje shlukované automatizované požiadavky
# ("...your computer or network may be sending automated queries...", HTTP 503).
# Live overenie ukázalo, že tento blok sa spúšťa aj pri jednej požiadavke z bežnej
# siete, keď sa v krátkom čase pošle veľa detailových fetchov naraz — preto
# obmedzujeme súbežnosť samostatným semaforom nezávisle od toho, koľko patentov
# sa spracúva paralelne na vyššej úrovni (patent_evidence_pack).
_GOOGLE_PATENTS_CONCURRENCY = 2
_GOOGLE_PATENTS_SEMAPHORE = asyncio.Semaphore(_GOOGLE_PATENTS_CONCURRENCY)
_BOT_BLOCK_MARKERS = (
    "sending automated queries",
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
)


def _is_bot_block_page(html: str) -> bool:
    """Rozpozná Google stránku s upozornením na automatizované požiadavky."""
    lower = (html or "")[:4000].lower()
    return any(marker in lower for marker in _BOT_BLOCK_MARKERS)


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


async def _wayback_patent_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Skúsi načítať archivovanú snímku patentovej stránky z Wayback Machine.

    Best-effort náhrada, keď Google Patents zablokuje priamy prístup; Wayback
    dostupnosť pre konkrétnu stránku nie je garantovaná.
    """
    avail_timeout = max(3.0, min(8.0, timeout_ms / 3000))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=avail_timeout
    ) as client:
        response = await client.get("https://archive.org/wayback/available", params={"url": url})
        payload = response.json() if response.status_code < 400 else {}
    closest = ((payload or {}).get("archived_snapshots") or {}).get("closest") or {}
    snapshot_url = str(closest.get("url") or "").strip()
    if not snapshot_url or not closest.get("available"):
        return "", ""
    fetch_timeout = max(5.0, min(15.0, timeout_ms / 1000))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=fetch_timeout
    ) as client:
        response = await client.get(snapshot_url)
        if response.status_code >= 400:
            return "", ""
        html = response.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True) if soup else ""
    return html, text


class BotBlockedError(RuntimeError):
    """Signalizuje, že zdroj zablokoval prístup ako podozrivý na automatizáciu."""


@dataclass(frozen=True)
class PatentFetchProvider:
    name: str
    fetch: Callable[[str, int], Awaitable[tuple[str, str]]]


async def _google_patents_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Načíta Google Patents stránku staticky alebo cez Chromium fallback.

    Súbežnosť voči patents.google.com je obmedzená spoločným semaforom, aby
    sa znížila šanca na spustenie ochrany proti automatizovaným požiadavkam.
    """
    if _patent_country_prefers_static(url):
        try:
            html, text = await _static_patent_fetch(url, timeout_ms)
            if html and not _is_bot_block_page(html):
                return html, text
        except Exception:
            pass
    async with _GOOGLE_PATENTS_SEMAPHORE:
        html, text = await fetch_page_html_and_text(
            url, timeout_ms=max(5000, min(timeout_ms, 60000))
        )
    if _is_bot_block_page(html):
        wayback_html, wayback_text = await _wayback_patent_fetch(url, timeout_ms)
        if wayback_html and not _is_bot_block_page(wayback_html):
            return wayback_html, wayback_text
        raise BotBlockedError("Google Patents returned an automated-query block page.")
    return html, text


PATENT_FETCH_PROVIDERS = (PatentFetchProvider("google_patents", _google_patents_fetch),)

# Markery, podľa ktorých sa v texte oficiálneho PDF rozpozná sekcia nárokov.
_PDF_CLAIMS_MARKERS = (
    "what is claimed",
    "i claim",
    "we claim",
    "the invention claimed is",
    "claims",
)
_PDF_MIN_WORDS = 200


def _pdf_claims_present(text: str) -> bool:
    """Zistí, či text oficiálneho PDF obsahuje sekciu patentových nárokov."""
    lower = (text or "").lower()
    return any(marker in lower for marker in _PDF_CLAIMS_MARKERS)


def _pdf_fields(url: str, pdf_url: str, text: str, attempt_log: list[dict[str, object]]) -> str:
    """Zostaví výstup patent_fetch z plného textu oficiálneho patentového PDF.

    Patentové PDF majú dvojstĺpcovú sadzbu a pri extrakcii sa riadky oboch
    stĺpcov prekladajú, takže doslovné znenie jedného nároku sa z nich nedá
    spoľahlivo odcitovať. Text je však plnohodnotný na overenie pokrytia
    prvkov dotazu (to pracuje s výskytom termínov), preto sa posiela ako
    COVERAGE_TOKENS a nie ako čitateľný citát.
    """
    has_claims = _pdf_claims_present(text)
    word_count = len(text.split())
    # CLAIM1 a ABSTRACT zámerne nesú štandardné "nenájdené" sentinely: doslovný
    # citát z dvojstĺpcového PDF by bol zlomený, preto sa nezobrazuje ani
    # nezapočítava do pokrytia. Skutočný obsah dokumentu ide cez COVERAGE_TOKENS.
    lines = [
        "PATENT_NUMBER: Unknown was identified from the page.",
        "FILED: Unknown was identified on the page.",
        "ASSIGNEE: Unknown was identified on the page.",
        "CLAIM1: No first claim quote was extracted because the official PDF uses a two-column layout.",
        "ABSTRACT: No abstract section was quoted; the full official PDF text was used for element coverage.",
        f"STATUS: {'OK' if has_claims else 'PARTIAL'} was returned for this extraction.",
        f"EVIDENCE_LEVEL: {'CLAIM_VERIFIED' if has_claims else 'ABSTRACT_VERIFIED'}",
        f"PDF_CLAIMS_SECTION: {'present' if has_claims else 'absent'}",
        f"PDF_SOURCE_URL: {pdf_url}",
        f"PDF_FULLTEXT_WORDS: {word_count}",
    ]
    body = clean_output("\n".join(lines))
    token_line = unique_coverage_tokens(text)
    if token_line:
        body += f"\nCOVERAGE_TOKENS: {token_line}"
    return _with_fetch_diagnostics(body, "google_patents_pdf", attempt_log)


async def _patent_pdf_fetch(pdf_url: str, timeout_ms: int) -> str:
    """Stiahne oficiálne patentové PDF a vráti z neho extrahovaný text."""
    timeout_s = max(10.0, min(timeout_ms / 1000.0 * 2, 40.0))
    return await pdf_fetch_text(pdf_url, timeout_s=timeout_s, max_pages=30)


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
    claims_container = soup.select_one(".claims, #claims, section[itemprop='claims']")
    claims = soup_text(claims_container if claims_container is not None else soup.select_one("claim-text"))
    description = ""
    for selector in (
        "section[itemprop='description']",
        "#descriptionText",
        "#description",
        ".description",
        "[itemprop='description']",
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = soup_text(node)
        if len(text.split()) >= 20:
            description = text
            break
    claim1 = _extract_first_claim_from_html(soup)
    if not claim1:
        claim1 = _extract_claim1(f"{claims}\n{rendered_text}")
    has_claim = "No first claim" not in claim1
    has_abstract = bool(abstract and "No abstract section" not in abstract)
    status = "OK" if has_claim else "PARTIAL" if has_abstract else "FAILED"
    evidence_level = "CLAIM_VERIFIED" if has_claim else "ABSTRACT_VERIFIED" if has_abstract else "FETCH_FAILED"
    claims_compact = re.sub(r"\s+", " ", claims or "").strip()
    description_compact = re.sub(r"\s+", " ", description or "").strip()
    lines = [
        f"PATENT_NUMBER: {patent_no} was identified from the page.",
        f"FILED: {filed} was identified on the page.",
        f"ASSIGNEE: {assignee} was identified on the page.",
        f"CLAIM1: {trim_words(claim1, 80)}",
        f"ABSTRACT: {trim_words(abstract or 'No abstract section was found.', 50)}",
        f"STATUS: {status} was returned for this extraction.",
        f"EVIDENCE_LEVEL: {evidence_level}",
    ]
    if claims_compact and len(claims_compact.split()) >= 8:
        lines.append(f"CLAIMS_TEXT: {trim_words(claims_compact, 1200)}")
    if description_compact and len(description_compact.split()) >= 20:
        lines.append(f"DESCRIPTION_TEXT: {trim_words(description_compact, 1500)}")
    return _with_fetch_diagnostics("\n".join(lines), provider, attempt_log)


async def patent_fetch(url: str, timeout_ms: int = 30000, pdf_url: str = "") -> str:
    """Načíta patentový dokument a vytiahne z neho základné údaje.

    Ak je známa adresa oficiálneho PDF, použije sa prednostne: obsahuje plný
    text nárokov aj opisu vynálezu a na rozdiel od HTML stránky nepodlieha
    ochrane proti automatizovaným požiadavkam. HTML stránka slúži ako záloha.
    """
    cache_key = f"{url}|{max(5000, min(timeout_ms, 60000))}|{pdf_url}"
    cached = _PATENT_FETCH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    attempt_log: list[dict[str, object]] = []
    last_error = ""
    was_blocked = False
    provider = PATENT_FETCH_PROVIDERS[0]

    if pdf_url:
        started = time.perf_counter()
        try:
            pdf_text = await _patent_pdf_fetch(pdf_url, timeout_ms)
        except Exception as exc:
            pdf_text = ""
            last_error = str(exc)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if pdf_text and len(pdf_text.split()) >= _PDF_MIN_WORDS:
            attempt_log.append(
                {
                    "provider": "google_patents_pdf",
                    "attempt": 1,
                    "status": "ok",
                    "word_count": len(pdf_text.split()),
                    "elapsed_ms": elapsed_ms,
                }
            )
            result = _pdf_fields(url, pdf_url, pdf_text, attempt_log)
            _PATENT_FETCH_CACHE.set(cache_key, result)
            return result
        attempt_log.append(
            {
                "provider": "google_patents_pdf",
                "attempt": 1,
                "status": "empty" if not last_error else "failed",
                "elapsed_ms": elapsed_ms,
            }
        )
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
            _PATENT_FETCH_CACHE.set(cache_key, result)
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
        except BotBlockedError as exc:
            was_blocked = True
            last_error = str(exc)
            attempt_log.append(
                {
                    "provider": provider.name,
                    "attempt": attempt,
                    "status": "blocked",
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

    if was_blocked:
        return _with_fetch_diagnostics(
            "\n".join(
                [
                    "PATENT_NUMBER: Unknown was identified from the page.",
                    "FILED: Unknown was identified on the page.",
                    "ASSIGNEE: Unknown was identified on the page.",
                    "CLAIM1: No claim excerpt could be extracted because the source blocked automated access.",
                    "ABSTRACT: No abstract excerpt could be extracted because the source blocked automated access.",
                    "STATUS: BLOCKED was returned for this extraction.",
                    "EVIDENCE_LEVEL: FETCH_BLOCKED",
                ]
            ),
            provider.name,
            attempt_log,
        )
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
