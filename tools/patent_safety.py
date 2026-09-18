"""Credential containment at patent retrieval and evidence boundaries."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

_SECRET_NAME = re.compile(r"key|token|secret|password|authorization|signature|credential|^auth$|^sig$", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_AUTH = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=%-]+", re.I)
_ASSIGNMENT = re.compile(
    r"\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|signature)"
    r"[\"']?\s*[=:]\s*[\"']?)[^\s,;\"'}]+", re.I,
)


def public_document_url(url: str) -> bool:
    """Never send a credential-bearing target to a document reader or archive."""
    try:
        if re.search(r"\s", url):
            return False
        decoded = url
        for _ in range(3):
            decoded = unquote(decoded)
        # Reader/archive wrappers and redirect parameters can embed another URL.
        if re.search(r"https?://[^/\s<>]+@", decoded, re.I):
            return False
        if any(_SECRET_NAME.search(key) for key in re.findall(r"[?&]([^=&#\s]+)=", decoded)):
            return False
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not any(_SECRET_NAME.search(key) for key, _ in parse_qsl(parsed.query))
        )
    except ValueError:
        return False


def redact_patent_text(value: str) -> str:
    """Redact configured secrets and credential syntax before text/token storage."""
    text = str(value or "")
    secrets = {
        variant
        for name, secret in os.environ.items()
        if _SECRET_NAME.search(name) and len(secret.strip()) >= 6
        for variant in (secret.strip(), quote(secret.strip(), safe=""), quote_plus(secret.strip()))
    }
    for secret in sorted(secrets, key=lambda item: (-len(item), item)):
        text = text.replace(secret, "[REDACTED]")
    # Do not retain an authenticated URL even with just its password obscured.
    text = _URL.sub(
        lambda match: match.group() if public_document_url(unquote(match.group())) else "[REDACTED_URL]",
        text,
    )
    text = _AUTH.sub("[REDACTED_AUTH]", text)
    return _ASSIGNMENT.sub(r"\1[REDACTED]", text)


def sanitize_patent_value(value: Any) -> Any:
    """Sanitize patent payloads without changing non-text fields or structure."""
    if isinstance(value, str):
        return redact_patent_text(value)
    if isinstance(value, dict):
        return {redact_patent_text(str(key)): sanitize_patent_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_patent_value(item) for item in value]
    return value
