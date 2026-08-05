"""Spoločné zoradenie nájdených dôkazov podľa relevancie."""

from __future__ import annotations

from typing import Any


_RELEVANCE_RANK: dict[str, int] = {
    "focused": 3,
    "direct": 3,
    "exact": 3,
    "adjacent": 2,
    "loose": 2,
    "generic": 1,
    "": 0,
}


def _hit_sort_key(hit: dict[str, Any]) -> tuple[int, float]:
    """Určí poradie jedného nálezu podľa typu relevancie a číselného skóre."""
    rel = str(hit.get("relevance") or "").strip().lower()
    rank = _RELEVANCE_RANK.get(rel, 0)
    try:
        score = float(hit.get("relevance_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return (rank, score)


def sort_hits_by_relevance(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vráti nový zoznam nálezov zoradený podľa typu relevancie a skóre."""
    return sorted(hits, key=_hit_sort_key, reverse=True)
