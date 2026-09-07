"""Deterministic query expansion with synonyms and acronym expansions.

Fills the `synonyms` field of the query envelope, which was previously always
empty, and generates additional search variants for retry attempts. The lexicon
is deliberately small and holds only highly reliable technical equivalents, so
that a variant never changes what the query means.
"""

from __future__ import annotations

import re

# Bidirectional technical synonyms; both keys and values are lowercase phrases.
_SYNONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("smart", "intelligent"),
    ("mobile application", "smartphone app"),
    ("mobile app", "mobile application"),
    ("anomaly detection", "outlier detection"),
    ("predictive maintenance", "condition-based maintenance"),
    ("notification", "alert"),
    ("fault", "failure"),
    ("real-time", "realtime"),
    ("wireless charging", "inductive charging"),
    ("machine learning", "ML"),
    ("artificial intelligence", "AI"),
    ("unmanned aerial vehicle", "drone"),
)

# Acronyms and their expansions; expansion works in both directions.
_ACRONYMS: dict[str, str] = {
    "iot": "internet of things",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "gps": "global positioning system",
    "rfid": "radio frequency identification",
    "nfc": "near field communication",
    "uav": "unmanned aerial vehicle",
    "ev": "electric vehicle",
    "ble": "bluetooth low energy",
    "lidar": "light detection and ranging",
    "nlp": "natural language processing",
    "ocr": "optical character recognition",
    "hvac": "heating ventilation and air conditioning",
    "vr": "virtual reality",
    "ar": "augmented reality",
}


def _build_mapping() -> dict[str, tuple[str, ...]]:
    """Build a bidirectional map from phrases to their equivalents."""
    mapping: dict[str, set[str]] = {}
    for left, right in _SYNONYM_PAIRS:
        mapping.setdefault(left.lower(), set()).add(right)
        mapping.setdefault(right.lower(), set()).add(left)
    for acronym, expansion in _ACRONYMS.items():
        mapping.setdefault(acronym, set()).add(expansion)
        mapping.setdefault(expansion.lower(), set()).add(acronym.upper())
    return {key: tuple(sorted(values)) for key, values in mapping.items()}


_MAPPING = _build_mapping()
# Longer phrases are tried first so "mobile application" wins over "application".
_PHRASES_BY_LENGTH = sorted(_MAPPING, key=lambda phrase: -len(phrase))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Build a case-insensitive regex pattern matching a whole phrase."""
    return re.compile(rf"\b{re.escape(phrase)}\b", flags=re.IGNORECASE)


def applicable_synonyms(query: str, max_terms: int = 8) -> list[str]:
    """Return equivalent terms for the phrases that occur in the query."""
    text = (query or "").lower()
    found: list[str] = []
    seen: set[str] = set()
    for phrase in _PHRASES_BY_LENGTH:
        if len(found) >= max_terms:
            break
        if not _phrase_pattern(phrase).search(text):
            continue
        for replacement in _MAPPING[phrase]:
            key = replacement.lower()
            if key in seen or key in text:
                continue
            seen.add(key)
            found.append(replacement)
            if len(found) >= max_terms:
                break
    return found


def synonym_query_variants(query: str, max_variants: int = 2) -> list[str]:
    """Build query variants with synonyms or acronyms substituted in.

    Each variant replaces the occurrences of a single phrase with its
    equivalent, which keeps the meaning of the query intact and keeps the
    variants readable.
    """
    original = re.sub(r"\s+", " ", str(query or "")).strip()
    if not original:
        return []
    variants: list[str] = []
    seen: set[str] = {original.lower()}
    for phrase in _PHRASES_BY_LENGTH:
        if len(variants) >= max_variants:
            break
        pattern = _phrase_pattern(phrase)
        if not pattern.search(original):
            continue
        for replacement in _MAPPING[phrase]:
            candidate = pattern.sub(replacement, original)
            candidate = re.sub(r"\s+", " ", candidate).strip()
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            variants.append(candidate)
            break
    return variants
