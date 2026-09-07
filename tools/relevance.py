"""Local relevance scoring for retrieved evidence."""

from __future__ import annotations

import math
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
# Below this many candidates, term-rarity weighting has no statistical meaning.
_MIN_CORPUS_FOR_IDF = 3
_DEFAULT_IDF_WEIGHT = 1.0


# Plurals are stripped first. The original ordering turned "application" into
# "applicate" but "applications" into "application", so the singular and plural
# of the same word never matched. Likewise the length-4 guard left "logs"
# untouched while "log" stayed "log", so a document about logs earned no credit
# against a query mentioning logs.
_PLURAL_RULES = (
    ("ies", "y"),
    ("sses", "ss"),
    ("shes", "sh"),
    ("ches", "ch"),
    ("xes", "x"),
    ("zes", "z"),
    ("s", ""),
)
_DERIVATIONAL_RULES = (
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
)


def _stem(token: str) -> str:
    """Reduce an English token to an approximate word stem."""
    if len(token) <= 3:
        return token
    if not token.endswith("ss"):
        for suffix, replacement in _PLURAL_RULES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                token = token[: -len(suffix)] + replacement
                break
    if len(token) <= 4:
        return token
    for suffix, replacement in _DERIVATIONAL_RULES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + replacement
    return token


def tokens(text: str) -> set[str]:
    """Split text into simple normalised tokens."""
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if token not in STOPWORDS and len(token) >= 2
    }


def build_corpus_idf(documents: list[str]) -> dict[str, float]:
    """Weight query terms by how rare they are within the candidate set.

    Without this weighting every query term counts equally, so a document from a
    completely different domain can pass on shared generic vocabulary alone (for
    example "machine", "learning", "real", "time", "detection"). Terms occurring
    in nearly every candidate carry no discriminating information and are given a
    lower weight; rare domain terms are given a higher one.

    The weights are computed over the candidate set just retrieved rather than a
    fixed word list. That keeps the system free of built-in domain vocabularies
    and lets it adapt to whatever topic the query is about.
    """
    doc_token_sets = [tokens(document) for document in documents if (document or "").strip()]
    total = len(doc_token_sets)
    if total < _MIN_CORPUS_FOR_IDF:
        return {}
    document_frequency: dict[str, int] = {}
    for token_set in doc_token_sets:
        for token in token_set:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return {
        token: math.log(1.0 + total / (1.0 + frequency))
        for token, frequency in document_frequency.items()
    }


def _weighted_overlap_ratio(
    query_tokens: set[str], overlap: set[str], idf: dict[str, float] | None
) -> float:
    """Return the share of the query covered, optionally weighted by term rarity."""
    if not idf:
        return len(overlap) / len(query_tokens)
    total_weight = sum(idf.get(token, _DEFAULT_IDF_WEIGHT) for token in query_tokens)
    if total_weight <= 0:
        return len(overlap) / len(query_tokens)
    matched_weight = sum(idf.get(token, _DEFAULT_IDF_WEIGHT) for token in overlap)
    return matched_weight / total_weight


def evidence_score(
    query: str,
    text: str,
    evidence_type: EvidenceType,
    evidence_level: str | None = None,
    idf: dict[str, float] | None = None,
) -> float:
    """Compute an approximate relevance score for a text against a query."""
    query_tokens = tokens(query)
    text_tokens = tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = query_tokens & text_tokens
    score = _weighted_overlap_ratio(query_tokens, overlap, idf) * 8.0
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
    """Return the query tokens with generic and structural words removed."""
    return {token for token in tokens(query) if token not in BROAD_QUERY_TERMS and len(token) >= 3}


_GENERIC_MECHANISM_NORMALIZED = frozenset(
    {token for token in GENERIC_MECHANISM_TOKENS}
    | {_stem(token) for token in GENERIC_MECHANISM_TOKENS}
)


def _strip_mechanism(token: str) -> bool:
    """Decide whether a token describes a generic mechanism rather than the subject."""
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
    """Select the tokens that most define what the query is about."""
    return {token for token in query_terms if not _strip_mechanism(token)}


def salient_query_tokens(query: str, idf: dict[str, float], top_n: int = 6) -> set[str]:
    """Select the query's most discriminating terms by their rarity in the corpus.

    These act as a domain anchor: a document containing none of the terms most
    specific to the query is about something else, even when it shares generic
    methodological vocabulary.
    """
    candidates = discriminative_tokens(query) or tokens(query)
    if not idf or not candidates:
        return set()
    # `candidates` is a set, and IDF ties are common (any two tokens appearing in
    # the same number of candidate documents share a weight exactly). Sorting by
    # weight alone left tied tokens in set-iteration order, which depends on
    # Python's per-process string hash seed — so when a tie group straddled the
    # top_n cut, which anchors survived changed between runs and the same query
    # could accept different documents. The token itself is a deterministic
    # secondary key; it carries no meaning, it only has to be stable.
    ranked = sorted(candidates, key=lambda token: (-idf.get(token, _DEFAULT_IDF_WEIGHT), token))
    return set(ranked[: max(1, top_n)])


def is_relevant(
    query: str,
    text: str,
    evidence_type: EvidenceType,
    threshold: float | None = None,
    idf: dict[str, float] | None = None,
) -> bool:
    """Check whether a text meets the minimum relevance threshold for a query."""
    text_tokens = tokens(text)
    if evidence_type == "PUBLICATION":
        query_tokens = tokens(query)
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
    if idf:
        salient = salient_query_tokens(query, idf)
        if salient and not (salient & text_tokens):
            return False
    return evidence_score(query, text, evidence_type, idf=idf) >= (
        threshold if threshold is not None else THRESHOLDS[evidence_type]
    )
