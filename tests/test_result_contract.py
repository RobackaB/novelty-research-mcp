"""Testy stavových markerov výsledkov."""

from __future__ import annotations

from tools.result_contract import (
    NormalizedResult,
    evidence_level_multiplier,
    has_hits,
    parse_completed_marker,
    parse_error_count,
    parse_reliable_no_results_marker,
    parse_status_marker,
    prepend_markers,
    status_markers,
)


def _result(**kwargs) -> NormalizedResult:
    base = dict(status="ok", completed=True, reliable_no_results=False)
    base.update(kwargs)
    return NormalizedResult(**base)


def test_status_markers_roundtrip():
    text = status_markers(_result(query="smart lock"))
    assert parse_status_marker(text) == "ok"
    assert parse_completed_marker(text) is True
    assert parse_reliable_no_results_marker(text) is False
    assert parse_error_count(text) == 0
    assert "QUERY: smart lock" in text


def test_error_markers_serialized():
    result = _result(status="failed", completed=False, errors=[{"type": "boom", "message": "x failed"}])
    text = status_markers(result)
    assert parse_status_marker(text) == "failed"
    assert parse_error_count(text) == 1
    assert "ERROR: boom - x failed" in text


def test_parse_returns_none_for_missing_markers():
    assert parse_status_marker("plain text") is None
    assert parse_completed_marker("plain text") is None
    assert parse_error_count("plain text") is None


def test_prepend_markers_keeps_body():
    text = prepend_markers(_result(), "BODY CONTENT")
    assert text.endswith("BODY CONTENT")
    assert text.startswith("STATUS: OK")


def test_has_hits_detects_urls_outside_markers():
    body = prepend_markers(_result(), "Source page URL for this result: https://example.com/page")
    assert has_hits(body)
    assert not has_hits(status_markers(_result()))


def test_evidence_level_multiplier_known_and_default():
    assert evidence_level_multiplier("claim_verified") > 1.0
    assert evidence_level_multiplier("fetch_failed") < 0.5
    assert evidence_level_multiplier("nonsense") == 0.7
    assert evidence_level_multiplier(None) == 0.7
