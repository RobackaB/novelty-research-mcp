"""Strict offline study contracts; no acquisition, labelling or metric execution.

Version one accepts synthetic artifacts only. References identify separately
versioned inputs; their authenticity must be checked by the importing boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


PROTOCOL_VERSION = "goal5c.protocol.v1"
CAPTURE_COMMIT = "98bda6c8dbe66f06606329bb929437b00c520467"
SOURCE_TYPES = frozenset({"patent", "publication", "web"})


class ContractError(ValueError):
    """An offline artifact violates the registered contract."""


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ContractError("invalid JSON value")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON primitives deterministically with a final LF.

    Dictionary order is irrelevant; list order and exact strings are preserved.
    Reject non-JSON values instead of coercing tuples or numeric mapping keys.
    """
    try:
        _json_value(value)
        return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError("invalid JSON value") from None


def sha256_json(value: Any) -> str:
    """Hash the exact canonical UTF-8 serialization, including its final LF."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, fields: set[str]) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ContractError("invalid object fields")


def _text(value: Any) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError("invalid nonempty text")


def _list(value: Any, *, nonempty: bool = False) -> None:
    if type(value) is not list or (nonempty and not value):
        raise ContractError("invalid list")


def _choice(value: Any, choices: Any) -> None:
    if type(value) is not str or value not in choices:
        raise ContractError("invalid enum")


def _header(value: dict[str, Any], schema: str) -> None:
    if (value["schema_version"] != schema
            or value["protocol_version"] != PROTOCOL_VERSION
            or value["data_class"] != "synthetic"):
        raise ContractError("unsupported contract version or data class")


def _claim_unique(value: Any, seen: set[Any]) -> None:
    if value in seen:
        raise ContractError("duplicate identity")
    seen.add(value)


def validate_truth_catalog(value: dict[str, Any]) -> None:
    """Validate source-independent truth and explicit representation mappings.

    No reconciliation is inferred. Distinct provisional entities stay distinct;
    assigning multiple sources to one verified truth is explicit in mappings.
    """
    canonical_json_bytes(value)
    _object(value, {"schema_version", "protocol_version", "data_class",
                    "catalog_version", "truths", "example_mappings",
                    "representation_assessments"})
    _header(value, "goal5c.truth_catalog.v1")
    _text(value["catalog_version"])
    for field in ("truths", "example_mappings", "representation_assessments"):
        _list(value[field])
    truths: dict[str, dict[str, Any]] = {}
    truth_keys: set[Any] = set()
    for row in value["truths"]:
        _object(row, {"truth_id", "query_id", "document_entity_id",
                      "document_version_id", "rubric_version", "label"})
        for field in ("truth_id", "query_id", "document_entity_id",
                      "document_version_id", "rubric_version"):
            _text(row[field])
        _choice(row["label"], {"relevant", "not_relevant", "uncertain"})
        if row["truth_id"] in truths:
            raise ContractError("duplicate truth identifier")
        _claim_unique(tuple(row[field] for field in (
            "query_id", "document_entity_id", "document_version_id", "rubric_version"
        )), truth_keys)
        truths[row["truth_id"]] = row
    mappings: dict[str, dict[str, Any]] = {}
    for row in value["example_mappings"]:
        _object(row, {"example_id", "source_type", "query_id", "truth_id"})
        for field in ("example_id", "query_id", "truth_id"):
            _text(row[field])
        _choice(row["source_type"], SOURCE_TYPES)
        truth = truths.get(row["truth_id"])
        if truth is None or truth["query_id"] != row["query_id"]:
            raise ContractError("invalid truth reference")
        if row["example_id"] in mappings:
            raise ContractError("duplicate example mapping")
        mappings[row["example_id"]] = row
    representation_keys: set[Any] = set()
    for row in value["representation_assessments"]:
        _object(row, {"representation_id", "example_id", "evidence_basis", "assessment"})
        _text(row["representation_id"])
        _text(row["example_id"])
        if row["example_id"] not in mappings:
            raise ContractError("invalid representation reference")
        _choice(row["evidence_basis"], {"title_snippet", "abstract", "full_document", "unavailable"})
        _choice(row["assessment"], {"supports_relevant", "supports_not_relevant", "insufficient_information"})
        if (row["evidence_basis"] == "unavailable"
                and row["assessment"] != "insufficient_information"):
            raise ContractError("unavailable evidence cannot support a judgment")
        _claim_unique((row["example_id"], row["representation_id"]), representation_keys)


_SCOPES = {
    "fp1_core": ("First-attempt frozen-pool core-scorer benchmark", "first_attempt"),
    "observed_gate": ("Observed production-gate audit", "recorded_gate_attempts"),
    "workflow_observed": ("Observed production workflow with policy-directed retries", "policy_directed_attempts"),
}
_GATES = frozenset({"url_gate", "content_gate", "provider_filter", "dedupe",
                    "merge_collision", "merged_rerank", "fill_back", "domain_anchor",
                    "threshold_filter", "truncation"})
_COUNT_METRICS = frozenset({"query_count", "candidate_count", "label_coverage"})
_QUALITY_METRICS = frozenset({"query_macro_precision", "query_macro_recall", "query_macro_f1"})
_RANKING_METRICS = frozenset({"query_macro_average_precision", "query_macro_precision_at_5"})
_RELEVANCE_GATES = frozenset({"content_gate", "provider_filter", "merged_rerank",
                              "fill_back", "domain_anchor", "threshold_filter"})


def _validate_attempt_scope(namespace: str, attempt: Any) -> None:
    if type(attempt) is not int or attempt < 1:
        raise ContractError("invalid attempt ordinal")
    if namespace == "fp1_core" and attempt != 1:
        raise ContractError("first-attempt pool contains retry inputs")


def _validate_family_split(family: str, split: str, assignments: dict[str, str]) -> None:
    if family in assignments and assignments[family] != split:
        raise ContractError("intent family crosses query splits")
    assignments[family] = split


def validate_measurement_contract(value: dict[str, Any]) -> None:
    """Check declared scope and membership without computing any metric.

    Observations declare every candidate, representation and IDF input used by
    the eventual evaluator. Input completeness needs separate extraction checks.
    """
    canonical_json_bytes(value)
    _object(value, {"schema_version", "protocol_version", "data_class", "namespace",
                    "scope_title", "source_type", "boundary", "attempt_scope",
                    "metric_names", "acquisition_version", "pool_version", "config_version",
                    "truth_version", "retry_policy", "query_cohort", "observations",
                    "capture_bundle_sha256", "truth_catalog_sha256"})
    _header(value, "goal5c.measurement.v1")
    for field in ("capture_bundle_sha256", "truth_catalog_sha256"):
        digest = value[field]
        if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContractError("invalid measurement input digest")
    _choice(value["namespace"], _SCOPES)
    _choice(value["source_type"], SOURCE_TYPES)
    namespace = value["namespace"]
    title, attempt_scope = _SCOPES[namespace]
    if value["scope_title"] != title or value["attempt_scope"] != attempt_scope:
        raise ContractError("measurement scope mismatch")
    for field in ("acquisition_version", "pool_version", "config_version", "truth_version"):
        _text(value[field])
    if namespace == "fp1_core":
        if value["boundary"] != "core_scorer" or value["retry_policy"] is not None:
            raise ContractError("first-attempt boundary mismatch")
    else:
        _text(value["retry_policy"])
        _choice(value["boundary"], _GATES if namespace == "observed_gate" else {"final_evidence"})
    prefix = f"{namespace}.{value['source_type'].lower()}."
    if namespace != "fp1_core":
        prefix += value["boundary"] + "."
    metrics = _COUNT_METRICS
    if namespace != "observed_gate" or value["boundary"] in _RELEVANCE_GATES:
        metrics |= _QUALITY_METRICS
    if namespace == "fp1_core":
        metrics |= _RANKING_METRICS
    _list(value["metric_names"], nonempty=True)
    metric_names: set[Any] = set()
    for name in value["metric_names"]:
        _choice(name, {prefix + metric for metric in metrics})
        _claim_unique(name, metric_names)
    _list(value["query_cohort"], nonempty=True)
    queries: set[Any] = set()
    families: dict[str, str] = {}
    for row in value["query_cohort"]:
        _object(row, {"query_id", "intent_family_id", "split"})
        _text(row["query_id"])
        _text(row["intent_family_id"])
        _choice(row["split"], {"discovery", "confirmatory", "pilot"})
        _claim_unique(row["query_id"], queries)
        _validate_family_split(row["intent_family_id"], row["split"], families)
    _list(value["observations"])
    observations: set[Any] = set()
    for row in value["observations"]:
        _object(row, {"query_id", "example_id", "attempt", "role", "execution_id",
                      "representation_id", "raw_event_id"})
        for field in ("query_id", "example_id", "execution_id", "representation_id", "raw_event_id"):
            _text(row[field])
        if row["query_id"] not in queries:
            raise ContractError("observation outside query cohort")
        _validate_attempt_scope(namespace, row["attempt"])
        _choice(row["role"], {"candidate", "representation", "idf"})
        _claim_unique((row["raw_event_id"], row["representation_id"], row["role"]), observations)


def _index_rows(rows: Any, key: str) -> dict[str, dict[str, Any]]:
    _list(rows)
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if type(row) is not dict or key not in row:
            raise ContractError("invalid bundle reference row")
        _text(row[key])
        if row[key] in indexed:
            raise ContractError("ambiguous bundle reference")
        indexed[row[key]] = row
    return indexed


def _require_match(condition: bool) -> None:
    if not condition:
        raise ContractError("measurement input reference mismatch")


def validate_measurement_inputs(
    contract: dict[str, Any], bundle: dict[str, Any], truth_catalog: dict[str, Any],
) -> None:
    """Bind measurement declarations to imported events and shared truth.

    This checks references, not raw-snapshot authenticity, reconciliation quality,
    pool completeness or final workflow selection. Import the snapshot first.
    """
    validate_measurement_contract(contract)
    validate_truth_catalog(truth_catalog)
    canonical_json_bytes(bundle)
    try:
        _header(bundle, "goal5c.capture_bundle.v1")
        _require_match(contract["capture_bundle_sha256"] == sha256_json(bundle))
        _require_match(contract["truth_catalog_sha256"] == sha256_json(truth_catalog))
        _require_match(bundle["capture_commit"] == CAPTURE_COMMIT)
        _require_match(contract["truth_version"] == truth_catalog["catalog_version"])
        queries = _index_rows(bundle["queries"], "query_id")
        executions = _index_rows(bundle["executions"], "execution_id")
        events = _index_rows(bundle["raw_events"], "raw_event_id")
        examples = _index_rows(bundle["examples"], "example_id")
        representations = _index_rows(bundle["representations"], "representation_id")
        mappings = _index_rows(truth_catalog["example_mappings"], "example_id")
        for mapping in mappings.values():
            example = examples[mapping["example_id"]]
            _require_match(mapping["query_id"] == example["query_id"]
                           and mapping["source_type"] == example["source_type"]
                           and mapping["query_id"] in queries)
        for assessment in truth_catalog["representation_assessments"]:
            representation = representations[assessment["representation_id"]]
            _require_match(representation["example_id"] == assessment["example_id"])
        for query in contract["query_cohort"]:
            _require_match(query["intent_family_id"] == queries[query["query_id"]]["intent_family_id"])
        for row in contract["observations"]:
            example = examples[row["example_id"]]
            execution = executions[row["execution_id"]]
            event = events[row["raw_event_id"]]
            captured = event["captured"]
            representation = representations[row["representation_id"]]
            mapping = mappings[row["example_id"]]
            _require_match(example["query_id"] == execution["query_id"] == row["query_id"]
                           == mapping["query_id"])
            _require_match(example["source_type"] == execution["source_type"]
                           == captured["source_type"] == contract["source_type"])
            _require_match(type(execution["attempt"]) is int and type(captured["attempt"]) is int
                           and execution["attempt"] == captured["attempt"] == row["attempt"])
            _require_match(event["execution_id"] == row["execution_id"]
                           and captured["run_id"] == execution["run_id"]
                           and captured["session_id"] == execution["session_id"])
            _require_match(captured["query_fingerprint"] == queries[row["query_id"]]["query_fingerprint"]
                           and captured["query_envelope_hash"] == execution["query_envelope_hash"])
            _require_match(captured["example_id"] == representation["example_id"] == row["example_id"])
            _require_match(type(captured["dataset_eligible"]) is int and captured["dataset_eligible"] == 1)
            _require_match(captured["title"] == representation["title"]
                           and captured["score_text"] == representation["score_text"])
            _require_match(row["raw_event_id"] in representation["raw_event_ids"]
                           and row["raw_event_id"] in example["raw_event_ids"]
                           and row["representation_id"] in example["representation_ids"])
            if contract["namespace"] == "fp1_core":
                _require_match(row["raw_event_id"] in representation["fp1_provenance_event_ids"]
                               and not execution["fp1_exclusion_reasons"])
            if contract["namespace"] == "observed_gate":
                _require_match(captured["decision_stage"] == contract["boundary"])
    except (KeyError, TypeError, IndexError):
        raise ContractError("invalid measurement input references") from None
