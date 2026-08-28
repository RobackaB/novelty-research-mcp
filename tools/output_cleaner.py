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


_MAX_UNESCAPE_PASSES = 3

# Balast, ktorý sa dostáva do súhrnov z navigácie stránok, README súborov
# a z výstupu záložných čítačiek obsahu. Nenesie žiadnu dôkaznú hodnotu.
_BOILERPLATE_PATTERNS = (
    # Markdown nadpisy a "# Repository:" hlavička z čítačky README.
    re.compile(r"(?m)^\s*#{1,6}\s*(?:Repository\s*:)?\s*"),
    # Metadáta repozitárov: "- Stars: 0 - Forks: 0 - Watchers: 3".
    re.compile(r"[-–—]?\s*(?:Stars|Forks|Watchers|Issues|Contributors|Followers)\s*:\s*[\d,]*\s*", re.IGNORECASE),
    # Navigačné a marketingové výzvy.
    re.compile(
        r"\b(?:click here(?:\s+to\s+\w+(?:\s+\w+){0,3})?|is available now|skip to (?:content|main)"
        r"|sign in|log in|subscribe(?:\s+now)?|read more|learn more|get started|contact us"
        r"|view cart|continue shopping|check it out|back to top|share this)\b[\s.:,!]*",
        re.IGNORECASE,
    ),
)
# Opakovaný rovnaký výraz oddelený zvislou čiarou: "Rootly | Rootly ...".
_PIPE_REPEAT_RE = re.compile(r"\b([\w][\w .'-]{0,40}?)\s*\|\s*\1\b", re.IGNORECASE)
# Bezprostredne zopakované slovo: "Product Product".
_WORD_REPEAT_RE = re.compile(r"\b(\w{2,})(\s+\1\b)+", re.IGNORECASE)


def unescape_entities(text: str) -> str:
    """Rozkóduje HTML entity aj pri viacnásobnom zakódovaní ("&amp;amp;")."""
    current = str(text or "")
    for _ in range(_MAX_UNESCAPE_PASSES):
        decoded = html.unescape(current)
        if decoded == current:
            break
        current = decoded
    return current


def collapse_repeats(text: str) -> str:
    """Zlúči bezprostredne zopakované slová a výrazy oddelené zvislou čiarou."""
    collapsed = _PIPE_REPEAT_RE.sub(r"\1", str(text or ""))
    collapsed = _WORD_REPEAT_RE.sub(r"\1", collapsed)
    return re.sub(r"\s+", " ", collapsed).strip()


def strip_boilerplate(text: str) -> str:
    """Odstráni navigačný a metadátový balast bez dôkaznej hodnoty."""
    cleaned = str(text or "")
    for pattern in _BOILERPLATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .,;:|-–—")


def strip_leading_title(summary: str, title: str) -> str:
    """Odstráni zo začiatku súhrnu zopakovaný názov zdroja.

    Načítané stránky často začínajú vlastným nadpisom, ktorý sa v reporte
    zobrazuje hneď nad súhrnom; bez odstránenia je nález uvedený dvakrát.
    """
    body = str(summary or "").strip()
    head = re.sub(r"\s+", " ", str(title or "")).strip()
    if not body or len(head) < 8:
        return body
    normalized_body = re.sub(r"\s+", " ", body)
    if normalized_body.lower().startswith(head.lower()):
        return normalized_body[len(head) :].lstrip(" .,:;–—-|")
    return normalized_body


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
