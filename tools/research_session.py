"""Tools for a research session persisted in a SQLite database."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decision_capture import CollectorContext, DecisionCollector, _warn_capture_failure
from .evidence_quality import grade_source, hit_quality_score
from .final_answer_pack import final_answer_pack
from .merge_evidence_pack import merge_evidence_pack
from .patent_evidence_pack import patent_evidence_pack
from .publication_evidence_pack import publication_evidence_pack
from .query_expansion import applicable_synonyms, synonym_query_variants
from .query_normalize import clean_tool_query
from .relevance import discriminative_tokens
from .user_answer import build_user_answer_payload
from .web_evidence_pack import web_evidence_pack

LOGGER = logging.getLogger(__name__)

SOURCE_TYPES = ("patent", "publication", "web")
SOURCE_TO_MERGED_KEY = {
    "patent": "patents",
    "publication": "publications",
    "web": "web",
}
SOURCE_TO_TOOL = {
    "patent": "patent_evidence_to_session",
    "publication": "publication_evidence_to_session",
    "web": "web_evidence_to_session",
}
DEFAULT_MAX_ATTEMPTS_PER_SOURCE = 2
MAX_ATTEMPTS_BY_SOURCE_DEFAULT: dict[str, int] = {
    "patent": 4,
    "publication": 3,
    "web": 3,
}


def _max_attempts_for_source(source_type: str, session_max: int) -> int:
    """Return the attempt limit for a specific source type."""
    per_source = MAX_ATTEMPTS_BY_SOURCE_DEFAULT.get(source_type, DEFAULT_MAX_ATTEMPTS_PER_SOURCE)
    return max(1, max(int(session_max or 0), per_source))


SCHEMA_VERSION = "research.v1"
TOOL_VERSION = "research_session.v1"
SESSION_CLOSED_STATUSES = {"finalizing", "completed"}
SOURCE_AGENT_BY_TYPE = {
    "patent": "patent_sqlite_writer_agent",
    "publication": "publication_sqlite_writer_agent",
    "web": "web_sqlite_writer_agent",
}


def _now() -> str:
    """Return the current UTC time in a single consistent ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    """Serialise a value into readable JSON text."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _compact_json(value: Any) -> str:
    """Serialise a value into compact JSON text."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _db_path() -> Path:
    """Return the path to the SQLite database for research sessions."""
    configured = os.getenv("RESEARCH_SESSION_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "research_sessions.sqlite3"


def _open_connection() -> sqlite3.Connection:
    """Open a SQLite connection and prepare the database schema."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection wrapped in a transaction, always closing it afterwards."""
    conn = _open_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create and migrate the tables needed to store a research session."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_sessions (
            session_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL DEFAULT 'research.v1',
            original_query TEXT NOT NULL DEFAULT '',
            query_envelope_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'created',
            max_attempts_per_source INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS evidence_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
            source_type TEXT NOT NULL CHECK(source_type IN ('patent', 'publication', 'web')),
            run_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '',
            normalized_query TEXT NOT NULL DEFAULT '',
            query_hash TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'failed',
            completed INTEGER NOT NULL DEFAULT 0,
            reliable_no_results INTEGER NOT NULL DEFAULT 0,
            hit_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT NOT NULL DEFAULT '',
            quality_grade TEXT NOT NULL DEFAULT 'missing',
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            tool_version TEXT NOT NULL DEFAULT 'research_session.v1',
            evidence_json TEXT NOT NULL DEFAULT '',
            normalized_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, source_type, attempt)
        );

        CREATE INDEX IF NOT EXISTS idx_evidence_session_source
        ON evidence_results(session_id, source_type, attempt DESC, id DESC);

        CREATE VIEW IF NOT EXISTS source_attempts AS
        SELECT
            id, session_id, run_id, source_type, attempt AS attempt_no, query,
            normalized_query, query_hash, status, started_at, completed_at,
            error_type, reliable_no_results, tool_version, created_at, updated_at
        FROM evidence_results;

        CREATE TABLE IF NOT EXISTS raw_evidence_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL CHECK(source_type IN ('patent', 'publication', 'web')),
            attempt INTEGER NOT NULL DEFAULT 1,
            canonical_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            verified_url INTEGER NOT NULL DEFAULT 0,
            evidence_level TEXT NOT NULL DEFAULT 'unverified',
            relevance_score REAL NOT NULL DEFAULT 0,
            evidence_quality_score REAL NOT NULL DEFAULT 0,
            provider TEXT NOT NULL DEFAULT '',
            attempt_log_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_raw_evidence_session_source
        ON raw_evidence_items(session_id, source_type, canonical_id);

        CREATE TABLE IF NOT EXISTS checklist_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL DEFAULT '',
            snapshot_no INTEGER NOT NULL DEFAULT 1,
            can_finalize INTEGER NOT NULL DEFAULT 0,
            retry_plan_json TEXT NOT NULL DEFAULT '[]',
            coverage_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS final_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE CASCADE,
            schema_version TEXT NOT NULL DEFAULT 'research.v1',
            input_hash TEXT NOT NULL DEFAULT '',
            answer_text TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT '',
            user_answer_text TEXT NOT NULL DEFAULT '',
            debug_report_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(session_id, input_hash)
        );

        CREATE TABLE IF NOT EXISTS trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "research_sessions", "schema_version", "TEXT NOT NULL DEFAULT 'research.v1'")
    _ensure_column(conn, "research_sessions", "query_envelope_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "research_sessions", "max_attempts_per_source", "INTEGER NOT NULL DEFAULT 2")
    _ensure_column(conn, "evidence_results", "run_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "normalized_query", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "query_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "error_type", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "quality_grade", "TEXT NOT NULL DEFAULT 'missing'")
    _ensure_column(conn, "evidence_results", "started_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "completed_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "evidence_results", "tool_version", "TEXT NOT NULL DEFAULT 'research_session.v1'")
    _ensure_column(conn, "raw_evidence_items", "evidence_quality_score", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "raw_evidence_items", "provider", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "raw_evidence_items", "attempt_log_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "final_answers", "verdict", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "final_answers", "confidence", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "final_answers", "user_answer_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "final_answers", "debug_report_text", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
        ("research_v1_quality_user_answer", _now()),
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS deduped_best_evidence_view AS
        SELECT *
        FROM raw_evidence_items rei
        WHERE rei.id = (
            SELECT rei2.id
            FROM raw_evidence_items rei2
            WHERE rei2.session_id = rei.session_id
              AND rei2.source_type = rei.source_type
              AND COALESCE(NULLIF(rei2.canonical_id, ''), rei2.url, rei2.title) =
                  COALESCE(NULLIF(rei.canonical_id, ''), rei.url, rei.title)
            ORDER BY rei2.verified_url DESC, rei2.relevance_score DESC, rei2.id ASC
            LIMIT 1
        )
        """
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a missing column to an existing table."""
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _clean_session_id(session_id: str | None) -> str:
    """Sanitise or generate a safe session identifier."""
    text = str(session_id or "").strip()
    if not text:
        return f"rs_{uuid.uuid4().hex[:16]}"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)[:120] or f"rs_{uuid.uuid4().hex[:16]}"


def _source_type(value: str) -> str:
    """Validate and normalise a source type."""
    source_type = str(value or "").strip().lower()
    if source_type not in SOURCE_TYPES:
        raise ValueError("source_type must be one of: patent, publication, web")
    return source_type


def normalize_query_for_hash(query: str) -> str:
    """Normalise a query into a stable form for hashing."""
    normalized = unicodedata.normalize("NFKC", str(query or ""))
    normalized = re.sub(r"\s+", " ", normalized.strip())
    return normalized.lower()


def query_hash(query: str) -> str:
    """Compute a short stable hash of the normalised query."""
    return hashlib.sha256(normalize_query_for_hash(query).encode("utf-8")).hexdigest()[:24]


def _input_hash(value: Any) -> str:
    """Compute a hash of the input data used to cache the final answer."""
    return hashlib.sha256(_compact_json(value).encode("utf-8")).hexdigest()


def _status_to_evidence_status(status: str, completed: bool, hit_count: int, reliable_no_results: bool) -> str:
    """Convert an evidence pack status into the status stored for that source."""
    if status == "partial_failure":
        return "partial_failure"
    if status == "ok" and hit_count > 0:
        return "ok_with_hits"
    if status == "ok" and completed and reliable_no_results:
        return "reliable_no_results"
    if status in {
        "provider_error",
        "timeout",
        "parse_error",
        "verification_failed",
        "rate_limited",
        "duplicate_noop",
        "invalid_source_type",
        "rejected_session_closed",
        "attempt_budget_exceeded",
    }:
        return status
    return status or "failed"


def _source_from_merged(merged_text: str, source_type: str) -> dict[str, Any]:
    """Extract the normalised section for one source from the merged output."""
    try:
        merged = json.loads(merged_text)
    except json.JSONDecodeError:
        return _missing_source(source_type, "merge_parse_failed", "merge_evidence_pack returned malformed JSON.")
    source = merged.get(SOURCE_TO_MERGED_KEY[source_type])
    if isinstance(source, dict):
        return source
    return _missing_source(source_type, "missing_normalized_source", "Could not normalize source evidence.")


def _normalize_source_payload(evidence_json: Any, source_type: str, query: str = "") -> dict[str, Any]:
    """Normalise one source's data through the shared merged format."""
    kwargs = {
        "patent_evidence_json": None,
        "publication_evidence_json": None,
        "web_evidence_json": None,
        "original_query": query,
    }
    if source_type == "patent":
        kwargs["patent_evidence_json"] = evidence_json
    elif source_type == "publication":
        kwargs["publication_evidence_json"] = evidence_json
    else:
        kwargs["web_evidence_json"] = evidence_json
    return _source_from_merged(merge_evidence_pack(**kwargs), source_type)


def _missing_source(source_type: str, error_type: str, message: str) -> dict[str, Any]:
    """Build a normalised record for a missing or unusable source."""
    return {
        "source_type": source_type,
        "status": "failed",
        "completed": False,
        "reliable_no_results": False,
        "hits": [],
        "errors": [{"type": error_type, "message": message}],
        "warnings": [message],
    }


def _as_bool_int(value: Any) -> int:
    """Convert a boolean into the database representation 0 or 1."""
    return 1 if value is True else 0


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely convert a value to an integer, falling back to a default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to a float, falling back to a default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    """Return the value as a list, or an empty list for any other type."""
    return value if isinstance(value, list) else []


def _raw_evidence_text(value: Any) -> str:
    """Convert raw evidence data into the text stored in the database."""
    if isinstance(value, str):
        return value
    return _compact_json(value)


def _safe_payload(value: Any) -> str:
    """Truncate JSON data written to the diagnostic log."""
    text = _compact_json(value)
    return text[:4000]


def _safe_json_loads(value: Any, default: Any = None) -> Any:
    """Safely parse JSON, returning a default on failure."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _compact_warnings(warnings: list[Any], limit: int = 3) -> list[str]:
    """Shorten a list of warnings into brief texts for logging."""
    return [re.sub(r"\s+", " ", str(item)).strip()[:240] for item in warnings[:limit] if item]


def _truncated_warnings(warnings: list[Any]) -> list[str]:
    """Truncate the text of each warning without limiting how many there are.

    Used in ACK responses instead of the raw warnings, which can contain
    fragments of a failed fetch (part of a scraped page, for example). The
    supervisor is prompted to see only short control fields, so long raw text
    must not reach it even incidentally.
    """
    return [re.sub(r"\s+", " ", str(item)).strip()[:240] for item in warnings if item]


def _record_trace(
    conn: sqlite3.Connection,
    session_id: str,
    event_type: str,
    status: str,
    *,
    run_id: str = "",
    agent_name: str = "",
    tool_name: str = "",
    source_type: str = "",
    attempt: int = 0,
    payload: Any = None,
) -> None:
    """Store a diagnostic event in the trace_events table."""
    conn.execute(
        """INSERT INTO trace_events(
            session_id, run_id, event_type, agent_name, tool_name, source_type,
            attempt, status, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            str(run_id or ""),
            event_type,
            agent_name,
            tool_name,
            source_type,
            int(attempt or 0),
            status,
            _safe_payload(payload or {}),
            _now(),
        ),
    )


def _ensure_session(conn: sqlite3.Connection, session_id: str, original_query: str = "") -> None:
    """Create a research session, or fill in the original query on an existing one."""
    timestamp = _now()
    conn.execute(
        """INSERT INTO research_sessions(
            session_id, schema_version, original_query, status,
            max_attempts_per_source, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, 'created', ?, ?, ?, '{}')
        ON CONFLICT(session_id) DO UPDATE SET
            original_query = CASE
                WHEN research_sessions.original_query = '' AND excluded.original_query != '' THEN excluded.original_query
                ELSE research_sessions.original_query
            END,
            updated_at = excluded.updated_at
        """,
        (session_id, SCHEMA_VERSION, original_query, DEFAULT_MAX_ATTEMPTS_PER_SOURCE, timestamp, timestamp),
    )


def _set_session_status(conn: sqlite3.Connection, session_id: str, status: str) -> None:
    """Update the session status without overwriting an already completed session."""
    current = _session_row(conn, session_id)
    if current and str(current["status"]) == "completed" and status != "completed":
        return
    conn.execute(
        "UPDATE research_sessions SET status=?, updated_at=? WHERE session_id=?",
        (status, _now(), session_id),
    )


def _next_attempt(conn: sqlite3.Connection, session_id: str, source_type: str) -> int:
    """Compute the next attempt number for a given source."""
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS max_attempt FROM evidence_results WHERE session_id=? AND source_type=?",
        (session_id, source_type),
    ).fetchone()
    return int(row["max_attempt"] or 0) + 1


def _latest_rows(conn: sqlite3.Connection, session_id: str) -> dict[str, sqlite3.Row]:
    """Load the most recent stored attempt for each source type."""
    rows: dict[str, sqlite3.Row] = {}
    for source_type in SOURCE_TYPES:
        row = conn.execute(
            """SELECT *
            FROM evidence_results
            WHERE session_id=? AND source_type=?
            ORDER BY attempt DESC, id DESC
            LIMIT 1
            """,
            (session_id, source_type),
        ).fetchone()
        if row:
            rows[source_type] = row
    return rows


_QUALITY_GRADE_RANK = {
    "strong": 5,
    "medium": 4,
    "reliable_no_results": 3,
    "weak": 2,
    "failed_retrieval": 1,
    "missing": 0,
}
_STATUS_RANK = {
    "ok": 2,
    "partial_failure": 1,
    "failed": 0,
}


def _row_quality_key(row: sqlite3.Row, source_type: str) -> tuple[int, int, float, int]:
    """Compute the comparison key ranking the quality of a stored attempt."""
    normalized = _safe_json_loads(row["normalized_json"], {}) or {}
    graded = grade_source(normalized if isinstance(normalized, dict) else None, source_type)
    grade = str(row["quality_grade"] or "").strip() or str(graded.get("quality_grade") or "missing")
    status = str(row["status"] or "failed")
    top_hit_quality = _safe_float(graded.get("top_hit_quality", 0.0) or 0.0)
    attempt = int(row["attempt"] or 0)
    return (
        _QUALITY_GRADE_RANK.get(grade, -1),
        _STATUS_RANK.get(status, -1),
        top_hit_quality,
        attempt,
    )


def _best_attempt_rows(conn: sqlite3.Connection, session_id: str) -> dict[str, sqlite3.Row]:
    """Select the highest-quality attempt for each source type."""
    rows: dict[str, sqlite3.Row] = {}
    for source_type in SOURCE_TYPES:
        candidates = conn.execute(
            "SELECT * FROM evidence_results WHERE session_id=? AND source_type=?",
            (session_id, source_type),
        ).fetchall()
        if not candidates:
            continue
        rows[source_type] = max(candidates, key=lambda r: _row_quality_key(r, source_type))
    return rows


_RETRY_SATURATION_RATIO = 0.8
_RETRY_SATURATION_MIN_INSERTED = 1


def _is_source_retry_saturated(latest_row: sqlite3.Row | None) -> bool:
    """Determine whether the last retry returned mostly duplicate hits."""
    if not latest_row:
        return False
    try:
        attempt = int(latest_row["attempt"] or 0)
    except (KeyError, IndexError, TypeError):
        return False
    if attempt <= 1:
        return False
    normalized = _safe_json_loads(latest_row["normalized_json"], {}) or {}
    stats = normalized.get("__dedupe_stats") if isinstance(normalized, dict) else None
    if not isinstance(stats, dict):
        return False
    inserted = int(stats.get("inserted_hits") or 0)
    deduped = int(stats.get("deduped_hits") or 0)
    if inserted < _RETRY_SATURATION_MIN_INSERTED:
        return False
    return (deduped / max(inserted, 1)) >= _RETRY_SATURATION_RATIO


def _retrieval_status_notes(
    latest: dict[str, sqlite3.Row],
    best: dict[str, sqlite3.Row],
) -> list[str]:
    """Build notes for when the best attempt is older than the most recent retry."""
    notes: list[str] = []
    for source_type in SOURCE_TYPES:
        latest_row = latest.get(source_type)
        best_row = best.get(source_type)
        if not latest_row or not best_row:
            continue
        if int(latest_row["attempt"]) == int(best_row["attempt"]):
            continue
        latest_status = str(latest_row["status"] or "").lower()
        if latest_status not in {"partial_failure", "failed"}:
            continue
        notes.append(
            f"The latest retry for {source_type} retrieval was {latest_status} "
            f"(attempt {int(latest_row['attempt'])}); reported quality is from "
            f"an earlier successful attempt {int(best_row['attempt'])}."
        )
    return notes


def _attempt_metadata(conn: sqlite3.Connection, session_id: str) -> dict[str, dict[str, int]]:
    """Build a brief overview of the latest state for each source type."""
    out: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        """
        SELECT
            source_type,
            MAX(attempt) AS latest_attempt,
            COALESCE(SUM(warning_count), 0) AS warning_total,
            COALESCE(SUM(error_count), 0) AS error_total
        FROM evidence_results
        WHERE session_id=?
        GROUP BY source_type
        """,
        (session_id,),
    ).fetchall()
    for row in rows:
        out[str(row["source_type"])] = {
            "latest_attempt": int(row["latest_attempt"] or 0),
            "warning_count": int(row["warning_total"] or 0),
            "error_count": int(row["error_total"] or 0),
        }
    return out


def _attempt_counts(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Return the number of stored attempts for each source type."""
    rows = conn.execute(
        """SELECT source_type, COUNT(*) AS attempt_count
        FROM evidence_results
        WHERE session_id=?
        GROUP BY source_type
        """,
        (session_id,),
    ).fetchall()
    return {str(row["source_type"]): int(row["attempt_count"] or 0) for row in rows}


def _budget_exceeded_sources(
    conn: sqlite3.Connection,
    session_id: str,
    per_source_max: dict[str, int] | None = None,
) -> set[str]:
    """Determine which sources have already exceeded their allowed attempt count."""
    rows = conn.execute(
        """
        SELECT source_type, MAX(attempt) AS rejected_attempt
        FROM trace_events
        WHERE session_id=? AND status='attempt_budget_exceeded'
          AND source_type != ''
        GROUP BY source_type
        """,
        (session_id,),
    ).fetchall()
    if per_source_max is None:
        return {str(row["source_type"]) for row in rows if row["source_type"]}
    exhausted: set[str] = set()
    for row in rows:
        st = str(row["source_type"])
        if not st:
            continue
        rejected_attempt = int(row["rejected_attempt"] or 0)
        budget = int(per_source_max.get(st) or DEFAULT_MAX_ATTEMPTS_PER_SOURCE)
        if rejected_attempt >= budget:
            exhausted.add(st)
    return exhausted


def _max_attempts(conn: sqlite3.Connection, session_id: str) -> int:
    """Load the general attempt limit stored with the session."""
    session = _session_row(conn, session_id)
    if not session:
        return DEFAULT_MAX_ATTEMPTS_PER_SOURCE
    return max(1, _safe_int(session["max_attempts_per_source"], DEFAULT_MAX_ATTEMPTS_PER_SOURCE))


def _max_attempts_for_session_source(conn: sqlite3.Connection, session_id: str, source_type: str) -> int:
    """Return the attempt limit for a given source within a specific session."""
    session_max = _max_attempts(conn, session_id)
    return _max_attempts_for_source(source_type, session_max)


def _row_summary(row: sqlite3.Row, include_evidence: bool = False) -> dict[str, Any]:
    """Build a brief JSON summary of an evidence result from a database row."""
    summary: dict[str, Any] = {
        "source_type": row["source_type"],
        "attempt": int(row["attempt"]),
        "run_id": row["run_id"],
        "agent_name": row["agent_name"],
        "query": row["query"],
        "normalized_query": row["normalized_query"],
        "query_hash": row["query_hash"],
        "status": row["status"],
        "completed": bool(row["completed"]),
        "reliable_no_results": bool(row["reliable_no_results"]),
        "hit_count": int(row["hit_count"]),
        "warning_count": int(row["warning_count"]),
        "error_count": int(row["error_count"]),
        "quality_grade": str(row["quality_grade"] or "missing") if "quality_grade" in row.keys() else "missing",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_evidence:
        try:
            summary["evidence"] = json.loads(row["normalized_json"])
        except json.JSONDecodeError:
            summary["evidence"] = _missing_source(
                str(row["source_type"]),
                "stored_json_malformed",
                "Stored normalized evidence JSON is malformed.",
            )
    return summary


def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """Load the database row for a specific research session."""
    return conn.execute("SELECT * FROM research_sessions WHERE session_id=?", (session_id,)).fetchone()


def _row_has_focused_hit(row: sqlite3.Row | None) -> bool:
    """Determine whether a stored result contains at least one focused hit."""
    if not row:
        return False
    try:
        parsed = json.loads(row["normalized_json"])
    except json.JSONDecodeError:
        return False
    for hit in _as_list(parsed.get("hits") if isinstance(parsed, dict) else None):
        if isinstance(hit, dict) and str(hit.get("relevance") or "").lower() == "focused":
            return True
    return False


def _ensure_decision_schema(conn: sqlite3.Connection) -> None:
    """Create evaluation storage only inside the fail-open capture transaction."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_candidate_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version TEXT NOT NULL DEFAULT 'candidate_decision.v1',
            query_fingerprint TEXT NOT NULL DEFAULT '',
            query_envelope_hash TEXT NOT NULL DEFAULT '',
            example_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            decision_stage TEXT NOT NULL DEFAULT '',
            decision_reason TEXT NOT NULL DEFAULT '',
            candidate_identity TEXT NOT NULL DEFAULT '',
            candidate_identity_kind TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            score_text TEXT NOT NULL DEFAULT '',
            decision_query TEXT NOT NULL DEFAULT '',
            query_variant TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            score_at_decision REAL,
            threshold_at_decision REAL,
            retained INTEGER,
            dataset_eligible INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_decisions_fingerprint "
        "ON evaluation_candidate_decisions(query_fingerprint, source_type, decision_stage)"
    )


def _persist_decision_events(collector: Any) -> None:
    """Write collected decision events to their own table. Fail-open by design.

    Capture is diagnostic. Any failure here is logged and swallowed: it must not
    change the returned evidence, the ACK status, the retry behaviour, the
    candidate ordering or any relevance decision.

    score_text gets its own TEXT column rather than a diagnostic JSON blob,
    because _safe_payload truncates those to 4000 characters and the dataset
    builder needs the exact text the scorer saw.
    """
    if collector is None:
        return
    try:
        events = list(getattr(collector, "events", []) or [])
        if not events:
            return
        now = _now()
        rows = [
            (
                e.get("schema_version", ""), e.get("query_fingerprint", ""),
                e.get("query_envelope_hash", ""), e.get("example_id", ""),
                e.get("session_id", ""), e.get("run_id", ""), e.get("source_type", ""),
                int(e.get("attempt", 0) or 0), e.get("decision_stage", ""),
                e.get("decision_reason", ""), e.get("candidate_identity", ""),
                e.get("candidate_identity_kind", ""), e.get("title", ""),
                e.get("score_text", ""), e.get("decision_query", ""), e.get("query_variant", ""), e.get("provider", ""),
                e.get("score_at_decision"), e.get("threshold_at_decision"),
                None if e.get("retained") is None else int(bool(e.get("retained"))),
                int(bool(e.get("dataset_eligible", True))),
                _compact_json(e.get("payload")) if e.get("payload") else "",
                now,
            )
            for e in events
        ]
        with _connect() as conn:
            # Explicit BEGIN includes DDL in the same rollback boundary as the
            # batch insert. Evaluation failures never poison production opens.
            conn.execute("BEGIN")
            _ensure_decision_schema(conn)
            conn.executemany(
                """
                INSERT INTO evaluation_candidate_decisions(
                    schema_version, query_fingerprint, query_envelope_hash, example_id,
                    session_id, run_id, source_type, attempt, decision_stage,
                    decision_reason, candidate_identity, candidate_identity_kind, title,
                    score_text, decision_query, query_variant, provider, score_at_decision,
                    threshold_at_decision, retained, dataset_eligible, payload_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
    except Exception as exc:  # noqa: BLE001 - capture must never affect retrieval
        _warn_capture_failure(LOGGER, "decision persistence failed; evidence is unaffected", exc)


def _new_decision_collector(
    session_id: str, run_id: str, source_type: str, attempt: int, original_query: str
) -> DecisionCollector | None:
    """Build a collector carrying the provenance only this layer owns.

    Returns None if capture setup fails for any reason. Setup runs before the
    retrieval-error boundary, so an exception escaping here would turn a healthy
    retrieval into a provider_error and trigger a needless retry. Capture is
    diagnostic: losing it must cost nothing but the diagnostics.
    """
    try:
        return _build_decision_collector(session_id, run_id, source_type, attempt, original_query)
    except Exception as exc:  # noqa: BLE001 - capture setup must never break retrieval
        _warn_capture_failure(LOGGER, "decision capture setup failed; continuing without capture", exc)
        return None


def _build_decision_collector(
    session_id: str, run_id: str, source_type: str, attempt: int, original_query: str
) -> DecisionCollector:
    """Read session provenance and construct the collector."""
    from .decision_capture import query_envelope_hash, query_fingerprint

    # Read the session's ORIGINAL query and its stored envelope. The fingerprint
    # must identify the user's query, not the cleaned or per-attempt search text,
    # so retries and variants stay one evaluation sample. Missing rows are a real
    # condition (the session may not exist yet), not an error to hide.
    envelope_hash = ""
    session_query = str(original_query or "")
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        row = _session_row(conn, clean_id)
    if row is not None:
        stored_query = str(row["original_query"] or "")
        if stored_query:
            session_query = stored_query
        envelope = _safe_json_loads(row["query_envelope_json"], {})
        atoms = envelope.get("critical_requirements_atomic") if isinstance(envelope, dict) else None
        envelope_hash = query_envelope_hash(atoms or [])
    return DecisionCollector(
        context=CollectorContext(
            session_id=clean_id,
            run_id=str(run_id or ""),
            source_type=str(source_type or ""),
            attempt=int(attempt or 0),
            query_fingerprint=query_fingerprint(session_query),
            query_envelope_hash=envelope_hash,
        )
    )


def research_session_start(original_query: str, session_id: str = "") -> str:
    """Create or load a research session and return its identifier."""
    cleaned_query = clean_tool_query(original_query)
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        _ensure_session(conn, clean_id, cleaned_query)
        _record_trace(
            conn,
            clean_id,
            "session_started",
            "ok",
            tool_name="research_session_start",
            payload={"query_hash": query_hash(cleaned_query)},
        )
        conn.commit()
    return _json(
        {
            "schema_version": SCHEMA_VERSION,
            "source_type": "research_session",
            "ok": True,
            "status": "ok",
            "session_id": clean_id,
            "original_query": cleaned_query,
            "query_hash": query_hash(cleaned_query),
            "expected_sources": list(SOURCE_TYPES),
            "db_path": str(_db_path()),
            "next_step": "Call research_session_understand_query, then call patent_evidence_to_session, publication_evidence_to_session, and web_evidence_to_session with this session_id.",
        }
    )


def _query_terms_for_envelope(query: str) -> list[str]:
    """Select the main meaningful terms used to build the query envelope."""
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "into", "using", "about",
        "does", "exist", "already", "concept", "idea", "what", "which", "there",
        "use", "uses", "whether",
    }
    out: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"(?u)\b[\w-]{3,}\b", (query or "").lower()):
        token = token.strip("_-")
        if not token or token in stop or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _detect_query_language(query: str) -> str:
    """Estimate whether the query contains non-English characters."""
    text = (query or "").lower()
    if re.search(r"[^\x00-\x7f]", text):
        return "non_english"
    return "en"


def _focused_variant(terms: list[str], start: int = 0, width: int = 10) -> str:
    """Build a shorter search query variant from the selected terms."""
    return " ".join(terms[start:start + width]).strip()


def _compact_query_text(text: str, max_tokens: int = 28) -> str:
    """Truncate query text to a limited number of tokens."""
    tokens = re.findall(r"[\w-]+", str(text or ""))
    return " ".join(tokens[:max_tokens]).strip()

_WEB_INTENT_TERMS = ("features", "manual", "support")

def _web_query_variants(
    base_variants: list[str],
    core_subject: str,
    atomic: list[dict[str, Any]],
) -> list[str]:
    """Build several web query variants to improve result coverage."""
    out: list[str] = list(base_variants[:3])
    core = re.sub(r"\s+", " ", str(core_subject or "")).strip()
    if not core:
        for atom in atomic:
            if isinstance(atom, dict) and atom.get("category") == "object_or_form_factor" and atom.get("label"):
                core = str(atom["label"]).strip()
                break
    if core:
        added = 0
        for atom in atomic:
            if added >= 3:
                break
            if not isinstance(atom, dict):
                continue
            if atom.get("category") not in {"function", "constraint", "mechanism_or_principle"}:
                continue
            terms = [str(t).strip() for t in (atom.get("terms") or []) if str(t).strip()]
            if not terms:
                continue
            variant = _compact_query_text(f"{core} {' '.join(terms[:4])}", 12)
            if variant:
                out.append(variant)
                added += 1
        out.append(_compact_query_text(f"{core} {_WEB_INTENT_TERMS[0]}", 10))
    return [variant for variant in dict.fromkeys(out) if variant][:7]

_LOW_PRIORITY_TAIL_CATEGORY = "object_or_form_factor"


def _query_variants_from_atoms(clean_query: str, key_terms: list[str], atomic: list[dict[str, Any]]) -> list[str]:
    """Build search variants from the query's atomic requirements."""
    labels = [
        re.sub(r"\s+", " ", str(atom.get("label") or "")).strip()
        for atom in atomic
        if isinstance(atom, dict) and str(atom.get("label") or "").strip()
    ]
    if not labels:
        compact = _focused_variant(key_terms, 0, 12) or clean_query
        alternate = _focused_variant(key_terms, 2, 10) or compact
        return [variant for variant in dict.fromkeys([clean_query, compact, alternate]) if variant]

    core = ""
    for atom in atomic:
        if atom.get("category") == "object_or_form_factor" and atom.get("label"):
            core = str(atom["label"]).strip()
            break
    if not core:
        core = labels[0]

    full_atom_variant = _compact_query_text(" ".join(labels), 32)
    # Order the remaining requirements so that more specific categories (function,
    # mechanism, constraint) come before more general ones (subject/form). If the
    # query has to be trimmed to the token limit, the least discriminating parts
    # are dropped first rather than whichever happened to come last.
    other_atoms = [
        atom
        for atom in atomic
        if isinstance(atom, dict) and str(atom.get("label") or "").strip() != core
    ]
    prioritized_other_atoms = sorted(
        other_atoms,
        key=lambda atom: 1 if atom.get("category") == _LOW_PRIORITY_TAIL_CATEGORY else 0,
    )
    tail_labels = [
        re.sub(r"\s+", " ", str(atom.get("label") or "")).strip() for atom in prioritized_other_atoms
    ]
    tail_variant = _compact_query_text(" ".join([core, *tail_labels]), 24)
    return [
        variant
        for variant in dict.fromkeys([clean_query, full_atom_variant, tail_variant])
        if variant
    ]

_NEGATION_PREFIXES = ("without", "no ", "not ", "non-", "free of", "exclude", "excluding")

_ACTION_LIKE_SUFFIXES = ("ing", "tion", "sion")


def _looks_action_like(token: str) -> bool:
    """Determine purely morphologically whether a token looks like an action or function word."""
    text = (token or "").lower()
    return len(text) >= 5 and any(text.endswith(suffix) for suffix in _ACTION_LIKE_SUFFIXES)

_PHRASE_FILLER_TOKENS = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "done",
    "exist", "exists", "existing", "exists?",
    "already", "concept", "system?", "method?", "use", "uses", "using",
    "of", "to", "in", "on", "at", "as", "or",
    "and", "for", "with", "by", "when",
    "whether", "if", "there",
    "check", "checks", "verify", "verifies", "determine", "determines",
})

_LOOSE_MODIFIER_SUFFIXES = ("able", "ible", "less", "free", "based")

_PREDICATE_MARKER = "\uf8ff"

_META_LEADING_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:check|verify|confirm|determine|investigate|find\s+out|tell\s+me)"
        r"[,\s]+(?:whether|if|that)\s+",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:does|do|is|are)\s+there\s+(?:already\s+)?(?:exist\w*\s+)?", re.IGNORECASE),
    re.compile(r"^(?:whether|if)\s+(?:there\s+)?(?:already\s+)?exist\w*\s+", re.IGNORECASE),
    re.compile(r"^(?:over(?:te)?|zisti(?:te)?|prever(?:te)?|posud(?:te)?|posúď(?:te)?)[,\s]+(?:či|ci|že|ze)\s+", re.IGNORECASE),
    re.compile(r"^(?:či|ci)?\s*existuj\w*\s+", re.IGNORECASE),
    re.compile(r"^(?:there\s+)?(?:already\s+)?exists?\s+(?=(?:a|an|any|the)\b)", re.IGNORECASE),
)
_META_TRAILING_PATTERNS = (
    re.compile(r"[\s,;:–-]*(?:does|do)\s+(?:this|it|that|these|those)\s+(?:already\s+)?exist\w*[\s?]*$", re.IGNORECASE),
    re.compile(r"[\s,;:–-]*already\s+exists?[\s?]*$", re.IGNORECASE),
    re.compile(r"[\s,;:–-]*(?:už\s+)?(?:také\s+niečo\s+)?existuje[\s?]*$", re.IGNORECASE),
)

def _strip_meta_verification_framing(text: str) -> str:
    """Strip generic verification phrasing from a query without touching its technical content."""
    original = re.sub(r"\s+", " ", str(text or "")).strip()
    stripped = original
    for _ in range(3): 
        before = stripped
        for pattern in _META_LEADING_PATTERNS:
            stripped = pattern.sub("", stripped, count=1).lstrip()
        for pattern in _META_TRAILING_PATTERNS:
            stripped = pattern.sub("", stripped, count=1).rstrip()
        if stripped == before:
            break
    stripped = stripped.strip().strip(",;:").strip()
    return stripped if stripped else original


def _phrase_informative_token_count(phrase: str) -> int:
    """Count the meaningful tokens in a phrase after filtering out filler."""
    tokens = re.findall(r"[\w-]+", (phrase or "").lower())
    return sum(
        1 for tok in tokens
        if tok not in _PHRASE_FILLER_TOKENS and (len(tok) >= 4 or "-" in tok)
    )


def _phrase_has_atomic_facet_token(phrase: str) -> bool:
    """Decide whether a short phrase is specific enough to be covered on its own."""
    for tok in re.findall(r"[\w-]+", phrase or ""):
        if tok.lower() in _PHRASE_FILLER_TOKENS:
            continue
        if "-" in tok:
            return True
        if any(ch.isdigit() for ch in tok):
            return True
        if tok.isupper() and 2 <= len(tok) <= 6:
            return True
    return False

def _first_content_token(phrase: str) -> str:
    """Return the phrase's first meaningful token in original word order."""
    for tok in re.findall(r"[\w-]+", (phrase or "").lower()):
        if tok not in _PHRASE_FILLER_TOKENS:
            return tok
    return ""

def _looks_clause_initial_verb(token: str) -> bool:
    """Recognise whether a clause's first word looks like a finite verb."""
    text = (token or "").lower()
    if len(text) < 4 or text in _PHRASE_FILLER_TOKENS or "-" in text:
        return False
    return text.endswith(("s", "ing", "ed"))

def _split_into_phrases_tagged(query: str) -> list[tuple[str, bool]]:
    """Split a query into phrases and identify the parts carrying their own verb."""
    text = re.sub(r"[?!.;]", ",", str(query or ""))
    text = re.sub(
        r",\s*(?:and|with|via|by|through|for|using|uses?|having|including|comprising)\b",
        ",",
        text,
        flags=re.IGNORECASE,
    )
    
    text = re.sub(
        r"\s+(?:that|which|where|who)\s+(?=\w{3,}(?:s|ing|ed)\b)",
        f", {_PREDICATE_MARKER}",
        text,
        flags=re.IGNORECASE,
    )

    splitter = re.compile(
        r"\s*,\s*"
        r"|\s+(and|with|via|by|through|for|using|uses?|having|including|comprising)\s+",
        flags=re.IGNORECASE,
    )
    parts = splitter.split(text)
    cleaned: list[tuple[str, str | None, str | None]] = []  
    i = 0
    prev_sep: str | None = None
    while i < len(parts):
        phrase_raw = parts[i]
        sep_after = parts[i + 1] if i + 1 < len(parts) else None
        clean = re.sub(r"\s+", " ", str(phrase_raw or "")).strip()
        if clean and len(clean.replace(_PREDICATE_MARKER, "").strip()) >= 3:
            cleaned.append((clean, prev_sep, sep_after))
        prev_sep = str(sep_after).lower() if sep_after else None
        i += 2

    tagged: list[tuple[str, bool, str | None, str | None]] = []
    seen_predicate = False
    for phrase, sep_before, sep_after in cleaned:
        predicate = phrase.startswith(_PREDICATE_MARKER)
        phrase = phrase.replace(_PREDICATE_MARKER, "").strip()
        first = re.findall(r"[\w-]+", phrase.lower())
        if not predicate and seen_predicate:
            predicate = bool(first) and _looks_clause_initial_verb(first[0])
        if (
            not predicate
            and sep_before == "and"
            and tagged
            and tagged[-1][1]
        ):
            prev_verb = _first_content_token(tagged[-1][0])
            if prev_verb:
                phrase = f"{prev_verb} {phrase}"
                predicate = True
        tagged.append((phrase, predicate, sep_before, sep_after))
        seen_predicate = seen_predicate or predicate

    if len(tagged) <= 1:
        return [(phrase, predicate) for phrase, predicate, _sb, _sa in tagged]

    def _joined(left: str, separator: str | None, right: str) -> str:
        """Join two phrases, preserving the meaningful conjunction between them."""
        middle = f" {separator} " if separator else " "
        return re.sub(r"\s+", " ", f"{left}{middle}{right}").strip()

    merged: list[tuple[str, bool]] = []
    pending_thin: tuple[str, bool, str | None] | None = None
    last_count = len(tagged)
    for idx, (phrase, predicate, sep_before, sep_after) in enumerate(tagged):
        is_thin = _phrase_informative_token_count(phrase) <= 1
        has_facet = _phrase_has_atomic_facet_token(phrase)
        if pending_thin is not None:
            phrase = _joined(pending_thin[0], pending_thin[2], phrase)
            predicate = predicate or pending_thin[1]
            pending_thin = None
            is_thin = False
        if is_thin and not has_facet:
            forward = bool(sep_after) and idx + 1 < last_count
            if forward:
                pending_thin = (phrase, predicate, sep_after)
                continue
            if merged:
                merged[-1] = (_joined(merged[-1][0], sep_before, phrase), merged[-1][1])
                continue
            pending_thin = (phrase, predicate, sep_after)
            continue
        merged.append((phrase, predicate))
    if pending_thin is not None:
        if merged:
            merged[-1] = (_joined(merged[-1][0], None, pending_thin[0]), merged[-1][1])
        else:
            merged.append((pending_thin[0], pending_thin[1]))
    return merged


def _split_into_phrases(query: str) -> list[str]:
    """Split a query into meaningful phrases without producing overly thin segments."""
    return [phrase for phrase, _predicate in _split_into_phrases_tagged(query)]


def _extract_query_facets(query: str, key_terms: list[str]) -> dict[str, Any]:
    """Break a query into the main parts needed for further processing."""
    lower = (query or "").lower()
    tokens = set(key_terms)

    control_terms: list[str] = []
    mechanism_terms = sorted(token for token in tokens if "-" in token)
    safety_terms: list[str] = []
    function_terms = sorted(token for token in tokens if _looks_action_like(token))

    negative: list[str] = []
    for prefix in _NEGATION_PREFIXES:
        for match in re.finditer(rf"\b{re.escape(prefix.strip())}\s+(\w+(?:\s+\w+){{0,3}})", lower):
            phrase = match.group(1).strip()
            if phrase and phrase not in negative:
                negative.append(phrase)

    atomic: list[dict[str, Any]] = _atomic_requirements_from_query(query, key_terms)

    critical_labels = [item["label"] for item in atomic if item.get("label")]
    if not critical_labels:
        phrases = _split_into_phrases(query)
        for phrase in phrases:
            phrase_lower = phrase.lower()
            if any(term in phrase_lower for term in key_terms[:8]):
                if phrase_lower not in {item.lower() for item in critical_labels}:
                    critical_labels.append(phrase)
        critical_labels = critical_labels[:5]

    return {
        "core_subject": (
            atomic[0]["label"]
            if atomic and atomic[0].get("category") == "object_or_form_factor"
            else (key_terms[0] if key_terms else "")
        ).strip(),
        "function": function_terms,
        "mechanism_or_principle": mechanism_terms,
        "activation_or_control_feature": control_terms,
        "safety_or_constraint_features": safety_terms,
        "critical_requirements": critical_labels,
        "critical_requirements_atomic": atomic,
        "negative_requirements": negative,
    }


def _is_loose_modifier(token: str) -> bool:
    """Determine whether a token acts as a loose modifier of the main concept."""
    t = token.lower()
    return any(t.endswith(suf) for suf in _LOOSE_MODIFIER_SUFFIXES)


def _phrase_meaningful_tokens(phrase: str) -> list[str]:
    """Select the meaningful tokens from a phrase and drop generic filler."""
    tokens = re.findall(r"[\w-]+", (phrase or "").lower())
    out: list[str] = []
    for tok in tokens:
        if tok in _PHRASE_FILLER_TOKENS:
            continue
        if len(tok) < 3 and "-" not in tok:
            continue
        out.append(tok)
    return out


def _is_generic_head_noun(token: str) -> bool:
    """Decide whether a token is too generic to serve as a head noun."""
    from .result_contract import GENERIC_MECHANISM_TOKENS
    return token in GENERIC_MECHANISM_TOKENS

_LABEL_DROP_TOKENS = frozenset({"a", "an", "the"})

def _phrase_display_label(phrase: str) -> str:
    """Build a readable label from a phrase, preserving important internal relations."""
    raw_tokens = re.findall(r"[\w-]+", phrase or "")
    start = 0
    end = len(raw_tokens)
    while start < end and raw_tokens[start].lower() in _PHRASE_FILLER_TOKENS:
        start += 1
    while end > start and raw_tokens[end - 1].lower() in _PHRASE_FILLER_TOKENS:
        end -= 1
    kept: list[str] = []
    for tok in raw_tokens[start:end]:
        if tok.lower() in _LABEL_DROP_TOKENS:
            continue
        if tok.isupper() and 2 <= len(tok) <= 6:
            kept.append(tok)
        else:
            kept.append(tok.lower())
    return " ".join(kept).strip()

def _token_display_map(phrase: str) -> dict[str, str]:
    """Build a map from tokens to their displayable forms using the input phrase."""
    mapping: dict[str, str] = {}
    for raw in re.findall(r"[\w-]+", phrase or ""):
        lowered = raw.lower()
        if raw.isupper() and 2 <= len(raw) <= 6:
            mapping.setdefault(lowered, raw)
        else:
            mapping.setdefault(lowered, lowered)
    return mapping

def _label_from_tokens(tokens: list[str], display_map: dict[str, str] | None = None) -> str:
    """Assemble a user-readable label from a list of tokens."""
    mapping = display_map or {}
    return " ".join(mapping.get(token, token) for token in tokens if token).strip()

def _is_action_head(token: str) -> bool:
    """Determine morphologically whether a token can head an action or function phrase."""
    return _looks_action_like(token)


def _normalize_atom_term(token: str) -> str:
    """Normalise an atomic requirement token without domain-specific form mappings."""
    return (token or "").lower().strip()

_TOO_BROAD_ATOM_TOKEN_LIMIT = 4
_SINGLE_TOKEN_ATOM_MIN_LEN = 6
_GENERIC_HEAD_NOUNS = frozenset({
    "product", "device", "system", "method", "apparatus", "tool",
    "thing", "item", "object", "machine", "unit", "component",
    "module", "feature", "function", "process", "service",
    "produkt", "zariadenie", "systém", "metóda", "nástroj",
    "vec", "predmet", "stroj", "modul", "funkcia",
})

def _is_informative_atom_token(token: str) -> bool:
    """Determine whether a token carries enough information to be its own requirement."""
    text = str(token or "").strip().lower()
    return bool(text) and (len(text) >= 4 or "-" in text)

def _append_atom(
    atomic: list[dict[str, Any]],
    seen_labels: set[str],
    *,
    category: str,
    label: str,
    terms: list[str],
) -> None:
    """Append an atomic requirement unless it is a duplicate or too generic."""
    clean_label = re.sub(r"\s+", " ", label).strip()
    if not clean_label:
        return
    key = clean_label.lower()
    if key in seen_labels:
        return
    clean_terms = []
    seen_terms: set[str] = set()
    for term in terms:
        clean = _normalize_atom_term(str(term))
        if clean and clean not in seen_terms:
            clean_terms.append(clean)
            seen_terms.add(clean)
    if len(clean_terms) == 1:
        only = clean_terms[0]
        if only in _GENERIC_HEAD_NOUNS:
            LOGGER.info("atom_rejected_uninformative: generic_noun=%s", only)
            return
        is_informative_standalone = "-" in only or len(only) >= _SINGLE_TOKEN_ATOM_MIN_LEN
        if not is_informative_standalone:
            LOGGER.info(
                "atom_rejected_uninformative category=%s label=%r terms=%s",
                category,
                clean_label,
                clean_terms,
            )
            return
    seen_labels.add(key)
    informative_count = sum(1 for term in clean_terms if _is_informative_atom_token(term))
    too_broad = informative_count > _TOO_BROAD_ATOM_TOKEN_LIMIT
    atomic.append(
        {
            "category": category,
            "terms": clean_terms,
            "label": clean_label,
            "too_broad_for_element_retry": too_broad,
        }
    )

def _purpose_clause_atoms(
    phrase: str,
    atomic: list[dict[str, Any]],
    seen_labels: set[str],
) -> bool:
    """Build atomic requirements from the phrases expressing purpose in the query."""
    match = re.search(
        r"^(?P<prefix>.+?)\s+to\s+(?P<verb>verify|verifies|verified|verifying|confirm|confirms|confirmed|validate|validates|validated|check|checks|checked)\s+(?:whether|if|that)?\s*(?P<clause>.+)$",
        phrase,
        flags=re.IGNORECASE,
    )
    if not match:
        return False
    prefix = match.group("prefix").strip()
    clause = match.group("clause").strip()
    for chunk in _chunk_split_phrase(prefix):
        chunk_tokens = re.findall(r"[\w-]+", chunk.lower())
        if not chunk_tokens:
            continue
        _append_atom(
            atomic,
            seen_labels,
            category=_classify_chunk(chunk_tokens),
            label=chunk,
            terms=chunk_tokens,
        )
    before_count = len(atomic)
    clause_terms = _phrase_meaningful_tokens(clause)[:4]
    if clause_terms:
        display_map = _token_display_map(phrase)
        clause_label = _label_from_tokens(clause_terms, display_map)
        _append_atom(
            atomic,
            seen_labels,
            category="constraint",
            label=clause_label,
            terms=clause_terms,
        )
    return len(atomic) > before_count

def _chunk_split_phrase(phrase: str, predicate: bool = False) -> list[str]:
    """Split a complex phrase into smaller requirement parts."""
    tokens = _phrase_meaningful_tokens(phrase)
    if not tokens:
        return []
    display_map = _token_display_map(phrase)

    starts_with_verb = bool(tokens) and (predicate or _looks_action_like(tokens[0]))
    if not starts_with_verb:
        while len(tokens) > 1 and _is_generic_head_noun(tokens[-1]):
            tokens.pop()

    compound_indices = [i for i, t in enumerate(tokens) if "-" in t]
    loose_indices = [
        i for i, t in enumerate(tokens)
        if i not in compound_indices and _is_loose_modifier(t)
    ]
    noun_indices = [
        i for i in range(len(tokens))
        if i not in compound_indices and i not in loose_indices
    ]

    total_modifiers = len(compound_indices) + len(loose_indices)
    if total_modifiers < 2 or len(noun_indices) < 2:
        return [_label_from_tokens(tokens, display_map)]

    used: set[int] = set()
    chunks: list[tuple[int, list[str]]] = []

    for ci in compound_indices:
        head = ci + 1
        while head < len(tokens) and head in used:
            head += 1
        if head < len(tokens) and head in noun_indices:
            chunks.append((ci, [tokens[ci], tokens[head]]))
            used.add(ci)
            used.add(head)
        else:
            chunks.append((ci, [tokens[ci]]))
            used.add(ci)

    remaining_nouns = [i for i in noun_indices if i not in used]
    for li in loose_indices:
        if li in used or not remaining_nouns:
            continue
        rightward = [n for n in remaining_nouns if n > li]
        ni = rightward[0] if rightward else remaining_nouns[0]
        chunks.append((li, [tokens[li], tokens[ni]]))
        used.add(li)
        used.add(ni)
        remaining_nouns = [n for n in remaining_nouns if n != ni]

    leftover = [i for i in range(len(tokens)) if i not in used]
    if leftover:
        chunks.append((leftover[0], [tokens[i] for i in leftover]))

    chunks.sort(key=lambda c: c[0])
    return [_label_from_tokens(c[1], display_map) for c in chunks if c[1]]

def _classify_chunk(tokens: list[str], predicate: bool = False) -> str:
    """Assign a neutral category to a query part based on its linguistic structure."""
    if not tokens:
        return "object_or_form_factor"

    if predicate:
        return "function"

    def _is_subject_noun(t: str) -> bool:
        """Determine whether a token can serve as the subject noun."""
        if _looks_action_like(t):
            return False
        if _is_loose_modifier(t):
            return False
        if "-" in t:  
            return False
        return True

    subject_nouns = [t for t in tokens if _is_subject_noun(t)]
    has_compound = any("-" in t for t in tokens)
    head = tokens[-1]

    if _is_action_head(head):
        return "function"

    if not subject_nouns:
        if has_compound:
            return "mechanism_or_principle"
        if any(_looks_action_like(t) for t in tokens):
            return "function"
        return "object_or_form_factor"

    if has_compound and len(subject_nouns) <= 1:
        return "mechanism_or_principle"

    return "object_or_form_factor"


def _atomic_requirements_from_query(query: str, key_terms: list[str]) -> list[dict[str, Any]]:
    """Build the list of atomic requirements from the input query."""
    tagged_phrases = _split_into_phrases_tagged(query)
    if not tagged_phrases:
        return []
    key_term_lookup = [term.lower() for term in key_terms]
    atomic: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for phrase, predicate in tagged_phrases:
        phrase_lower = phrase.lower()
        if not any(term in phrase_lower for term in key_term_lookup):
            continue
        if not predicate and _purpose_clause_atoms(phrase, atomic, seen_labels):
            continue

        chunks = _chunk_split_phrase(phrase, predicate=predicate)
        single_chunk = len(chunks) == 1
        for chunk in chunks:
            chunk_tokens = re.findall(r"[\w-]+", chunk.lower())
            if not chunk_tokens:
                continue
            category = _classify_chunk(chunk_tokens, predicate=predicate)
            label = _phrase_display_label(phrase) if single_chunk else chunk
            _append_atom(
                atomic,
                seen_labels,
                category=category,
                label=label or chunk,
                terms=chunk_tokens,
            )

    return _consolidate_atoms(atomic)

def _consolidate_atoms(atomic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop atomic requirements that add no meaning beyond more specific ones."""
    if len(atomic) <= 1:
        return atomic
    term_sets = [frozenset(str(t).lower() for t in (atom.get("terms") or [])) for atom in atomic]
    kept: list[dict[str, Any]] = []
    for index, atom in enumerate(atomic):
        terms = term_sets[index]
        redundant = bool(terms) and any(
            other_index != index
            and terms < term_sets[other_index]
            for other_index in range(len(atomic))
        )
        if not redundant:
            kept.append(atom)
    return kept or atomic


def _merge_display_labels(
    analysis_atoms: list[dict[str, Any]],
    display_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach labels from the original query to the analytic atoms where they align safely."""
    if not analysis_atoms:
        return []
    reliable = (
        bool(display_atoms)
        and len(display_atoms) == len(analysis_atoms)
        and all(str(atom.get("label") or "").strip() for atom in display_atoms)
    )
    if not reliable:
        return [dict(atom) for atom in analysis_atoms]
    merged: list[dict[str, Any]] = []
    for analysis_atom, display_atom in zip(analysis_atoms, display_atoms):
        item = dict(analysis_atom)
        item["label"] = str(display_atom.get("label") or "").strip()
        merged.append(item)
    return merged

def _build_query_envelope(original_query: str, english_query: str = "") -> dict[str, Any]:
    """Build a structured description of the query for the later research steps."""
    clean_query = clean_tool_query(original_query)
    clean_english = clean_tool_query(english_query)
    analysis_query = _strip_meta_verification_framing(clean_english or clean_query)
    key_terms = _query_terms_for_envelope(analysis_query)
    discriminators = sorted(discriminative_tokens(analysis_query))
    facets = _extract_query_facets(analysis_query, key_terms)
    display_query = _strip_meta_verification_framing(clean_query)
    display_facets = _extract_query_facets(display_query, _query_terms_for_envelope(display_query))
    variants = _query_variants_from_atoms(
        analysis_query,
        key_terms,
        facets.get("critical_requirements_atomic", []),
    )
    synonyms = applicable_synonyms(analysis_query)
    expansion_variants = synonym_query_variants(analysis_query, max_variants=2)
    source_variants = [
        variant
        for variant in dict.fromkeys([*variants[:3], *expansion_variants])
        if variant
    ][:5]
    analysis_atoms = facets.get("critical_requirements_atomic", []) or []
    display_atoms = display_facets.get("critical_requirements_atomic", []) or []
    atoms = _merge_display_labels(analysis_atoms, display_atoms) if clean_english else analysis_atoms
    discriminators_after_generic = [
        token for token in discriminators
        if token not in _GENERIC_HEAD_NOUNS
        and token not in _PHRASE_FILLER_TOKENS
    ]
    query_too_generic = bool(not atoms and not discriminators_after_generic)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_type": "query_envelope",
        "original_query": clean_query,
        "normalized_query": normalize_query_for_hash(clean_query),
        "language": _detect_query_language(clean_query),
        "english_query": clean_english,
        "understanding_query": analysis_query,
        "query_summary": clean_query,
        "invention_summary": clean_query,
        "key_terms": key_terms[:20],
        "discriminative_terms": discriminators[:20],
        "core_subject": facets["core_subject"],
        "function": facets["function"],
        "mechanism_or_principle": facets["mechanism_or_principle"],
        "activation_or_control_feature": facets["activation_or_control_feature"],
        "safety_or_constraint_features": facets["safety_or_constraint_features"],
        "critical_requirements": facets["critical_requirements"],
        "critical_requirements_atomic": atoms,
        "optional_requirements": [],
        "negative_requirements": facets["negative_requirements"],
        "synonyms": synonyms,
        "exact_combination_criteria": facets["critical_requirements"],
        "query_variants": {
            "patent": source_variants,
            "publication": source_variants,
            "web": [
                variant
                for variant in dict.fromkeys(
                    [
                        *_web_query_variants(variants, facets.get("core_subject") or "", atoms),
                        *expansion_variants,
                    ]
                )
                if variant
            ][:7],
        },
        "query_too_generic": query_too_generic,
        "notes": [
            "Deterministic heuristic query understanding; no external search/LLM was used.",
            "Facets and atomic requirements are derived from the query itself via general language-level heuristics; no domain-specific vocabularies are used.",
        ],
    }


def research_session_understand_query(
    session_id: str,
    original_query: str = "",
    run_id: str = "",
    english_query: str = "",
) -> str:
    """Store and decompose the user's query for the later research steps."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if not session:
            return _json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_type": "query_envelope",
                    "status": "failed",
                    "session_id": clean_id,
                    "errors": [{"type": "unknown_session", "message": "No research session exists for this session_id."}],
                }
            )
        query = clean_tool_query(original_query or str(session["original_query"] or ""))
        envelope = _build_query_envelope(query, english_query=english_query)
        conn.execute(
            "UPDATE research_sessions SET query_envelope_json=?, updated_at=? WHERE session_id=?",
            (_compact_json(envelope), _now(), clean_id),
        )
        _record_trace(
            conn,
            clean_id,
            "query_understood",
            "ok",
            run_id=run_id,
            tool_name="research_session_understand_query",
            payload={
                "language": envelope["language"],
                "key_term_count": len(envelope["key_terms"]),
                "critical_requirement_count": len(envelope.get("critical_requirements", [])),
                "variant_counts": {key: len(value) for key, value in envelope["query_variants"].items()},
            },
        )
        conn.commit()
    return _json(
        {
            "schema_version": SCHEMA_VERSION,
            "source_type": "query_envelope",
            "status": "ok",
            "session_id": clean_id,
            "run_id": run_id,
            "language": envelope["language"],
            "key_term_count": len(envelope["key_terms"]),
            "critical_requirement_count": len(envelope.get("critical_requirements", [])),
            "query_variant_counts": {key: len(value) for key, value in envelope["query_variants"].items()},
            "stored": True,
        }
    )

def _error_ack(
    status: str,
    session_id: str,
    source_type: str,
    attempt_no: int,
    q_hash: str,
    message: str,
    *,
    run_id: str = "",
    expected_source_type: str = "",
    received_source_type: str = "",
    english_query: str = "",
) -> str:
    """Build a uniform JSON response for a rejected or failed write."""
    diag = _english_query_diagnostics(source_type, english_query)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "source_type": "research_session_write",
        "evidence_source_type": source_type,
        "source_type_written": source_type,
        "session_id": session_id,
        "run_id": run_id,
        "attempt_no": attempt_no,
        "query_hash": q_hash,
        "retrieval_status": status,
        "quality_grade": "failed_retrieval",
        "evidence_quality": "failed_retrieval",
        "usable_for_final": False,
        "needs_retry": status not in {"attempt_budget_exceeded", "rejected_session_closed", "invalid_source_type"},
        "hit_count": 0,
        "inserted_hits": 0,
        "deduped_hits": 0,
        "top_hit_quality": 0.0,
        "exact_combination_candidate_found": False,
        "warning_count": 1,
        "error_count": 1,
        "compact_warnings": _compact_warnings([message]),
        "schema_valid": True,
        "was_duplicate_call": False,
        "warnings": _truncated_warnings([message]),
        **diag,
    }
    if expected_source_type:
        payload["expected_source_type"] = expected_source_type
    if received_source_type:
        payload["received_source_type"] = received_source_type
    return _json(payload)

def _english_query_diagnostics(source_type: str, english_query: str) -> dict[str, bool]:
    """Return flags indicating whether an English query was used for a source."""
    received = bool((english_query or "").strip())
    source = _source_type(source_type)
    return {
        "english_query_received": received,
        "english_query_used_for_search": received and source in {"patent", "publication", "web"},
        "english_query_used_for_scoring": received and source in {"patent", "publication", "web"},
    }

def _add_english_query_diagnostics(ack_text: str, source_type: str, english_query: str) -> str:
    """Add English-query diagnostics to an existing JSON ack text."""
    try:
        payload = json.loads(ack_text)
    except Exception:
        return ack_text
    if not isinstance(payload, dict):
        return ack_text
    payload.update(_english_query_diagnostics(source_type, english_query))
    return _json(payload)

def _begin_source_attempt(
    session_id: str,
    source_type: str,
    query: str,
    *,
    run_id: str = "",
    attempt_no: int = 0,
    agent_name: str = "",
    tool_name: str = "",
) -> dict[str, Any]:
    """Open a new attempt for a source, or explain why one cannot be created."""
    clean_id = _clean_session_id(session_id)
    clean_source = _source_type(source_type)
    clean_query = clean_tool_query(query)
    normalized_query = normalize_query_for_hash(clean_query)
    q_hash = query_hash(clean_query)
    timestamp = _now()
    with _connect() as conn:
        _ensure_session(conn, clean_id, clean_query)
        session = _session_row(conn, clean_id)
        session_status = str(session["status"] or "") if session else ""
        requested_attempt = _safe_int(attempt_no)
        store_attempt = requested_attempt if requested_attempt > 0 else _next_attempt(conn, clean_id, clean_source)
        max_attempts = _max_attempts_for_session_source(conn, clean_id, clean_source)
        existing = conn.execute(
            """SELECT *
            FROM evidence_results
            WHERE session_id=? AND source_type=? AND attempt=?
            """,
            (clean_id, clean_source, store_attempt),
        ).fetchone()

        if existing and str(existing["query_hash"] or "") == q_hash and str(existing["status"] or "") != "running":
            _record_trace(
                conn,
                clean_id,
                "source_attempt_duplicate",
                "duplicate_noop",
                run_id=run_id,
                agent_name=agent_name,
                tool_name=tool_name,
                source_type=clean_source,
                attempt=store_attempt,
                payload={"query_hash": q_hash},
            )
            conn.commit()
            return {
                "action": "duplicate",
                "session_id": clean_id,
                "source_type": clean_source,
                "attempt": store_attempt,
                "query": clean_query,
                "query_hash": q_hash,
                "run_id": run_id,
                "row": existing,
            }

        if session_status in SESSION_CLOSED_STATUSES:
            _record_trace(
                conn,
                clean_id,
                "source_attempt_rejected",
                "rejected_session_closed",
                run_id=run_id,
                agent_name=agent_name,
                tool_name=tool_name,
                source_type=clean_source,
                attempt=store_attempt,
                payload={"session_status": session_status},
            )
            conn.commit()
            return {
                "action": "error",
                "ack": _error_ack(
                    "rejected_session_closed",
                    clean_id,
                    clean_source,
                    store_attempt,
                    q_hash,
                    f"Session status is {session_status}; new writes are closed.",
                    run_id=run_id,
                ),
            }

        if store_attempt > max_attempts:
            _record_trace(
                conn,
                clean_id,
                "source_attempt_rejected",
                "attempt_budget_exceeded",
                run_id=run_id,
                agent_name=agent_name,
                tool_name=tool_name,
                source_type=clean_source,
                attempt=store_attempt,
                payload={"max_attempts_per_source": max_attempts},
            )
            conn.commit()
            return {
                "action": "error",
                "ack": _error_ack(
                    "attempt_budget_exceeded",
                    clean_id,
                    clean_source,
                    store_attempt,
                    q_hash,
                    "Attempt number exceeds max_attempts_per_source.",
                    run_id=run_id,
                ),
            }

        if existing and str(existing["query_hash"] or "") != q_hash:
            _record_trace(
                conn,
                clean_id,
                "source_attempt_rejected",
                "attempt_conflict",
                run_id=run_id,
                agent_name=agent_name,
                tool_name=tool_name,
                source_type=clean_source,
                attempt=store_attempt,
                payload={"existing_query_hash": existing["query_hash"], "received_query_hash": q_hash},
            )
            conn.commit()
            return {
                "action": "error",
                "ack": _error_ack(
                    "attempt_conflict",
                    clean_id,
                    clean_source,
                    store_attempt,
                    q_hash,
                    "Attempt number is already used with a different query_hash.",
                    run_id=run_id,
                ),
            }

        _set_session_status(conn, clean_id, "collecting")
        conn.execute(
            """
            INSERT INTO evidence_results(
                session_id, source_type, run_id, agent_name, query, normalized_query, query_hash,
                attempt, status, completed, reliable_no_results, hit_count, warning_count,
                error_count, error_type, started_at, completed_at, tool_version,
                evidence_json, normalized_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, 0, 0, 0, 0, '', ?, '', ?, '', '{}', ?, ?)
            ON CONFLICT(session_id, source_type, attempt) DO UPDATE SET
                run_id=excluded.run_id,
                agent_name=excluded.agent_name,
                query=excluded.query,
                normalized_query=excluded.normalized_query,
                query_hash=excluded.query_hash,
                status='running',
                started_at=excluded.started_at,
                updated_at=excluded.updated_at
            """,
            (
                clean_id,
                clean_source,
                str(run_id or ""),
                str(agent_name or ""),
                clean_query,
                normalized_query,
                q_hash,
                store_attempt,
                timestamp,
                TOOL_VERSION,
                timestamp,
                timestamp,
            ),
        )
        _record_trace(
            conn,
            clean_id,
            "source_attempt_started",
            "running",
            run_id=run_id,
            agent_name=agent_name,
            tool_name=tool_name,
            source_type=clean_source,
            attempt=store_attempt,
            payload={"query_hash": q_hash},
        )
        conn.commit()
        return {
            "action": "started",
            "session_id": clean_id,
            "source_type": clean_source,
            "attempt": store_attempt,
            "query": clean_query,
            "query_hash": q_hash,
            "run_id": run_id,
        }


def _duplicate_ack(begin: dict[str, Any]) -> str:
    """Build the response for a repeated call whose result was already stored."""
    row = begin["row"]
    quality_grade = str(row["quality_grade"] or "missing") if "quality_grade" in row.keys() else "missing"
    return _json(
        {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "duplicate_noop",
            "source_type": "research_session_write",
            "session_id": begin["session_id"],
            "run_id": begin.get("run_id", ""),
            "evidence_source_type": begin["source_type"],
            "source_type_written": begin["source_type"],
            "attempt_no": begin["attempt"],
            "query_hash": begin["query_hash"],
            "retrieval_status": str(row["status"] or "failed"),
            "evidence_status": _status_to_evidence_status(
                str(row["status"] or "failed"),
                bool(row["completed"]),
                int(row["hit_count"] or 0),
                bool(row["reliable_no_results"]),
            ),
            "quality_grade": quality_grade,
            "evidence_quality": quality_grade,
            "usable_for_final": quality_grade in {"strong", "medium", "reliable_no_results"},
            "needs_retry": False,
            "hit_count": int(row["hit_count"] or 0),
            "inserted_hits": 0,
            "deduped_hits": 0,
            "top_hit_quality": 0.0,
            "exact_combination_candidate_found": False,
            "warning_count": int(row["warning_count"] or 0),
            "error_count": int(row["error_count"] or 0),
            "compact_warnings": [],
            "schema_valid": True,
            "was_duplicate_call": True,
            "warnings": [],
        }
    )

def _canonical_id(source_type: str, hit: dict[str, Any]) -> str:
    """Build a stable identifier by which a hit can be deduplicated."""
    candidates = []
    if source_type == "patent":
        candidates = ["patent_number", "publication_number", "family_id", "id", "url"]
    elif source_type == "publication":
        candidates = ["doi", "arxiv_id", "semantic_scholar_id", "paper_id", "url", "title"]
    else:
        candidates = ["canonical_url", "url", "title"]
    for key in candidates:
        value = str(hit.get(key) or "").strip()
        if value:
            return value.lower()
    return hashlib.sha256(_compact_json(hit).encode("utf-8")).hexdigest()[:24]


def _insert_raw_items(
    conn: sqlite3.Connection,
    session_id: str,
    run_id: str,
    source_type: str,
    attempt: int,
    hits: list[Any],
) -> tuple[int, int]:
    """Store individual hits in raw_evidence_items and count the duplicates."""
    inserted = 0
    duplicate = 0
    timestamp = _now()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        canonical = _canonical_id(source_type, hit)
        same_attempt_existing = conn.execute(
            """SELECT id
            FROM raw_evidence_items
            WHERE session_id=? AND source_type=? AND attempt=? AND canonical_id=?
            LIMIT 1
            """,
            (session_id, source_type, attempt, canonical),
        ).fetchone()
        prior_existing = conn.execute(
            """
            SELECT id
            FROM raw_evidence_items
            WHERE session_id=? AND source_type=? AND canonical_id=?
            LIMIT 1
            """,
            (session_id, source_type, canonical),
        ).fetchone()
        if prior_existing:
            duplicate += 1
        if same_attempt_existing:
            continue
        raw = _compact_json(hit)
        attempt_log = hit.get("attempt_log_json")
        if not attempt_log:
            attempt_log = hit.get("attempt_log")
        attempt_log_json = _compact_json(attempt_log if isinstance(attempt_log, list) else [])
        conn.execute(
            """
            INSERT INTO raw_evidence_items(
                session_id, run_id, source_type, attempt, canonical_id, title, url,
                verified_url, evidence_level, relevance_score, evidence_quality_score,
                provider, attempt_log_json, summary, raw_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                source_type,
                attempt,
                canonical,
                str(hit.get("title") or "")[:500],
                str(hit.get("url") or hit.get("final_url") or "")[:1000],
                _as_bool_int(hit.get("verified_url")),
                str(hit.get("evidence_level") or "unverified")[:80],
                _safe_float(hit.get("relevance_score") or hit.get("score") or 0),
                hit_quality_score(hit, source_type),
                str(hit.get("provider") or "")[:120],
                attempt_log_json[:4000],
                str(hit.get("summary") or hit.get("snippet") or "")[:4000],
                raw[:12000],
                timestamp,
            ),
        )
        inserted += 1
    return inserted, duplicate

def research_session_save_evidence(
    session_id: str,
    source_type: str,
    evidence_json: Any,
    query: str = "",
    agent_name: str = "",
    attempt: int = 0,
    run_id: str = "",
    english_query: str = "",
) -> str:
    """Store one source's result in the research database."""
    clean_id = _clean_session_id(session_id)
    clean_source = _source_type(source_type)
    clean_query = clean_tool_query(query)
    begin = _begin_source_attempt(
        clean_id,
        clean_source,
        clean_query,
        run_id=run_id,
        attempt_no=attempt,
        agent_name=agent_name,
        tool_name=SOURCE_TO_TOOL.get(clean_source, "research_session_save_evidence"),
    )
    if begin["action"] == "duplicate":
        return _add_english_query_diagnostics(_duplicate_ack(begin), clean_source, english_query)
    if begin["action"] == "error":
        return _add_english_query_diagnostics(str(begin["ack"]), clean_source, english_query)
    store_attempt = int(begin["attempt"])
    q_hash = str(begin["query_hash"])
    normalized = _normalize_source_payload(evidence_json, clean_source, clean_query)
    hits = _as_list(normalized.get("hits"))
    errors = _as_list(normalized.get("errors"))
    warnings = _as_list(normalized.get("warnings"))
    quality = grade_source(normalized, clean_source)
    timestamp = _now()
    with _connect() as conn:
        _ensure_session(conn, clean_id, clean_query)
        english_query_clean = clean_tool_query(english_query)
        if english_query_clean:
            session_row = _session_row(conn, clean_id)
            stored_envelope = (
                _safe_json_loads(session_row["query_envelope_json"], {})
                if session_row is not None
                else {}
            )
            if not isinstance(stored_envelope, dict) or stored_envelope.get("english_query") != english_query_clean:
                original_for_envelope = str(session_row["original_query"] or clean_query) if session_row is not None else clean_query
                refreshed_envelope = _build_query_envelope(
                    original_for_envelope,
                    english_query=english_query_clean,
                )
                conn.execute(
                    "UPDATE research_sessions SET query_envelope_json=?, updated_at=? WHERE session_id=?",
                    (_compact_json(refreshed_envelope), timestamp, clean_id),
                )
        inserted_hits, deduped_hits = _insert_raw_items(
            conn,
            clean_id,
            str(run_id or ""),
            clean_source,
            store_attempt,
            hits,
        )
        if isinstance(normalized, dict):
            normalized["__dedupe_stats"] = {
                "input_hit_count": len(hits),
                "inserted_hits": int(inserted_hits or 0),
                "deduped_hits": int(deduped_hits or 0),
            }
            if english_query_clean:
                normalized["__english_query"] = english_query_clean
        conn.execute(
            """
            UPDATE evidence_results
            SET agent_name=?,
                status=?,
                completed=?,
                reliable_no_results=?,
                hit_count=?,
                warning_count=?,
                error_count=?,
                error_type=?,
                quality_grade=?,
                completed_at=?,
                evidence_json=?,
                normalized_json=?,
                updated_at=?
            WHERE session_id=? AND source_type=? AND attempt=?
            """,
            (
                str(agent_name or ""),
                str(normalized.get("status") or "failed"),
                _as_bool_int(normalized.get("completed")),
                _as_bool_int(normalized.get("reliable_no_results")),
                len(hits),
                len(warnings),
                len(errors),
                str(errors[0].get("type") if errors and isinstance(errors[0], dict) else ""),
                str(quality["quality_grade"]),
                timestamp,
                _raw_evidence_text(evidence_json),
                _compact_json(normalized),
                timestamp,
                clean_id,
                clean_source,
                store_attempt,
            ),
        )
        conn.execute("UPDATE research_sessions SET updated_at=? WHERE session_id=?", (timestamp, clean_id))
        _record_trace(
            conn,
            clean_id,
            "source_attempt_completed",
            str(normalized.get("status") or "failed"),
            run_id=run_id,
            agent_name=agent_name,
            tool_name=SOURCE_TO_TOOL.get(clean_source, "research_session_save_evidence"),
            source_type=clean_source,
            attempt=store_attempt,
            payload={
                "query_hash": q_hash,
                "hit_count": len(hits),
                "warning_count": len(warnings),
                "error_count": len(errors),
                "quality_grade": quality["quality_grade"],
            },
        )
        conn.commit()

    return _json(
        {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source_type": "research_session_write",
            "status": "written",
            "stored": True,
            "session_id": clean_id,
            "run_id": run_id,
            "evidence_source_type": clean_source,
            "source_type_written": clean_source,
            "attempt": store_attempt,
            "attempt_no": store_attempt,
            "retrieval_status": str(normalized.get("status") or "failed"),
            "query_hash": q_hash,
            "evidence_status": _status_to_evidence_status(
                str(normalized.get("status") or "failed"),
                normalized.get("completed") is True,
                len(hits),
                normalized.get("reliable_no_results") is True,
            ),
            "quality_grade": quality["quality_grade"],
            "evidence_quality": quality["quality_grade"],
            "usable_for_final": quality["usable_for_final"],
            "needs_retry": quality["needs_retry"],
            "top_hit_quality": quality.get("top_hit_quality", 0.0),
            "exact_combination_candidate_found": quality.get("exact_combination_candidate_found", False),
            "completed": normalized.get("completed") is True,
            "reliable_no_results": normalized.get("reliable_no_results") is True,
            "hit_count": len(hits),
            "inserted_hits": inserted_hits,
            "deduped_hits": deduped_hits,
            "was_duplicate_call": False,
            "warning_count": len(warnings),
            "error_count": len(errors),
            "compact_warnings": _compact_warnings(warnings),
            "schema_valid": True,
            "warnings": _truncated_warnings(warnings),
            **_english_query_diagnostics(clean_source, english_query),
        }
    )

def research_session_record_failure(
    session_id: str,
    source_type: str,
    query: str,
    status: str = "provider_error",
    error_type: str = "tool_error",
    message: str = "",
    attempt_no: int = 0,
    run_id: str = "",
    agent_name: str = "",
    english_query: str = "",
) -> str:
    """Store a source's failed attempt in the session as a structured error."""
    clean_id = _clean_session_id(session_id)
    clean_source = _source_type(source_type)
    clean_query = clean_tool_query(query)
    begin = _begin_source_attempt(
        clean_id,
        clean_source,
        clean_query,
        run_id=run_id,
        attempt_no=attempt_no,
        agent_name=agent_name,
        tool_name=SOURCE_TO_TOOL.get(clean_source, "research_session_record_failure"),
    )
    if begin["action"] == "duplicate":
        return _add_english_query_diagnostics(_duplicate_ack(begin), clean_source, english_query)
    if begin["action"] == "error":
        return _add_english_query_diagnostics(str(begin["ack"]), clean_source, english_query)
    timestamp = _now()
    normalized = _missing_source(clean_source, error_type, message or status)
    normalized["status"] = status
    english_query_clean = clean_tool_query(english_query)
    if english_query_clean:
        normalized["__english_query"] = english_query_clean
    with _connect() as conn:
        if english_query_clean:
            session_row = _session_row(conn, clean_id)
            stored_envelope = (
                _safe_json_loads(session_row["query_envelope_json"], {})
                if session_row is not None
                else {}
            )
            if not isinstance(stored_envelope, dict) or stored_envelope.get("english_query") != english_query_clean:
                original_for_envelope = str(session_row["original_query"] or clean_query) if session_row is not None else clean_query
                refreshed_envelope = _build_query_envelope(
                    original_for_envelope,
                    english_query=english_query_clean,
                )
                conn.execute(
                    "UPDATE research_sessions SET query_envelope_json=?, updated_at=? WHERE session_id=?",
                    (_compact_json(refreshed_envelope), timestamp, clean_id),
                )
        conn.execute(
            """UPDATE evidence_results
            SET status=?, error_type=?, error_count=1, warning_count=1, quality_grade='failed_retrieval', completed_at=?,
                evidence_json=?, normalized_json=?, updated_at=?
            WHERE session_id=? AND source_type=? AND attempt=?
            """,
            (
                status,
                error_type,
                timestamp,
                _compact_json(normalized),
                _compact_json(normalized),
                timestamp,
                clean_id,
                clean_source,
                int(begin["attempt"]),
            ),
        )
        _record_trace(
            conn,
            clean_id,
            "source_attempt_failed",
            status,
            run_id=run_id,
            agent_name=agent_name,
            tool_name=SOURCE_TO_TOOL.get(clean_source, "research_session_record_failure"),
            source_type=clean_source,
            attempt=int(begin["attempt"]),
            payload={"error_type": error_type, "message": message[:500]},
        )
        conn.commit()
    return _error_ack(
        status,
        clean_id,
        clean_source,
        int(begin["attempt"]),
        str(begin["query_hash"]),
        message or status,
        run_id=run_id,
        english_query=english_query,
    )

def research_session_get(session_id: str, include_evidence: bool = False, include_history: bool = False) -> str:
    """Load the state of a research session from the database."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if not session:
            return _json(
                {
                    "source_type": "research_session_snapshot",
                    "status": "failed",
                    "session_id": clean_id,
                    "errors": [{"type": "unknown_session", "message": "No research session exists for this session_id."}],
                }
            )
        latest = _latest_rows(conn, clean_id)
        best = _best_attempt_rows(conn, clean_id)
        attempts = _attempt_counts(conn, clean_id)
        payload: dict[str, Any] = {
            "source_type": "research_session_snapshot",
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "session_id": clean_id,
            "original_query": session["original_query"],
            "query_envelope_available": bool(_safe_json_loads(session["query_envelope_json"], {})),
            "session_status": session["status"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "attempt_counts": {source: attempts.get(source, 0) for source in SOURCE_TYPES},
            "latest": {
                source: _row_summary(row, include_evidence=include_evidence)
                for source, row in latest.items()
            },
            "best": {
                source: _row_summary(row, include_evidence=include_evidence)
                for source, row in best.items()
            },
            "best_attempt_per_source": {
                source: int(row["attempt"]) for source, row in best.items()
            },
            "missing_sources": [source for source in SOURCE_TYPES if source not in latest],
        }
        if include_history:
            history_rows = conn.execute(
                """
                SELECT *
                FROM evidence_results
                WHERE session_id=?
                ORDER BY source_type ASC, attempt ASC, id ASC
                """,
                (clean_id,),
            ).fetchall()
            payload["history"] = [_row_summary(row, include_evidence=include_evidence) for row in history_rows]
        return _json(payload)

def _atoms_missing_full_coverage(
    latest_rows: dict[str, sqlite3.Row],
    atomic_requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find the atomic requirements that no stored hit covers."""
    if not isinstance(atomic_requirements, list) or not atomic_requirements:
        return []
    all_blobs: list[str] = []
    for row in (latest_rows or {}).values():
        evidence = _safe_json_loads(row["evidence_json"], {}) or {}
        for hit in (evidence.get("hits") or []):
            if not isinstance(hit, dict):
                continue
            blob = " ".join(
                str(hit.get(field) or "")
                for field in ("title", "summary", "snippet")
            ).lower()
            if blob:
                all_blobs.append(blob)
    if not all_blobs:
        return [
            atom for atom in atomic_requirements
            if not atom.get("too_broad_for_element_retry")
        ]
    uncovered: list[dict[str, Any]] = []
    for atom in atomic_requirements:
        if atom.get("too_broad_for_element_retry"):
            continue
        terms = [str(t).lower() for t in (atom.get("terms") or []) if str(t).strip()]
        if not terms:
            continue
        if not any(all(term in blob for term in terms) for blob in all_blobs):
            uncovered.append(atom)
    return uncovered

def _planned_query(original_query: str, source_type: str, attempt_count: int, query_envelope: dict[str, Any] | None = None) -> str:
    """Determine which query variant to use for the current attempt."""
    base = clean_tool_query(original_query)
    variants = []
    if isinstance(query_envelope, dict):
        source_variants = (query_envelope.get("query_variants") or {}).get(source_type)
        if isinstance(source_variants, list):
            variants = [clean_tool_query(str(item or "")) for item in source_variants if str(item or "").strip()]
    variants = [variant for variant in dict.fromkeys([base, *variants]) if variant]
    if not variants:
        return base
    index = min(max(attempt_count, 0), len(variants) - 1)
    return variants[index]

def _writer_query(session_id: str, query: str, source_type: str, attempt_no: int) -> str:
    """Determine the query the writer should record with the result."""
    clean_query = clean_tool_query(query)
    if clean_query:
        return clean_query
    with _connect() as conn:
        session = _session_row(conn, _clean_session_id(session_id))
        if not session:
            return clean_query
        original_query = str(session["original_query"] or "")
        query_envelope = _safe_json_loads(session["query_envelope_json"], {}) or {}
    return _planned_query(
        original_query,
        source_type,
        max(_safe_int(attempt_no, 1) - 1, 0),
        query_envelope if isinstance(query_envelope, dict) else None,
    )

def _source_check(
    source_type: str,
    row: sqlite3.Row | None,
    attempt_count: int,
    original_query: str,
    max_attempts_per_source: int,
    query_envelope: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate one source and prepare a retry action if needed."""
    actions: list[dict[str, Any]] = []
    if not row:
        budget_exhausted = attempt_count >= max_attempts_per_source
        check = {
            "source_type": source_type,
            "state": "retry_budget_exhausted" if budget_exhausted else "missing",
            "quality_grade": "missing",
            "attempt_count": attempt_count,
            "ready": budget_exhausted,
            "blocking": not budget_exhausted,
            "usable_for_final": False,
            "needs_retry": not budget_exhausted,
            "top_hit_quality": 0.0,
            "exact_combination_candidate_found": False,
            "reason": (
                "No evidence has been stored for this source type and the retry budget is exhausted."
                if budget_exhausted
                else "No evidence has been stored for this source type."
            ),
        }
    else:
        status = str(row["status"] or "failed")
        completed = bool(row["completed"])
        reliable_no_results = bool(row["reliable_no_results"])
        hit_count = int(row["hit_count"] or 0)
        normalized = _safe_json_loads(row["normalized_json"], {}) or {}
        quality = grade_source(normalized if isinstance(normalized, dict) else None, source_type)
        quality_grade = str(quality.get("quality_grade") or "missing")
        if quality_grade == "strong":
            state = "strong"
            ready = True
            blocking = False
            reason = "Source has verified strong evidence."
        elif quality_grade == "medium":
            if status == "partial_failure" and attempt_count < max_attempts_per_source:
                state = "needs_retry"
                ready = False
                blocking = True
                reason = "Source has medium evidence but retrieval was partial; retry while budget remains."
            else:
                state = "medium"
                ready = True
                blocking = False
                reason = "Source has moderate usable evidence."
        elif quality_grade == "reliable_no_results":
            state = "reliable_no_results"
            ready = True
            blocking = False
            reason = "Source completed and reported reliable no-results."
        else:
            state = "needs_retry" if attempt_count < max_attempts_per_source else "retry_budget_exhausted"
            ready = attempt_count >= max_attempts_per_source
            blocking = attempt_count < max_attempts_per_source
            reason = (
                "Source has only weak, failed, or incomplete evidence; retry if budget remains."
                if blocking
                else "Source has only weak, failed, or incomplete evidence and retry budget is exhausted."
            )
        check = {
            "source_type": source_type,
            "state": state,
            "quality_grade": quality_grade,
            "attempt_count": attempt_count,
            "latest_attempt": int(row["attempt"]),
            "status": status,
            "completed": completed,
            "reliable_no_results": reliable_no_results,
            "hit_count": hit_count,
            "warning_count": int(row["warning_count"] or 0),
            "error_count": int(row["error_count"] or 0),
            "ready": ready,
            "blocking": blocking,
            "usable_for_final": quality.get("usable_for_final") is True,
            "needs_retry": blocking,
            "top_hit_quality": quality.get("top_hit_quality", 0.0),
            "top_evidence_level": quality.get("top_evidence_level", ""),
            "exact_combination_candidate_found": quality.get("exact_combination_candidate_found", False),
            "reason": reason,
        }

    if check["blocking"]:
        actions.append(
            {
                "source_agent": SOURCE_AGENT_BY_TYPE[source_type],
                "direct_mcp_tool": SOURCE_TO_TOOL[source_type],
                "source_type": source_type,
                "tool": SOURCE_TO_TOOL[source_type],
                "session_id": "",
                "query": _planned_query(original_query, source_type, attempt_count, query_envelope),
                "attempt_no": attempt_count + 1,
                "reason": check["reason"],
            }
        )
    return check, actions

def research_session_checklist(
    session_id: str,
    max_attempts_per_source: int = DEFAULT_MAX_ATTEMPTS_PER_SOURCE,
    min_total_hits: int = 1,
    run_id: str = "",
) -> str:
    """Check the state of the research session and propose any next steps."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if not session:
            return _json(
                {
                    "source_type": "research_session_checklist",
                    "status": "failed",
                    "session_id": clean_id,
                    "complete": False,
                    "can_finalize": False,
                    "needs_supervisor_loop": False,
                    "errors": [{"type": "unknown_session", "message": "No research session exists for this session_id."}],
                }
            )
        session_max = _safe_int(session["max_attempts_per_source"], DEFAULT_MAX_ATTEMPTS_PER_SOURCE)
        max_attempts = max(1, session_max)
        _set_session_status(conn, clean_id, "checking")
        latest = _latest_rows(conn, clean_id)
        best = _best_attempt_rows(conn, clean_id)
        per_attempt_meta = _attempt_metadata(conn, clean_id)
        attempts = _attempt_counts(conn, clean_id)
        per_source_max = {
            st: _max_attempts_for_source(st, max_attempts) for st in SOURCE_TYPES
        }
        budget_exhausted = _budget_exceeded_sources(conn, clean_id, per_source_max)
        original_query = str(session["original_query"] or "")
        query_envelope = _safe_json_loads(session["query_envelope_json"], {}) or {}
        conn.commit()

    source_checks: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for source_type in SOURCE_TYPES:
        effective_attempts = attempts.get(source_type, 0)
        source_max = per_source_max[source_type]
        if source_type in budget_exhausted:
            effective_attempts = max(effective_attempts, source_max)
        check, source_actions = _source_check(
            source_type,
            best.get(source_type),
            effective_attempts,
            original_query,
            source_max,
            query_envelope if isinstance(query_envelope, dict) else None,
        )
        if source_type in budget_exhausted:
            check["budget_exhausted"] = True
        meta = per_attempt_meta.get(source_type)
        if meta:
            check["latest_attempt"] = meta["latest_attempt"]
            check["warning_count"] = meta["warning_count"]
            check["error_count"] = meta["error_count"]
        if _is_source_retry_saturated(latest.get(source_type)):
            check["retry_saturated"] = True
            check["needs_retry"] = False
            check["blocking"] = False
            check["ready"] = True
            source_actions = []
        for action in source_actions:
            action["session_id"] = clean_id
        source_checks.append(check)
        actions.extend(source_actions)

    total_hits = sum(int(check.get("hit_count") or 0) for check in source_checks)
    all_sources_present = all(source_type in best for source_type in SOURCE_TYPES)
    all_reliable_no_results = all(check["quality_grade"] == "reliable_no_results" for check in source_checks)
    blocking_checks = [check for check in source_checks if check.get("blocking")]
    min_hits = max(0, _safe_int(min_total_hits))
    quality_grades = {str(check["source_type"]): str(check["quality_grade"]) for check in source_checks}
    strong_or_medium_exists = any(grade in {"strong", "medium"} for grade in quality_grades.values())
    low_total_evidence = total_hits < min_hits and not all_reliable_no_results and not strong_or_medium_exists

    if low_total_evidence and not blocking_checks:
        retryable = [
            check
            for check in source_checks
            if int(check.get("attempt_count") or 0) < per_source_max[str(check["source_type"])]
            and check["quality_grade"] not in {"strong", "medium", "reliable_no_results"}
        ]
        for check in retryable:
            actions.append(
                {
                    "source_type": check["source_type"],
                    "source_agent": SOURCE_AGENT_BY_TYPE[str(check["source_type"])],
                    "direct_mcp_tool": SOURCE_TO_TOOL[check["source_type"]],
                    "tool": SOURCE_TO_TOOL[check["source_type"]],
                    "session_id": clean_id,
                    "query": _planned_query(
                        original_query,
                        check["source_type"],
                        int(check.get("attempt_count") or 0),
                        query_envelope if isinstance(query_envelope, dict) else None,
                    ),
                    "attempt_no": int(check.get("attempt_count") or 0) + 1,
                    "reason": "No strong or moderate evidence exists yet and this source can still be retried.",
                }
            )

    if not actions:
        atomic = (query_envelope or {}).get("critical_requirements_atomic") if isinstance(query_envelope, dict) else None
        if isinstance(atomic, list) and atomic:
            uncovered = _atoms_missing_full_coverage(latest, atomic)
            if uncovered:
                for check in source_checks:
                    if check.get("blocking"):
                        continue
                    if check.get("retry_saturated"):
                        continue
                    source_type_str = str(check["source_type"])
                    if int(check.get("attempt_count") or 0) >= per_source_max[source_type_str]:
                        continue
                    if check["quality_grade"] not in {"strong", "medium"}:
                        continue
                    source_index = SOURCE_TYPES.index(source_type_str) if source_type_str in SOURCE_TYPES else 0
                    atom = uncovered[source_index % len(uncovered)]
                    atom_label = str(atom.get("label") or "").strip() or original_query
                    analysis_base = str(
                        (query_envelope or {}).get("understanding_query") or ""
                    ).strip() if isinstance(query_envelope, dict) else ""
                    if not analysis_base:
                        analysis_base = original_query
                    atom_terms = [
                        str(term).strip()
                        for term in (atom.get("terms") or [])
                        if str(term).strip()
                    ]
                    atom_search_text = " ".join(atom_terms) or atom_label
                    if source_type_str == "web":
                        core_subject = str((query_envelope or {}).get("core_subject") or "").strip() if isinstance(query_envelope, dict) else ""
                        web_base = core_subject or analysis_base
                        targeted_query = _compact_query_text(f"{web_base} {atom_search_text}", 12) or f"{web_base} {atom_search_text}".strip()
                    else:
                        targeted_query = f"{analysis_base} {atom_search_text}".strip()
                    actions.append(
                        {
                            "source_type": source_type_str,
                            "source_agent": SOURCE_AGENT_BY_TYPE[source_type_str],
                            "direct_mcp_tool": SOURCE_TO_TOOL[source_type_str],
                            "tool": SOURCE_TO_TOOL[source_type_str],
                            "session_id": clean_id,
                            "query": targeted_query,
                            "attempt_no": int(check.get("attempt_count") or 0) + 1,
                            "reason": (
                                f"Source is {check['quality_grade']} but no single document covers atomic "
                                f"requirement {atom.get('category')!r} ({atom_label!r}); running a targeted follow-up."
                            ),
                            "element_guided": True,
                            "missing_atom": atom_label,
                        }
                    )

    needs_loop = bool(actions)
    complete = all_sources_present and not blocking_checks and not low_total_evidence
    can_finalize = all_sources_present and not blocking_checks and not needs_loop
    if all_sources_present and not blocking_checks and low_total_evidence and not actions:
        can_finalize = True
    coverage = quality_grades
    has_strong = any(check["quality_grade"] == "strong" for check in source_checks)
    has_medium = any(check["quality_grade"] == "medium" for check in source_checks)
    has_weak = any(check["quality_grade"] == "weak" for check in source_checks)
    has_failed = any(check["quality_grade"] == "failed_retrieval" for check in source_checks)
    has_budget_exhausted = any(check.get("budget_exhausted") for check in source_checks)
    has_partial_secondary = has_weak or has_failed or has_budget_exhausted

    stop_reason = None
    if can_finalize:
        if any(check.get("exact_combination_candidate_found") for check in source_checks):
            stop_reason = "exact_combination_found"
        elif all_reliable_no_results:
            stop_reason = "no_reliable_match_after_complete_search"
        elif has_strong and has_partial_secondary:
            stop_reason = "strong_primary_source_with_partial_secondary_retrieval"
        elif has_strong:
            stop_reason = "strong_primary_source_complete_retrieval"
        elif has_medium and has_partial_secondary:
            stop_reason = "close_match_found_with_weak_secondary_sources"
        elif has_medium:
            stop_reason = "close_match_found"
        elif has_budget_exhausted or (has_weak and not has_strong and not has_medium):
            stop_reason = "retry_budget_exhausted_with_partial_results"
        elif has_weak or has_failed:
            stop_reason = "mixed_results_partial_retrieval"
        elif has_partial_secondary:
            stop_reason = "adjacent_only_results"
        else:
            stop_reason = "provider_failures_no_useful_next_action"
    elif not actions:
        stop_reason = "provider_failures_no_useful_next_action"
    with _connect() as conn:
        snapshot_no = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM checklist_snapshots WHERE session_id=?",
                (clean_id,),
            ).fetchone()["count"]
            or 0
        ) + 1
        conn.execute(
            """
            INSERT INTO checklist_snapshots(
                session_id, run_id, snapshot_no, can_finalize, retry_plan_json, coverage_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (clean_id, str(run_id or ""), snapshot_no, _as_bool_int(can_finalize), _compact_json(actions), _compact_json(coverage), _now()),
        )
        _record_trace(
            conn,
            clean_id,
            "checklist_completed",
            "ok",
            run_id=run_id,
            tool_name="research_session_checklist",
            payload={"can_finalize": can_finalize, "retry_action_count": len(actions), "coverage": coverage},
        )
        conn.commit()

    return _json(
        {
            "schema_version": SCHEMA_VERSION,
            "source_type": "research_session_checklist",
            "ok": True,
            "status": "ok",
            "session_id": clean_id,
            "run_id": run_id,
            "complete": complete,
            "can_finalize": can_finalize,
            "needs_supervisor_loop": needs_loop,
            "coverage": coverage,
            "source_quality_grades": quality_grades,
            "retry_actions": actions,
            "stop_reason": stop_reason,
            "warnings": [],
            "max_attempts_per_source": max_attempts,
            "max_attempts_per_source_by_type": per_source_max,
            "min_total_hits": min_hits,
            "total_hit_count": total_hits,
            "all_sources_present": all_sources_present,
            "all_reliable_no_results": all_reliable_no_results,
            "source_checks": source_checks,
            "supervisor_instruction": (
                "Call the recommended evidence_to_session tools, then call research_session_checklist again."
                if needs_loop
                else "Call research_session_user_answer for the default user-facing answer, or research_session_final_answer only for a debug report."
            ),
        }
    )

def research_session_plan_next(session_id: str) -> str:
    """Return suggested next search steps based on the state of the session."""
    clean_id = _clean_session_id(session_id)
    checklist = json.loads(research_session_checklist(clean_id))
    actions = checklist.get("retry_actions") if isinstance(checklist, dict) else []
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        original_query = str(session["original_query"] or "") if session else ""
        query_envelope = _safe_json_loads(session["query_envelope_json"], {}) if session else {}
    plans = []
    for action in _as_list(actions):
        source_type = str(action.get("source_type") or "")
        if source_type not in SOURCE_TYPES:
            continue
        plans.append(
            {
                "source_type": source_type,
                "tool": SOURCE_TO_TOOL[source_type],
                "query": action.get("query") or _planned_query(
                    original_query,
                    source_type,
                    1,
                    query_envelope if isinstance(query_envelope, dict) else None,
                ),
                "reason": action.get("reason") or "Checklist requested more evidence.",
            }
        )
    return _json(
        {
            "source_type": "research_session_plan",
            "status": "ok",
            "session_id": clean_id,
            "original_query": original_query,
            "plans": plans,
            "instruction": "Use these plans as tool inputs for the next evidence_to_session calls. If plans is empty, finalize from the session.",
        }
    )

def _best_attempt_normalized(conn: sqlite3.Connection, session_id: str, source_type: str) -> dict[str, Any] | None:
    """Load the normalised results from the best available attempt for a source."""
    row = _best_attempt_rows(conn, session_id).get(source_type)
    if not row:
        return None
    try:
        parsed = json.loads(row["normalized_json"])
    except json.JSONDecodeError:
        return _missing_source(source_type, "stored_json_malformed", "Stored normalized evidence JSON is malformed.")
    return parsed if isinstance(parsed, dict) else None

def research_session_merge(session_id: str, original_query: str = "") -> str:
    """Merge the best stored evidence for the given research session."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if not session:
            return _json(
                {
                    "source_type": "merged_evidence_pack",
                    "query": clean_tool_query(original_query),
                    "overall_status": "failed",
                    "warnings": ["No research session exists for this session_id."],
                    "errors": [{"type": "unknown_session", "message": "No research session exists for this session_id."}],
                }
            )
        query = clean_tool_query(original_query or str(session["original_query"] or ""))
        patent = _best_attempt_normalized(conn, clean_id, "patent")
        publication = _best_attempt_normalized(conn, clean_id, "publication")
        web = _best_attempt_normalized(conn, clean_id, "web")
    return merge_evidence_pack(
        patent_evidence_json=patent,
        publication_evidence_json=publication,
        web_evidence_json=web,
        original_query=query,
    )

def research_session_final_answer(
    session_id: str,
    original_query: str = "",
    draft_answer: str = "",
    force_regenerate: bool = False,
    run_id: str = "",
) -> str:
    """Build the final answer from the session's stored evidence."""
    return research_session_final_answer_cached(
        session_id=session_id,
        original_query=original_query,
        draft_answer=draft_answer,
        force_regenerate=force_regenerate,
        run_id=run_id,
    )

def research_session_final_answer_cached(
    session_id: str,
    original_query: str = "",
    draft_answer: str = "",
    force_regenerate: bool = False,
    run_id: str = "",
) -> str:
    """Build the final debug answer, or reuse its cached version."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if session:
            _set_session_status(conn, clean_id, "finalizing")
            conn.commit()
    merged = research_session_merge(session_id=clean_id, original_query=original_query)
    hash_value = _input_hash({"merged": merged, "original_query": clean_tool_query(original_query), "draft_answer": draft_answer})
    with _connect() as conn:
        existing = None
        if not force_regenerate:
            existing = conn.execute(
                "SELECT answer_text FROM final_answers WHERE session_id=? AND input_hash=? ORDER BY id DESC LIMIT 1",
                (clean_id, hash_value),
            ).fetchone()
        if existing:
            _set_session_status(conn, clean_id, "completed")
            _record_trace(
                conn,
                clean_id,
                "final_answer_duplicate",
                "duplicate_noop",
                run_id=run_id,
                tool_name="research_session_final_answer",
                payload={"input_hash": hash_value},
            )
            conn.commit()
            return str(existing["answer_text"])
    answer = final_answer_pack(merged_pack=merged, original_query=original_query, draft_answer=draft_answer)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO final_answers(session_id, schema_version, input_hash, answer_text, debug_report_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, input_hash) DO UPDATE SET
                answer_text=excluded.answer_text,
                debug_report_text=excluded.debug_report_text
            """,
            (clean_id, SCHEMA_VERSION, hash_value, answer, answer, _now()),
        )
        _set_session_status(conn, clean_id, "completed")
        _record_trace(
            conn,
            clean_id,
            "final_answer_completed",
            "ok",
            run_id=run_id,
            tool_name="research_session_final_answer",
            payload={"input_hash": hash_value, "answer_chars": len(answer)},
        )
        conn.commit()
    return answer

def research_session_user_answer(
    session_id: str,
    original_query: str = "",
    debug_mode: bool = False,
    force_regenerate: bool = False,
    run_id: str = "",
) -> str:
    """Build the user-facing output from the evidence stored in the database."""
    clean_id = _clean_session_id(session_id)
    with _connect() as conn:
        session = _session_row(conn, clean_id)
        if not session:
            error_message = "No research session exists for this session_id."
            if not debug_mode:
                return f"# Research session not found\n\n{error_message}"
            return _json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_type": "research_session_user_answer",
                    "status": "failed",
                    "session_id": clean_id,
                    "errors": [{"type": "unknown_session", "message": error_message}],
                }
            )
        query = clean_tool_query(original_query or str(session["original_query"] or ""))
        query_envelope = _safe_json_loads(session["query_envelope_json"], {}) or {}
        latest = _latest_rows(conn, clean_id)
        best = _best_attempt_rows(conn, clean_id)
        _set_session_status(conn, clean_id, "finalizing")
        conn.commit()
    retrieval_status_notes = _retrieval_status_notes(latest, best)
    merged = research_session_merge(session_id=clean_id, original_query=query)
    hash_value = _input_hash({"kind": "user_answer", "merged": merged, "original_query": query, "debug_mode": bool(debug_mode)})
    with _connect() as conn:
        existing = None
        if not force_regenerate:
            existing = conn.execute(
                """
                SELECT user_answer_text, verdict, confidence, debug_report_text
                FROM final_answers
                WHERE session_id=? AND input_hash=?
                ORDER BY id DESC LIMIT 1
                """,
                (clean_id, hash_value),
            ).fetchone()
        if existing and str(existing["user_answer_text"] or ""):
            _set_session_status(conn, clean_id, "completed")
            _record_trace(
                conn,
                clean_id,
                "user_answer_duplicate",
                "duplicate_noop",
                run_id=run_id,
                tool_name="research_session_user_answer",
                payload={"input_hash": hash_value},
            )
            conn.commit()
            payload = build_user_answer_payload(
                merged_pack=merged,
                original_query=query,
                query_envelope=query_envelope if isinstance(query_envelope, dict) else {},
                debug_report=str(existing["debug_report_text"] or ""),
                debug_mode=debug_mode,
                retrieval_status_notes=retrieval_status_notes,
            )
            payload.update({"schema_version": SCHEMA_VERSION, "session_id": clean_id, "run_id": run_id})
            if debug_mode:
                return _json(payload)
            return str(payload.get("user_answer") or "")
    debug_report = final_answer_pack(merged_pack=merged, original_query=query) if debug_mode else ""
    payload = build_user_answer_payload(
        merged_pack=merged,
        original_query=query,
        query_envelope=query_envelope if isinstance(query_envelope, dict) else {},
        debug_report=debug_report,
        debug_mode=debug_mode,
        retrieval_status_notes=retrieval_status_notes,
    )
    payload.update({"schema_version": SCHEMA_VERSION, "session_id": clean_id, "run_id": run_id})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO final_answers(
                session_id, schema_version, input_hash, answer_text, verdict,
                confidence, user_answer_text, debug_report_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, input_hash) DO UPDATE SET
                answer_text=excluded.answer_text,
                verdict=excluded.verdict,
                confidence=excluded.confidence,
                user_answer_text=excluded.user_answer_text,
                debug_report_text=excluded.debug_report_text
            """,
            (
                clean_id,
                SCHEMA_VERSION,
                hash_value,
                str(payload["user_answer"]),
                str(payload["verdict"]),
                str(payload["confidence"]),
                str(payload["user_answer"]),
                debug_report,
                _now(),
            ),
        )
        _set_session_status(conn, clean_id, "completed")
        _record_trace(
            conn,
            clean_id,
            "user_answer_completed",
            "ok",
            run_id=run_id,
            tool_name="research_session_user_answer",
            payload={
                "input_hash": hash_value,
                "verdict": payload["verdict"],
                "confidence": payload["confidence"],
                "word_count": payload.get("word_count"),
            },
        )
        conn.commit()
    if debug_mode:
        return _json(payload)
    return str(payload.get("user_answer") or "")

def _load_atomic_requirements(session_id: str) -> list[dict[str, Any]] | None:
    """Load the atomic requirements from the session's stored query envelope."""
    try:
        with _connect() as conn:
            session = _session_row(conn, _clean_session_id(session_id))
            if session is None:
                return None
            envelope = _safe_json_loads(session["query_envelope_json"], {}) or {}
            if not isinstance(envelope, dict):
                return None
            raw_atomic = envelope.get("critical_requirements_atomic") or []
            if not isinstance(raw_atomic, list):
                return None
            atoms = [atom for atom in raw_atomic if isinstance(atom, dict)]
            return atoms or None
    except Exception:
        return None


def _envelope_query_too_generic(session_id: str) -> bool:
    """Check whether the query envelope flags the query as too generic."""
    try:
        with _connect() as conn:
            row = _session_row(conn, _clean_session_id(session_id))
            if row is None:
                return False
            envelope = _safe_json_loads(row["query_envelope_json"], {}) or {}
            return bool(envelope.get("query_too_generic"))
    except Exception:
        return False

def _too_generic_short_circuit(
    session_id: str,
    source_type: str,
    query: str,
    attempt_no: int,
    run_id: str,
    agent_name: str,
    english_query: str = "",
) -> str:
    """Skip the provider calls and store a reliable no-results record for a too-generic query."""
    payload = {
        "source_type": source_type,
        "status": "ok",
        "completed": True,
        "reliable_no_results": True,
        "hits": [],
        "warnings": [
            "query_too_generic: envelope flagged the query as too generic for meaningful retrieval; no providers were called."
        ],
        "errors": [],
    }
    return research_session_save_evidence(
        session_id=session_id,
        source_type=source_type,
        evidence_json=payload,
        query=clean_tool_query(query),
        agent_name=agent_name,
        attempt=int(attempt_no or 0),
        run_id=run_id,
        english_query=english_query,
    )

async def patent_evidence_to_session(
    session_id: str,
    query: str,
    max_results: int = 10,
    max_fetches: int = 6,
    fetch_timeout_ms: int = 18000,
    attempt_no: int = 0,
    run_id: str = "",
    source_type: str = "patent",
    english_query: str = "",
) -> str:
    """Retrieve patent evidence and store it against the current research session."""
    english_query_clean = (english_query or "").strip()
    received_source = str(source_type or "").strip().lower()
    if received_source and received_source != "patent":
        return _error_ack("invalid_source_type", _clean_session_id(session_id), "patent", _safe_int(attempt_no), query_hash(query), "Envelope source_type did not match patent writer.", run_id=run_id, expected_source_type="patent", received_source_type=received_source, english_query=english_query_clean)
    clean_query = _writer_query(session_id, query, "patent", attempt_no)
    begin = _begin_source_attempt(
        session_id,
        "patent",
        clean_query,
        run_id=run_id,
        attempt_no=attempt_no,
        agent_name="patent_sqlite_writer_agent",
        tool_name="patent_evidence_to_session",
    )
    if begin["action"] == "duplicate":
        return _add_english_query_diagnostics(_duplicate_ack(begin), "patent", english_query_clean)
    if begin["action"] == "error":
        return _add_english_query_diagnostics(str(begin["ack"]), "patent", english_query_clean)
    if _envelope_query_too_generic(session_id):
        return _too_generic_short_circuit(
            session_id=session_id,
            source_type="patent",
            query=clean_query,
            attempt_no=int(begin["attempt"]),
            run_id=run_id,
            agent_name="patent_sqlite_writer_agent",
            english_query=english_query_clean,
        )
    atomic_requirements = _load_atomic_requirements(session_id)
    try:
        _collector = _new_decision_collector(
            begin["session_id"], run_id, "patent", int(begin["attempt"]), clean_query
        )
        try:
            evidence = await patent_evidence_pack(
                query=clean_query,
                max_results=max_results,
                max_fetches=max_fetches,
                fetch_timeout_ms=fetch_timeout_ms,
                atomic_requirements=atomic_requirements,
                english_query=english_query_clean,
                _collector=_collector,
            )
        finally:
            # Fail-open: persists whatever was collected before a provider
            # failure, and can never turn a successful retrieval into a failure.
            _persist_decision_events(_collector)
    except TimeoutError as exc:
        return research_session_record_failure(session_id, "patent", clean_query, "timeout", "timeout", str(exc), int(begin["attempt"]), run_id, "patent_sqlite_writer_agent", english_query_clean)
    except Exception as exc:
        return research_session_record_failure(session_id, "patent", clean_query, "provider_error", exc.__class__.__name__, str(exc), int(begin["attempt"]), run_id, "patent_sqlite_writer_agent", english_query_clean)
    return research_session_save_evidence(
        session_id=session_id,
        source_type="patent",
        evidence_json=evidence,
        query=clean_query,
        agent_name="patent_sqlite_writer_agent",
        attempt=int(begin["attempt"]),
        run_id=run_id,
        english_query=english_query_clean,
    )

async def publication_evidence_to_session(
    session_id: str,
    query: str,
    max_results: int = 6,
    max_fetches: int = 2,
    fetch_timeout_s: float = 12.0,
    attempt_no: int = 0,
    run_id: str = "",
    source_type: str = "publication",
    english_query: str = "",
) -> str:
    """Retrieve publication evidence and store it against the current research session."""
    english_query_clean = (english_query or "").strip()
    received_source = str(source_type or "").strip().lower()
    if received_source and received_source != "publication":
        return _error_ack("invalid_source_type", _clean_session_id(session_id), "publication", _safe_int(attempt_no), query_hash(query), "Envelope source_type did not match publication writer.", run_id=run_id, expected_source_type="publication", received_source_type=received_source, english_query=english_query_clean)
    clean_query = _writer_query(session_id, query, "publication", attempt_no)
    begin = _begin_source_attempt(
        session_id,
        "publication",
        clean_query,
        run_id=run_id,
        attempt_no=attempt_no,
        agent_name="publication_sqlite_writer_agent",
        tool_name="publication_evidence_to_session",
    )
    if begin["action"] == "duplicate":
        return _add_english_query_diagnostics(_duplicate_ack(begin), "publication", english_query_clean)
    if begin["action"] == "error":
        return _add_english_query_diagnostics(str(begin["ack"]), "publication", english_query_clean)
    if _envelope_query_too_generic(session_id):
        return _too_generic_short_circuit(
            session_id=session_id,
            source_type="publication",
            query=clean_query,
            attempt_no=int(begin["attempt"]),
            run_id=run_id,
            agent_name="publication_sqlite_writer_agent",
            english_query=english_query_clean,
        )
    atomic_requirements = _load_atomic_requirements(session_id)
    try:
        _collector = _new_decision_collector(
            begin["session_id"], run_id, "publication", int(begin["attempt"]), clean_query
        )
        try:
            evidence = await publication_evidence_pack(
                query=clean_query,
                max_results=max_results,
                max_fetches=max_fetches,
                fetch_timeout_s=fetch_timeout_s,
                english_query=english_query_clean,
                atomic_requirements=atomic_requirements,
                _collector=_collector,
            )
        finally:
            # Fail-open: persists whatever was collected before a provider
            # failure, and can never turn a successful retrieval into a failure.
            _persist_decision_events(_collector)
    except TimeoutError as exc:
        return research_session_record_failure(session_id, "publication", clean_query, "timeout", "timeout", str(exc), int(begin["attempt"]), run_id, "publication_sqlite_writer_agent", english_query_clean)
    except Exception as exc:
        return research_session_record_failure(session_id, "publication", clean_query, "provider_error", exc.__class__.__name__, str(exc), int(begin["attempt"]), run_id, "publication_sqlite_writer_agent", english_query_clean)
    return research_session_save_evidence(
        session_id=session_id,
        source_type="publication",
        evidence_json=evidence,
        query=clean_query,
        agent_name="publication_sqlite_writer_agent",
        attempt=int(begin["attempt"]),
        run_id=run_id,
        english_query=english_query_clean,
    )

async def web_evidence_to_session(
    session_id: str,
    query: str,
    max_results: int = 6,
    max_fetches: int = 3,
    timeout_ms: int = 14000,
    attempt_no: int = 0,
    run_id: str = "",
    source_type: str = "web",
    english_query: str = "",
) -> str:
    """Retrieve web evidence and store it against the current research session."""
    english_query_clean = (english_query or "").strip()
    received_source = str(source_type or "").strip().lower()
    if received_source and received_source != "web":
        return _error_ack("invalid_source_type", _clean_session_id(session_id), "web", _safe_int(attempt_no), query_hash(query), "Envelope source_type did not match web writer.", run_id=run_id, expected_source_type="web", received_source_type=received_source, english_query=english_query_clean)
    clean_query = _writer_query(session_id, query, "web", attempt_no)
    begin = _begin_source_attempt(
        session_id,
        "web",
        clean_query,
        run_id=run_id,
        attempt_no=attempt_no,
        agent_name="web_sqlite_writer_agent",
        tool_name="web_evidence_to_session",
    )
    if begin["action"] == "duplicate":
        return _add_english_query_diagnostics(_duplicate_ack(begin), "web", english_query_clean)
    if begin["action"] == "error":
        return _add_english_query_diagnostics(str(begin["ack"]), "web", english_query_clean)
    if _envelope_query_too_generic(session_id):
        return _too_generic_short_circuit(
            session_id=session_id,
            source_type="web",
            query=clean_query,
            attempt_no=int(begin["attempt"]),
            run_id=run_id,
            agent_name="web_sqlite_writer_agent",
            english_query=english_query_clean,
        )
    atomic_requirements = _load_atomic_requirements(session_id)
    try:
        _collector = _new_decision_collector(
            begin["session_id"], run_id, "web", int(begin["attempt"]), clean_query
        )
        try:
            evidence = await web_evidence_pack(
                query=clean_query,
                max_results=max_results,
                max_fetches=max_fetches,
                timeout_ms=timeout_ms,
                english_query=english_query_clean,
                atomic_requirements=atomic_requirements,
                _collector=_collector,
            )
        finally:
            # Fail-open: persists whatever was collected before a provider
            # failure, and can never turn a successful retrieval into a failure.
            _persist_decision_events(_collector)
    except TimeoutError as exc:
        return research_session_record_failure(session_id, "web", clean_query, "timeout", "timeout", str(exc), int(begin["attempt"]), run_id, "web_sqlite_writer_agent", english_query_clean)
    except Exception as exc:
        return research_session_record_failure(session_id, "web", clean_query, "provider_error", exc.__class__.__name__, str(exc), int(begin["attempt"]), run_id, "web_sqlite_writer_agent", english_query_clean)
    return research_session_save_evidence(
        session_id=session_id,
        source_type="web",
        evidence_json=evidence,
        query=clean_query,
        agent_name="web_sqlite_writer_agent",
        attempt=int(begin["attempt"]),
        run_id=run_id,
        english_query=english_query_clean,
    )
