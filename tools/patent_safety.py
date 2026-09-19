"""Credential containment at patent retrieval and evidence boundaries."""

from __future__ import annotations

import os
import ipaddress
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

_PATENT_HOSTS = frozenset({
    "patents.google.com", "patentimages.storage.googleapis.com",
    "archive.org", "web.archive.org", "r.jina.ai", "www.gstatic.com",
})


def _public_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject single-label names, numeric shorthand and encoded authorities.
        return bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", host))
    return address.is_global and not any((address.is_reserved, address.is_multicast,
        address.is_loopback, address.is_link_local, address.is_unspecified, address.is_private))


def _credential_free_url(url: str) -> bool:
    """Credential redaction is separate from permission to fetch a URL."""
    try:
        if re.search(r"[\s\\]", url):
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


def public_document_url(url: str) -> bool:
    """Reject credentials and syntactically internal/reserved URL targets.

    Network callers additionally use patent_document_url's exact host allowlist;
    this generic predicate alone is not a DNS-rebinding defense.
    """
    if not _credential_free_url(url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.port not in {None, 80, 443} or not _public_host(parsed.hostname or ""):
            return False
        decoded = url
        for _ in range(3):
            decoded = unquote(decoded)
        return all(_public_host(urlsplit("https://" + authority).hostname or "")
                   for authority in re.findall(r"https?://([^/?#\s]+)", decoded, re.I))
    except ValueError:
        return False


def patent_document_url(url: str) -> bool:
    """Only existing patent document services may receive fetch traffic."""
    if not public_document_url(url):
        return False
    decoded = url
    for _ in range(3):
        decoded = unquote(decoded)
    return all(
        (urlsplit("https://" + authority).hostname or "").lower().rstrip(".") in _PATENT_HOSTS
        for authority in re.findall(r"https?://([^/?#\s]+)", decoded, re.I)
    )


async def guard_patent_request(request: Any) -> None:
    """Check every HTTP request, including redirects, before transport I/O."""
    if not patent_document_url(str(request.url)):
        raise ValueError("Unsafe patent document target rejected.")


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
        lambda match: match.group() if _credential_free_url(unquote(match.group())) else "[REDACTED_URL]",
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
