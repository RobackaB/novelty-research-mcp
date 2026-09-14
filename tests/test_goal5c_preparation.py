"""Synthetic preparation regression and fault-injection controls; no providers."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from eval.goal5c import preparation as prep
from eval.goal5c import __main__ as cli
from eval.goal5c.contracts import ContractError, canonical_json_bytes, sha256_json
from tools.decision_capture import example_id
from test_goal5c_snapshot import synthetic_capture, _rehash


def make_plan(bundle):
    return {
        "schema_version": prep.PLAN_VERSION, "protocol_version": prep.PROTOCOL_VERSION,
        "data_class": "synthetic", "plan_version": "synthetic-plan-1",
        "capture_bundle_sha256": sha256_json(bundle), "blind_seed": "19" * 32,
        "rubric_version": prep.RUBRIC_VERSION,
        "queries": [{"query_id": row["query_id"], "topic_card": "Compare fictional document evidence.",
                     "approved_translation": "", "reviewer": "synthetic-reviewer",
                     "review_note": "Synthetic topic-card equivalence checked."} for row in bundle["queries"]],
        "identity_reviews": [{
            "example_id": row["example_id"], "resolution": "resolved",
            "document_entity_id": "synthetic-document", "document_version_id": "version-1",
            "document_locator": "https://example.invalid/synthetic-document",
            "possible_equivalence": [], "reviewer": "synthetic-reviewer",
            "review_note": "Synthetic metadata verifies that these are mirrors of one document version.",
        } for row in bundle["examples"]],
        "projection_reviews": [{
            "representation_id": row["representation_id"],
            "title_spans": [[0, len(row["title"])]] if row["title"] else [],
            "content_spans": [[0, len(row["score_text"])]] if row["score_text"] else [],
            "reviewer": "synthetic-reviewer", "review_note": "All visible synthetic lines reviewed.",
        } for row in bundle["representations"]],
    }


@pytest.fixture()
def prepared_inputs(synthetic_capture):
    path, manifest = synthetic_capture
    return path, manifest, make_plan(prep.import_snapshot(path, manifest))


def test_shared_document_has_one_blank_item_and_source_specific_inventory(prepared_inputs):
    worksheet, analyst = prep.prepare_snapshot(*prepared_inputs)
    assert len(worksheet["items"]) == 1
    item = worksheet["items"][0]
    assert set(item) == {"item_id", "topic_card", "approved_translation", "title", "content",
                         "document_locator", "response"}
    assert item["response"] == {"label": None, "evidence_basis": None, "rationale": "", "supporting_passage": ""}
    assert len(analyst["unblinding"][0]["example_ids"]) == 2
    assert len(analyst["unblinding"][0]["truth_key"]) == 4
    assert analyst["unblinding"][0]["raw_event_id"].endswith(":1")  # earliest rejected trace, not later restoration
    assert sorted(len(row["raw_event_ids"]) for row in analyst["dispositions"]) == [1, 4]
    assert {row["source_type"] for row in analyst["first_attempt_inventory"]} == {"publication", "web"}
    assert all(row["metric_ready"] is False for row in analyst["first_attempt_inventory"])
    assert analyst["worksheet_sha256"] == sha256_json(worksheet)
    assert analyst["scope"] == "observed_first_attempt_candidate_inventory"


@pytest.mark.parametrize("field,value", [
    ("data_class", "private"), ("schema_version", "unknown"), ("rubric_version", "unknown"),
    ("capture_bundle_sha256", "0" * 64), ("blind_seed", "short"), ("blind_seed", True),
])
def test_invalid_plan_is_rejected(prepared_inputs, field, value):
    path, manifest, plan = prepared_inputs
    plan[field] = value
    with pytest.raises(ContractError):
        prep.prepare_snapshot(path, manifest, plan)


@pytest.mark.parametrize("collection", ["queries", "identity_reviews", "projection_reviews"])
@pytest.mark.parametrize("mutation", ["omit", "duplicate", "unknown_field", "no_review"])
def test_reviews_cover_all_inputs_exactly(prepared_inputs, collection, mutation):
    path, manifest, plan = prepared_inputs
    if mutation == "omit":
        plan[collection].pop()
    elif mutation == "duplicate":
        plan[collection].append(deepcopy(plan[collection][0]))
    elif mutation == "unknown_field":
        plan[collection][0]["retained"] = True
    else:
        plan[collection][0]["review_note"] = ""
    with pytest.raises(ContractError):
        prep.prepare_snapshot(path, manifest, plan)


def test_reimport_rejects_changed_snapshot_even_with_old_valid_plan(prepared_inputs):
    path, manifest, plan = prepared_inputs
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET score_at_decision=123")
    with pytest.raises(ContractError, match="digest"):
        prep.prepare_snapshot(path, manifest, plan)


def test_reimport_does_not_modify_snapshot(prepared_inputs):
    path, _, _ = prepared_inputs
    before = path.read_bytes(), path.stat().st_mtime_ns
    first = prep.prepare_snapshot(*prepared_inputs)
    second = prep.prepare_snapshot(*prepared_inputs)
    assert tuple(canonical_json_bytes(item) for item in first) == tuple(canonical_json_bytes(item) for item in second)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("resolution", ["provisional", "excluded"])
def test_unresolved_examples_stay_in_inventory_without_fabricated_truth(prepared_inputs, resolution):
    path, manifest, plan = prepared_inputs
    for row in plan["identity_reviews"]:
        row.update(resolution=resolution, document_entity_id="", document_version_id="", document_locator="")
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"] == analyst["unblinding"] == []
    assert len(analyst["dispositions"]) == len(analyst["first_attempt_inventory"]) == 2
    assert all(row["identity_resolution"] == resolution for row in analyst["first_attempt_inventory"])


def test_text_hash_identity_is_not_automatically_resolved(prepared_inputs):
    path, manifest, _ = prepared_inputs
    with closing(sqlite3.connect(path)) as conn, conn:
        for source in ("publication", "web"):
            fingerprint = manifest["queries"][0]["query_fingerprint"]
            conn.execute("UPDATE evaluation_candidate_decisions SET candidate_identity=?, "
                         "candidate_identity_kind='text_hash', example_id=? WHERE source_type=?",
                         ("a" * 32, example_id(fingerprint, source, "a" * 32), source))
    _rehash(path, manifest)
    plan = make_plan(prep.import_snapshot(path, manifest))
    for row in plan["identity_reviews"]:
        row.update(resolution="provisional", document_entity_id="", document_version_id="", document_locator="")
        row["possible_equivalence"] = [other["example_id"] for other in plan["identity_reviews"] if other != row]
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"] == []
    assert len(analyst["dispositions"]) == 2


def test_distinct_document_versions_do_not_share_truth(prepared_inputs):
    path, manifest, plan = prepared_inputs
    plan["identity_reviews"][0]["document_version_id"] = "version-2"
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert len(worksheet["items"]) == len(analyst["unblinding"]) == 2


def test_resolved_and_provisional_inventory_keys_cannot_collide(prepared_inputs):
    path, manifest, _ = prepared_inputs
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM evaluation_candidate_decisions WHERE id=1").fetchone())
        row.pop("id")
        row["candidate_identity"] = "synthetic:second-document"
        row["example_id"] = example_id(row["query_fingerprint"], row["source_type"], row["candidate_identity"])
        conn.execute("INSERT INTO evaluation_candidate_decisions (" + ",".join(row) + ") VALUES ("
                     + ",".join("?" for _ in row) + ")", tuple(row.values()))
    manifest["executions"][0]["declared_event_count"] += 1
    _rehash(path, manifest)
    plan = make_plan(prep.import_snapshot(path, manifest))
    for review in plan["identity_reviews"]:
        if review["example_id"] == row["example_id"]:
            review.update(resolution="provisional", document_entity_id="", document_version_id="", document_locator="")
        else:
            review.update(document_entity_id="provisional", document_version_id=row["example_id"])
    _, analyst = prep.prepare_snapshot(path, manifest, plan)
    publication = [item for item in analyst["first_attempt_inventory"] if item["source_type"] == "publication"]
    assert len(publication) == 2
    assert {item["identity_resolution"] for item in publication} == {"resolved", "provisional"}


def test_retry_packet_evidence_never_replaces_first_attempt_inventory(prepared_inputs):
    path, manifest, plan = prepared_inputs
    bundle = prep.import_snapshot(path, manifest)
    for row in plan["projection_reviews"]:
        representation = next(item for item in bundle["representations"]
                              if item["representation_id"] == row["representation_id"])
        if representation["fp1_provenance_event_ids"]:
            row["content_spans"] = []
            row["review_note"] = "Synthetic first representation has no displayable substantive line."
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"][0]["content"] == "Synthetic retry-only text"
    assert all(item["score_text"] == "Synthetic first representation" for item in analyst["first_attempt_inventory"])


def test_no_reviewed_content_withholds_entity_but_not_inventory(prepared_inputs):
    path, manifest, plan = prepared_inputs
    for row in plan["projection_reviews"]:
        row["content_spans"] = []
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"] == []
    assert len(analyst["withheld_entities"]) == 1
    assert len(analyst["first_attempt_inventory"]) == 2


def test_missing_and_unexecuted_sources_remain_visible(prepared_inputs):
    path, manifest, _ = prepared_inputs
    manifest["executions"] = [row for row in manifest["executions"] if row["source_type"] != "patent"]
    for row in manifest["executions"]:
        if row["source_type"] == "web":
            row.update(capture_status="missing", declared_event_count=0)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM evidence_results WHERE source_type='patent'")
        conn.execute("DELETE FROM evaluation_candidate_decisions WHERE source_type IN ('web','patent')")
    _rehash(path, manifest)
    plan = make_plan(prep.import_snapshot(path, manifest))
    _, analyst = prep.prepare_snapshot(path, manifest, plan)
    slots = {row["source_type"]: row for row in analyst["source_slots"]}
    assert set(slots) == {"patent", "publication", "web"}
    assert slots["patent"]["state"] == "unexecuted"
    assert slots["web"]["execution_ids"]
    assert all(row["pool_completeness"] == "unverified" for row in slots.values())
    assert any(row["capture_status"] == "missing" for row in analyst["executions"])


def test_decisions_do_not_change_blinded_packet_or_selected_text(prepared_inputs):
    path, manifest, plan = prepared_inputs
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET provider='synthetic_bias_marker', "
                     "score_at_decision=9000, threshold_at_decision=9999")
        conn.execute("UPDATE evaluation_candidate_decisions SET decision_reason='accepted', retained=1 WHERE id=1")
    _rehash(path, manifest)
    changed_plan = make_plan(prep.import_snapshot(path, manifest))
    changed, changed_audit = prep.prepare_snapshot(path, manifest, changed_plan)
    assert canonical_json_bytes(changed) == canonical_json_bytes(worksheet)
    assert [row["score_text"] for row in analyst["first_attempt_inventory"]] == [
        row["score_text"] for row in changed_audit["first_attempt_inventory"]]
    assert "synthetic_bias_marker" not in canonical_json_bytes(changed).decode()


@pytest.mark.parametrize("field", ["topic_card", "approved_translation"])
def test_visible_query_fields_cannot_leak_metadata(prepared_inputs, field):
    path, manifest, plan = prepared_inputs
    plan["queries"][0][field] = "SOURCE: OpenAlex"
    with pytest.raises(ContractError):
        prep.prepare_snapshot(path, manifest, plan)


@pytest.mark.parametrize("locator", [
    "javascript:alert(1)", "file:///private", "https://user:pass@example.invalid/doc",
    "https://example.invalid/doc?retained=true", "https://example.invalid/doc?retained%3Dtrue",
    "https://example.invalid/doc?%2570rovider%253DOpenAlex", "https://example.invalid/doc#score=9",
])
def test_locator_cannot_expose_metadata_or_unsafe_schemes(prepared_inputs, locator):
    path, manifest, plan = prepared_inputs
    for row in plan["identity_reviews"]:
        row["document_locator"] = locator
    with pytest.raises(ContractError):
        prep.prepare_snapshot(path, manifest, plan)


def test_meaningful_document_locator_query_is_preserved(prepared_inputs):
    path, manifest, plan = prepared_inputs
    for row in plan["identity_reviews"]:
        row["document_locator"] = "https://example.invalid/document?id=ABC"
    worksheet, _ = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"][0]["document_locator"].endswith("?id=ABC")


def test_opaque_item_ids_and_order_ignore_plan_row_order(prepared_inputs):
    path, manifest, plan = prepared_inputs
    worksheet, _ = prep.prepare_snapshot(path, manifest, plan)
    for key in ("queries", "identity_reviews", "projection_reviews"):
        plan[key].reverse()
    reordered, _ = prep.prepare_snapshot(path, manifest, plan)
    assert reordered == worksheet
    plan["blind_seed"] = "72" * 32
    reseeded, _ = prep.prepare_snapshot(path, manifest, plan)
    assert reseeded["items"][0]["item_id"] != worksheet["items"][0]["item_id"]


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_equivalent_span_plans_preserve_worksheet_and_inventory(prepared_inputs, ending):
    path, manifest, _ = prepared_inputs
    text = "A" + ending + "B" + ending
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET score_text=? WHERE id=1", (text,))
    _rehash(path, manifest)
    bundle = prep.import_snapshot(path, manifest)
    plan = make_plan(bundle)
    worksheet, analyst = prep.prepare_snapshot(path, manifest, plan)
    assert worksheet["items"][0]["content"] == text
    representation = next(row for row in bundle["representations"] if row["score_text"] == text)
    for first_end in (1, 1 + len(ending)):
        segmented = deepcopy(plan)
        review = next(row for row in segmented["projection_reviews"]
                      if row["representation_id"] == representation["representation_id"])
        review["content_spans"] = [[0, first_end], [text.index("B"), len(text)]]
        changed_worksheet, changed_analyst = prep.prepare_snapshot(path, manifest, segmented)
        assert canonical_json_bytes(changed_worksheet) == canonical_json_bytes(worksheet)
        assert changed_analyst["first_attempt_inventory"] == analyst["first_attempt_inventory"]
        assert changed_analyst["preparation_plan_sha256"] != analyst["preparation_plan_sha256"]
        assert review in changed_analyst["projection_reviews"]


def test_cli_regenerates_separate_artifacts_across_hash_seeds(prepared_inputs, tmp_path):
    path, manifest, plan = prepared_inputs
    manifest_path, plan_path = tmp_path / "manifest.json", tmp_path / "plan.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    plan_path.write_bytes(canonical_json_bytes(plan))
    outputs = []
    for seed in ("1", "97"):
        directory = tmp_path / f"prepared-{seed}"
        args = [sys.executable, "-m", "eval.goal5c", "prepare-synthetic", "--manifest", str(manifest_path),
                "--snapshot", str(path), "--plan", str(plan_path), "--output-dir", str(directory)]
        completed = subprocess.run(args, env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        outputs.append({item.name: item.read_bytes() for item in directory.iterdir()})
        retry = subprocess.run(args, capture_output=True, text=True)
        assert retry.returncode == 2
        assert outputs[-1] == {item.name: item.read_bytes() for item in directory.iterdir()}
    assert outputs[0] == outputs[1]
    assert set(outputs[0]) == {"worksheet.json", "analyst-only.json", "COMPLETE.json"}


def test_preparation_output_must_be_external_and_new(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(ContractError):
        cli.write_preparation(repo / "study", {}, {})
    assert not (repo / "study").exists()


def test_failed_pair_write_has_no_completion_seal(tmp_path, monkeypatch):
    directory = tmp_path / "incomplete"
    original = cli.write_bundle

    def fail_worksheet(path, value):
        if path.name == "worksheet.json":
            raise ContractError("synthetic write failure")
        original(path, value)

    monkeypatch.setattr(cli, "write_bundle", fail_worksheet)
    with pytest.raises(ContractError):
        cli.write_preparation(directory, {}, {})
    assert not (directory / "COMPLETE.json").exists()


def test_missing_review_guard_negative_control(prepared_inputs, monkeypatch):
    monkeypatch.setattr(prep, "_exact_index", lambda rows, key, expected: prep._index_rows(rows, key))
    # An undeclared query could silently alter the review roster without this guard.
    path, manifest, plan = prepared_inputs
    plan["queries"].append({**plan["queries"][0], "query_id": "undeclared"})
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(ContractError):
            prep.prepare_snapshot(path, manifest, plan)


def test_retry_inventory_guard_negative_control(prepared_inputs, monkeypatch):
    def use_retry(representation, events):
        return [events[identifier] for identifier in representation["raw_event_ids"]
                if events[identifier]["captured"]["attempt"] > 1]

    monkeypatch.setattr(prep, "_fp1_events", use_retry)
    with pytest.raises(AssertionError):
        test_retry_packet_evidence_never_replaces_first_attempt_inventory(prepared_inputs)


def test_locator_metadata_guard_negative_control(prepared_inputs, monkeypatch):
    monkeypatch.setattr(prep, "_locator", lambda _value: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_locator_cannot_expose_metadata_or_unsafe_schemes(
            prepared_inputs, "https://example.invalid/doc?retained%3Dtrue")
