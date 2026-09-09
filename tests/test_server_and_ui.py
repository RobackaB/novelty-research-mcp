"""Tests of MCP tool registration and the terminal banner."""

from __future__ import annotations

import json

import server
import terminal_ui

EXPECTED_TOOLS = {
    "research_session_start",
    "research_session_understand_query",
    "patent_evidence_to_session",
    "publication_evidence_to_session",
    "web_evidence_to_session",
    "research_session_checklist",
    "research_session_user_answer",
}


async def test_all_workflow_tools_registered():
    tools = await server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_safe_writer_ack_shape():
    ack = json.loads(server._safe_writer_ack("web", "rs_x", 1, "run", "provider_error", "boom"))
    assert ack["ok"] is False
    assert ack["status"] == "provider_error"
    assert ack["needs_retry"] is True
    assert ack["usable_for_final"] is False
    assert ack["compact_warnings"] == ["boom"]


def test_banner_lists_registered_tools(monkeypatch):
    monkeypatch.delenv("MCP_NO_UI", raising=False)
    banner = terminal_ui.startup_banner("0.0.0.0", 8000, "/mcp")
    for tool_name in EXPECTED_TOOLS:
        assert tool_name in banner
    assert "http://host.docker.internal:8000/mcp" in banner
    assert "http://127.0.0.1:8000/mcp" in banner


def test_banner_suppressed_by_env(monkeypatch, capsys):
    monkeypatch.setenv("MCP_NO_UI", "1")
    terminal_ui.print_startup_banner("0.0.0.0", 8000, "/mcp")
    assert capsys.readouterr().out == ""


def test_banner_reports_key_status(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    banner = terminal_ui.startup_banner("0.0.0.0", 8000, "/mcp")
    assert "configured" in banner
    assert "missing" in banner
