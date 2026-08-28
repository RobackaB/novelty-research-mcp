"""Testy hodnotenia kvality zdrojov a verdiktov."""

from __future__ import annotations

from tools.evidence_quality import (
    decide_verdict_and_confidence,
    grade_source,
    hit_quality_score,
)


def _hit(**kwargs):
    base = {"title": "t", "evidence_level": "search_snippet_only", "relevance": "focused"}
    base.update(kwargs)
    return base


def test_hit_quality_score_bounds():
    score = hit_quality_score(_hit(relevance_score=25.0))
    assert 0.0 <= score <= 10.0
    assert hit_quality_score(_hit(relevance_score="not-a-number")) >= 0.0


def test_grade_source_missing():
    graded = grade_source(None)
    assert graded["quality_grade"] == "missing"
    assert graded["needs_retry"] is True


def test_grade_source_reliable_no_results():
    source = {"status": "ok", "completed": True, "reliable_no_results": True, "hits": []}
    graded = grade_source(source)
    assert graded["quality_grade"] == "reliable_no_results"
    assert graded["usable_for_final"] is True


def test_grade_source_failed_when_no_hits_and_not_reliable():
    source = {"status": "failed", "completed": False, "reliable_no_results": False, "hits": []}
    assert grade_source(source)["quality_grade"] == "failed_retrieval"


def test_grade_source_strong_hit():
    source = {
        "status": "ok",
        "completed": True,
        "reliable_no_results": False,
        "hits": [_hit(evidence_level="claim_verified", relevance="focused", relevance_score=7.5)],
    }
    graded = grade_source(source, "patent")
    assert graded["quality_grade"] == "strong"
    assert graded["usable_for_final"] is True


def test_grade_source_two_verified_snippets_is_medium():
    hits = [
        _hit(evidence_level="search_snippet_only", verified_url=True, relevance="focused"),
        _hit(evidence_level="search_snippet_only", verified_url=True, relevance="focused"),
    ]
    source = {"status": "ok", "completed": True, "reliable_no_results": False, "hits": hits}
    assert grade_source(source, "web")["quality_grade"] == "medium"


def test_grade_source_all_failed_levels():
    hits = [_hit(evidence_level="fetch_failed"), _hit(evidence_level="fetch_timeout")]
    source = {"status": "partial_failure", "completed": False, "reliable_no_results": False, "hits": hits}
    assert grade_source(source)["quality_grade"] == "failed_retrieval"


def test_grade_source_partial_failure_all_adjacent_downgrades():
    hits = [
        _hit(evidence_level="claim_verified", relevance="adjacent"),
        _hit(evidence_level="search_snippet_only", relevance="generic"),
    ]
    source = {"status": "partial_failure", "completed": False, "reliable_no_results": False, "hits": hits}
    graded = grade_source(source, "patent")
    assert graded["quality_grade"] in {"weak", "medium"}
    assert graded["quality_grade"] != "strong"


def _grades(**mapping):
    out = {}
    for source, grade in mapping.items():
        out[source] = {"quality_grade": grade, "exact_combination_candidate_found": False}
    return out


def test_verdict_all_reliable_no_results():
    verdict, confidence, retrieval = decide_verdict_and_confidence(
        _grades(patent="reliable_no_results", publication="reliable_no_results", web="reliable_no_results")
    )
    assert verdict == "no_reliable_prior_art"
    assert retrieval == "reliable_no_results"
    assert confidence == "medium"


def test_verdict_strong_source_close_prior_art():
    verdict, confidence, _ = decide_verdict_and_confidence(
        _grades(patent="strong", publication="medium", web="medium")
    )
    assert verdict == "close_prior_art"
    assert confidence in {"medium", "high"}


def test_verdict_exact_candidate_wins():
    grades = _grades(patent="strong", publication="medium", web="medium")
    grades["patent"]["exact_combination_candidate_found"] = True
    verdict, _, _ = decide_verdict_and_confidence(grades)
    assert verdict == "exact_match"


def test_verdict_partial_retrieval_when_all_weak():
    verdict, confidence, retrieval = decide_verdict_and_confidence(
        _grades(patent="weak", publication="failed_retrieval", web="missing")
    )
    assert verdict == "partial_retrieval"
    assert confidence == "low"
    assert retrieval in {"partial", "failed"}


def test_confidence_capped_on_incomplete_retrieval():
    verdict, confidence, retrieval = decide_verdict_and_confidence(
        _grades(patent="strong", publication="strong", web="failed_retrieval")
    )
    assert retrieval == "degraded"
    assert confidence != "high"
