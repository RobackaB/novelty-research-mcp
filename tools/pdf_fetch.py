"""Stiahnutie PDF dokumentu a extrakcia textu pre hlbšiu analýzu dôkazov.

PDF zdroje (datasheety, manuály, odborné články) sa predtým zachovávali
iba ako snippet z vyhľadávača. Tento modul umožňuje spracovať celý
dokument, takže pokrytie požiadaviek a relevancia sa počítajú z jeho
skutočného obsahu.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re

import httpx

from .output_cleaner import USER_AGENT

LOGGER = logging.getLogger(__name__)

PDF_MAX_BYTES = 15 * 1024 * 1024
PDF_MAX_PAGES = 25
PDF_MIN_TEXT_WORDS = 12


def extract_pdf_text(data: bytes, max_pages: int = PDF_MAX_PAGES) -> str:
    """Extrahuje čistý text z PDF dát; pri chybe alebo prázdnom obsahu vráti prázdny text."""
    if not data:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        LOGGER.info("pypdf is not installed; PDF text extraction is unavailable.")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return ""
        pages = reader.pages[: max(1, max_pages)]
        chunks: list[str] = []
        for page in pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            text = re.sub(r"[ \t]+", " ", text).strip()
            if text:
                chunks.append(text)
        combined = "\n".join(chunks).strip()
        if len(combined.split()) < PDF_MIN_TEXT_WORDS:
            return ""
        return combined
    except Exception as exc:
        LOGGER.info("PDF text extraction failed: %s", exc)
        return ""


def _looks_like_pdf(content_type: str, data: bytes) -> bool:
    """Overí, či odpoveď skutočne obsahuje PDF dokument."""
    if content_type and "pdf" in content_type:
        return True
    return data[:5] == b"%PDF-"


async def pdf_fetch_text(
    url: str,
    timeout_s: float = 20.0,
    max_pages: int = PDF_MAX_PAGES,
    max_bytes: int = PDF_MAX_BYTES,
) -> str:
    """Stiahne PDF dokument a vráti jeho text; pri akejkoľvek chybe vráti prázdny text."""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=max(5.0, min(timeout_s, 40.0)),
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return ""
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    LOGGER.info("PDF at %s exceeds size cap (%s bytes); skipping.", url, content_length)
                    return ""
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        LOGGER.info("PDF at %s exceeded size cap while streaming; skipping.", url)
                        return ""
                data = bytes(buffer)
        if not _looks_like_pdf(content_type, data):
            return ""
        return await asyncio.to_thread(extract_pdf_text, data, max_pages)
    except Exception as exc:
        LOGGER.info("PDF fetch failed for %s: %s", url, exc)
        return ""
