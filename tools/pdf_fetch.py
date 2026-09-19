"""Downloading a PDF document and extracting its text for deeper evidence analysis.

PDF sources -- datasheets, manuals, scholarly articles -- were previously kept
only as a search engine snippet. This module makes it possible to process the
whole document, so requirement coverage and relevance are computed from its
actual content.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Awaitable, Callable

import httpx

from .output_cleaner import USER_AGENT
from ._provider_errors import provider_error_message

LOGGER = logging.getLogger(__name__)

PDF_MAX_BYTES = 15 * 1024 * 1024
PDF_MAX_PAGES = 25
PDF_MIN_TEXT_WORDS = 12


def extract_pdf_text(data: bytes, max_pages: int = PDF_MAX_PAGES) -> str:
    """Extract plain text from PDF data; return empty text on error or empty content."""
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
        LOGGER.info("PDF text extraction failed: %s", provider_error_message(exc))
        return ""


def _looks_like_pdf(content_type: str, data: bytes) -> bool:
    """Check whether the response really contains a PDF document."""
    if content_type and "pdf" in content_type:
        return True
    return data[:5] == b"%PDF-"


async def pdf_fetch_text(
    url: str,
    timeout_s: float = 20.0,
    max_pages: int = PDF_MAX_PAGES,
    max_bytes: int = PDF_MAX_BYTES,
    *, request_guard: Callable[[httpx.Request], Awaitable[None]] | None = None,
) -> str:
    """Download a PDF document and return its text; return empty text on any error."""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=max(5.0, min(timeout_s, 40.0)),
            event_hooks={"request": [request_guard]} if request_guard else None,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return ""
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    LOGGER.info("PDF exceeds size cap; skipping.")
                    return ""
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        LOGGER.info("PDF exceeded size cap while streaming; skipping.")
                        return ""
                data = bytes(buffer)
        if not _looks_like_pdf(content_type, data):
            return ""
        return await asyncio.to_thread(extract_pdf_text, data, max_pages)
    except Exception as exc:
        LOGGER.info("PDF fetch failed: %s", provider_error_message(exc))
        return ""
