"""Deterministická expanzia dotazu o synonymá a rozpisy akronymov.

Modul vypĺňa pole `synonyms` v query envelope (doteraz vždy prázdne)
a generuje dodatočné vyhľadávacie varianty pre retry pokusy. Lexikón je
zámerne malý a obsahuje len vysoko spoľahlivé technické ekvivalenty,
aby varianty nemenili význam dotazu.
"""

from __future__ import annotations

import re

# Obojsmerné technické synonymá; kľúč aj hodnoty sú frázy v lowercase.
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

# Akronymy a ich rozpisy; expanzia funguje oboma smermi.
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
    """Zostaví obojsmernú mapu fráz na ich ekvivalenty."""
    mapping: dict[str, set[str]] = {}
    for left, right in _SYNONYM_PAIRS:
        mapping.setdefault(left.lower(), set()).add(right)
        mapping.setdefault(right.lower(), set()).add(left)
    for acronym, expansion in _ACRONYMS.items():
        mapping.setdefault(acronym, set()).add(expansion)
        mapping.setdefault(expansion.lower(), set()).add(acronym.upper())
    return {key: tuple(sorted(values)) for key, values in mapping.items()}


_MAPPING = _build_mapping()
# Dlhšie frázy skúšame skôr, aby "mobile application" malo prednosť pred "application".
_PHRASES_BY_LENGTH = sorted(_MAPPING, key=lambda phrase: -len(phrase))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Vytvorí regex vzor pre celofrázovú zhodu bez ohľadu na veľkosť písmen."""
    return re.compile(rf"\b{re.escape(phrase)}\b", flags=re.IGNORECASE)


def applicable_synonyms(query: str, max_terms: int = 8) -> list[str]:
    """Vráti ekvivalentné termíny pre frázy, ktoré sa nachádzajú v dotaze."""
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
    """Vytvorí varianty dotazu so zamenenými synonymami alebo akronymami.

    Každý variant nahrádza výskyty jednej frázy jej ekvivalentom, aby
    zostal význam dotazu zachovaný a varianty boli čitateľné.
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
