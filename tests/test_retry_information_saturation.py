"""Retry decisions from real persisted attempts, without provider/network calls."""

import json

import pytest

from tools import research_session as rs


def _hit(source, identity=1, **changes):
    hit = {
        "title": "Sensor document", "summary": "Temperature sensor evidence",
        "url": f"https://example.org/document/{identity}",
        "evidence_level": "fetched_excerpt", "verified_url": True,
        "relevance": "direct", "relevance_score": 7.0,
    }
    if source == "patent":
        hit["patent_number"] = f"US123456{identity}B2"
    if source == "publication":
        hit["doi"] = f"10.1234/{identity}"
    return {**hit, **changes}


def _save(session, source, attempt, hits, query="sensor", **changes):
    pack = {
        "source_type": source, "status": "ok", "completed": True,
        "hits": hits, "errors": [], "warnings": [],
        "reliable_no_results": not hits, **changes,
    }
    ack = json.loads(rs.research_session_save_evidence(
        session, source, pack, query=query, attempt=attempt,
    ))
    assert ack["status"] == "written", ack
    return ack


def _scenario(source, case):
    session = json.loads(rs.research_session_start("sensor"))["session_id"]
    envelope = {
        "understanding_query": "sensor", "core_subject": "sensor",
        "query_variants": {s: ["sensor"] for s in rs.SOURCE_TYPES},
        "critical_requirements_atomic": [],
    }
    if case == "variant":
        envelope["query_variants"][source] += ["sensor battery-free"]
    if case == "atom":
        envelope["critical_requirements_atomic"] = [{
            "category": "power", "label": "battery-free", "terms": ["battery-free"],
        }]
    with rs._connect() as conn:
        conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                     (json.dumps(envelope), session))
        conn.commit()
    before = [_hit(source)]
    after = [_hit(source)]
    kwargs = {}
    if case == "level":
        before = [_hit(source, evidence_level="search_snippet_only")]
    if case == "verification":
        before = [_hit(source, verified_url=False)]
    if case == "text":
        after = [_hit(source, summary="Temperature sensor with newly retrieved technical details")]
    if case == "new":
        before = [_hit(source, i) for i in range(5)]
        after = before + [_hit(source, 5)]  # 5/6 duplicates still exceeds 0.8.
    if case == "weak":
        before = after = [_hit(source, evidence_level="search_snippet_only", verified_url=False)]
    if case == "partial":
        kwargs = {"status": "partial_failure", "completed": False,
                  "errors": [{"type": "timeout", "message": "Provider timeout"}]}
    if case == "degraded":
        after = [_hit(source, evidence_level="search_snippet_only", verified_url=False)]
    for other in rs.SOURCE_TYPES:
        if other != source:
            _save(session, other, 1, [])
    _save(session, source, 1, before)
    _save(session, source, 2, after, **kwargs)
    return session


def _assert_retry(session, source, case):
    checklist = json.loads(rs.research_session_checklist(session))
    check = next(c for c in checklist["source_checks"] if c["source_type"] == source)
    assert not check.get("retry_saturated"), check
    assert check["needs_retry"] is True
    assert check["ready"] is False
    actions = [a for a in checklist["retry_actions"] if a["source_type"] == source]
    assert len(actions) == 1, checklist
    assert actions[0]["attempt_no"] == 3
    assert not checklist["can_finalize"]
    if case == "variant":
        assert actions[0]["query"] == "sensor battery-free"
    if case == "atom":
        assert actions[0]["element_guided"] is True
        assert actions[0]["missing_atom"] == "battery-free"
    return checklist


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
@pytest.mark.parametrize("case", ["variant", "level", "verification", "text", "new", "weak", "partial", "degraded", "atom"])
def test_information_path_survives_duplicate_retry(temp_db, source, case):
    _assert_retry(_scenario(source, case), source, case)


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
def test_identical_complete_evidence_exhausted_plan_can_saturate(temp_db, source):
    session = _scenario(source, "unchanged")
    result = json.loads(rs.research_session_checklist(session))
    check = next(c for c in result["source_checks"] if c["source_type"] == source)
    assert check["retry_saturated"] is True
    assert result["retry_actions"] == []
    assert result["can_finalize"] is True


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
def test_budget_stops_retries_despite_remaining_paths(temp_db, source):
    session = _scenario(source, "variant")
    budget = rs._max_attempts_for_source(source, 2)
    for attempt in range(3, budget + 1):
        _save(session, source, attempt, [_hit(source, evidence_level="search_snippet_only")])
    result = json.loads(rs.research_session_checklist(session))
    assert not any(a["source_type"] == source for a in result["retry_actions"])
    ack = json.loads(rs.research_session_save_evidence(session, source, {}, attempt=budget + 1))
    assert ack["status"] == "attempt_budget_exceeded"


def test_best_attempt_unchanged_and_plan_deterministic(temp_db):
    session = _scenario("patent", "degraded")
    with rs._connect() as conn:
        before = dict(rs._best_attempt_rows(conn, session)["patent"])
    assert before["attempt"] == 1
    first = _assert_retry(session, "patent", "degraded")
    second = _assert_retry(session, "patent", "degraded")
    assert first["retry_actions"] == second["retry_actions"]
    with rs._connect() as conn:
        assert dict(rs._best_attempt_rows(conn, session)["patent"]) == before


def test_element_retry_not_blocked_by_other_source_retry(temp_db):
    session = _scenario("patent", "atom")
    _save(session, "web", 2, [_hit("web", evidence_level="search_snippet_only")])
    result = _assert_retry(session, "patent", "atom")
    assert any(a["source_type"] == "web" for a in result["retry_actions"])


def test_missing_envelope_cannot_certify_saturation(temp_db):
    session = _scenario("patent", "unchanged")
    with rs._connect() as conn:
        conn.execute("UPDATE research_sessions SET query_envelope_json='{}' WHERE session_id=?", (session,))
        conn.commit()
    result = json.loads(rs.research_session_checklist(session))
    assert not any(c.get("retry_saturated") for c in result["source_checks"])


def test_unused_variant_uses_recorded_queries_not_attempt_index(temp_db):
    session = _scenario("patent", "variant")
    with rs._connect() as conn:
        envelope = json.loads(rs._session_row(conn, session)["query_envelope_json"])
        envelope["query_variants"]["patent"] = ["sensor", "  SENSOR  ", "untried", "already used"]
        conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                     (json.dumps(envelope), session))
        conn.execute("UPDATE evidence_results SET query='already used' WHERE session_id=? AND source_type='patent' AND attempt=2", (session,))
        conn.commit()
    result = _assert_retry(session, "patent", "other")
    assert result["retry_actions"][0]["query"] == "untried"


def test_hit_order_does_not_prevent_saturation(temp_db):
    session = _scenario("patent", "unchanged")
    _save(session, "patent", 3, [dict(reversed(list(_hit("patent").items())))])
    result = json.loads(rs.research_session_checklist(session))
    assert next(c for c in result["source_checks"] if c["source_type"] == "patent")["retry_saturated"]


def test_budget_exceeded_trace_still_prevents_retry(temp_db):
    session = _scenario("patent", "variant")
    ack = json.loads(rs.research_session_save_evidence(session, "patent", {}, attempt=99))
    assert ack["status"] == "attempt_budget_exceeded"
    result = json.loads(rs.research_session_checklist(session))
    assert not any(a["source_type"] == "patent" for a in result["retry_actions"])


def test_first_attempt_quality_readiness_unchanged(temp_db):
    session = json.loads(rs.research_session_start("sensor"))["session_id"]
    for source in rs.SOURCE_TYPES:
        _save(session, source, 1, [_hit(source)])
    result = json.loads(rs.research_session_checklist(session))
    assert result["can_finalize"] is True
    assert result["retry_actions"] == []


def _old_ratio_only(row, **ignored_context):
    if not row or int(row["attempt"]) <= 1:
        return False
    stats = json.loads(row["normalized_json"]).get("__dedupe_stats", {})
    inserted = int(stats.get("inserted_hits") or 0)
    return inserted >= 1 and int(stats.get("deduped_hits") or 0) / inserted >= 0.8


@pytest.mark.parametrize("case", ["variant", "level", "verification", "text", "new", "weak", "partial", "atom"])
def test_restored_ratio_defect_fails_regression_assertion(temp_db, monkeypatch, case):
    session = _scenario("patent", case)
    _assert_retry(session, "patent", case)
    monkeypatch.setattr(rs, "_is_source_retry_saturated", _old_ratio_only)
    # The same production-path assertion must FAIL with the historical defect.
    with pytest.raises(AssertionError, match="retry_saturated"):
        _assert_retry(session, "patent", case)


# --- diagnostic timing is not information gain -------------------------------

def _timing_scenario(*, before_log, after_log, after_changes=None):
    """Two patent attempts with the same substantive hit, exhausted plan.

    Variants exhausted, no uncovered atom, latest complete and usable, dedupe
    ratio qualifying: everything except evidence equality already permits
    saturation, so the outcome turns solely on how hits are compared.
    """
    session = json.loads(rs.research_session_start("sensor"))["session_id"]
    envelope = {
        "understanding_query": "sensor", "core_subject": "sensor",
        "query_variants": {s: ["sensor"] for s in rs.SOURCE_TYPES},
        "critical_requirements_atomic": [],
    }
    with rs._connect() as conn:
        conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                     (json.dumps(envelope), session))
        conn.commit()
    for other in rs.SOURCE_TYPES:
        if other != "patent":
            _save(session, other, 1, [])
    _save(session, "patent", 1, [_hit("patent", attempt_log=before_log)])
    _save(session, "patent", 2, [_hit("patent", attempt_log=after_log, **(after_changes or {}))])
    return session


_LOG_A = [{"provider": "google_patents", "attempt": 1, "status": "ok", "elapsed_ms": 800}]
_LOG_B = [{"provider": "google_patents", "attempt": 1, "status": "ok", "elapsed_ms": 950}]


def _patent_check(session):
    checklist = json.loads(rs.research_session_checklist(session))
    return next(c for c in checklist["source_checks"] if c["source_type"] == "patent")


def test_attempt_log_timing_difference_does_not_block_saturation(temp_db):
    """elapsed_ms 800 vs 950 is diagnostics, not new information."""
    check = _patent_check(_timing_scenario(before_log=_LOG_A, after_log=_LOG_B))
    assert check["retry_saturated"] is True, check
    assert check["needs_retry"] is False
    assert check["ready"] is True


def test_identical_attempt_logs_also_saturate(temp_db):
    """Control: the timing case must not be the only reason saturation works."""
    check = _patent_check(_timing_scenario(before_log=_LOG_A, after_log=_LOG_A))
    assert check["retry_saturated"] is True, check


@pytest.mark.parametrize(
    "field, value",
    [
        ("evidence_level", "claim_verified"),
        ("verified_url", False),
        ("summary", "Temperature sensor with newly retrieved claim text"),
        ("relevance", "adjacent"),
        ("relevance_score", 9.0),
    ],
    ids=["evidence_level", "verified_url", "summary", "relevance", "relevance_score"],
)
def test_substantive_change_still_prevents_saturation_despite_timing(temp_db, field, value):
    """Excluding attempt_log must not weaken any substantive comparison."""
    check = _patent_check(
        _timing_scenario(before_log=_LOG_A, after_log=_LOG_B, after_changes={field: value})
    )
    assert not check.get("retry_saturated"), check
    assert check["needs_retry"] is True


def test_excluded_keys_are_narrow():
    """Only diagnostic-only keys may be excluded from the signature."""
    assert rs._RETRY_SIGNATURE_EXCLUDED_KEYS == frozenset({"attempt_log", "attempt_log_json"})


def test_signature_projects_rather_than_mutating_the_hit():
    hit = _hit("patent", attempt_log=_LOG_A)
    rs._retry_evidence_signature(hit)
    assert hit["attempt_log"] == _LOG_A, "the stored hit must not be mutated"


def test_signature_compares_unknown_substantive_keys_by_default():
    """A new field is compared unless explicitly excluded."""
    a = rs._retry_evidence_signature(_hit("patent", some_future_field="x"))
    b = rs._retry_evidence_signature(_hit("patent", some_future_field="y"))
    assert a != b


def _raw_json_signature(hit):
    """The historical comparison: full raw-hit JSON equality."""
    return json.dumps(hit, sort_keys=True, ensure_ascii=False)


def test_restored_raw_hit_equality_fails_the_timing_regression(temp_db, monkeypatch):
    """Defect-restoration control for this blocker."""
    session = _timing_scenario(before_log=_LOG_A, after_log=_LOG_B)
    assert _patent_check(session)["retry_saturated"] is True

    monkeypatch.setattr(rs, "_retry_evidence_signature", _raw_json_signature)
    check = _patent_check(session)
    assert not check.get("retry_saturated"), (
        "raw-hit equality should have treated the timing difference as information gain"
    )
    assert check["needs_retry"] is True
