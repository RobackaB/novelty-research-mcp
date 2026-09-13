"""Synthetic contract validation and explicit guard-removal negative controls."""

from __future__ import annotations

import copy
import hashlib

import pytest

from eval.goal5c import contracts as gc


def truth_catalog():
    return {
        "schema_version": "goal5c.truth_catalog.v1",
        "protocol_version": gc.PROTOCOL_VERSION,
        "data_class": "synthetic",
        "catalog_version": "synthetic.truth.v1",
        "truths": [{
            "truth_id": "truth-1", "query_id": "query-1",
            "document_entity_id": "document-1", "document_version_id": "version-1",
            "rubric_version": "rubric.v1", "label": "relevant",
        }],
        "example_mappings": [
            {"example_id": "publication-example", "source_type": "publication", "query_id": "query-1", "truth_id": "truth-1"},
            {"example_id": "web-example", "source_type": "web", "query_id": "query-1", "truth_id": "truth-1"},
        ],
        "representation_assessments": [
            {"representation_id": "abstract-1", "example_id": "publication-example", "evidence_basis": "abstract", "assessment": "supports_relevant"},
            {"representation_id": "snippet-1", "example_id": "web-example", "evidence_basis": "title_snippet", "assessment": "insufficient_information"},
        ],
    }


def measurement(namespace="fp1_core"):
    scope = {
        "fp1_core": ("First-attempt frozen-pool core-scorer benchmark", "first_attempt", "core_scorer", None),
        "observed_gate": ("Observed production-gate audit", "recorded_gate_attempts", "merged_rerank", "retry.v1"),
        "workflow_observed": ("Observed production workflow with policy-directed retries", "policy_directed_attempts", "final_evidence", "retry.v1"),
    }
    title, attempt_scope, boundary, retry_policy = scope[namespace]
    prefix = namespace + ".publication."
    if namespace != "fp1_core":
        prefix += boundary + "."
    return {
        "schema_version": "goal5c.measurement.v1", "protocol_version": gc.PROTOCOL_VERSION,
        "data_class": "synthetic", "namespace": namespace, "scope_title": title,
        "source_type": "publication", "boundary": boundary, "attempt_scope": attempt_scope,
        "metric_names": [prefix + "query_macro_f1"],
        "acquisition_version": "synthetic.acquisition.v1", "pool_version": "synthetic.pool.v1",
        "config_version": "synthetic.config.v1", "truth_version": "synthetic.truth.v1",
        "capture_bundle_sha256": "1" * 64,
        "truth_catalog_sha256": "2" * 64,
        "retry_policy": retry_policy,
        "query_cohort": [{"query_id": "query-1", "intent_family_id": "family-1", "split": "discovery"}],
        "observations": [{"query_id": "query-1", "example_id": "publication-example", "attempt": 1,
                          "role": "candidate", "execution_id": "execution-1",
                          "representation_id": "abstract-1", "raw_event_id": "snapshot:1"}],
    }


def test_canonical_utf8_digest_is_exact_and_preserves_list_order():
    expected = '{"a":[2,1],"z":"ž"}\n'.encode("utf-8")
    assert gc.canonical_json_bytes({"z": "ž", "a": [2, 1]}) == expected
    assert gc.sha256_json({"z": "ž", "a": [2, 1]}) == hashlib.sha256(expected).hexdigest()
    assert gc.sha256_json({"a": [1, 2], "z": "ž"}) != gc.sha256_json({"a": [2, 1], "z": "ž"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1, 2), {1: "numeric key"}, {"x": object()}, "\ud800"])
def test_canonical_serialization_rejects_non_json_without_payload_leak(value):
    with pytest.raises(gc.ContractError, match="^invalid JSON value$"):
        gc.canonical_json_bytes(value)


def test_canonical_serialization_rejects_cycle():
    value = []
    value.append(value)
    with pytest.raises(gc.ContractError, match="^invalid JSON value$"):
        gc.canonical_json_bytes(value)


def test_one_document_truth_serves_both_sources_without_mutating_input():
    value = truth_catalog()
    original = copy.deepcopy(value)
    gc.validate_truth_catalog(value)
    assert value == original
    assert len({row["truth_id"] for row in value["example_mappings"]}) == 1
    assert {row["assessment"] for row in value["representation_assessments"]} == {
        "supports_relevant", "insufficient_information",
    }


@pytest.mark.parametrize("label", ["relevant", "not_relevant"])
def test_duplicate_document_truth_is_rejected_even_with_another_identifier(label):
    value = truth_catalog()
    value["truths"].append(dict(value["truths"][0], truth_id="other", label=label))
    with pytest.raises(gc.ContractError, match="duplicate identity"):
        gc.validate_truth_catalog(value)


def test_different_document_versions_remain_distinct():
    value = truth_catalog()
    value["truths"].append(dict(value["truths"][0], truth_id="new-version", document_version_id="version-2", label="not_relevant"))
    gc.validate_truth_catalog(value)


@pytest.mark.parametrize("field", ["source_type", "provider", "score", "retained", "candidate_identity"])
def test_authoritative_truth_cannot_include_pipeline_metadata(field):
    value = truth_catalog()
    value["truths"][0][field] = "private request"
    with pytest.raises(gc.ContractError, match="^invalid object fields$"):
        gc.validate_truth_catalog(value)


@pytest.mark.parametrize("field,new", [("truth_id", "unknown"), ("query_id", "other-query")])
def test_mapping_cannot_cross_query_or_unknown_truth(field, new):
    value = truth_catalog()
    value["example_mappings"][0][field] = new
    with pytest.raises(gc.ContractError, match="invalid truth reference"):
        gc.validate_truth_catalog(value)


def test_example_mapping_cannot_be_duplicated_or_redirected():
    value = truth_catalog()
    value["example_mappings"].append(dict(value["example_mappings"][0], source_type="web"))
    with pytest.raises(gc.ContractError, match="duplicate example mapping"):
        gc.validate_truth_catalog(value)


def test_unavailable_representation_cannot_support_relevance():
    value = truth_catalog()
    value["representation_assessments"][0]["evidence_basis"] = "unavailable"
    with pytest.raises(gc.ContractError, match="unavailable evidence"):
        gc.validate_truth_catalog(value)


def test_representation_assessment_requires_known_example():
    value = truth_catalog()
    value["representation_assessments"][0]["example_id"] = "unknown"
    with pytest.raises(gc.ContractError, match="invalid representation reference"):
        gc.validate_truth_catalog(value)


@pytest.mark.parametrize("factory,validator", [(truth_catalog, gc.validate_truth_catalog), (measurement, gc.validate_measurement_contract)])
@pytest.mark.parametrize("field,new", [("data_class", "private"), ("protocol_version", "future"), ("schema_version", "future")])
def test_contracts_are_versioned_and_synthetic_only(factory, validator, field, new):
    value = factory()
    value[field] = new
    with pytest.raises(gc.ContractError, match="unsupported contract"):
        validator(value)


@pytest.mark.parametrize("namespace", ["fp1_core", "observed_gate", "workflow_observed"])
def test_measurement_namespaces_are_valid_and_input_is_unchanged(namespace):
    value = measurement(namespace)
    if namespace != "fp1_core":
        value["observations"][0]["attempt"] = 3
    original = copy.deepcopy(value)
    gc.validate_measurement_contract(value)
    assert value == original


@pytest.mark.parametrize("field,new", [
    ("scope_title", "Whole-system performance"), ("attempt_scope", "all_attempts"),
    ("boundary", "final_evidence"), ("retry_policy", "retry.v1"),
    ("metric_names", ["workflow_observed.publication.final_evidence.query_macro_f1"]),
    ("metric_names", ["system_f1"]), ("pool_version", ""),
    ("truth_version", ""), ("acquisition_version", ""), ("config_version", ""),
])
def test_first_attempt_cannot_be_presented_as_workflow(field, new):
    value = measurement()
    value[field] = new
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)


@pytest.mark.parametrize("role", ["candidate", "representation", "idf"])
def test_retry_contamination_is_rejected_for_every_scorer_input(role):
    value = measurement()
    value["observations"][0].update(attempt=2, role=role)
    with pytest.raises(gc.ContractError, match="first-attempt pool contains retry inputs"):
        gc.validate_measurement_contract(value)


@pytest.mark.parametrize("attempt", [True, False, 0, -1, 1.0, "1"])
def test_attempt_must_be_a_positive_integer_not_bool(attempt):
    value = measurement("workflow_observed")
    value["observations"][0]["attempt"] = attempt
    with pytest.raises(gc.ContractError, match="invalid attempt ordinal"):
        gc.validate_measurement_contract(value)


def family_leak():
    value = measurement()
    value["query_cohort"].append({"query_id": "query-2", "intent_family_id": "family-1", "split": "confirmatory"})
    return value


def test_related_original_queries_cannot_cross_discovery_confirmation():
    with pytest.raises(gc.ContractError, match="intent family crosses query splits"):
        gc.validate_measurement_contract(family_leak())


def test_query_cannot_appear_twice_under_different_family():
    value = measurement()
    value["query_cohort"].append({"query_id": "query-1", "intent_family_id": "family-2", "split": "confirmatory"})
    with pytest.raises(gc.ContractError, match="duplicate identity"):
        gc.validate_measurement_contract(value)


def test_observation_must_belong_to_registered_cohort():
    value = measurement()
    value["observations"][0]["query_id"] = "other"
    with pytest.raises(gc.ContractError, match="observation outside query cohort"):
        gc.validate_measurement_contract(value)


@pytest.mark.parametrize("namespace", ["observed_gate", "workflow_observed"])
def test_actual_workflow_scope_requires_retry_policy(namespace):
    value = measurement(namespace)
    value["retry_policy"] = None
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)


@pytest.mark.parametrize("boundary", ["url_gate", "dedupe", "merge_collision", "truncation"])
def test_structural_gate_does_not_claim_relevance_metrics(boundary):
    value = measurement("observed_gate")
    value["boundary"] = boundary
    prefix = "observed_gate.publication." + boundary + "."
    value["metric_names"] = [prefix + "query_macro_f1"]
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)
    value["metric_names"] = [prefix + "candidate_count"]
    gc.validate_measurement_contract(value)


@pytest.mark.parametrize("namespace", ["observed_gate", "workflow_observed"])
def test_rank_metrics_require_explicit_frozen_pool_ranking(namespace):
    value = measurement(namespace)
    prefix = namespace + ".publication." + value["boundary"] + "."
    value["metric_names"] = [prefix + "query_macro_average_precision"]
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)


def test_negative_control_removed_attempt_guard_breaks_rejection_oracle(monkeypatch):
    value = measurement()
    value["observations"][0]["attempt"] = 2
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)
    monkeypatch.setattr(gc, "_validate_attempt_scope", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gc.ContractError):
            gc.validate_measurement_contract(value)


def test_negative_control_removed_family_guard_breaks_rejection_oracle(monkeypatch):
    value = family_leak()
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(value)
    monkeypatch.setattr(gc, "_validate_family_split", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gc.ContractError):
            gc.validate_measurement_contract(value)


def test_negative_control_source_specific_truth_duplicates_break_oracle(monkeypatch):
    value = truth_catalog()
    value["truths"].append(dict(value["truths"][0], truth_id="web-truth", label="not_relevant"))
    value["example_mappings"][1]["truth_id"] = "web-truth"
    with pytest.raises(gc.ContractError):
        gc.validate_truth_catalog(value)
    monkeypatch.setattr(gc, "_claim_unique", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gc.ContractError):
            gc.validate_truth_catalog(value)


def bound_measurement_inputs():
    """A minimal synthetic imported bundle plus matching measurement and truth."""
    contract = measurement()
    truth = truth_catalog()
    event = {
        "raw_event_id": "snapshot:1", "execution_id": "execution-1",
        "captured": {
            "example_id": "publication-example", "source_type": "publication", "attempt": 1,
            "run_id": "run-1", "session_id": "session-1", "query_fingerprint": "fingerprint-1",
            "query_envelope_hash": "envelope-1", "dataset_eligible": 1,
            "title": "Synthetic title", "score_text": "Synthetic abstract",
            "decision_stage": "merged_rerank",
        },
    }
    bundle = {
        "schema_version": "goal5c.capture_bundle.v1", "protocol_version": gc.PROTOCOL_VERSION,
        "data_class": "synthetic", "capture_commit": gc.CAPTURE_COMMIT,
        "queries": [{"query_id": "query-1", "intent_family_id": "family-1", "query_fingerprint": "fingerprint-1"}],
        "executions": [{
            "execution_id": "execution-1", "query_id": "query-1", "source_type": "publication",
            "attempt": 1, "run_id": "run-1", "session_id": "session-1",
            "query_envelope_hash": "envelope-1", "fp1_exclusion_reasons": [],
        }],
        "raw_events": [event],
        "examples": [
            {"example_id": "publication-example", "query_id": "query-1", "source_type": "publication",
             "raw_event_ids": ["snapshot:1"], "representation_ids": ["abstract-1"]},
            {"example_id": "web-example", "query_id": "query-1", "source_type": "web",
             "raw_event_ids": [], "representation_ids": ["snippet-1"]},
        ],
        "representations": [
            {"representation_id": "abstract-1", "example_id": "publication-example",
             "title": "Synthetic title", "score_text": "Synthetic abstract", "raw_event_ids": ["snapshot:1"],
             "fp1_provenance_event_ids": ["snapshot:1"]},
            {"representation_id": "snippet-1", "example_id": "web-example"},
        ],
    }
    contract["capture_bundle_sha256"] = gc.sha256_json(bundle)
    contract["truth_catalog_sha256"] = gc.sha256_json(truth)
    return contract, bundle, truth


def test_bound_measurement_connects_event_text_run_and_shared_truth():
    inputs = bound_measurement_inputs()
    before = copy.deepcopy(inputs)
    gc.validate_measurement_inputs(*inputs)
    assert inputs == before


@pytest.mark.parametrize("field", ["execution_id", "representation_id", "raw_event_id"])
def test_measurement_requires_specific_nonempty_input_references(field):
    contract = measurement()
    contract["observations"][0][field] = ""
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_contract(contract)


@pytest.mark.parametrize("field", ["query_id", "example_id", "execution_id", "representation_id", "raw_event_id"])
def test_missing_bound_reference_is_rejected(field):
    contract, bundle, truth = bound_measurement_inputs()
    contract["observations"][0][field] = "missing-reference"
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


@pytest.mark.parametrize("field,new", [
    ("source_type", "web"), ("attempt", 2), ("attempt", True), ("run_id", "other-run"),
    ("session_id", "other-session"), ("query_fingerprint", "other-query"),
    ("query_envelope_hash", "other-envelope"), ("example_id", "web-example"),
    ("dataset_eligible", 0), ("title", "altered title"), ("score_text", "retry-only text"),
])
def test_captured_event_cannot_disagree_with_declared_input(field, new):
    contract, bundle, truth = bound_measurement_inputs()
    bundle["raw_events"][0]["captured"][field] = new
    contract["capture_bundle_sha256"] = gc.sha256_json(bundle)
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


@pytest.mark.parametrize("field", ["raw_event_ids", "fp1_provenance_event_ids"])
def test_first_attempt_requires_representation_event_provenance(field):
    contract, bundle, truth = bound_measurement_inputs()
    bundle["representations"][0][field] = []
    contract["capture_bundle_sha256"] = gc.sha256_json(bundle)
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


def test_truth_version_must_match_loaded_catalog():
    contract, bundle, truth = bound_measurement_inputs()
    contract["truth_version"] = "another-version"
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


def test_cohort_family_must_match_capture_registry():
    contract, bundle, truth = bound_measurement_inputs()
    contract["query_cohort"][0]["intent_family_id"] = "invented-family"
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


def test_observed_gate_requires_actual_recorded_gate():
    _, bundle, truth = bound_measurement_inputs()
    contract = measurement("observed_gate")
    contract["capture_bundle_sha256"] = gc.sha256_json(bundle)
    contract["truth_catalog_sha256"] = gc.sha256_json(truth)
    gc.validate_measurement_inputs(contract, bundle, truth)
    bundle["raw_events"][0]["captured"]["decision_stage"] = "candidate_created"
    contract["capture_bundle_sha256"] = gc.sha256_json(bundle)
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


def test_negative_control_removed_binding_guard_breaks_retry_text_oracle(monkeypatch):
    inputs = bound_measurement_inputs()
    inputs[1]["raw_events"][0]["captured"]["score_text"] = "retry-only text"
    inputs[0]["capture_bundle_sha256"] = gc.sha256_json(inputs[1])
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(*inputs)
    monkeypatch.setattr(gc, "_require_match", lambda *args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(gc.ContractError):
            gc.validate_measurement_inputs(*inputs)


def test_bound_bundle_digest_prevents_silent_content_changes():
    contract, bundle, truth = bound_measurement_inputs()
    bundle["queries"][0]["unrelated_metadata"] = "altered"
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)


def test_bound_truth_digest_prevents_label_changes_under_same_version():
    contract, bundle, truth = bound_measurement_inputs()
    truth["truths"][0]["label"] = "not_relevant"
    with pytest.raises(gc.ContractError):
        gc.validate_measurement_inputs(contract, bundle, truth)
