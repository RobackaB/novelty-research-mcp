"""Synthetic preregistration rehearsal; no recruitment or acquisition authority."""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import re
from typing import Any

from tools.decision_capture import query_fingerprint

from .contracts import (
    ContractError, PROTOCOL_VERSION, _header, _list, _object, _text,
    canonical_json_bytes, sha256_json,
)

REGISTRY_VERSION = "goal5c.query_registry.v1"
PLAN_VERSION = "goal5c.freeze_plan.v1"
FREEZE_VERSION = "goal5c.synthetic_freeze.v1"
SAMPLING_RULE = "stratified_hmac_original_query.v1"
STRATA = ("invention", "scientific", "operational")
PLANNED_SOURCE_BUDGETS = {"patent": 4, "publication": 3, "web": 3}
OPERATIONAL_GATES = (
    "named_data_steward", "institutional_and_processing_basis_review",
    "participant_materials_and_consent", "secure_storage_and_access",
    "annotator_approval", "registry_identity_separation", "retention_and_withdrawal",
    "semantic_preserving_redaction_review",
)


def _require(condition: bool) -> None:
    if not condition:
        raise ContractError("invalid synthetic freeze declaration")


def _identifier(value: Any) -> None:
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) is not None)


def _utc(value: Any) -> datetime:
    _require(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is not None)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ContractError("invalid declared UTC time") from None


def _unique(value: str, seen: set[str]) -> None:
    _require(value not in seen)
    seen.add(value)


def validate_registry(registry: dict) -> None:
    """Validate synthetic original-query declarations, never participant records."""
    canonical_json_bytes(registry)
    _object(registry, {"schema_version", "protocol_version", "data_class", "registry_version", "queries"})
    _header(registry, REGISTRY_VERSION)
    _identifier(registry["registry_version"])
    _list(registry["queries"], nonempty=True)
    ids, fingerprints, families = set(), set(), set()
    for row in registry["queries"]:
        _object(row, {"query_id", "original_query", "query_fingerprint", "intent_family_id",
                      "stratum", "language", "topic_card", "approved_translation",
                      "provenance", "registered_at"})
        for field in ("query_id", "intent_family_id"):
            _identifier(row[field])
        for field in ("original_query", "language", "topic_card"):
            _text(row[field])
        _require(type(row["approved_translation"]) is str)
        _require(type(row["stratum"]) is str and row["stratum"] in STRATA)
        _require(row["query_fingerprint"] == query_fingerprint(row["original_query"]))
        _object(row["provenance"], {"kind", "fixture_reference"})
        _require(row["provenance"]["kind"] == "synthetic_fixture")
        _text(row["provenance"]["fixture_reference"])
        _utc(row["registered_at"])
        _unique(row["query_id"], ids)
        _unique(row["query_fingerprint"], fingerprints)
        # Global uniqueness includes pilots, selected originals and reserves.
        _unique(row["intent_family_id"], families)


def _validate_allocation(quotas: Any) -> None:
    _object(quotas, set(STRATA))
    _require(all(type(value) is int and value > 0 for value in quotas.values()))
    _require(25 <= sum(quotas.values()) <= 30)
    _require(max(quotas.values()) - min(quotas.values()) <= 1)


def validate_freeze_plan(plan: dict, registry: dict) -> None:
    """Check a declared pre-outcome schedule and allocation, not their authenticity."""
    validate_registry(registry)
    canonical_json_bytes(plan)
    _object(plan, {"schema_version", "protocol_version", "data_class", "freeze_version",
                   "registry_sha256", "sampling_seed", "pilot_query_ids", "study_quotas",
                   "frozen_at", "pilot_window", "study_window", "outcomes_observed"})
    _header(plan, PLAN_VERSION)
    _identifier(plan["freeze_version"])
    _require(plan["registry_sha256"] == sha256_json(registry))
    _require(type(plan["sampling_seed"]) is str
             and re.fullmatch(r"[0-9a-f]{64}", plan["sampling_seed"]) is not None)
    _require(plan["outcomes_observed"] is False)
    _validate_allocation(plan["study_quotas"])
    _list(plan["pilot_query_ids"])
    _require(len(plan["pilot_query_ids"]) == 3)
    for identifier in plan["pilot_query_ids"]:
        _identifier(identifier)
    _require(len(set(plan["pilot_query_ids"])) == 3)
    rows = {row["query_id"]: row for row in registry["queries"]}
    _require(set(plan["pilot_query_ids"]) <= set(rows))
    for field in ("pilot_window", "study_window"):
        _object(plan[field], {"start", "end"})
    frozen = _utc(plan["frozen_at"])
    _require(all(_utc(row["registered_at"]) <= frozen for row in rows.values()))
    _require(frozen < _utc(plan["pilot_window"]["start"]) < _utc(plan["pilot_window"]["end"])
             < _utc(plan["study_window"]["start"]) < _utc(plan["study_window"]["end"]))
    for stratum in STRATA:
        count = sum(row["stratum"] == stratum and row["query_id"] not in plan["pilot_query_ids"]
                    for row in rows.values())
        _require(count >= plan["study_quotas"][stratum])


def _sampling_key(row: dict, seed: str) -> tuple[str, str]:
    message = canonical_json_bytes([SAMPLING_RULE, row["stratum"], row["query_fingerprint"]])
    return hmac.new(bytes.fromhex(seed), message, hashlib.sha256).hexdigest(), row["query_id"]


def freeze_registry(registry: dict, plan: dict) -> dict:
    """Return a deterministic rehearsal roster with all real execution disabled.

    This samples originals only. No candidate counts, retries or observed outcomes
    enter selection. Caller must keep all input/output study artifacts outside Git.
    """
    validate_freeze_plan(plan, registry)
    rows = {row["query_id"]: row for row in registry["queries"]}
    pilot_ids = set(plan["pilot_query_ids"])
    study, reserves = [], []
    for stratum in STRATA:
        ordered = sorted((row for row in rows.values() if row["stratum"] == stratum
                          and row["query_id"] not in pilot_ids),
                         key=lambda row: _sampling_key(row, plan["sampling_seed"]))
        count = plan["study_quotas"][stratum]
        study.extend(ordered[:count])
        reserves.extend(ordered[count:])
    return {
        "schema_version": FREEZE_VERSION, "protocol_version": PROTOCOL_VERSION,
        "data_class": "synthetic", "freeze_version": plan["freeze_version"],
        "scope": "synthetic_preregistration_rehearsal", "sampling_unit": "original_query",
        "sampling_rule": SAMPLING_RULE, "sampling_seed": plan["sampling_seed"],
        "registry_sha256": sha256_json(registry), "freeze_plan_sha256": sha256_json(plan),
        "frozen_at": plan["frozen_at"], "pilot_window": plan["pilot_window"],
        "study_window": plan["study_window"], "study_quotas": plan["study_quotas"],
        "pilot_queries": [rows[identifier] for identifier in sorted(pilot_ids)],
        "discovery_queries": study, "ordered_reserves": reserves,
        "planned_execution": {
            "sources_per_query": sorted(PLANNED_SOURCE_BUDGETS),
            "maximum_attempts_per_source": dict(PLANNED_SOURCE_BUDGETS),
            "retry_policy": "normal_checklist_directed_only",
            "fresh_process_per_original_query": True,
            "observed_execution_count": 0,
            "automatic_reserve_promotion": False,
            "same_query_reacquisition": "later_reviewed_technical_failure_only_maximum_one",
        },
        "real_execution_authorized": False,
        "operational_gates": [{"gate": gate, "status": "not_verified"} for gate in OPERATIONAL_GATES],
        "actual_pilot_completed": False, "empirical_protocol_freeze_completed": False,
        "confirmatory_threshold_selection_authorized": False,
    }
