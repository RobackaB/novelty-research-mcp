"""Záložné načítanie webovej stránky cez službu r.jina.ai."""

from __future__ import annotations

import os

import httpx

from .output_cleaner import USER_AGENT, clean_output


def _jina_headers() -> dict[str, str]:
    """Zostaví hlavičky pre Jina Reader; s kľúčom má vyšší limit požiadaviek."""
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def fetch_via_jina(url: str, timeout_s: float = 20.0) -> str:
    """Načíta stránku cez Jina Reader a vráti očistený text."""
    reader_url = f"https://r.jina.ai/{url}"
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_jina_headers(),
        timeout=timeout_s,
    ) as client:
        response = await client.get(reader_url)
        response.raise_for_status()
        return clean_output(response.text)
