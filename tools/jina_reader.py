"""Záložné načítanie webovej stránky cez službu r.jina.ai."""

from __future__ import annotations

import httpx

from .output_cleaner import USER_AGENT, clean_output


async def fetch_via_jina(url: str, timeout_s: float = 20.0) -> str:
    """Načíta stránku cez Jina Reader a vráti očistený text."""
    reader_url = f"https://r.jina.ai/{url}"
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout_s,
    ) as client:
        response = await client.get(reader_url)
        response.raise_for_status()
        return clean_output(response.text)
