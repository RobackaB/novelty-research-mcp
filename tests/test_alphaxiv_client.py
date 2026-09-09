"""Tests of the AlphaXiv MCP client against a real in-memory MCP server (no network)."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from tools.alphaxiv_client import (
    alphaxiv_api_key,
    discover_papers,
    discover_papers_via_session,
    extract_call_result_items,
)


def _build_test_server(papers_by_call):
    """Build a test FastMCP server exposing a discover_papers tool.

    `papers_by_call` is a list of responses, one per call; the last is repeated
    if the tool is called more times than there are entries.
    """
    server = FastMCP("alphaxiv-test")
    calls: list[dict] = []

    @server.tool()
    def discover_papers(keywords: list[str], question: str = "", difficulty: int = 5) -> list[dict]:
        calls.append({"keywords": keywords, "question": question, "difficulty": difficulty})
        index = min(len(calls) - 1, len(papers_by_call) - 1)
        return papers_by_call[index]

    return server, calls


async def test_discover_papers_via_session_parses_list_return():
    server, calls = _build_test_server(
        [
            [
                {
                    "title": "Smart Lock Security Study",
                    "arxivId": "2301.00001",
                    "publicationDate": "2023-01-15",
                    "organizations": ["MIT"],
                    "abstractPreview": "A study of smart door locks controlled by a mobile application.",
                }
            ]
        ]
    )
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        items = await discover_papers_via_session(session, "smart door lock mobile application")

    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Smart Lock Security Study"
    assert item["arxivId"] == "2301.00001"
    assert calls[0]["question"] == "smart door lock mobile application"
    assert "smart" in calls[0]["keywords"]


async def test_discover_papers_via_session_multiple_papers():
    server, _calls = _build_test_server(
        [
            [
                {"title": "Paper A", "arxivId": "2301.00001"},
                {"title": "Paper B", "arxivId": "2301.00002"},
            ]
        ]
    )
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        items = await discover_papers_via_session(session, "smart lock")

    titles = {item["title"] for item in items}
    assert titles == {"Paper A", "Paper B"}


async def test_discover_papers_via_session_empty_result():
    server, _calls = _build_test_server([[]])
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        items = await discover_papers_via_session(session, "obscure query with no matches")
    assert items == []


async def test_discover_papers_via_session_tool_error_returns_empty():
    server = FastMCP("alphaxiv-error-test")

    @server.tool()
    def discover_papers(keywords: list[str], question: str = "", difficulty: int = 5) -> list[dict]:
        raise RuntimeError("upstream failure")

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        items = await discover_papers_via_session(session, "smart lock")
    assert items == []


# --- extract_call_result_items unit-level coverage on hand-built results ----

class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, content=None, structuredContent=None, isError=False):
        self.content = content or []
        self.structuredContent = structuredContent
        self.isError = isError


def test_extract_call_result_items_error_result_is_empty():
    assert extract_call_result_items(_FakeResult(isError=True)) == []


def test_extract_call_result_items_structured_result_key():
    result = _FakeResult(structuredContent={"result": [{"title": "A"}, {"title": "B"}]})
    items = extract_call_result_items(result)
    assert [item["title"] for item in items] == ["A", "B"]


def test_extract_call_result_items_structured_papers_key():
    result = _FakeResult(structuredContent={"papers": [{"title": "A"}]})
    assert extract_call_result_items(result) == [{"title": "A"}]


def test_extract_call_result_items_structured_plain_list():
    result = _FakeResult(structuredContent=[{"title": "A"}])
    assert extract_call_result_items(result) == [{"title": "A"}]


def test_extract_call_result_items_multiple_json_text_blocks():
    result = _FakeResult(content=[_FakeTextBlock('{"title": "A"}'), _FakeTextBlock('{"title": "B"}')])
    items = extract_call_result_items(result)
    assert [item["title"] for item in items] == ["A", "B"]


def test_extract_call_result_items_single_json_array_block():
    result = _FakeResult(content=[_FakeTextBlock('[{"title": "A"}, {"title": "B"}]')])
    items = extract_call_result_items(result)
    assert [item["title"] for item in items] == ["A", "B"]


def test_extract_call_result_items_non_json_text_falls_back_to_joined_text():
    result = _FakeResult(content=[_FakeTextBlock("Title: A\nAbstract: something about locks")])
    items = extract_call_result_items(result)
    assert items == ["Title: A\nAbstract: something about locks"]


def test_extract_call_result_items_empty_when_nothing_usable():
    assert extract_call_result_items(_FakeResult()) == []


# --- discover_papers (network entry point) short-circuit behavior ----------

def test_alphaxiv_api_key_reads_env(monkeypatch):
    monkeypatch.delenv("ALPHAXIV_API_KEY", raising=False)
    assert alphaxiv_api_key() == ""
    monkeypatch.setenv("ALPHAXIV_API_KEY", " secret ")
    assert alphaxiv_api_key() == "secret"


async def test_discover_papers_without_key_returns_empty_without_network(monkeypatch):
    monkeypatch.delenv("ALPHAXIV_API_KEY", raising=False)
    assert await discover_papers("smart lock") == []


async def test_discover_papers_empty_query_short_circuits(monkeypatch):
    monkeypatch.setenv("ALPHAXIV_API_KEY", "test-key")
    assert await discover_papers("   ") == []
