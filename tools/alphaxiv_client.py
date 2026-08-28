"""Klient pre vzdialený AlphaXiv MCP server.

AlphaXiv (alphaxiv.org) vystavuje výskumné nástroje ako skutočný MCP server
na https://api.alphaxiv.org/mcp/v1 s autorizáciou cez API kľúč v hlavičke
Authorization: Bearer <kľúč> (Settings > API Keys na alphaxiv.org). Tento
modul volá jeho nástroj `discover_papers` na doplnkové publikačné
vyhľadávanie. Integrácia je vždy voliteľná: bez nastaveného
ALPHAXIV_API_KEY sa nepokúša o žiadne pripojenie.
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
    """Vráti klienta Streamable HTTP transportu naprieč verziami balíka mcp.

    Funkcia sa vo verzii 2.0 premenovala z `streamablehttp_client` na
    `streamable_http_client`. Import prebieha až pri volaní, takže nedostupnosť
    tohto voliteľného transportu nikdy nezhodí import celého balíka.
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
    """Načíta AlphaXiv API kľúč z premenných prostredia."""
    return os.getenv("ALPHAXIV_API_KEY", "").strip()


def extract_call_result_items(result: CallToolResult) -> list[Any]:
    """Vytiahne zoznam položiek z výsledku volania MCP nástroja.

    Skúša najprv structuredContent (bežné pre nástroje s deklarovanou
    výstupnou schémou), potom textové bloky obsahu — každý blok môže byť
    samostatný JSON objekt (FastMCP takto serializuje zoznamy) alebo jeden
    blok s celým JSON poľom. Ak nič nie je platný JSON, vráti spojený text
    ako jednu položku pre záložný textový parser.
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
    """Zavolá nástroj discover_papers na už pripojenej MCP relácii.

    Vydelená od sieťového pripojenia, aby sa dala testovať cez in-memory
    MCP server bez akéhokoľvek prístupu na sieť.
    """
    keywords = [term for term in query.split() if term][:12]
    result = await session.call_tool(
        "discover_papers",
        {"keywords": keywords, "question": query, "difficulty": difficulty},
    )
    return extract_call_result_items(result)


async def discover_papers(query: str, timeout_s: float = ALPHAXIV_TIMEOUT_S) -> list[Any]:
    """Pripojí sa k AlphaXiv MCP serveru a vyhľadá relevantné publikácie.

    Vráti prázdny zoznam, ak kľúč nie je nastavený alebo volanie akokoľvek
    zlyhá; tento provider je vždy len doplnkový a nesmie zhodiť ani spomaliť
    publikačné vyhľadávanie nad rámec vlastného časového limitu.
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
