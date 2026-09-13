"""Validate synthetic capture snapshots without opening production storage.

Manifest assertions are provenance evidence, not proof of origin or consent.
This first-phase importer deliberately refuses real-study classifications.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from tools.decision_capture import (
    DECISION_REASONS, DECISION_STAGES, example_id, query_envelope_hash,
    query_fingerprint,
)

from .contracts import CAPTURE_COMMIT, PROTOCOL_VERSION, ContractError, canonical_json_bytes, sha256_json

MANIFEST_VERSION = "goal5c.capture_manifest.v1"
BUNDLE_VERSION = "goal5c.capture_bundle.v1"
SOURCES = {"patent", "publication", "web"}
CAPTURE_STATES = {"observed", "missing", "known_loss", "cache_only", "empty_output"}
# Pinned to the approved capture commit, not merely the broader enum vocabulary.
SEMANTICS = {
    "patent": {
        "cache_lookup": {"cache_hit", "cache_miss"}, "dedupe": {"duplicate_of"},
        "truncation": {"beyond_limit"}, "domain_anchor": {"accepted", "no_discriminative_term"},
        "threshold_filter": {"accepted", "below_threshold", "relaxed_threshold_applied", "floor_applied"},
    },
    "publication": {
        "provider_filter": {"accepted", "below_threshold", "below_overlap_gate", "no_discriminative_term"},
        "merged_rerank": {"accepted", "below_threshold", "below_overlap_gate", "no_discriminative_term"},
        "dedupe": {"accepted", "duplicate_of"}, "truncation": {"accepted", "beyond_limit"},
        "fill_back": {"restored_to_meet_minimum"},
    },
    "web": {
        "url_gate": {"accepted", "url_policy_rejected"}, "content_gate": {"below_overlap_gate"},
        "provider_filter": {"accepted", "below_threshold"}, "merge_collision": {"discarded_duplicate_url"},
        "merged_rerank": {"accepted", "below_threshold", "no_discriminative_term"},
        "fill_back": {"restored_after_empty_rerank"}, "truncation": {"beyond_limit"},
    },
}
EVENT_COLUMNS = (
    "id", "schema_version", "query_fingerprint", "query_envelope_hash", "example_id",
    "session_id", "run_id", "source_type", "attempt", "decision_stage", "decision_reason",
    "candidate_identity", "candidate_identity_kind", "title", "score_text", "decision_query",
    "query_variant", "provider", "score_at_decision", "threshold_at_decision", "retained",
    "dataset_eligible", "payload_json", "created_at",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _fields(value: Any, fields: set[str], message: str) -> None:
    _require(isinstance(value, dict) and set(value) == fields, message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) is not None


def _digest(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _parse_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ContractError("nonfinite JSON value")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
        canonical_json_bytes(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError("invalid JSON input") from exc


def load_json(path: Path) -> Any:
    """Reject duplicate keys/nonfinite values instead of silently repairing JSON."""
    try:
        return _parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ContractError("invalid JSON input") from exc


def _envelope_digest(envelope: Any) -> str:
    _require(envelope is None or isinstance(envelope, dict), "invalid envelope snapshot")
    try:
        return query_envelope_hash(
            envelope.get("critical_requirements_atomic") if envelope is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid atomic requirement snapshot") from exc


def validate_manifest(manifest: Any) -> None:
    """Validate acquisition declarations; real acquisition remains gated."""
    canonical_json_bytes(manifest)
    _fields(manifest, {
        "schema_version", "protocol_version", "data_class", "batch_id", "capture_commit",
        "capture_tree_clean", "snapshot_sha256", "environment_sha256", "configuration_sha256",
        "queries", "executions",
    }, "invalid manifest fields")
    _require(manifest["schema_version"] == MANIFEST_VERSION, "unsupported manifest version")
    _require(manifest["protocol_version"] == PROTOCOL_VERSION, "unsupported protocol version")
    _require(manifest["data_class"] == "synthetic", "real-study inputs are not enabled")
    _require(manifest["capture_commit"] == CAPTURE_COMMIT, "unapproved capture commit")
    _require(manifest["capture_tree_clean"] is True, "capture tree must be declared clean")
    _require(_identifier(manifest["batch_id"]), "invalid batch identifier")
    for field in ("snapshot_sha256", "environment_sha256", "configuration_sha256"):
        _require(_digest(manifest[field]), "invalid provenance digest")
    _require(isinstance(manifest["queries"], list) and bool(manifest["queries"]), "empty query registry")
    queries = {}
    fingerprints = set()
    for query in manifest["queries"]:
        _fields(query, {"query_id", "original_query", "query_fingerprint", "intent_family_id", "stratum"},
                "invalid query record")
        _require(_identifier(query["query_id"]) and _identifier(query["intent_family_id"]),
                 "invalid query identifier")
        _require(_text(query["original_query"]), "missing registered original query")
        _require(isinstance(query["stratum"], str) and query["stratum"] in {"invention", "scientific", "operational"},
                 "invalid intent stratum")
        _require(query["query_fingerprint"] == query_fingerprint(query["original_query"]),
                 "registered fingerprint mismatch")
        _require(query["query_id"] not in queries and query["query_fingerprint"] not in fingerprints,
                 "duplicate original query")
        queries[query["query_id"]] = query
        fingerprints.add(query["query_fingerprint"])
    _require(isinstance(manifest["executions"], list) and bool(manifest["executions"]),
             "empty execution registry")
    keys, execution_ids, session_queries = set(), set(), {}
    for execution in manifest["executions"]:
        _fields(execution, {
            "execution_id", "query_id", "session_id", "run_id", "source_type", "attempt",
            "envelope", "query_envelope_hash", "call_query", "capture_status", "declared_event_count",
        }, "invalid execution record")
        _require(all(_identifier(execution[key]) for key in (
            "execution_id", "query_id", "session_id", "run_id",
        )), "invalid execution identifier")
        _require(execution["query_id"] in queries, "execution references unknown query")
        _require(isinstance(execution["source_type"], str) and execution["source_type"] in SOURCES, "invalid source")
        _require(type(execution["attempt"]) is int and execution["attempt"] > 0, "invalid attempt")
        _require(_text(execution["call_query"]), "missing executed query")
        _require(isinstance(execution["capture_status"], str) and execution["capture_status"] in CAPTURE_STATES,
                 "invalid capture state")
        _require(type(execution["declared_event_count"]) is int
                 and execution["declared_event_count"] >= 0, "invalid event count")
        _require(execution["query_envelope_hash"] == _envelope_digest(execution["envelope"]),
                 "envelope fingerprint mismatch")
        key = (execution["session_id"], execution["source_type"], execution["attempt"])
        _require(key not in keys and execution["execution_id"] not in execution_ids,
                 "ambiguous execution declaration")
        previous = session_queries.setdefault(execution["session_id"], execution["query_id"])
        _require(previous == execution["query_id"], "session references multiple original queries")
        keys.add(key)
        execution_ids.add(execution["execution_id"])


def _snapshot_bytes(path: Path) -> bytes:
    _require(path.is_file(), "snapshot file is missing")
    _require(not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")),
             "snapshot has active SQLite sidecars")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ContractError("snapshot cannot be read") from exc


def _read_snapshot(path: Path, expected_digest: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Open only an already finalized file; never invoke production migrations."""
    _require(hashlib.sha256(_snapshot_bytes(path)).hexdigest() == expected_digest,
             "snapshot digest mismatch")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            _require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "invalid SQLite snapshot")
            tables = {row[0]: row[1] for row in conn.execute("SELECT name, type FROM sqlite_master")}
            for name in ("research_sessions", "evidence_results"):
                _require(tables.get(name) == "table", "missing production provenance table")
            sessions = [dict(row) for row in conn.execute(
                "SELECT session_id, original_query FROM research_sessions ORDER BY session_id",
            )]
            attempts = [dict(row) for row in conn.execute(
                "SELECT session_id, run_id, source_type, attempt, query, status, completed, "
                "reliable_no_results, error_count FROM evidence_results "
                "ORDER BY session_id, source_type, attempt",
            )]
            events = []
            if "evaluation_candidate_decisions" in tables:
                _require(tables["evaluation_candidate_decisions"] == "table", "capture object is not a table")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(evaluation_candidate_decisions)")}
                _require(columns == set(EVENT_COLUMNS), "unsupported capture columns")
                events = [dict(row) for row in conn.execute(
                    "SELECT " + ", ".join(EVENT_COLUMNS) + " FROM evaluation_candidate_decisions ORDER BY id",
                )]
    except sqlite3.Error as exc:
        raise ContractError("invalid SQLite capture schema") from exc
    _require(hashlib.sha256(_snapshot_bytes(path)).hexdigest() == expected_digest,
             "snapshot changed during import")
    return sessions, attempts, events


def _decision_kind(event: dict) -> str:
    if not event["dataset_eligible"]:
        return "trace_only"
    if event["decision_stage"] == "fill_back":
        return "restoration"
    if event["decision_stage"] in {"domain_anchor", "content_gate"}:
        return "text_gate"
    if event["decision_stage"] in {"provider_filter", "merged_rerank", "threshold_filter"}:
        return "relevance_gate"
    return "structural"


def _validate_event(event: dict, execution: dict, query: dict) -> None:
    for field in EVENT_COLUMNS:
        if field not in {"id", "attempt", "score_at_decision", "threshold_at_decision", "retained", "dataset_eligible"}:
            _require(isinstance(event[field], str), "invalid event text field")
    _require(type(event["id"]) is int and event["id"] > 0, "invalid event identity")
    _require(type(event["attempt"]) is int and event["attempt"] > 0, "invalid event attempt")
    _require(event["schema_version"] == "candidate_decision.v1", "unsupported capture schema")
    _require(event["query_fingerprint"] == query["query_fingerprint"], "event original query mismatch")
    _require(event["query_envelope_hash"] == execution["query_envelope_hash"], "event envelope mismatch")
    _require(event["run_id"] == execution["run_id"], "event execution mismatch")
    _require(event["decision_stage"] in DECISION_STAGES and event["decision_reason"] in DECISION_REASONS,
             "unknown decision semantics")
    _require(event["decision_reason"] in SEMANTICS[event["source_type"]].get(event["decision_stage"], set()),
             "unsupported source stage or reason")
    _require(event["candidate_identity_kind"] in {"canonical_id", "url", "text_hash"}
             and _text(event["candidate_identity"]), "invalid candidate identity")
    _require(event["example_id"] == example_id(query["query_fingerprint"], event["source_type"],
                                               event["candidate_identity"]), "example identity mismatch")
    if event["candidate_identity_kind"] == "text_hash":
        _require(_digest(event["candidate_identity"], 32), "invalid fallback identity")
    if event["payload_json"]:
        _require(isinstance(_parse_json(event["payload_json"]), dict), "invalid event payload")
    _require(type(event["dataset_eligible"]) is int and event["dataset_eligible"] in (0, 1),
             "invalid candidate eligibility")
    _require(event["retained"] is None or (type(event["retained"]) is int and event["retained"] in (0, 1)),
             "invalid stage outcome")
    for field in ("score_at_decision", "threshold_at_decision"):
        value = event[field]
        _require(value is None or (type(value) in (float, int) and math.isfinite(value)), "invalid numeric decision")
    if event["decision_stage"] == "cache_lookup":
        _require(event["dataset_eligible"] == 0, "cache observation cannot be a candidate")
    if event["decision_stage"] not in {"provider_filter", "merged_rerank", "domain_anchor", "threshold_filter", "fill_back"}:
        _require(event["retained"] is None, "structural outcome cannot assert relevance")
    else:
        accepted = event["decision_reason"] in {
            "accepted", "relaxed_threshold_applied", "floor_applied", "restored_to_meet_minimum",
            "restored_after_empty_rerank",
        }
        _require(event["retained"] == int(accepted), "stage reason contradicts outcome")


def import_snapshot(snapshot: Path, manifest: dict) -> dict:
    """Return a deterministic synthetic bundle; do not label or select documents."""
    validate_manifest(manifest)
    path = snapshot.resolve()
    sessions, attempts, events = _read_snapshot(path, manifest["snapshot_sha256"])
    for attempt in attempts:
        _require(type(attempt["attempt"]) is int and attempt["attempt"] > 0, "invalid stored attempt")
        _require(all(type(attempt[key]) is int and attempt[key] in (0, 1)
                     for key in ("completed", "reliable_no_results")), "invalid stored completion flag")
        _require(type(attempt["error_count"]) is int and attempt["error_count"] >= 0, "invalid stored error count")
        _require(all(isinstance(attempt[key], str) for key in ("session_id", "run_id", "source_type", "query", "status")),
                 "invalid stored attempt text")
    queries = {query["query_id"]: query for query in manifest["queries"]}
    executions = {(item["session_id"], item["source_type"], item["attempt"]): item
                  for item in manifest["executions"]}
    session_rows = {item["session_id"]: item for item in sessions}
    _require(len(session_rows) == len(sessions), "duplicate session rows")
    attempt_rows = {(item["session_id"], item["source_type"], item["attempt"]): item for item in attempts}
    _require(len(attempt_rows) == len(attempts), "duplicate attempt rows")
    _require(set(attempt_rows) <= set(executions), "undeclared source attempt")
    _require(set(session_rows) == {item["session_id"] for item in executions.values()}, "undeclared session")
    grouped = {key: [] for key in executions}
    for event in events:
        key = (event["session_id"], event["source_type"], event["attempt"])
        _require(key in executions, "event has no execution declaration")
        execution = executions[key]
        _validate_event(event, execution, queries[execution["query_id"]])
        grouped[key].append(event)
    _require(len({event["id"] for event in events}) == len(events), "duplicate raw event identity")
    execution_output = []
    execution_reasons = {}
    for key, execution in sorted(executions.items()):
        query = queries[execution["query_id"]]
        stored = session_rows[execution["session_id"]]["original_query"]
        _require(stored == query["original_query"], "stored original differs from registry")
        rows = grouped[key]
        _require(len(rows) == execution["declared_event_count"], "declared event count mismatch")
        state = execution["capture_status"]
        _require(state not in {"missing", "empty_output"} or not rows, "empty capture state has events")
        _require(state not in {"observed", "cache_only"} or bool(rows), "observed capture has no events")
        if state == "cache_only":
            _require(all(row["decision_stage"] == "cache_lookup" and row["dataset_eligible"] == 0 for row in rows),
                     "cache-only execution contains candidate decisions")
        if state == "observed":
            _require(any(row["decision_stage"] != "cache_lookup" for row in rows), "cache-only capture misdeclared")
        attempt = attempt_rows.get(key)
        if attempt is not None:
            _require(attempt["run_id"] == execution["run_id"] and attempt["query"] == execution["call_query"],
                     "attempt provenance mismatch")
        complete = bool(attempt and attempt["status"] == "ok" and attempt["completed"] == 1
                        and attempt["error_count"] == 0)
        if state == "empty_output":
            _require(complete and attempt["reliable_no_results"] == 1, "empty pool lacks completion evidence")
        reasons = []
        if not execution["query_envelope_hash"]:
            reasons.append("unavailable_envelope")
        if not complete:
            reasons.append("incomplete_source_attempt")
        if state != "observed":
            reasons.append(state)
        if state == "empty_output":
            # Reliable no-results refers to the returned evidence, not to the
            # materialized pool: every materialized document may be rejected.
            reasons.append("unverified_empty_pool")
        if execution["attempt"] != 1:
            reasons.append("retry_not_fp1")
        execution_reasons[key] = reasons
        execution_output.append({**execution, "source_attempt": attempt, "fp1_exclusion_reasons": reasons})
    examples, representations, raw_output = {}, {}, []
    for event in events:
        key = (event["session_id"], event["source_type"], event["attempt"])
        execution = executions[key]
        raw_id = f"{manifest['snapshot_sha256']}:{event['id']}"
        raw_output.append({"raw_event_id": raw_id, "execution_id": execution["execution_id"],
                           "decision_kind": _decision_kind(event),
                           "gate_outcome": (False if event["decision_stage"] == "content_gate"
                                            else None if event["retained"] is None else bool(event["retained"])),
                           "captured": event})
        if not event["dataset_eligible"]:
            continue
        identifier = event["example_id"]
        item = examples.setdefault(identifier, {
            "example_id": identifier, "query_id": execution["query_id"], "source_type": event["source_type"],
            "candidate_identity": event["candidate_identity"], "candidate_identity_kind": event["candidate_identity_kind"],
            "identity_status": "unreconciled", "raw_event_ids": [], "representation_ids": [],
        })
        _require(item["candidate_identity_kind"] == event["candidate_identity_kind"], "ambiguous identity kinds")
        item["raw_event_ids"].append(raw_id)
        representation = {"example_id": identifier, "title": event["title"], "score_text": event["score_text"]}
        representation_id = sha256_json(representation)
        entry = representations.setdefault(representation_id, {
            "representation_id": representation_id, **representation, "raw_event_ids": [],
            "fp1_provenance_event_ids": [],
        })
        entry["raw_event_ids"].append(raw_id)
        if not execution_reasons[key] and _text(event["score_text"]):
            entry["fp1_provenance_event_ids"].append(raw_id)
        if representation_id not in item["representation_ids"]:
            item["representation_ids"].append(representation_id)
    return {
        "schema_version": BUNDLE_VERSION, "protocol_version": PROTOCOL_VERSION, "data_class": "synthetic",
        "batch_id": manifest["batch_id"], "capture_commit": manifest["capture_commit"],
        "manifest_sha256": sha256_json(manifest), "snapshot_sha256": manifest["snapshot_sha256"],
        "queries": sorted(manifest["queries"], key=lambda item: item["query_id"]),
        "executions": execution_output, "raw_events": raw_output,
        "examples": [examples[key] for key in sorted(examples)],
        "representations": [representations[key] for key in sorted(representations)],
    }
