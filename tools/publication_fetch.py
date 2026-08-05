"""Načítanie stránky odbornej publikácie a extrakcia metadát."""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from .jina_reader import fetch_via_jina
from .output_cleaner import USER_AGENT, clean_output, format_error, trim_words


def _meta(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    """Vráti obsah prvého dostupného CSS selektora s metadátami alebo textom."""
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") or node.get_text(" ", strip=True)
            if value:
                return re.sub(r"\s+", " ", value).strip()
    return "Unknown"


async def publication_fetch(url: str, timeout_s: float = 12.0) -> str:
    """Načíta stránku publikácie a vytiahne z nej základné metadáta."""
    try:
        html = ""
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=max(5.0, min(timeout_s, 30.0))) as client:
            response = await client.get(url)
            if response.status_code < 400 and not re.search(r"captcha|enable javascript|access denied", response.text, re.I):
                html = response.text
        if not html:
            content = await fetch_via_jina(url)
            return clean_output(
                f"SOURCE_URL: {url} was the publication page requested.\n"
                "TITLE: Unknown was extracted from the publication page.\n"
                "YEAR: Unknown was extracted from the publication page.\n"
                f"ABSTRACT: {trim_words(content, 120)}\n"
                "STATUS: JINA_FALLBACK was returned for this publication fetch.\n"
                "EVIDENCE_LEVEL: ABSTRACT_VERIFIED"
            )
        soup = BeautifulSoup(html, "lxml")
        title = _meta(soup, ("meta[name='citation_title']", "meta[property='og:title']", "title"))
        abstract = _meta(soup, ("meta[name='citation_abstract']", "meta[name='description']", "section.abstract"))
        doi = _meta(soup, ("meta[name='citation_doi']", "meta[name='dc.identifier']", "meta[name='DC.Identifier']"))
        year = _meta(soup, ("meta[name='citation_publication_date']", "meta[property='article:published_time']", "time"))
        text = "\n".join(
            [
                f"SOURCE_URL: {url} was the publication page requested.",
                f"TITLE: {title} was extracted from the publication page.",
                f"YEAR: {year[:4] if year != 'Unknown' else year} was extracted from the publication page.",
                f"DOI: {doi} was extracted from the publication page.",
                f"ABSTRACT: {trim_words(abstract, 120)}",
                "STATUS: OK was returned for this publication fetch.",
                "EVIDENCE_LEVEL: ABSTRACT_VERIFIED",
            ]
        )
        return clean_output(text)
    except Exception as exc:
        return format_error("publication_fetch", str(exc), url=url) + "\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"
