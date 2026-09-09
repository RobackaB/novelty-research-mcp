"""Client for the remote AlphaXiv MCP server.

AlphaXiv (alphaxiv.org) exposes its research tools as a real MCP server at
https://api.alphaxiv.org/mcp/v1, authorised by an API key in the
Authorization: Bearer <key> header (Settings > API Keys on alphaxiv.org). This
module calls its `discover_papers` tool as a supplementary publication search.
The integration is always optional: with no ALPHAXIV_API_KEY set, no connection
is attempted at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp import ClientSession
from mcp.types import CallToolResult

LOGGER = logging.getLogger(__name__)


def _streamable_http_client():
    """Return the Streamable HTTP transport client across versions of the mcp package.

    The function was renamed in 2.0 from `streamablehttp_client` to
    `streamable_http_client`. The import happens at call time, so the absence of
    this optional transport can never break the import of the whole package.
    """
    from mcp.client import streamable_http as transport

    for name in ("streamable_http_client", "streamablehttp_client"):
        client = getattr(transport, name, None)
        if client is not None:
            return client
    raise ImportError("Streamable HTTP client is not available in the installed mcp package.")

ALPHAXIV_MCP_URL = "https://api.alphaxiv.org/mcp/v1"
ALPHAXIV_TIMEOUT_S = 25.0
_STRUCTURED_LIST_KEYS = ("result", "papers", "results", "data", "items")


def alphaxiv_api_key() -> str:
    """Read the AlphaXiv API key from the environment variables."""
    return os.getenv("ALPHAXIV_API_KEY", "").strip()


def extract_call_result_items(result: CallToolResult) -> list[Any]:
    """Extract the list of items from an MCP tool call result.

    Tries structuredContent first, which is usual for tools that declare an
    output schema, then the textual content blocks. Each block may be a separate
    JSON object -- this is how FastMCP serialises lists -- or one block holding
    the whole JSON array. If nothing is valid JSON, the joined text is returned
    as a single item for the fallback text parser.
    """
    if result.isError:
        return []
    structured = result.structuredContent
    if isinstance(structured, dict):
        for key in _STRUCTURED_LIST_KEYS:
            nested = structured.get(key)
            if isinstance(nested, list):
                return nested
        if structured:
            return [structured]
    elif isinstance(structured, list):
        return structured

    items: list[Any] = []
    text_parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        text_parts.append(text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            items.extend(parsed)
        elif isinstance(parsed, dict):
            items.append(parsed)
    if items:
        return items
    joined = "\n\n".join(text_parts).strip()
    return [joined] if joined else []


async def discover_papers_via_session(
    session: ClientSession, query: str, difficulty: int = 5
) -> list[Any]:
    """Call the discover_papers tool on an already connected MCP session.

    Kept separate from the network connection so that it can be tested against
    an in-memory MCP server without any network access at all.
    """
    keywords = [term for term in query.split() if term][:12]
    result = await session.call_tool(
        "discover_papers",
        {"keywords": keywords, "question": query, "difficulty": difficulty},
    )
    return extract_call_result_items(result)


async def discover_papers(query: str, timeout_s: float = ALPHAXIV_TIMEOUT_S) -> list[Any]:
    """Connect to the AlphaXiv MCP server and search for relevant publications.

    Returns an empty list if the key is not set or the call fails in any way.
    This provider is always supplementary and must never break publication
    search, nor slow it down beyond its own timeout.
    """
    api_key = alphaxiv_api_key()
    if not api_key or not query.strip():
        return []

    async def _run() -> list[Any]:
        async with _streamable_http_client()(
            ALPHAXIV_MCP_URL, headers={"Authorization": f"Bearer {api_key}"}
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await discover_papers_via_session(session, query)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except Exception as exc:
        LOGGER.info("alphaxiv discover_papers failed: %s", exc)
        return []
