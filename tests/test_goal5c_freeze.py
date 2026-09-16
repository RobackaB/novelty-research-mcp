"""Synthetic preregistration, sampling independence and gate negative controls."""

from __future__ import annotations

import copy
import hashlib
import hmac

import pytest

from eval.goal5c import freeze as gf
from eval.goal5c.contracts import canonical_json_bytes, sha256_json
from tools.decision_capture import query_fingerprint


def synthetic_registry():
    rows = []
    for stratum in gf.STRATA:
        for number in range(14):
            identifier = f"{stratum}-{number:02}"
            text = f"Synthetic {stratum} information need {number}"
            rows.append({
                "query_id": identifier, "original_query": text,
                "query_fingerprint": query_fingerprint(text),
                "intent_family_id": "family-" + identifier,
                "stratum": stratum, "language": "en",
                "topic_card": "Fictional fixture need " + identifier,
                "approved_translation": "", "registered_at": "2026-01-01T00:00:00Z",
                "provenance": {"kind": "synthetic_fixture", "fixture_reference": "fictional-fixture.v1"},
            })
    return {
        "schema_version": gf.REGISTRY_VERSION, "protocol_version": gf.PROTOCOL_VERSION,
        "data_class": "synthetic", "registry_version": "synthetic.registry.v1", "queries": rows,
    }


def synthetic_plan(registry):
    return {
        "schema_version": gf.PLAN_VERSION, "protocol_version": gf.PROTOCOL_VERSION,
        "data_class": "synthetic", "freeze_version": "synthetic.freeze.v1",
        "registry_sha256": sha256_json(registry), "sampling_seed": "12" * 32,
        "pilot_query_ids": [f"{stratum}-00" for stratum in gf.STRATA],
        "study_quotas": dict.fromkeys(gf.STRATA, 10),
        "frozen_at": "2026-01-02T00:00:00Z",
        "pilot_window": {"start": "2026-01-03T00:00:00Z", "end": "2026-01-04T00:00:00Z"},
        "study_window": {"start": "2026-01-05T00:00:00Z", "end": "2026-01-06T00:00:00Z"},
        "outcomes_observed": False,
    }


def _ids(rows):
    return [row["query_id"] for row in rows]


def _roster(result):
    return [_ids(result[field]) for field in ("pilot_queries", "discovery_queries", "ordered_reserves")]


def test_freeze_accounts_for_every_original_once_with_separate_pilots_and_reserves():
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    before = copy.deepcopy((registry, plan))
    result = gf.freeze_registry(registry, plan)
    pilots, discovery, reserves = _roster(result)
    assert (len(pilots), len(discovery), len(reserves)) == (3, 30, 9)
    assert len(set(pilots + discovery + reserves)) == 42
    assert set(pilots + discovery + reserves) == set(_ids(registry["queries"]))
    assert set(pilots) == set(plan["pilot_query_ids"])
    assert result["registry_sha256"] == sha256_json(registry)
    assert result["freeze_plan_sha256"] == sha256_json(plan)
    assert (registry, plan) == before
    assert canonical_json_bytes(result) == canonical_json_bytes(gf.freeze_registry(registry, plan))


def test_sampling_order_matches_independently_computed_hmac_and_fixed_stratum_quotas():
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    result = gf.freeze_registry(registry, plan)
    for stratum in gf.STRATA:
        candidates = [row for row in registry["queries"] if row["stratum"] == stratum
                      and row["query_id"] not in plan["pilot_query_ids"]]

        def expected_key(row):
            payload = canonical_json_bytes(["stratified_hmac_original_query.v1", stratum, row["query_fingerprint"]])
            digest = hmac.new(bytes.fromhex(plan["sampling_seed"]), payload, hashlib.sha256).hexdigest()
            return digest, row["query_id"]

        ordered = sorted(candidates, key=expected_key)
        assert [row for row in result["discovery_queries"] if row["stratum"] == stratum] == ordered[:10]
        assert [row for row in result["ordered_reserves"] if row["stratum"] == stratum] == ordered[10:]


def test_row_and_pilot_order_do_not_change_roster_but_parent_digest_preserves_input_history():
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    original = gf.freeze_registry(registry, plan)
    registry["queries"].reverse()
    plan["registry_sha256"] = sha256_json(registry)
    plan["pilot_query_ids"].reverse()
    reordered = gf.freeze_registry(registry, plan)
    assert _roster(original) == _roster(reordered)
    assert original["registry_sha256"] != reordered["registry_sha256"]
    assert original["freeze_plan_sha256"] != reordered["freeze_plan_sha256"]


def test_sampling_seed_changes_selection_and_is_reproducible():
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    original = gf.freeze_registry(registry, plan)
    plan["sampling_seed"] = "98" * 32
    changed = gf.freeze_registry(registry, plan)
    assert set(_ids(original["discovery_queries"])) != set(_ids(changed["discovery_queries"]))
    assert _ids(original["pilot_queries"]) == _ids(changed["pilot_queries"])
    assert changed == gf.freeze_registry(registry, plan)


def test_rehearsal_does_not_authorize_execution_or_claim_pilot_labels_metrics_confirmation():
    registry = synthetic_registry()
    result = gf.freeze_registry(registry, synthetic_plan(registry))
    assert result["scope"] == "synthetic_preregistration_rehearsal"
    assert result["sampling_unit"] == "original_query"
    assert result["data_class"] == "synthetic"
    for key in ("real_execution_authorized", "actual_pilot_completed",
                "empirical_protocol_freeze_completed", "confirmatory_threshold_selection_authorized"):
        assert result[key] is False
    assert {gate["gate"] for gate in result["operational_gates"]} == set(gf.OPERATIONAL_GATES)
    assert all(gate["status"] == "not_verified" for gate in result["operational_gates"])
    execution = result["planned_execution"]
    assert execution["observed_execution_count"] == 0
    assert execution["automatic_reserve_promotion"] is False
    assert execution["fresh_process_per_original_query"] is True
    assert execution["retry_policy"] == "normal_checklist_directed_only"
    assert execution["same_query_reacquisition"] == "later_reviewed_technical_failure_only_maximum_one"
    assert not {"labels", "metrics", "candidate_count", "scores", "confirmatory_queries"} & set(result)


def test_planned_source_budgets_match_current_production_defaults():
    from tools.research_session import MAX_ATTEMPTS_BY_SOURCE_DEFAULT

    registry = synthetic_registry()
    execution = gf.freeze_registry(registry, synthetic_plan(registry))["planned_execution"]
    assert execution["sources_per_query"] == ["patent", "publication", "web"]
    assert execution["maximum_attempts_per_source"] == MAX_ATTEMPTS_BY_SOURCE_DEFAULT


@pytest.mark.parametrize("quotas", [
    {"invention": 9, "scientific": 8, "operational": 8},
    {"invention": 9, "scientific": 9, "operational": 8},
    {"invention": 9, "scientific": 9, "operational": 9},
    {"invention": 10, "scientific": 9, "operational": 9},
    {"invention": 10, "scientific": 10, "operational": 9},
    {"invention": 10, "scientific": 10, "operational": 10},
])
def test_balanced_preregistered_cohorts_from_25_to_30_are_allowed(quotas):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    plan["study_quotas"] = quotas
    result = gf.freeze_registry(registry, plan)
    assert len(result["discovery_queries"]) == sum(quotas.values())
    assert len(result["ordered_reserves"]) == 39 - sum(quotas.values())


@pytest.mark.parametrize("quotas", [
    {"invention": 11, "scientific": 10, "operational": 9},
    dict.fromkeys(gf.STRATA, 8), dict.fromkeys(gf.STRATA, 11),
    {"invention": True, "scientific": 12, "operational": 12},
    {"invention": 10.0, "scientific": 10, "operational": 10},
    {"invention": 0, "scientific": 13, "operational": 13},
    {"invention": -1, "scientific": 13, "operational": 13},
    {"invention": 10, "scientific": 10},
    {**dict.fromkeys(gf.STRATA, 10), "extra": 1}, [],
])
def test_invalid_or_imbalanced_allocation_is_rejected(quotas):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    plan["study_quotas"] = quotas
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


@pytest.mark.parametrize("field", ["query_id", "query_fingerprint", "intent_family_id"])
def test_global_identity_duplicates_are_rejected_including_pilot_study_overlap(field):
    registry = synthetic_registry()
    registry["queries"][1][field] = registry["queries"][0][field]
    if field == "query_fingerprint":
        registry["queries"][1]["original_query"] = registry["queries"][0]["original_query"]
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, synthetic_plan(registry))


@pytest.mark.parametrize("equivalent", ["NEED\tFOR\nINVENTION", "Ｎｅｅｄ for invention", "need for invention"])
def test_normalized_query_duplicates_use_established_nfkc_whitespace_lower_identity(equivalent):
    registry = synthetic_registry()
    for row, text in zip(registry["queries"], ["Need for invention", equivalent]):
        row["original_query"] = text
        row["query_fingerprint"] = query_fingerprint(text)
    with pytest.raises(gf.ContractError):
        gf.validate_registry(registry)


def test_lower_identity_does_not_accidentally_casefold_sharp_s():
    registry = synthetic_registry()
    for row, text in zip(registry["queries"], ["Straße", "STRASSE"]):
        row["original_query"] = text
        row["query_fingerprint"] = query_fingerprint(text)
    gf.validate_registry(registry)
    assert registry["queries"][0]["query_fingerprint"] != registry["queries"][1]["query_fingerprint"]


@pytest.mark.parametrize("field,value", [
    ("query_fingerprint", "0" * 64), ("query_id", "bad id"), ("intent_family_id", ""),
    ("stratum", "patent"), ("stratum", []), ("language", ""), ("topic_card", ""),
    ("original_query", ""), ("approved_translation", None),
    ("provenance", {"kind": "participant", "fixture_reference": "claimed"}),
])
def test_invalid_original_query_declarations_are_rejected(field, value):
    registry = synthetic_registry()
    registry["queries"][0][field] = value
    with pytest.raises(gf.ContractError):
        gf.validate_registry(registry)


@pytest.mark.parametrize("value", ["2026-01-01", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00",
                                    "2026-01-01T00:00:00.001Z", "2026-02-30T00:00:00Z",
                                    "2026-01-01T24:00:00Z", "2026-01-01T00:00:60Z", 0, None])
@pytest.mark.parametrize("location", ["registered_at", "frozen_at", "pilot_start", "study_end"])
def test_timestamps_require_valid_explicit_second_precision_utc(value, location):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    if location == "registered_at":
        registry["queries"][0][location] = value
        plan["registry_sha256"] = sha256_json(registry)
    elif location == "frozen_at":
        plan[location] = value
    else:
        prefix, boundary = location.split("_")
        plan[prefix + "_window"][boundary] = value
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


@pytest.mark.parametrize("boundary", ["registered_after_freeze", "freeze_at_pilot_start", "pilot_reversed",
                                       "windows_touch", "study_reversed"])
def test_freeze_pilot_and_study_schedule_is_strictly_ordered(boundary):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    if boundary == "registered_after_freeze":
        registry["queries"][0]["registered_at"] = "2026-01-02T00:00:01Z"
        plan["registry_sha256"] = sha256_json(registry)
    elif boundary == "freeze_at_pilot_start":
        plan["frozen_at"] = plan["pilot_window"]["start"]
    elif boundary == "pilot_reversed":
        plan["pilot_window"]["end"] = "2026-01-02T00:00:00Z"
    elif boundary == "windows_touch":
        plan["study_window"]["start"] = plan["pilot_window"]["end"]
    else:
        plan["study_window"]["end"] = plan["study_window"]["start"]
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


@pytest.mark.parametrize("pilots", [[], ["invention-00"], ["invention-00"] * 3,
                                     ["invention-00", "scientific-00", "missing"],
                                     ["invention-00", "scientific-00", "operational-00", "invention-01"]])
def test_pilots_are_exactly_three_distinct_registered_originals(pilots):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    plan["pilot_query_ids"] = pilots
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


def test_insufficient_nonpilot_stratum_cannot_borrow_other_strata_or_promote_pilots():
    registry = synthetic_registry()
    registry["queries"] = [row for row in registry["queries"]
                           if row["stratum"] != "invention" or int(row["query_id"].split("-")[-1]) < 10]
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, synthetic_plan(registry))


@pytest.mark.parametrize("field,value", [
    ("sampling_seed", "A" * 64), ("sampling_seed", "12"), ("sampling_seed", 12),
    ("registry_sha256", "0" * 64), ("outcomes_observed", True), ("outcomes_observed", 0),
    ("outcomes_observed", None), ("freeze_version", ""),
])
def test_freeze_rejects_wrong_parent_seed_and_post_outcome_declarations(field, value):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    plan[field] = value
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


@pytest.mark.parametrize("target", ["registry", "query", "plan"])
@pytest.mark.parametrize("field,value", [
    ("candidate_count", 100), ("retry_count", 2), ("labels", ["relevant"]),
    ("participant_email", "fixture@example.invalid"), ("maximum_attempts_per_source", {"web": 20}),
    ("automatic_reserve_promotion", True), ("metric", 1.0),
])
def test_outcomes_private_identity_execution_overrides_are_not_contract_fields(target, field, value):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    destination = {"registry": registry, "query": registry["queries"][0], "plan": plan}[target]
    destination[field] = value
    plan["registry_sha256"] = sha256_json(registry)
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


@pytest.mark.parametrize("target", ["registry", "plan"])
@pytest.mark.parametrize("field,value", [("data_class", "real"), ("data_class", "private"),
                                         ("schema_version", "future.v2"), ("protocol_version", "goal5c.protocol.v2")])
def test_only_versioned_synthetic_inputs_are_accepted(target, field, value):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    {"registry": registry, "plan": plan}[target][field] = value
    plan["registry_sha256"] = sha256_json(registry)
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)


def test_negative_control_removing_uniqueness_admits_pilot_family_contamination(monkeypatch):
    registry = synthetic_registry()
    registry["queries"][1]["intent_family_id"] = registry["queries"][0]["intent_family_id"]
    plan = synthetic_plan(registry)
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)
    monkeypatch.setattr(gf, "_unique", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gf.ContractError):
            gf.freeze_registry(registry, plan)


def test_negative_control_removing_allocation_guard_admits_imbalanced_cohort(monkeypatch):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    plan["study_quotas"] = {"invention": 13, "scientific": 9, "operational": 8}
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)
    monkeypatch.setattr(gf, "_validate_allocation", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gf.ContractError):
            gf.freeze_registry(registry, plan)


def test_negative_control_constant_sampling_key_restores_row_order_dependence(monkeypatch):
    def ordering_oracle():
        registry = synthetic_registry()
        plan = synthetic_plan(registry)
        original = gf.freeze_registry(registry, plan)
        registry["queries"].reverse()
        plan["registry_sha256"] = sha256_json(registry)
        assert _roster(original) == _roster(gf.freeze_registry(registry, plan))

    ordering_oracle()
    monkeypatch.setattr(gf, "_sampling_key", lambda *args: ("", ""))
    with pytest.raises(AssertionError):
        ordering_oracle()


def test_negative_control_removing_synthetic_header_guard_admits_real_declarations(monkeypatch):
    registry = synthetic_registry()
    registry["data_class"] = "real"
    plan = synthetic_plan(registry)
    plan["data_class"] = "real"
    with pytest.raises(gf.ContractError):
        gf.freeze_registry(registry, plan)
    monkeypatch.setattr(gf, "_header", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gf.ContractError):
            gf.freeze_registry(registry, plan)
