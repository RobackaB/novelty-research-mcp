"""Guard: which hit becomes `top_hit` must not depend on caller ordering.

`grade_source` sorted (quality_score, hit) pairs by score alone and took the
first, so equal-scoring hits kept the order of the caller's list. `top_hit`
determines the returned `top_evidence_level`, which research_session stores and
which reaches output state, so the caller's list order silently decided part of
the result.

The tie is reachable in ordinary data: when a hit carries an explicit
`relevance_score`, `hit_quality_score` skips the evidence-level multiplier, so
two hits can score identically while differing in evidence level.

This is a determinism fix only. The secondary key is a stable identity tuple; its
final component happens to be `evidence_level`, but lexicographic order there
carries no claim that one evidence level outranks another.
"""

from __future__ import annotations

from tools.evidence_quality import grade_source, hit_quality_score


def _hit(canonical_id: str, evidence_level: str) -> dict:
    return {
        "title": "Anomaly detection in application logs",
        "url": f"https://example.com/{canonical_id}",
        "canonical_id": canonical_id,
        "evidence_level": evidence_level,
        "summary": "Detecting anomalies in application logs using machine learning.",
        "verified_url": True,
        "relevance": "focused",
        "relevance_score": 6.25,
    }


ALPHA = _hit("alpha", "claim_verified")
BRAVO = _hit("bravo", "abstract_verified")


def _pack(hits: list[dict]) -> dict:
    return {
        "source_type": "publication",
        "status": "ok",
        "completed": True,
        "reliable_no_results": False,
        "hits": hits,
        "errors": [],
        "warnings": [],
    }


def test_the_tie_is_actually_reachable():
    """Without a genuine tie this guard would pass against the broken code."""
    assert hit_quality_score(ALPHA, "publication") == hit_quality_score(BRAVO, "publication")
    assert ALPHA["evidence_level"] != BRAVO["evidence_level"]


def test_top_evidence_level_is_stable_under_caller_reordering():
    forward = grade_source(_pack([ALPHA, BRAVO]), "publication")
    reversed_order = grade_source(_pack([BRAVO, ALPHA]), "publication")
    assert forward["top_evidence_level"] == reversed_order["top_evidence_level"], (
        f"top_hit depends on caller ordering: {forward['top_evidence_level']!r} forward "
        f"vs {reversed_order['top_evidence_level']!r} reversed"
    )


def test_top_hit_quality_is_unaffected_by_ordering():
    """The score was never ambiguous; only which tied hit was reported."""
    forward = grade_source(_pack([ALPHA, BRAVO]), "publication")
    reversed_order = grade_source(_pack([BRAVO, ALPHA]), "publication")
    assert forward["top_hit_quality"] == reversed_order["top_hit_quality"]


def test_hits_missing_identity_fields_still_order_deterministically():
    """The identity tuple must stay total when canonical_id and url are absent."""
    bare_a = {"title": "", "evidence_level": "abstract_verified", "relevance_score": 6.25,
              "relevance": "focused", "verified_url": True, "summary": "s"}
    bare_b = {"title": "", "evidence_level": "claim_verified", "relevance_score": 6.25,
              "relevance": "focused", "verified_url": True, "summary": "s"}
    forward = grade_source(_pack([bare_a, bare_b]), "publication")
    reversed_order = grade_source(_pack([bare_b, bare_a]), "publication")
    assert forward["top_evidence_level"] == reversed_order["top_evidence_level"]
