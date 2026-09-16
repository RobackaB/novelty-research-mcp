"""Allowlisted provider failure diagnostics for evidence and application logs."""

from __future__ import annotations

import httpx


def provider_error_message(error: BaseException) -> str:
    """Report type/status without rendering messages, requests or response bodies.

    HTTP errors often include credential-bearing URLs in their string form.
    Other exception messages can also contain tokens or echoed provider payloads.
    Callers retain provider identity separately and must not use this diagnostic
    to select retrieval, fallback or retry behavior.
    """
    error_type = type(error).__name__
    if isinstance(error, httpx.HTTPStatusError):
        return f"{error_type} (HTTP {error.response.status_code})"
    return error_type
