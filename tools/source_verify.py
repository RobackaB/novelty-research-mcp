"""Overovanie dostupnosti URL adries nájdených zdrojov."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .output_cleaner import USER_AGENT, clean_output, format_error
from .result_contract import NormalizedResult, prepend_markers

URL_RE = re.compile(r"https?://[^\s)>\]}\"']+")
LOGGER = logging.getLogger(__name__)


def _looks_like_error_url(url: str) -> bool:
    """Zistí, či finálna URL adresa vyzerá ako chybová stránka."""
    return bool(re.search(r"(?:^|/)(?:error)?404(?:\.|/|$)|not[-_]?found", url or "", flags=re.IGNORECASE))


async def _verify_one(client: httpx.AsyncClient, url: str) -> str:
    """Overí jednu URL adresu cez HTTP a vráti textový stav."""
    try:
        response = await client.head(url)
        if response.status_code in {403, 405} or response.status_code >= 500:
            response = await client.get(url)
        final_url = str(response.url)
        status = "ALIVE" if response.status_code < 400 and not _looks_like_error_url(final_url) else "BROKEN"
        content_type = response.headers.get("content-type", "unknown").split(";")[0]
        return (
            f"Verification status for URL: {url} returned {status} with HTTP "
            f"{response.status_code}, final URL {final_url}, content type {content_type}."
        )
    except Exception as exc:
        return f"Verification status for URL: {url} returned BROKEN because {exc}."


def _url_text(urls: str | list[str] | tuple[str, ...] | Any) -> str:
    """Prevedie vstup s URL adresami na jeden text."""
    if isinstance(urls, str):
        return urls
    if isinstance(urls, (list, tuple)):
        return "\n".join(str(item) for item in urls)
    return str(urls or "")


async def verify_sources(urls: str | list[str], max_urls: int = 20) -> str:
    """Overí, či sú zadané URL adresy dostupné."""
    started = time.perf_counter()
    try:
        url_text = _url_text(urls)
        found = []
        for match in URL_RE.findall(url_text):
            clean = match.rstrip(".,;")
            if clean not in found:
                found.append(clean)
            if len(found) >= max(1, min(max_urls, 50)):
                break
        if not found:
            return prepend_markers(
                NormalizedResult(status="ok", completed=True, reliable_no_results=True),
                "No source URLs were found to verify.",
            )
        capped = max(1, min(max_urls, 10))
        found = found[:capped]
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=6.0) as client:
            lines = await asyncio.gather(*[_verify_one(client, url) for url in found])
        LOGGER.info("verify_sources elapsed_s=%.3f urls=%s", time.perf_counter() - started, len(found))
        return prepend_markers(
            NormalizedResult(status="ok", completed=True, reliable_no_results=False, hits=found),
            clean_output("\n".join(lines)),
        )
    except Exception as exc:
        return prepend_markers(
            NormalizedResult(
                status="failed",
                completed=False,
                reliable_no_results=False,
                errors=[{"type": "verify_sources_failed", "message": str(exc)}],
                notes=["Verification failure does not mean the sources are invalid."],
            ),
            format_error("verify_sources", str(exc)),
        )