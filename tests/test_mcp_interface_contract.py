"""Contract guard: the MCP interface Flowise sees must not change accidentally.

The 7 tool names, their parameter sets and their required parameters form the
interface an already-exported Flowise graph binds to. A rename or a dropped
parameter breaks that graph silently, because Flowise fetches tools/list at
runtime rather than storing the schema.

This fixture deliberately pins names, parameters and required flags but NOT the
description text, so a language experiment on the descriptions can proceed
without weakening the contract.
"""

from __future__ import annotations

import asyncio
from typing import get_type_hints

import server

EXPECTED = {
    "research_session_start": {
        "params": {"original_query", "session_id"},
        "required": {"original_query"},
    },
    "research_session_understand_query": {
        "params": {"session_id", "original_query", "run_id", "english_query"},
        "required": {"session_id"},
    },
    "patent_evidence_to_session": {
        "params": {
            "session_id", "query", "max_results", "max_fetches",
            "fetch_timeout_ms", "attempt_no", "run_id", "source_type", "english_query",
        },
        "required": set(),
    },
    "publication_evidence_to_session": {
        "params": {
            "session_id", "query", "max_results", "max_fetches",
            "fetch_timeout_s", "attempt_no", "run_id", "source_type", "english_query",
        },
        "required": set(),
    },
    "web_evidence_to_session": {
        "params": {
            "session_id", "query", "max_results", "max_fetches",
            "timeout_ms", "attempt_no", "run_id", "source_type", "english_query",
        },
        "required": set(),
    },
    "research_session_checklist": {
        "params": {"session_id", "max_attempts_per_source", "min_total_hits", "run_id"},
        "required": {"session_id"},
    },
    "research_session_user_answer": {
        "params": {"session_id", "original_query", "debug_mode", "force_regenerate", "run_id"},
        "required": {"session_id"},
    },
}


def _registered():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_exactly_the_seven_expected_tools_are_registered():
    assert set(_registered()) == set(EXPECTED)


def test_parameter_names_are_unchanged():
    for name, tool in _registered().items():
        actual = set((tool.inputSchema or {}).get("properties", {}))
        assert actual == EXPECTED[name]["params"], name


def test_required_parameters_are_unchanged():
    for name, tool in _registered().items():
        actual = set((tool.inputSchema or {}).get("required", []))
        assert actual == EXPECTED[name]["required"], name


def test_every_tool_has_a_non_empty_description():
    """Language is not pinned here on purpose; presence is."""
    for name, tool in _registered().items():
        assert (tool.description or "").strip(), name


def test_every_tool_wrapper_declares_text_return_type():
    """The registered wrappers are expected to return MCP text content."""
    for name in EXPECTED:
        assert get_type_hints(getattr(server, name)).get("return") is str, name
