"""Helpers for grading source quality and deciding a preliminary verdict."""

from __future__ import annotations

from typing import Any

from .result_contract import (
    FAILED_EVIDENCE_LEVELS,
    MEDIUM_EVIDENCE_LEVELS,
    STRONG_EVIDENCE_LEVELS,
    WEAK_EVIDENCE_LEVELS,
    evidence_level_multiplier,
)

QUALITY_GRADES = {"strong", "medium", "weak", "failed_retrieval", "reliable_no_results", "missing"}
VERDICTS = {"exact_match", "close_prior_art", "adjacent_only", "no_reliable_prior_art", "partial_retrieval"}
CONFIDENCES = {"high", "medium", "low"}
RETRIEVAL_STATES = {
    "complete",            # every source is usable
    "partial",             # at least one source is weak, failed or missing
    "degraded",            # a provider failure, timeout or rate limit occurred
    "mixed_partial",       # mixed source quality
    "reliable_no_results", # every source reliably returned no results
    "failed",              # every source failed or is missing
}


def _as_list(value: Any) -> list[Any]:
    """Return the value as a list, or an empty list."""
    return value if isinstance(value, list) else []


def _is_generic(hit: dict[str, Any]) -> bool:
    """Determine whether a hit is marked as too generic."""
    return str(hit.get("relevance") or "").strip().lower() == "generic"


def _is_adjacent_or_generic(hit: dict[str, Any]) -> bool:
    """Determine whether a hit is merely indirect, weakly related or too generic."""
    return str(hit.get("relevance") or "").strip().lower() in {"generic", "adjacent", "loose"}


def _is_direct(hit: dict[str, Any]) -> bool:
    """Determine whether a hit is exact, direct or focused on the query."""
    return str(hit.get("relevance") or "").strip().lower() in {"direct", "focused", "exact"}


def hit_quality_score(hit: dict[str, Any], source_type: str = "") -> float:
    """Compute a bounded quality score from a hit's cleaned metadata."""
    try:
        base = float(hit.get("relevance_score") or hit.get("score") or 0.0)
    except (TypeError, ValueError):
        base = 0.0
    level = str(hit.get("evidence_level") or "unverified").strip().lower()
    if base <= 0:
        if level in STRONG_EVIDENCE_LEVELS:
            base = 7.0
        elif level in MEDIUM_EVIDENCE_LEVELS:
            base = 5.0
        elif level in WEAK_EVIDENCE_LEVELS:
            base = 3.0
        elif level in FAILED_EVIDENCE_LEVELS:
            base = 0.5
        else:
            base = 2.0
    if not hit.get("relevance_score"):
        base *= evidence_level_multiplier(level)
    if _is_generic(hit):
        base *= 0.5
    elif _is_direct(hit):
        base *= 1.1
    if hit.get("verified_url") is True:
        base += 0.2
    return round(max(0.0, min(base, 10.0)), 2)


def grade_source(source: dict[str, Any] | None, source_type: str = "") -> dict[str, Any]:
    """Grade one source's quality without exposing the raw evidence."""
    if not source:
        return {
            "quality_grade": "missing",
            "top_hit_quality": 0.0,
            "usable_for_final": False,
            "needs_retry": True,
            "exact_combination_candidate_found": False,
        }
    status = str(source.get("status") or "failed").strip().lower()
    completed = source.get("completed") is True
    reliable_no_results = source.get("reliable_no_results") is True
    hits = [hit for hit in _as_list(source.get("hits")) if isinstance(hit, dict)]
    if not hits:
        if status == "ok" and completed and reliable_no_results:
            grade = "reliable_no_results"
            usable = True
        else:
            grade = "failed_retrieval"
            usable = False
        return {
            "quality_grade": grade,
            "top_hit_quality": 0.0,
            "usable_for_final": usable,
            "needs_retry": not usable,
            "exact_combination_candidate_found": False,
        }

    exact_candidate = any(hit.get("exact_combination_candidate_found") is True for hit in hits)
    scored_hits = sorted(((hit_quality_score(hit, source_type), hit) for hit in hits), reverse=True, key=lambda item: item[0])
    top_score, top_hit = scored_hits[0]
    levels = {str(hit.get("evidence_level") or "unverified").strip().lower() for hit in hits}
    strong_hit = any(
        str(hit.get("evidence_level") or "").strip().lower() in STRONG_EVIDENCE_LEVELS and not _is_generic(hit)
        for hit in hits
    )
    medium_hit = any(
        str(hit.get("evidence_level") or "").strip().lower() in MEDIUM_EVIDENCE_LEVELS and not _is_generic(hit)
        for hit in hits
    )
    verified_snippets = [
        hit
        for hit in hits
        if str(hit.get("evidence_level") or "").strip().lower() == "search_snippet_only"
        and hit.get("verified_url") is True
        and not _is_generic(hit)
    ]
    if strong_hit:
        grade = "strong"
    elif medium_hit or len(verified_snippets) >= 2:
        grade = "medium"
    elif levels and levels <= FAILED_EVIDENCE_LEVELS:
        grade = "failed_retrieval"
    else:
        grade = "weak"

    if grade in {"strong", "medium"}:
        partial_failure = status == "partial_failure"
        adjacent_count = sum(1 for hit in hits if _is_adjacent_or_generic(hit))
        all_adjacent = bool(hits) and adjacent_count == len(hits)
        majority_adjacent = bool(hits) and adjacent_count * 2 > len(hits)
        if partial_failure and all_adjacent:
            grade = "weak"
        elif majority_adjacent and grade == "strong":
            grade = "medium"

    if grade == "strong" and status == "partial_failure" and float(top_score) < 5.0:
        grade = "medium"
    if grade == "medium" and status == "partial_failure" and float(top_score) < 3.5:
        grade = "weak"
    return {
        "quality_grade": grade,
        "top_hit_quality": top_score,
        "top_evidence_level": str(top_hit.get("evidence_level") or "unverified").strip().lower(),
        "usable_for_final": grade in {"strong", "medium", "reliable_no_results"},
        "needs_retry": grade in {"weak", "failed_retrieval", "missing"},
        "exact_combination_candidate_found": exact_candidate,
    }


def _classify_retrieval(source_grades: dict[str, dict[str, Any]]) -> str:
    """Determine the overall retrieval state from the individual source grades."""
    grades = [str(item.get("quality_grade") or "missing") for item in source_grades.values()]
    if not grades:
        return "failed"
    if all(grade == "reliable_no_results" for grade in grades):
        return "reliable_no_results"
    if all(grade in {"failed_retrieval", "missing"} for grade in grades):
        return "failed"
    usable = {"strong", "medium", "reliable_no_results"}
    weak_or_failed = {"weak", "failed_retrieval", "missing"}
    if all(grade in usable for grade in grades):
        return "complete"
    has_strong_or_medium = any(grade in {"strong", "medium"} for grade in grades)
    has_failed = any(grade in {"failed_retrieval", "missing"} for grade in grades)
    has_weak = any(grade == "weak" for grade in grades)
    if has_strong_or_medium and has_failed:
        return "degraded"
    if has_strong_or_medium and has_weak:
        return "mixed_partial"
    if all(grade in weak_or_failed for grade in grades):
        return "partial"
    return "partial"


def decide_verdict_and_confidence(
    source_grades: dict[str, dict[str, Any]],
    *,
    critical_requirements: list[str] | None = None,
    requirement_coverage: dict[str, int] | None = None,
    single_hit_full_coverage: bool = False,
    original_query: str = "",
) -> tuple[str, str, str]:
    """Determine the preliminary verdict, the confidence and the retrieval state."""
    del original_query
    grades = [str(item.get("quality_grade") or "missing") for item in source_grades.values()]
    strong_count = grades.count("strong")
    medium_count = grades.count("medium")
    exact = any(item.get("exact_combination_candidate_found") is True for item in source_grades.values())
    incomplete = any(grade in {"missing", "failed_retrieval", "weak"} for grade in grades)
    all_reliable_no_results = bool(grades) and all(grade == "reliable_no_results" for grade in grades)
    retrieval = _classify_retrieval(source_grades)

    requirement_count = len(critical_requirements or [])
    coverage_values = list((requirement_coverage or {}).values())
    max_single_coverage = max(coverage_values) if coverage_values else 0
    full_coverage_in_one_source = (
        requirement_count > 0
        and max_single_coverage >= requirement_count
    )

    retrieval_complete = retrieval in {"complete", "reliable_no_results"}
    if exact:
        verdict = "exact_match"
    elif (
        strong_count
        and full_coverage_in_one_source
        and retrieval_complete
        and single_hit_full_coverage
    ):
        verdict = "exact_match"
    elif strong_count:
        verdict = "close_prior_art"
    elif medium_count >= 2:
        verdict = "close_prior_art"
    elif medium_count:
        verdict = "adjacent_only"
    elif all_reliable_no_results:
        verdict = "no_reliable_prior_art"
    else:
        verdict = "partial_retrieval"

    if strong_count >= 2 and not incomplete:
        confidence = "high"
    elif strong_count >= 1 or (medium_count >= 2 and not incomplete) or all_reliable_no_results:
        confidence = "medium"
    else:
        confidence = "low"

    if retrieval in {"partial", "degraded", "mixed_partial", "failed"}:
        if confidence == "high":
            confidence = "medium"

    if requirement_count >= 2 and confidence == "high":
        if max_single_coverage * 2 < requirement_count:
            confidence = "medium"

    return verdict, confidence, retrieval
