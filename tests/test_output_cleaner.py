"""Tests of text cleaning and truncation."""

from __future__ import annotations

from tools.output_cleaner import clean_output, first_match, format_error, strip_json_fences, trim_words
from tools.patent_evidence_pack import _compute_claim_coverage, _normalize_patent_identity


def test_clean_output_strips_html_and_noise():
    text = "<html><body><p>Useful technical content line here</p><p>cookie policy</p></body></html>"
    cleaned = clean_output(text)
    assert "Useful technical content" in cleaned
    assert "cookie" not in cleaned.lower()


def test_clean_output_keeps_structured_labels():
    cleaned = clean_output("STATUS: OK\nCONTENT: value line with words")
    assert "STATUS: OK" in cleaned


def test_trim_words():
    assert trim_words("one two three four", 2) == "one two..."
    assert trim_words("one two", 5) == "one two"


def test_strip_json_fences():
    assert strip_json_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert strip_json_fences("plain") == "plain"


def test_format_error_includes_url():
    error_text = format_error("tool_x", "boom", url="https://x")
    assert "TOOL_ERROR: tool_x" in error_text
    assert "URL: https://x" in error_text


def test_first_match_default():
    assert first_match([r"missing (\w+)"], "no matches here") == "Unknown"
    assert first_match([r"value: (\w+)"], "value: found") == "found"


def test_normalize_patent_identity_prefers_url_id():
    title, number = _normalize_patent_identity(
        "US9999999B1 - Smart lock - Google Patents",
        "US1111111A",
        "https://patents.google.com/patent/US1234567B2/en",
    )
    assert number == "US1234567B2"
    assert "Google Patents" not in title
    assert "Smart lock" in title


def test_compute_claim_coverage():
    atoms = [
        {"terms": ["mobile", "application"]},
        {"terms": ["entry", "history"]},
        {"terms": ["fingerprint"]},
    ]
    claim = "1. A lock controlled by a mobile application that logs entry history."
    coverage, matched = _compute_claim_coverage(claim, atoms)
    assert matched == 2
    assert coverage == round(2 / 3, 4)
    assert _compute_claim_coverage("", atoms) == (0.0, 0)
    assert _compute_claim_coverage(claim, None) == (0.0, 0)
