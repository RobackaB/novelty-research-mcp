"""Testy čistenia vstupných dotazov."""

from __future__ import annotations

from tools.query_normalize import clean_tool_query, coerce_query_input


def test_plain_string_passthrough():
    assert clean_tool_query("smart door lock with mobile app") == "smart door lock with mobile app"


def test_whitespace_collapse():
    assert clean_tool_query("  smart   lock \n system ") == "smart lock system"


def test_leading_label_removed():
    assert clean_tool_query("query: smart lock") == "smart lock"
    assert clean_tool_query("QUESTION:   smart lock") == "smart lock"


def test_dict_input_extracts_query_key():
    assert clean_tool_query({"query": "predictive maintenance"}) == "predictive maintenance"


def test_dict_input_structural_keys_ignored():
    value = {"query": "anomaly detection", "max_results": 10, "filters": {"language": "en"}}
    assert clean_tool_query(value) == "anomaly detection"


def test_json_string_input_parsed():
    assert clean_tool_query('{"query": "vibration analysis"}') == "vibration analysis"


def test_list_input_joined_unique():
    assert coerce_query_input(["door lock", "door lock", "mobile app"]) == "door lock mobile app"


def test_none_input_returns_empty():
    assert clean_tool_query(None) == ""


def test_malformed_json_falls_back_to_stripped_text():
    result = clean_tool_query('{"query": "smart lock"')
    assert "smart lock" in result
    assert "{" not in result and "}" not in result


def test_nested_terms_lists_extracted():
    value = {"keywords": ["vibration", "temperature"], "query": "electric motor"}
    result = clean_tool_query(value)
    assert "electric motor" in result
    assert "vibration" in result
