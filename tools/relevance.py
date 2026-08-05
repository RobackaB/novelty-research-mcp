"""Lokálne hodnotenie relevancie nájdených dôkazov."""

from __future__ import annotations

import re
from typing import Literal

from .result_contract import GENERIC_MECHANISM_TOKENS, evidence_level_multiplier

EvidenceType = Literal["WEB", "PATENT", "PUBLICATION"]

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "use",
    "there", "existing", "instead", "about", "already", "idea", "concept", "does",
    "is", "are", "a", "an", "of", "to", "in", "on", "by", "or", "as", "be",
    "was", "has", "have", "not", "but", "so", "it", "at", "no", "do", "if",
    "je", "pre", "na", "do", "sa", "vo", "ako",
}
PATENT_STRUCTURAL = {
    "patent", "claim", "claims", "assignee", "filed", "invention", "apparatus",
    "method", "system", "device", "process", "embodiment", "wherein", "comprising",
}
PUBLICATION_STRUCTURAL = {
    "abstract", "doi", "journal", "authors", "publication", "study", "experimental",
    "review", "paper", "research", "conference", "arxiv", "semantic",
}
WEB_STRUCTURAL = {
    "technical", "specification", "product", "datasheet", "commercial",
    "documentation", "official", "prototype", "standard",
}
BROAD_QUERY_TERMS = STOPWORDS | PATENT_STRUCTURAL | PUBLICATION_STRUCTURAL | WEB_STRUCTURAL | {
    "system", "device", "method", "process", "invention", "implementation",
    "development", "prior", "art", "results",
}
THRESHOLDS: dict[EvidenceType, float] = {"PATENT": 3.0, "PUBLICATION": 3.0, "WEB": 2.8}


def _stem(token: str) -> str:
    """Zjednoduší anglický token na približný koreň slova."""
    if len(token) <= 4:
        return token
    for suffix, replacement in (
        ("ization", "ize"),
        ("ational", "ate"),
        ("ation", "ate"),
        ("iveness", "ive"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("ically", "ic"),
        ("ing", ""),
        ("edly", ""),
        ("ed", ""),
        ("ers", ""),
        ("er", ""),
        ("ies", "y"),
        ("s", ""),
    ):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + replacement
    return token


def tokens(text: str) -> set[str]:
    """Rozdelí text na jednoduché normalizované tokeny."""
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if token not in STOPWORDS and len(token) >= 2
    }


def evidence_score(query: str, text: str, evidence_type: EvidenceType, evidence_level: str | None = None) -> float:
    """Vypočíta orientačné skóre relevancie textu voči dotazu."""
    query_tokens = tokens(query)
    text_tokens = tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = query_tokens & text_tokens
    score = (len(overlap) / len(query_tokens)) * 8.0
    if evidence_type == "PATENT" and text_tokens & PATENT_STRUCTURAL:
        score += 0.5
    elif evidence_type == "PUBLICATION" and text_tokens & PUBLICATION_STRUCTURAL:
        score += 0.5
    elif evidence_type == "WEB" and text_tokens & WEB_STRUCTURAL:
        score += 0.3
    rare_overlap = {token for token in overlap if len(token) >= 6}
    if rare_overlap:
        score += min(len(rare_overlap) * 0.3, 1.0)
    if evidence_level:
        score *= evidence_level_multiplier(evidence_level)
    return round(max(0.0, min(score, 10.0)), 2)


def discriminative_tokens(query: str) -> set[str]:
    """Vráti tokeny dotazu bez všeobecných a štruktúrnych slov."""
    return {token for token in tokens(query) if token not in BROAD_QUERY_TERMS and len(token) >= 3}


_GENERIC_MECHANISM_NORMALIZED = frozenset(
    {token for token in GENERIC_MECHANISM_TOKENS}
    | {_stem(token) for token in GENERIC_MECHANISM_TOKENS}
)


def _strip_mechanism(token: str) -> bool:
    """Určí, či token opisuje skôr všeobecný mechanizmus než predmet dotazu."""
    if token in _GENERIC_MECHANISM_NORMALIZED:
        return True
    parts = [part for part in re.split(r"[-_/]+", token) if part]
    if len(parts) <= 1:
        return False
    parts_normalized = {part for part in parts} | {_stem(part) for part in parts}
    if all(part in _GENERIC_MECHANISM_NORMALIZED or len(part) <= 2 for part in parts_normalized):
        return any(part in _GENERIC_MECHANISM_NORMALIZED for part in parts_normalized)
    return False


def subject_anchors(query_terms: set[str]) -> set[str]:
    """Vyberie z dotazu tokeny, ktoré najviac určujú jeho predmet."""
    return {token for token in query_terms if not _strip_mechanism(token)}


def is_relevant(query: str, text: str, evidence_type: EvidenceType, threshold: float | None = None) -> bool:
    """Overí, či text spĺňa minimálnu hranicu relevancie voči dotazu."""
    if evidence_type == "PUBLICATION":
        query_tokens = tokens(query)
        text_tokens = tokens(text)
        shared = query_tokens & text_tokens
        required_shared = 2
        if len(query_tokens) >= 6:
            required_shared = 3
        if len(query_tokens) >= 4 and len(shared) < required_shared:
            return False
        discriminators = discriminative_tokens(query)
        if discriminators:
            shared_discriminators = discriminators & text_tokens
            required_discriminators = min(len(discriminators), max(2, min(3, (len(discriminators) + 1) // 2)))
            if len(shared_discriminators) < required_discriminators:
                return False
        product_anchors = subject_anchors(discriminators)
        if product_anchors and not (product_anchors & text_tokens):
            return False
        if not product_anchors and discriminators and not (discriminators & text_tokens):
            return False
    return evidence_score(query, text, evidence_type) >= (threshold if threshold is not None else THRESHOLDS[evidence_type])
