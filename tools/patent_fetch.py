"""Fetching a patent page and extracting its basic patent fields."""

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
from ._provider_errors import provider_error_message
from .patent_safety import public_document_url, patent_document_url, guard_patent_request, redact_patent_text
from .chromium_scraper import PlaywrightTimeoutError, fetch_page_html_and_text
from .jina_reader import fetch_via_jina_preserving_structure as fetch_via_jina
from .patent_identity import (
    extract_abstract_section,
    extract_claims_section,
    evidence_level_for_content,
    promote_evidence_level,
    recognised_claims_section,
    resolve_identity,
    content_contains_publication,
    exact_publication_identity,
    publication_in_header,
    section_unavailable as _section_unavailable,
)
from .output_cleaner import USER_AGENT, first_match, soup_text, trim_words
from .pdf_fetch import pdf_fetch_text
from .requirement_match import unique_coverage_tokens

FETCH_CACHE_TTL_SECONDS = 3600
MAX_FETCH_ATTEMPTS = 2
_PATENT_FETCH_CACHE: TTLCache[str, str] = TTLCache(
    ttl_seconds=FETCH_CACHE_TTL_SECONDS, max_entries=256
)

# patents.google.com actively blocks bursts of automated requests
# ("...your computer or network may be sending automated queries...", HTTP 503).
# Live testing showed the block triggers even on a single request from an
# ordinary network when many detail fetches are sent in quick succession, so
# concurrency is capped by a dedicated semaphore independently of how many
# patents are being processed in parallel at the level above
# (patent_evidence_pack).
_GOOGLE_PATENTS_CONCURRENCY = 2
_GOOGLE_PATENTS_SEMAPHORE = asyncio.Semaphore(_GOOGLE_PATENTS_CONCURRENCY)
_BOT_BLOCK_MARKERS = (
    "sending automated queries",
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "verify you are human",
    "verify that you are human",
    "complete the captcha",
    "<title>access denied",
    "just a moment...",
)


def _is_bot_block_page(html: str) -> bool:
    """Recognise the Google page warning about automated queries."""
    lower = (html or "")[:4000].lower()
    return any(marker in lower for marker in _BOT_BLOCK_MARKERS)


async def _static_patent_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Fetch a patent page over HTTP without using a browser."""
    timeout_s = max(2.0, min(timeout_ms / 1000.0, 15.0))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=timeout_s,
        event_hooks={"request": [guard_patent_request]},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        html = response.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True) if soup else ""
    return html, text


async def _wayback_patent_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Try to read an archived snapshot of a patent page from the Wayback Machine.

    A best-effort substitute when Google Patents blocks direct access. Wayback
    coverage of any particular page is not guaranteed.
    """
    avail_timeout = max(3.0, min(8.0, timeout_ms / 3000))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=avail_timeout,
        event_hooks={"request": [guard_patent_request]},
    ) as client:
        response = await client.get("https://archive.org/wayback/available", params={"url": url})
        payload = response.json() if response.status_code < 400 else {}
    closest = ((payload or {}).get("archived_snapshots") or {}).get("closest") or {}
    snapshot_url = str(closest.get("url") or "").strip()
    if not snapshot_url or not closest.get("available") or not patent_document_url(snapshot_url):
        return "", ""
    fetch_timeout = max(5.0, min(15.0, timeout_ms / 1000))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=fetch_timeout,
        event_hooks={"request": [guard_patent_request]},
    ) as client:
        response = await client.get(snapshot_url)
        if response.status_code >= 400:
            return "", ""
        html = response.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True) if soup else ""
    return html, text


class BotBlockedError(RuntimeError):
    """Raised when a source blocks access as suspected automation."""


@dataclass(frozen=True)
class PatentFetchProvider:
    name: str
    fetch: Callable[[str, int], Awaitable[tuple[str, str]]]


async def _google_patents_fetch(url: str, timeout_ms: int) -> tuple[str, str]:
    """Render normally, without solving challenges or changing fingerprints."""
    async with _GOOGLE_PATENTS_SEMAPHORE:
        html, text = await fetch_page_html_and_text(
            url, timeout_ms=max(5000, min(timeout_ms, 60000)), allowed_url=patent_document_url
        )
    if _is_bot_block_page(html) or _is_bot_block_page(text):
        raise BotBlockedError("Patent source returned a challenge page.")
    return html, text


PATENT_FETCH_PROVIDERS = (
    PatentFetchProvider("google_patents_http", _static_patent_fetch),
    PatentFetchProvider("google_patents", _google_patents_fetch),
)
_PDF_MIN_WORDS = 200


def _pdf_claims_present(text: str) -> bool:
    """Require an identifiable, substantive claims section."""
    return recognised_claims_section(text)


def _pdf_fields(url: str, pdf_url: str, text: str, attempt_log: list[dict[str, object]]) -> str:
    """Keep auditable extracted PDF text and label only identified sections.

    PDF extraction can interleave columns; these are extraction excerpts, not
    certified quotations. The existing whole-document coverage tokens remain.
    """
    text = redact_patent_text(text)
    number = _patent_number_from_url(url)
    if not content_contains_publication(text, number) or _is_bot_block_page(text):
        return _with_fetch_diagnostics("STATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED", "google_patents_pdf", attempt_log)
    level = evidence_level_for_content(content=text, identity="normalised_number_in_content", min_words=_PDF_MIN_WORDS)
    if level == "search_snippet_only":
        return _with_fetch_diagnostics("STATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED", "google_patents_pdf", attempt_log)
    claim, abstract = _jina_sections(text)
    body = _reader_result(url, number, level, text, claim, abstract, [])
    body = _without_diagnostics(body)
    body += (
        f"\nPDF_CLAIMS_SECTION: {'present' if claim else 'absent'}"
        f"\nPDF_SOURCE_URL: {pdf_url}\nPDF_FULLTEXT_WORDS: {len(text.split())}"
    )
    return _with_fetch_diagnostics(body, "google_patents_pdf", attempt_log)


async def _patent_pdf_fetch(pdf_url: str, timeout_ms: int) -> str:
    """Download the official patent PDF and return the text extracted from it."""
    timeout_s = max(10.0, min(timeout_ms / 1000.0 * 2, 40.0))
    return await pdf_fetch_text(pdf_url, timeout_s=timeout_s, max_pages=30, request_guard=guard_patent_request)


def _extract_first_claim_from_html(soup: BeautifulSoup) -> str:
    """Try to extract the first patent claim from the HTML page structure."""
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
                if _section_unavailable(text, "claims?"):
                    continue
                if child_selector in {"li", "p", ".claim"} and not re.match(r"^1\s*[.)]\s+", text):
                    continue
                text = re.sub(r"^\s*1\s*[.)]\s*", "", text)
                if len(text.split()) >= 8:
                    return trim_words(text, 400)
    return ""


def _extract_claim1(claim_source: str) -> str:
    """Try to extract the first patent claim from the page's text content."""
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
    """Add the provider and the attempt diagnostics to a fetch tool's output."""
    return redact_patent_text(
        "\n".join(
            [
                body,
                f"PROVIDER: {provider}",
                f"ATTEMPT_LOG_JSON: {json.dumps(attempt_log, ensure_ascii=False, separators=(',', ':'))}",
            ]
        )
    )


def _meta_content(soup: BeautifulSoup, *names_or_props: str) -> str:
    """Return the content of the first HTML meta tag with the given name or property."""
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
    """Extract the patent fields from the fetched page's HTML and text."""
    html = redact_patent_text(html)
    rendered_text = redact_patent_text(rendered_text)
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("nav, header, footer, aside, script, style, img, svg, .related, .citations"):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    expected = _patent_number_from_url(url)
    declared = _meta_content(soup, "citation_patent_number", "DC.relation")
    patent_no = publication_in_header(declared) or publication_in_header(title)
    if not patent_no:
        patent_no = publication_in_header(soup.get_text(" ", strip=True))
    if not expected or patent_no != exact_publication_identity(expected):
        return _with_fetch_diagnostics("STATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED", provider, attempt_log)
    all_text = f"{rendered_text}\n{soup.get_text(' ', strip=True)}"
    filed = first_match([r"((?:19|20)\d{2}-\d{2}-\d{2})\s+Application filed", r"fil(?:ed|ing)\s*(?:date)?[:\s]+([A-Za-z0-9, -]+)"], all_text)
    assignee = first_match([r"Current Assignee\s+([^\n]+)", r"assignee(?: original)?[:\s]+([^.;\n]+)"], all_text)
    abstract = soup_text(soup.select_one(".abstract, [itemprop='abstract'], abstract"))
    if not abstract or len(abstract.split()) < 10:
        abstract = _meta_content(soup, "citation_abstract")
    if len(abstract.split()) < 10:
        abstract = ""
    if _section_unavailable(abstract, "abstract"):
        abstract = ""
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
        claim1 = _extract_claim1(claims) if claims else ""
    if not claim1 or "No first claim" in claim1:
        claim1 = extract_claims_section(rendered_text) or "No first claim was extracted from this page."
    if _section_unavailable(claim1, "claims?"):
        claim1 = "No first claim was extracted from this page."
    has_claim = "No first claim" not in claim1
    has_abstract = bool(abstract and "No abstract section" not in abstract)
    status = "OK" if has_claim else "PARTIAL" if has_abstract else "FAILED"
    evidence_level = "CLAIM_VERIFIED" if has_claim else "ABSTRACT_VERIFIED" if has_abstract else "FETCH_FAILED"
    excerpt = soup.get_text(" ", strip=True)
    if evidence_level == "FETCH_FAILED" and len(excerpt.split()) >= _JINA_MIN_WORDS:
        evidence_level, status = "FETCHED_EXCERPT", "PARTIAL"
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
    if evidence_level == "FETCHED_EXCERPT":
        lines.append(f"CONTENT: {trim_words(excerpt, 400)}")
    if claims_compact and len(claims_compact.split()) >= 8:
        lines.append(f"CLAIMS_TEXT: {trim_words(claims_compact, 1200)}")
    if description_compact and len(description_compact.split()) >= 20:
        lines.append(f"DESCRIPTION_TEXT: {trim_words(description_compact, 1500)}")
    return _with_fetch_diagnostics("\n".join(lines), provider, attempt_log)


def _patent_number_from_url(url: str) -> str:
    """Extract the publication number from a canonical patent URL.

    The canonical URL is the one identity the fetch layer always has, whichever
    provider discovered the candidate.
    """
    match = re.search(r"/patent/([A-Za-z]{2}[A-Za-z0-9]+)", str(url or ""))
    return match.group(1).upper() if match else ""


def _evidence_level_of(fetch_output: str) -> str:
    """Read the EVIDENCE_LEVEL marker back out of a fetch result."""
    match = re.search(r"^EVIDENCE_LEVEL:\s*(\S+)", fetch_output or "", re.MULTILINE)
    return match.group(1).strip().lower() if match else ""


def _reader_result(
    url: str,
    patent_number: str,
    level: str,
    content: str,
    claim_text: str,
    abstract_text: str,
    attempt_log: list[dict[str, object]],
) -> str:
    """Build a fetch result from Reader content, retaining auditable evidence.

    A claim_verified or abstract_verified result must carry the actual text it
    was promoted on, not only COVERAGE_TOKENS -- otherwise the level cannot be
    audited. Nothing is fabricated: the fields hold the recognised section
    verbatim, and the standard not-found sentinels are used when a section was
    not recognised.
    """
    claim_line = (
        f"CLAIM1: {trim_words(claim_text, 220)}"
        if claim_text
        else "CLAIM1: No first claim was extracted from the page."
    )
    abstract_line = (
        f"ABSTRACT: {trim_words(abstract_text, 160)}"
        if abstract_text
        else "ABSTRACT: No abstract was extracted from the page."
    )
    lines = [
        f"PATENT_NUMBER: {patent_number or 'Unknown'} was identified from the page.",
        "FILED: Unknown was identified on the page.",
        "ASSIGNEE: Unknown was identified on the page.",
        claim_line,
        abstract_line,
        "STATUS: OK was returned for this extraction.",
        f"EVIDENCE_LEVEL: {level.upper()}",
        f"CONTENT: {trim_words(content, 400)}",
        f"COVERAGE_TOKENS: {unique_coverage_tokens(content)}",
    ]
    return _with_fetch_diagnostics("\n".join(lines), "reader", attempt_log)


_JINA_MIN_WORDS = 120


async def _jina_patent_fetch(target_url: str, timeout_ms: int) -> str:
    """Fetch a patent document through the Reader backend. Fail-open.

    Reader is used as an alternate legitimate fetch backend, not as a means of
    circumventing access controls. It reads PDFs as well as HTML, so it serves
    as a second route to the official patent PDF when direct extraction fails.
    The optional JINA_API_KEY only raises the rate limit; without it the call is
    still attempted, and any failure falls through to the next source.
    """
    try:
        timeout_s = max(3.0, min(float(timeout_ms) / 1000.0, 30.0))
        return await fetch_via_jina(target_url, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 - an alternate backend must never break the chain
        return ""


def _jina_evidence(content: str, url: str, pdf_url: str, patent_number: str) -> tuple[str, str]:
    """Return (evidence_level, validated_content) for Reader output.

    Rejects block pages and error pages before anything else, then requires
    identity before promoting past a snippet. Substantive text alone reaches
    fetched_excerpt; claim_verified and abstract_verified additionally require a
    structurally recognised section, so Reader merely returning text -- or prose
    containing the word "claims" -- cannot produce a strong level.
    """
    # Preserve single-word section headings; clean_output drops e.g. Abstract.
    text = redact_patent_text(content or "").strip()
    marker = re.search(r"(?im)^Markdown Content:\s*", text)
    if marker:
        text = text[marker.end():]
    if not text or _is_bot_block_page(text):
        return ("", "")
    # Reader output is untrusted content from an alternate backend. A matching
    # canonical or PDF URL proves only which document was REQUESTED -- a redirect,
    # an interstitial or a different page would still satisfy it. Promoting
    # retrieved content therefore requires the content itself to carry the
    # requested publication number.
    identity = resolve_identity(patent_number=patent_number, content=text)
    if identity != "normalised_number_in_content":
        return ("", "")
    level = evidence_level_for_content(
        content=text, identity=identity, min_words=_JINA_MIN_WORDS
    )
    if level == "search_snippet_only":
        return ("", "")
    return (level, text)


def _jina_sections(content: str) -> tuple[str, str]:
    """Extract the recognised claims and abstract text, for auditability."""
    return (extract_claims_section(content), extract_abstract_section(content))


def _without_diagnostics(output: str) -> str:
    return "\n".join(line for line in output.splitlines() if not line.startswith(("PROVIDER:", "ATTEMPT_LOG_JSON:")))


def _backend_timeout(timeout_ms: int) -> float:
    return max(5.0, min(float(timeout_ms) / 1000, 60.0))


def patent_fetch_budget_seconds(timeout_ms: int, has_pdf: bool = True) -> float:
    """Bound the entire upgrade chain, without changing document/page depth."""
    seconds = _backend_timeout(timeout_ms)
    pdf_budget = max(10.0, min(seconds * 2, 40.0)) + seconds if has_pdf else 0.0
    return pdf_budget + (len(PATENT_FETCH_PROVIDERS) * MAX_FETCH_ATTEMPTS + 2) * seconds + 5.0


async def patent_fetch(url: str, timeout_ms: int = 30000, pdf_url: str = "") -> str:
    """Public MCP entry point; its signature remains unchanged."""
    return await patent_fetch_for_candidate(url, timeout_ms, pdf_url)


async def patent_fetch_for_candidate(
    url: str, timeout_ms: int = 30000, pdf_url: str = "", *, patent_number: str = "",
) -> str:
    """Try legitimate backends until claims are verified or all are exhausted.

    Every result must identify the exact publication. Evidence upgrades are
    monotonic; equal-strength results keep the first backend deterministically.
    """
    if not public_document_url(url):
        return "STATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED\nREASON: Non-public document URL rejected."
    number = exact_publication_identity(patent_number or _patent_number_from_url(url))
    if not re.fullmatch(r"[A-Z]{2}\d+(?:[A-Z]\d{0,2})?", number):
        return "STATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED\nREASON: Exact publication identity unavailable."
    # Discovery URLs remain in the evidence pack. They are not fetch targets:
    # verification uses the retained publication identity and existing hosts.
    url = f"https://patents.google.com/patent/{number}/en"
    cache_key = f"{url}|{max(5000, min(timeout_ms, 60000))}|{pdf_url}"
    cached = _PATENT_FETCH_CACHE.get(cache_key)
    if cached is not None:
        return redact_patent_text(cached)
    attempt_log: list[dict[str, object]] = []
    best_output, best_level, best_provider = "", "", ""
    last_error, was_blocked = "", False
    seconds = _backend_timeout(timeout_ms)

    def consider(output: str, provider: str) -> None:
        nonlocal best_output, best_level, best_provider
        level = _evidence_level_of(output)
        if level not in {"claim_verified", "abstract_verified", "fetched_excerpt"}:
            return
        if promote_evidence_level(best_level, level) != best_level:
            best_output, best_level, best_provider = _without_diagnostics(output), level, provider

    def finish() -> str:
        result = _with_fetch_diagnostics(best_output, best_provider, attempt_log)
        _PATENT_FETCH_CACHE.set(cache_key, result)
        return result

    async def read_backend(name: str, call: Callable[[], Awaitable[object]], limit: float, attempt: int = 1) -> object:
        nonlocal last_error, was_blocked
        entry: dict[str, object] = {"provider": name, "attempt": attempt}
        started = time.perf_counter()
        try:
            value = await asyncio.wait_for(call(), timeout=limit)
            text = " ".join(str(part) for part in value) if isinstance(value, tuple) else str(value or "")
            if _is_bot_block_page(text):
                raise BotBlockedError("Source returned a challenge page.")
            entry["status"] = "ok" if text.strip() else "empty"
            return value
        except (asyncio.TimeoutError, PlaywrightTimeoutError):
            last_error, entry["status"] = "timeout", "timeout"
        except BotBlockedError:
            was_blocked, entry["status"] = True, "blocked"
        except Exception as exc:
            last_error = provider_error_message(exc)
            entry.update(status="failed", error_type=type(exc).__name__)
        finally:
            entry["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            attempt_log.append(entry)
        return None

    async def reader(target: str, name: str) -> None:
        # Call Reader directly here so errors stay observable in the attempt log.
        raw = await read_backend(name, lambda: fetch_via_jina(target, timeout_s=min(seconds, 30.0)), seconds)
        level, content = _jina_evidence(str(raw or ""), url, pdf_url, number)
        if level:
            claim, abstract = _jina_sections(content)
            consider(_reader_result(url, number, level, content, claim, abstract, []), name)
            attempt_log[-1]["evidence_level"] = level
        elif raw:
            attempt_log[-1]["status"] = "rejected"

    if pdf_url and patent_document_url(pdf_url):
        raw = await read_backend("google_patents_pdf", lambda: _patent_pdf_fetch(pdf_url, timeout_ms), max(10.0, min(seconds * 2, 40.0)))
        if raw:
            output = _pdf_fields(url, pdf_url, str(raw), [])
            consider(output, "google_patents_pdf")
            if _evidence_level_of(output) == "fetch_failed":
                attempt_log[-1]["status"] = "rejected"
            else:
                attempt_log[-1]["evidence_level"] = _evidence_level_of(output)
        if best_level == "claim_verified":
            return finish()
        await reader(pdf_url, "google_patents_pdf_jina")
        if best_level == "claim_verified":
            return finish()
    elif pdf_url:
        attempt_log.append({"provider": "google_patents_pdf", "attempt": 1, "status": "unsafe_url_rejected"})

    for provider in PATENT_FETCH_PROVIDERS:
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            raw = await read_backend(provider.name, lambda: provider.fetch(url, timeout_ms), seconds, attempt)
            if raw:
                html, rendered = raw
                output = _extract_fields(url, html, rendered, provider.name, [])
                consider(output, provider.name)
                level = _evidence_level_of(output)
                attempt_log[-1]["evidence_level"] = level
                if level == "fetch_failed":
                    attempt_log[-1]["status"] = "rejected"
                break
            if attempt_log[-1]["status"] in {"blocked", "empty"}:
                break
            if attempt < MAX_FETCH_ATTEMPTS:
                await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
        if best_level == "claim_verified":
            return finish()

    await reader(url, "google_patents_html_jina")
    if best_level == "claim_verified":
        return finish()
    raw = await read_backend("wayback", lambda: _wayback_patent_fetch(url, timeout_ms), seconds)
    if raw:
        html, rendered = raw
        output = _extract_fields(url, html, rendered, "wayback", [])
        consider(output, "wayback")
        attempt_log[-1]["evidence_level"] = _evidence_level_of(output)
        if _evidence_level_of(output) == "fetch_failed":
            attempt_log[-1]["status"] = "rejected"
    if best_output:
        return finish()
    level = "FETCH_BLOCKED" if was_blocked else "FETCH_TIMEOUT" if last_error == "timeout" else "FETCH_FAILED"
    status = "BLOCKED" if was_blocked else "TIMEOUT" if last_error == "timeout" else "FAILED"
    return _with_fetch_diagnostics(
        f"STATUS: {status} was returned for this extraction.\nEVIDENCE_LEVEL: {level}\n"
        f"REASON: {last_error or 'No verified document content retrieved.'}",
        "none", attempt_log,
    )
