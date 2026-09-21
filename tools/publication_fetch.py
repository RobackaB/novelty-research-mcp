"""Fetching a scholarly publication page and extracting its metadata."""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from ._provider_errors import provider_error_message
from .jina_reader import fetch_via_jina
from .output_cleaner import USER_AGENT, clean_output, format_error, trim_words
from .patent_safety import redact_patent_text


def _substantive_text(value: str, minimum_words: int = 12) -> bool:
    """Conservatively reject placeholders and access/navigation boilerplate."""
    words = re.findall(r"[^\W\d_]+", value, flags=re.UNICODE)
    return (
        len(words) >= minimum_words and len({word.lower() for word in words}) >= min(8, minimum_words)
        and not re.search(
            r"\b(?:unknown|abstract (?:unavailable|not available)|no abstract|"
            r"sign in|log in|subscribe|access denied|enable javascript|captcha|"
            r"cookie|privacy policy|all rights reserved|skip to|navigation)\b",
            value, re.I,
        )
    )


def _abstract(soup: BeautifulSoup, minimum_words: int = 12) -> str:
    """Only explicit abstract markup can establish abstract provenance."""
    for node in soup.select("meta[name='citation_abstract'], section.abstract, div.abstract, [id='abstract']"):
        # Do not let nested navigation masquerade as an abstract section.
        fragment = BeautifulSoup(str(node), "lxml")
        for chrome in fragment.select("nav, header, footer, script, style, form"):
            chrome.decompose()
        value = node.get("content") if node.name == "meta" else fragment.get_text(" ", strip=True)
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if _substantive_text(value, minimum_words):
            return value
    return ""


def _meta(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    """Return the content of the first available CSS selector holding metadata or text."""
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") or node.get_text(" ", strip=True)
            if value:
                value = re.sub(r"\s+", " ", value).strip()
                if value and value.lower() not in {"unknown", "n/a", "none"}:
                    return value
    return "Unknown"


async def publication_fetch(url: str, timeout_s: float = 12.0) -> str:
    """Fetch a publication page and extract its basic metadata."""
    try:
        html = ""
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=max(5.0, min(timeout_s, 30.0))) as client:
            response = await client.get(url)
            if response.status_code < 400 and not re.search(r"captcha|enable javascript|access denied", response.text, re.I):
                html = response.text
        if not html:
            content = redact_patent_text(await fetch_via_jina(url))
            # Reader's shared cleaner removes section headings. Its arbitrary
            # page text cannot establish an identifiable abstract boundary.
            excerpt = trim_words(content, 120) if _substantive_text(content, 6) else ""
            level = "FETCHED_EXCERPT" if excerpt else "FETCH_FAILED"
            return redact_patent_text(clean_output(
                f"SOURCE_URL: {url} was the publication page requested.\n"
                "TITLE: Unknown was extracted from the publication page.\n"
                "YEAR: Unknown was extracted from the publication page.\n"
                f"CONTENT: {excerpt}\n"
                "STATUS: JINA_FALLBACK was returned for this publication fetch.\n"
                f"EVIDENCE_LEVEL: {level}"
            ))
        soup = BeautifulSoup(html, "lxml")
        title = _meta(soup, ("meta[name='citation_title']", "meta[property='og:title']", "title"))
        abstract = _abstract(soup)
        description = _meta(soup, ("meta[name='description']",))
        excerpt = ""
        if not abstract:
            excerpt = _abstract(soup, 6)
            if not excerpt and _substantive_text(description, 6):
                excerpt = description
        doi = _meta(soup, ("meta[name='citation_doi']", "meta[name='dc.identifier']", "meta[name='DC.Identifier']"))
        year = _meta(soup, ("meta[name='citation_publication_date']", "meta[property='article:published_time']", "time"))
        level = (
            "ABSTRACT_VERIFIED" if abstract else "FETCHED_EXCERPT" if excerpt
            else "VERIFIED_METADATA" if title != "Unknown" or doi != "Unknown"
            else "FETCH_FAILED"
        )
        text = "\n".join(
            [
                f"SOURCE_URL: {url} was the publication page requested.",
                f"TITLE: {title} was extracted from the publication page.",
                f"YEAR: {year[:4] if year != 'Unknown' else year} was extracted from the publication page.",
                f"DOI: {doi} was extracted from the publication page.",
                f"ABSTRACT: {trim_words(abstract, 120)}",
                f"CONTENT: {trim_words(excerpt, 120)}",
                "STATUS: OK was returned for this publication fetch.",
                f"EVIDENCE_LEVEL: {level}",
            ]
        )
        return redact_patent_text(clean_output(text))
    except Exception as exc:
        return redact_patent_text(format_error("publication_fetch", provider_error_message(exc), url=url)) + "\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"
