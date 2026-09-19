"""Fallback web page fetching through the r.jina.ai service."""

from __future__ import annotations

import os

import httpx

from .output_cleaner import USER_AGENT, clean_output
from .patent_safety import patent_document_url, guard_patent_request


def _jina_headers() -> dict[str, str]:
    """Build the headers for the Jina Reader; with a key the rate limit is higher."""
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def fetch_via_jina(url: str, timeout_s: float = 20.0, *, preserve_structure: bool = False) -> str:
    """Fetch a page through the Jina Reader and return its cleaned text."""
    reader_url = f"https://r.jina.ai/{url}"
    if preserve_structure and not patent_document_url(url):
        raise ValueError("Unsafe patent document target rejected.")
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_jina_headers(),
        timeout=timeout_s,
        event_hooks={"request": [guard_patent_request]} if preserve_structure else None,
    ) as client:
        response = await client.get(reader_url)
        response.raise_for_status()
        return response.text if preserve_structure else clean_output(response.text)


async def fetch_via_jina_preserving_structure(url: str, timeout_s: float = 20.0) -> str:
    """Patent section recognition needs single-word headings and line breaks."""
    return await fetch_via_jina(url, timeout_s=timeout_s, preserve_structure=True)
