"""Pomocné funkcie na čistenie vstupných dotazov z Flowise."""

from __future__ import annotations

import json
import re
from typing import Any


_QUERY_DICT_KEYS = ("query", "text", "original_query", "input", "question", "prompt", "value")

_TERM_LIST_KEYS = (
    "keywords", "terms", "must_include", "must_have", "include",
    "phrases", "synonyms", "tokens", "key_terms", "core", "subject",
    "topics", "concepts",
)

_PLAN_STRUCTURAL_KEYS = frozenset({
    "date_range", "exclude", "filters", "limit", "max_results",
    "min_results", "domains", "languages", "language", "country",
    "exclude_domains", "include_domains", "from_year", "to_year",
    "year_min", "year_max", "type", "kind", "format", "category",
    "metadata", "constraints",
})

_JSON_SYNTAX_RE = re.compile(r"[{}\[\]\"`]")


def _extract_terms(value: Any, depth: int = 0) -> list[str]:
    """Vytiahne textové časti dotazu z vnorených štruktúr."""
    if depth > 6: 
        return []
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (int, float, bool)):
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for key in _QUERY_DICT_KEYS:
            if key in value:
                out.extend(_extract_terms(value[key], depth + 1))
        for key in _TERM_LIST_KEYS:
            if key in value:
                out.extend(_extract_terms(value[key], depth + 1))
        for key, sub in value.items():
            k_lower = str(key).lower()
            if k_lower in _PLAN_STRUCTURAL_KEYS:
                continue
            if k_lower in _QUERY_DICT_KEYS or k_lower in _TERM_LIST_KEYS:
                continue
            if isinstance(sub, (dict, list)):
                out.extend(_extract_terms(sub, depth + 1))
            elif isinstance(sub, str):
                stripped = sub.strip()
                if stripped:
                    out.append(stripped)
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_extract_terms(item, depth + 1))
        return out
    return []


def _looks_like_serialized_structure(text: str) -> bool:
    """Zistí, či text vyzerá ako serializovaný JSON alebo zoznam."""
    s = (text or "").strip()
    if not s:
        return False
    if not (s.startswith("{") or s.startswith("[")):
        return False
    return bool(_JSON_SYNTAX_RE.search(s))


def _strip_json_syntax(text: str) -> str:
    """Odstráni zo vstupu JSON značky a technické štruktúrne slová."""
    cleaned = _JSON_SYNTAX_RE.sub(" ", text or "")
    cleaned = re.sub(r"[:,]+", " ", cleaned)
    tokens = cleaned.split()
    keep = []
    for tok in tokens:
        bare = tok.strip().lower().rstrip(",;:")
        if not bare:
            continue
        if bare in _PLAN_STRUCTURAL_KEYS:
            continue
        if bare in {"null", "none", "true", "false"}:
            continue
        keep.append(tok)
    return " ".join(keep).strip()


def coerce_query_input(value: Any) -> str:
    """Prevedie vstup z Flowise alebo MCP na čistý textový dotaz."""
    if value is None:
        return ""
    if isinstance(value, str):
        if _looks_like_serialized_structure(value):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return _strip_json_syntax(value)
            return coerce_query_input(parsed)
        return value
    if isinstance(value, (dict, list, tuple)):
        terms = _extract_terms(value)
        seen: set[str] = set()
        unique = []
        for term in terms:
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(term)
        joined = " ".join(unique).strip()
        return _strip_json_syntax(joined) if _JSON_SYNTAX_RE.search(joined) else joined
    return _strip_json_syntax(str(value))


def clean_tool_query(query: Any) -> str:
    """Odstráni úvodný štítok bez zmeny samotného dotazu."""
    text = re.sub(r"\s+", " ", coerce_query_input(query)).strip()
    text = re.sub(r"^(?:query|question|input)\s*:\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    if text.startswith("{") or text.startswith("["):
        text = _strip_json_syntax(text)
    return text
