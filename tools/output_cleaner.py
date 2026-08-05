"""Spoločné pomocné funkcie na čistenie a skracovanie textu."""

from __future__ import annotations

import html
import re
from typing import Iterable

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ALLOWED_PATENT_DOMAINS = {
    "patents.google.com",
    "worldwide.espacenet.com",
    "patentscope.wipo.int",
    "patents.justia.com",
    "lens.org",
}
BLOCKED_WEB_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "tiktok.com",
    "wikipedia.org",
    "x.com",
    "youtube.com",
}
STRUCTURED_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_ -]{1,48}:")

try:
    import lxml
    _BS4_PARSER = "lxml"
except ImportError:
    _BS4_PARSER = "html.parser"

from bs4 import BeautifulSoup


def clean_output(text: str) -> str:
    """Odstráni šum a znormalizuje textový výstup nástroja."""
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", "", text or "")
    text = BeautifulSoup(text, _BS4_PARSER).get_text("\n")
    banned = ("javascript:", "void(0)", "cookie", "privacy policy", "terms of service", "all rights reserved")
    lines = []
    for raw in html.unescape(text).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            lines.append("")
            continue
        is_structured_label = bool(STRUCTURED_LABEL_RE.match(line))
        if len(line.split()) < 2 and not is_structured_label:
            continue
        if any(term in line.lower() for term in banned):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def strip_json_fences(text: str) -> str:
    """Odstráni markdown wrapper okolo JSON textu."""
    text = re.sub(r"^```(?:json)?\s*\n?", "", (text or "").strip(), flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


def format_error(tool_name: str, reason: str, url: str | None = None) -> str:
    """Vytvorí jednotný text chyby pre zlyhaný nástroj."""
    lines = [f"TOOL_ERROR: {tool_name}", f"REASON: {reason}"]
    if url:
        lines.append(f"URL: {url}")
    return "\n".join(lines)


def trim_words(text: str, limit: int) -> str:
    """Skráti text na zadaný počet slov."""
    words = re.findall(r"\S+", re.sub(r"\s+", " ", text or "").strip())
    if len(words) <= limit:
        return " ".join(words)
    return f"{' '.join(words[:limit])}..."


def first_match(patterns: Iterable[str], text: str, default: str = "Unknown") -> str:
    """Vráti prvú hodnotu nájdenú pomocou zadaných regulárnych výrazov."""
    for pattern in patterns:
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if found:
            return found.group(1).strip(" .,:;")
    return default


def soup_text(node: BeautifulSoup | None) -> str:
    """Získa čistý text z uzla spracovaného cez BeautifulSoup."""
    return re.sub(r"\n{2,}", "\n", node.get_text("\n", strip=True) if node else "").strip()
