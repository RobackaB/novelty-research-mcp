"""Registers the MCP tools used by the research workflow with SQLite storage."""

import json
import logging
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

SERVER_ENV_PATH = Path(__file__).with_name(".env")
WORKSPACE_ENV_PATH = Path(__file__).resolve().parent.parent / "env.env"
load_dotenv(SERVER_ENV_PATH)
load_dotenv(WORKSPACE_ENV_PATH, override=True)

try:
    from mcp_research_server.tools.research_session import (
        patent_evidence_to_session as run_patent_evidence_to_session,
        publication_evidence_to_session as run_publication_evidence_to_session,
        research_session_checklist as run_research_session_checklist,
        research_session_understand_query as run_research_session_understand_query,
        research_session_start as run_research_session_start,
        research_session_user_answer as run_research_session_user_answer,
        web_evidence_to_session as run_web_evidence_to_session,
    )
except ImportError:
    from tools.research_session import (
        patent_evidence_to_session as run_patent_evidence_to_session,
        publication_evidence_to_session as run_publication_evidence_to_session,
        research_session_checklist as run_research_session_checklist,
        research_session_understand_query as run_research_session_understand_query,
        research_session_start as run_research_session_start,
        research_session_user_answer as run_research_session_user_answer,
        web_evidence_to_session as run_web_evidence_to_session,
    )

mcp = FastMCP(
    "Research Server",
    instructions="Use these MCP tools for source-grounded novelty and patentability research.",
)
LOGGER = logging.getLogger(__name__)


def _text(value: Any) -> str:
    """Convert an optional value from MCP input into text."""
    return "" if value is None else str(value)

def _safe_writer_ack(
    source_type: str,
    session_id: str,
    attempt_no: int,
    run_id: str,
    status: str,
    message: str,
) -> str:
    """Build a fallback response on error, so the tool always returns text to Flowise."""
    payload = {
        "schema_version": "research.v1",
        "ok": False,
        "status": status,
        "source_type": "research_session_write",
        "evidence_source_type": source_type,
        "source_type_written": source_type,
        "session_id": session_id,
        "run_id": run_id,
        "attempt_no": attempt_no,
        "retrieval_status": status,
        "evidence_status": status,
        "quality_grade": "failed_retrieval",
        "evidence_quality": "failed_retrieval",
        "usable_for_final": False,
        "needs_retry": True,
        "hit_count": 0,
        "inserted_hits": 0,
        "deduped_hits": 0,
        "top_hit_quality": 0.0,
        "exact_combination_candidate_found": False,
        "warning_count": 1,
        "error_count": 1,
        "compact_warnings": [message[:240]],
        "schema_valid": True,
        "was_duplicate_call": False,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

@mcp.tool()
async def research_session_start(original_query: str | None, session_id: str | None = "") -> str:
    """Vytvorí alebo aktualizuje SQLite research session a vráti jej identifikátor."""
    started = time.perf_counter()
    result = run_research_session_start(original_query=_text(original_query), session_id=_text(session_id))
    LOGGER.info("tool=research_session_start done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def research_session_understand_query(
    session_id: str | None,
    original_query: str | None = "",
    run_id: str | None = "",
    english_query: str | None = "",
) -> str:
    """Rozloží a uloží očistené údaje dotazu pre výskumnú reláciu."""
    started = time.perf_counter()
    result = run_research_session_understand_query(
        session_id=_text(session_id),
        original_query=_text(original_query),
        run_id=_text(run_id),
        english_query=_text(english_query),
    )
    LOGGER.info("tool=research_session_understand_query done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def patent_evidence_to_session(
    session_id: str | None = None,
    query: Any = None,
    max_results: int = 10,
    max_fetches: int = 6,
    fetch_timeout_ms: int = 18000,
    attempt_no: int = 0,
    run_id: str | None = "",
    source_type: str | None = "patent",
    english_query: str | None = "",
) -> str:
    """Získa patentové dôkazy a uloží kompaktný výsledok do SQLite session."""
    started = time.perf_counter()
    sess = _text(session_id)
    try:
        result = await run_patent_evidence_to_session(
            session_id=sess,
            query=query,
            max_results=max_results,
            max_fetches=max_fetches,
            fetch_timeout_ms=fetch_timeout_ms,
            attempt_no=attempt_no,
            run_id=_text(run_id),
            source_type=_text(source_type) or "patent",
            english_query=_text(english_query),
        )
    except Exception as exc:
        LOGGER.exception("tool=patent_evidence_to_session unhandled exception")
        result = _safe_writer_ack(
            "patent",
            sess,
            int(attempt_no or 0),
            _text(run_id),
            "provider_error",
            f"{exc.__class__.__name__}: {exc}",
        )
    LOGGER.info("tool=patent_evidence_to_session done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def publication_evidence_to_session(
    session_id: str | None = None,
    query: Any = None,
    max_results: int = 10,
    max_fetches: int = 4,
    fetch_timeout_s: float = 12.0,
    attempt_no: int = 0,
    run_id: str | None = "",
    source_type: str | None = "publication",
    english_query: str | None = "",
) -> str:
    """Získa publikačné dôkazy a uloží kompaktný výsledok do SQLite session."""
    started = time.perf_counter()
    sess = _text(session_id)
    try:
        result = await run_publication_evidence_to_session(
            session_id=sess,
            query=query,
            max_results=max_results,
            max_fetches=max_fetches,
            fetch_timeout_s=fetch_timeout_s,
            attempt_no=attempt_no,
            run_id=_text(run_id),
            source_type=_text(source_type) or "publication",
            english_query=_text(english_query),
        )
    except Exception as exc:
        LOGGER.exception("tool=publication_evidence_to_session unhandled exception")
        result = _safe_writer_ack(
            "publication",
            sess,
            int(attempt_no or 0),
            _text(run_id),
            "provider_error",
            f"{exc.__class__.__name__}: {exc}",
        )
    LOGGER.info("tool=publication_evidence_to_session done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def web_evidence_to_session(
    session_id: str | None = None,
    query: Any = None,
    max_results: int = 10,
    max_fetches: int = 5,
    timeout_ms: int = 14000,
    attempt_no: int = 0,
    run_id: str | None = "",
    source_type: str | None = "web",
    english_query: str | None = "",
) -> str:
    """Získa webové dôkazy a uloží kompaktný výsledok do SQLite session."""
    started = time.perf_counter()
    sess = _text(session_id)
    try:
        result = await run_web_evidence_to_session(
            session_id=sess,
            query=query,
            max_results=max_results,
            max_fetches=max_fetches,
            timeout_ms=timeout_ms,
            attempt_no=attempt_no,
            run_id=_text(run_id),
            source_type=_text(source_type) or "web",
            english_query=_text(english_query),
        )
    except Exception as exc:
        LOGGER.exception("tool=web_evidence_to_session unhandled exception")
        result = _safe_writer_ack(
            "web",
            sess,
            int(attempt_no or 0),
            _text(run_id),
            "provider_error",
            f"{exc.__class__.__name__}: {exc}",
        )
    LOGGER.info("tool=web_evidence_to_session done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def research_session_checklist(
    session_id: str | None,
    max_attempts_per_source: int = 2,
    min_total_hits: int = 1,
    run_id: str | None = "",
) -> str:
    """Skontroluje, či má research session dosť dôkazov na finalizáciu."""
    started = time.perf_counter()
    result = run_research_session_checklist(
        session_id=_text(session_id),
        max_attempts_per_source=max_attempts_per_source,
        min_total_hits=min_total_hits,
        run_id=_text(run_id),
    )
    LOGGER.info("tool=research_session_checklist done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

@mcp.tool()
async def research_session_user_answer(
    session_id: str | None,
    original_query: str | None = "",
    debug_mode: bool = False,
    force_regenerate: bool = False,
    run_id: str | None = "",
) -> str:
    """Vráti finálny používateľský report z dôkazov uložených v SQLite."""
    started = time.perf_counter()
    result = run_research_session_user_answer(
        session_id=_text(session_id),
        original_query=_text(original_query),
        debug_mode=debug_mode,
        force_regenerate=force_regenerate,
        run_id=_text(run_id),
    )
    LOGGER.info("tool=research_session_user_answer done elapsed_s=%.3f chars=%s", time.perf_counter() - started, len(result))
    return result

def main() -> None:
    """Start the MCP server over the stdio transport for direct local testing."""
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()