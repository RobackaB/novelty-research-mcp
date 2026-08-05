"""Pomocné funkcie na rozpoznanie patentových výsledkov."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .output_cleaner import ALLOWED_PATENT_DOMAINS

PATENT_NUMBER_RE = re.compile(r"\b((?:US|EP|WO|CN|JP|KR)\d{4,}[A-Z0-9]*)\b", re.IGNORECASE)


def extract_patent_number(*parts: str) -> str:
    """Vytiahne patentové číslo z textu, URL alebo názvu výsledku."""
    text = " ".join(part or "" for part in parts)
    match = PATENT_NUMBER_RE.search(unquote(text).upper())
    return match.group(1).upper() if match else "Unknown"


def _allowed_domain(url: str) -> bool:
    """Overí, či URL patrí do povolených patentových domén."""
    domain = urlsplit(url).netloc.lower()
    return any(domain.endswith(allowed) for allowed in ALLOWED_PATENT_DOMAINS)


def _is_root_or_portal(url: str, title: str) -> bool:
    """Zistí, či výsledok smeruje iba na portál alebo koreňovú stránku."""
    parsed = urlsplit(url)
    path = parsed.path.strip("/").lower()
    title_l = title.strip().lower()
    return not path or title_l in {"google patents", "patents", "lens.org"}


def _looks_like_patent_detail_url(url: str, patent_number: str) -> bool:
    """Zistí, či adresa vyzerá ako stránka detailu konkrétneho patentu."""
    parsed = urlsplit(url)
    domain = parsed.netloc.lower()
    url_u = unquote(url).upper()
    path_l = parsed.path.lower()
    query_l = parsed.query.lower()
    if domain.endswith("patents.google.com"):
        return f"/patent/{patent_number.lower()}" in path_l
    if domain.endswith("patents.justia.com"):
        return "/patent/" in path_l
    if domain.endswith("worldwide.espacenet.com"):
        return patent_number in url_u or "pn=" in query_l or "publication" in path_l or "family" in path_l
    if domain.endswith("patentscope.wipo.int"):
        return patent_number in url_u or "detail" in path_l
    if domain.endswith("lens.org"):
        return "patent" in path_l and (patent_number in url_u or "/lens/patent/" in path_l)
    return False


def _looks_like_search_ui(title: str, snippet: str) -> bool:
    """Zistí, či text výsledku vyzerá ako vyhľadávacie rozhranie."""
    text = f"{title} {snippet}".lower()
    noise_terms = ("group by", "results / page", "deduplicate by", "sort by", "about 10 results")
    return any(term in text for term in noise_terms)


def is_valid_patent_result(title: str, patent_number: str, url: str, snippet: str = "") -> bool:
    """Overí, či výsledok vyzerá ako konkrétna patentová stránka."""
    if not url or not _allowed_domain(url):
        return False
    if _is_root_or_portal(url, title):
        return False
    if patent_number == "Unknown" or not PATENT_NUMBER_RE.fullmatch(patent_number):
        return False
    if _looks_like_search_ui(title, snippet):
        return False
    return _looks_like_patent_detail_url(url, patent_number)
