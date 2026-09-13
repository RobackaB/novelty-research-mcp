"""Synthetic capture-to-bundle tests; no live providers or participant data."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from eval.goal5c import snapshot as capture
from eval.goal5c.__main__ import main, write_bundle
from eval.goal5c.contracts import CAPTURE_COMMIT, PROTOCOL_VERSION, ContractError, canonical_json_bytes
from eval.goal5c.contracts import sha256_json, validate_measurement_inputs
from tools import research_session as rs
from tools.decision_capture import CollectorContext, DecisionCollector, query_envelope_hash, query_fingerprint

QUERY = "Synthetic fixture: document comparison for a fictional research task"


def _rehash(path, manifest):
    manifest["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _save_synthetic_attempt(execution, *, with_hits=True, status="ok", completed=True,
                            reliable_no_results=False, errors=None):
    """Exercise production normalization and persistence, without providers."""
    return json.loads(rs.research_session_save_evidence(
        execution["session_id"], execution["source_type"], {
            "source_type": execution["source_type"], "status": status,
            "completed": completed, "reliable_no_results": reliable_no_results,
            "hits": [{"title": "Fictional document fixture",
                      "url": "https://example.invalid/synthetic-document",
                      "summary": "Synthetic first representation"}] if with_hits else [],
            "errors": errors or [], "warnings": [],
        }, query=execution["call_query"], attempt=execution["attempt"], run_id=execution["run_id"],
    ))


@pytest.fixture()
def synthetic_capture(temp_db):
    session_id = "rs_goal5c_synthetic"
    rs.research_session_start(QUERY, session_id=session_id)
    envelope = {"critical_requirements_atomic": []}
    envelope_hash = query_envelope_hash([])
    executions = []
    for source, attempt in (("publication", 1), ("web", 1), ("publication", 2), ("patent", 2)):
        run_id = f"synthetic-{source}-{attempt}"
        call_query = QUERY if attempt == 1 else "Synthetic retry fixture"
        collector = DecisionCollector(CollectorContext(
            session_id=session_id, run_id=run_id, source_type=source, attempt=attempt,
            query_fingerprint=query_fingerprint(QUERY), query_envelope_hash=envelope_hash,
        ))
        if source == "patent" and attempt == 2:
            collector.record(decision_stage="cache_lookup", decision_reason="cache_hit", dataset_eligible=False)
        else:
            identity = {"canonical_id": "synthetic:document-1"} if source == "publication" else {
                "url": "https://example.invalid/synthetic-document",
            }
            fields = dict(
                **identity, title="Fictional document fixture",
                score_text="Synthetic first representation" if attempt == 1 else "Synthetic retry-only text",
                decision_query=call_query, query_variant=call_query, provider="synthetic_provider",
            )
            if source == "publication":
                collector.record(**fields, decision_stage="provider_filter", decision_reason="below_threshold",
                                 retained=False, score_at_decision=2.0, threshold_at_decision=3.5)
                if attempt == 1:
                    collector.record(**fields, decision_stage="fill_back", decision_reason="restored_to_meet_minimum",
                                     retained=True)
                    collector.record(**fields, decision_stage="fill_back", decision_reason="restored_to_meet_minimum",
                                     retained=True)
            else:
                collector.record(**fields, decision_stage="content_gate", decision_reason="below_overlap_gate")
        rs._persist_decision_events(collector)
        executions.append({
            "execution_id": run_id, "query_id": "q_synthetic", "session_id": session_id,
            "run_id": run_id, "source_type": source, "attempt": attempt, "envelope": envelope,
            "query_envelope_hash": envelope_hash, "call_query": call_query,
            "capture_status": "cache_only" if source == "patent" and attempt == 2 else "observed",
            "declared_event_count": len(collector.events),
        })
        ack = _save_synthetic_attempt(executions[-1])
        assert ack["stored"] is True and ack["evidence_status"] == "ok_with_hits"
    manifest = {
        "schema_version": capture.MANIFEST_VERSION, "protocol_version": PROTOCOL_VERSION,
        "data_class": "synthetic", "batch_id": "synthetic-batch", "capture_commit": CAPTURE_COMMIT,
        "capture_tree_clean": True, "snapshot_sha256": "", "environment_sha256": "1" * 64,
        "configuration_sha256": "2" * 64,
        "queries": [{"query_id": "q_synthetic", "original_query": QUERY,
                     "query_fingerprint": query_fingerprint(QUERY), "intent_family_id": "synthetic-family",
                     "stratum": "scientific"}], "executions": executions,
    }
    _rehash(temp_db, manifest)
    return temp_db, manifest


def test_import_preserves_snapshot_and_raw_events(synthetic_capture, monkeypatch):
    path, manifest = synthetic_capture
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(rs, "_connect", lambda: pytest.fail("import attempted production schema setup"))
    first = capture.import_snapshot(path, manifest)
    second = capture.import_snapshot(path, manifest)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert len(first["raw_events"]) == 6
    assert len({row["raw_event_id"] for row in first["raw_events"]}) == 6
    assert len(first["examples"]) == 2  # no invented cross-source reconciliation
    publication = next(item for item in first["examples"] if item["source_type"] == "publication")
    assert publication["identity_status"] == "unreconciled"
    assert len(publication["raw_event_ids"]) == 4
    outcomes = [row["captured"]["retained"] for row in first["raw_events"]
                if row["captured"]["source_type"] == "publication"]
    assert outcomes == [0, 1, 1, 0]
    overlap = next(row for row in first["raw_events"] if row["captured"]["decision_stage"] == "content_gate")
    assert overlap["decision_kind"] == "text_gate" and overlap["captured"]["retained"] is None


def test_retry_representations_and_cache_do_not_enter_fp1(synthetic_capture):
    bundle = capture.import_snapshot(*synthetic_capture)
    retry = next(item for item in bundle["representations"] if item["score_text"] == "Synthetic retry-only text")
    assert retry["fp1_provenance_event_ids"] == []
    assert all(item["fp1_exclusion_reasons"] for item in bundle["executions"] if item["attempt"] == 2)
    cache_ids = {row["raw_event_id"] for row in bundle["raw_events"] if row["decision_kind"] == "trace_only"}
    assert cache_ids
    assert not cache_ids.intersection(event for item in bundle["examples"] for event in item["raw_event_ids"])


def test_writer_persisted_hits_are_complete_for_fp1(synthetic_capture):
    bundle = capture.import_snapshot(*synthetic_capture)
    for execution in bundle["executions"]:
        assert execution["source_attempt"]["status"] == "ok"
        assert "incomplete_source_attempt" not in execution["fp1_exclusion_reasons"]
        if execution["attempt"] == 1:
            assert execution["fp1_exclusion_reasons"] == []
    first_events = {row["raw_event_id"] for row in bundle["raw_events"]
                    if row["captured"]["attempt"] == 1}
    assert first_events
    assert first_events == {event for item in bundle["representations"]
                            for event in item["fp1_provenance_event_ids"]}


@pytest.mark.parametrize("field,value", [
    ("data_class", "private"), ("capture_commit", "0" * 40), ("capture_tree_clean", 1),
    ("schema_version", "candidate_decision.v1"), ("protocol_version", "goal5c.protocol.v0"),
    ("snapshot_sha256", "0" * 64),
])
def test_invalid_manifest_or_snapshot_is_rejected(synthetic_capture, field, value):
    path, manifest = synthetic_capture
    manifest[field] = value
    with pytest.raises(ContractError):
        capture.import_snapshot(path, manifest)


@pytest.mark.parametrize("field,value", [
    ("query_fingerprint", "0" * 32), ("query_envelope_hash", "0" * 32),
    ("example_id", "0" * 32), ("run_id", "undeclared-run"), ("decision_stage", "unknown_stage"),
    ("dataset_eligible", 3), ("score_at_decision", "not a number"),
])
def test_corrupt_event_provenance_is_rejected(synthetic_capture, field, value):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"UPDATE evaluation_candidate_decisions SET {field}=? WHERE id=1", (value,))
    _rehash(path, manifest)
    with pytest.raises(ContractError):
        capture.import_snapshot(path, manifest)


def test_missing_original_is_not_replaced_by_attempt_query(synthetic_capture):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE research_sessions SET original_query='' ")
    _rehash(path, manifest)
    with pytest.raises(ContractError, match="stored original"):
        capture.import_snapshot(path, manifest)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_live_sqlite_sidecars_are_rejected(synthetic_capture, suffix):
    path, manifest = synthetic_capture
    Path(str(path) + suffix).write_bytes(b"synthetic sidecar")
    with pytest.raises(ContractError, match="sidecars"):
        capture.import_snapshot(path, manifest)


def test_capture_view_cannot_impersonate_table(synthetic_capture):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("ALTER TABLE evaluation_candidate_decisions RENAME TO synthetic_events")
        conn.execute("CREATE VIEW evaluation_candidate_decisions AS SELECT * FROM synthetic_events")
    _rehash(path, manifest)
    with pytest.raises(ContractError, match="not a table"):
        capture.import_snapshot(path, manifest)


def test_unavailable_envelope_preserved_but_excluded(synthetic_capture):
    path, manifest = synthetic_capture
    for execution in manifest["executions"]:
        execution["envelope"] = None
        execution["query_envelope_hash"] = ""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET query_envelope_hash='' ")
    _rehash(path, manifest)
    bundle = capture.import_snapshot(path, manifest)
    assert len(bundle["raw_events"]) == 6
    assert all("unavailable_envelope" in item["fp1_exclusion_reasons"] for item in bundle["executions"])
    assert all(not item["fp1_provenance_event_ids"] for item in bundle["representations"])


@pytest.mark.parametrize("state", ["missing", "known_loss", "empty_output"])
def test_zero_rows_do_not_silently_mean_empty_pool(synthetic_capture, state):
    path, manifest = synthetic_capture
    for execution in manifest["executions"]:
        execution["capture_status"] = state
        execution["declared_event_count"] = 0
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE evaluation_candidate_decisions")
        if state == "empty_output":
            conn.execute("DELETE FROM evidence_results")
    if state == "empty_output":
        for execution in manifest["executions"]:
            ack = _save_synthetic_attempt(execution, with_hits=False, reliable_no_results=True)
            assert ack["stored"] is True and ack["evidence_status"] == "reliable_no_results"
    _rehash(path, manifest)
    bundle = capture.import_snapshot(path, manifest)
    assert bundle["raw_events"] == bundle["examples"] == []
    for item in bundle["executions"]:
        assert state in item["fp1_exclusion_reasons"]
        if state == "empty_output":
            assert "unverified_empty_pool" in item["fp1_exclusion_reasons"]
            assert "incomplete_source_attempt" not in item["fp1_exclusion_reasons"]
            assert item["source_attempt"]["status"] == "ok"
            assert item["source_attempt"]["completed"] == 1
            assert item["source_attempt"]["reliable_no_results"] == 1


@pytest.mark.parametrize("status,completed,errors", [
    ("partial_failure", False, [{"type": "synthetic_failure"}]),
    ("partial_failure", True, []),
    ("failed", True, []),
    ("ok", False, []),
    ("ok", True, [{"type": "synthetic_failure"}]),
])
def test_partial_source_retains_events_without_fp1_eligibility(synthetic_capture, status, completed, errors):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM evidence_results")
    for execution in manifest["executions"]:
        ack = _save_synthetic_attempt(execution, status=status, completed=completed, errors=errors)
        assert ack["stored"] is True
    _rehash(path, manifest)
    bundle = capture.import_snapshot(path, manifest)
    assert len(bundle["raw_events"]) == 6
    assert all("incomplete_source_attempt" in item["fp1_exclusion_reasons"] for item in bundle["executions"])
    assert all(not item["fp1_provenance_event_ids"] for item in bundle["representations"])


@pytest.mark.parametrize("status", ["ok_with_hits", "reliable_no_results"])
def test_acknowledgement_status_does_not_certify_stored_completion(synthetic_capture, status):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evidence_results SET status=?", (status,))
    _rehash(path, manifest)
    bundle = capture.import_snapshot(path, manifest)
    assert all("incomplete_source_attempt" in item["fp1_exclusion_reasons"] for item in bundle["executions"])
    assert all(not item["fp1_provenance_event_ids"] for item in bundle["representations"])


@pytest.mark.parametrize("empty_output", [False, True])
def test_acknowledgement_status_confusion_negative_control(synthetic_capture, monkeypatch, empty_output):
    # Inject the proposed status substitution: both real-writer success oracles
    # must detect it. The existing status='ok' predicate is not itself defective.
    monkeypatch.setattr(capture, "_source_attempt_complete", lambda attempt: bool(
        attempt and attempt["status"] in {"ok_with_hits", "reliable_no_results"}
        and attempt["completed"] == 1 and attempt["error_count"] == 0
    ))
    if empty_output:
        with pytest.raises(ContractError, match="completion evidence"):
            test_zero_rows_do_not_silently_mean_empty_pool(synthetic_capture, "empty_output")
    else:
        with pytest.raises(AssertionError):
            test_writer_persisted_hits_are_complete_for_fp1(synthetic_capture)


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_json_loader_rejects_ambiguous_values(tmp_path, content):
    path = tmp_path / "synthetic.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ContractError):
        capture.load_json(path)


def test_cli_roundtrip_and_refusal_to_overwrite(synthetic_capture, tmp_path, capsys):
    path, manifest = synthetic_capture
    manifest_path, output = tmp_path / "synthetic.json", tmp_path / "bundle.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    args = ["import-snapshot", "--manifest", str(manifest_path), "--snapshot", str(path), "--output", str(output)]
    assert main(args) == 0
    assert output.read_bytes() == canonical_json_bytes(capture.import_snapshot(path, manifest))
    original = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == original
    assert QUERY not in capsys.readouterr().out


def test_bundle_cannot_be_written_inside_git(tmp_path):
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    with pytest.raises(ContractError, match="outside Git"):
        write_bundle(repo / "raw.json", {"synthetic": True})
    assert not (repo / "raw.json").exists()


def test_bundle_is_deterministic_across_hash_seeds(synthetic_capture, tmp_path):
    path, manifest = synthetic_capture
    manifest_path = tmp_path / "synthetic.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    outputs = []
    for seed in ("1", "97"):
        output = tmp_path / f"bundle-{seed}.json"
        completed = subprocess.run([
            sys.executable, "-m", "eval.goal5c", "import-snapshot", "--manifest", str(manifest_path),
            "--snapshot", str(path), "--output", str(output),
        ], env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def test_event_provenance_negative_control(synthetic_capture, monkeypatch):
    monkeypatch.setattr(capture, "_validate_event", lambda *_args: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_corrupt_event_provenance_is_rejected(synthetic_capture, "query_fingerprint", "0" * 32)


def test_snapshot_digest_negative_control(synthetic_capture, monkeypatch):
    original = capture._read_snapshot

    def ignore_digest(path, _expected):
        return original(path, hashlib.sha256(path.read_bytes()).hexdigest())

    monkeypatch.setattr(capture, "_read_snapshot", ignore_digest)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_invalid_manifest_or_snapshot_is_rejected(synthetic_capture, "snapshot_sha256", "0" * 64)


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '[]'])
def test_invalid_payload_is_rejected_without_rewriting_raw_text(synthetic_capture, payload):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET payload_json=? WHERE id=1", (payload,))
    _rehash(path, manifest)
    original = path.read_bytes()
    with pytest.raises(ContractError):
        capture.import_snapshot(path, manifest)
    assert path.read_bytes() == original


def test_undeclared_execution_is_not_silently_dropped(synthetic_capture):
    path, manifest = synthetic_capture
    manifest["executions"].pop()
    with pytest.raises(ContractError, match="undeclared source attempt"):
        capture.import_snapshot(path, manifest)


def test_structural_reason_cannot_masquerade_as_numeric_gate(synthetic_capture):
    path, manifest = synthetic_capture
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET decision_stage='dedupe', "
                     "decision_reason='below_threshold', retained=NULL WHERE id=1")
    _rehash(path, manifest)
    with pytest.raises(ContractError, match="source stage or reason"):
        capture.import_snapshot(path, manifest)


def test_fallback_identity_is_not_rehashed_from_later_scoring_text(synthetic_capture):
    from tools.decision_capture import candidate_identity, example_id

    path, manifest = synthetic_capture
    original_identity, _kind = candidate_identity(title="Original synthetic title", score_text="Original synthetic text")
    identifier = example_id(query_fingerprint(QUERY), "publication", original_identity)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE evaluation_candidate_decisions SET candidate_identity=?, candidate_identity_kind='text_hash', "
                     "example_id=? WHERE source_type='publication'", (original_identity, identifier))
    _rehash(path, manifest)
    bundle = capture.import_snapshot(path, manifest)
    example = next(item for item in bundle["examples"] if item["source_type"] == "publication")
    assert example["candidate_identity"] == original_identity
    assert len(example["representation_ids"]) == 2


def test_empty_output_negative_control(synthetic_capture, monkeypatch):
    original = capture.import_snapshot

    def restore_false_empty_inference(*args):
        bundle = original(*args)
        for execution in bundle["executions"]:
            if execution["capture_status"] == "empty_output":
                execution["fp1_exclusion_reasons"] = []
        return bundle

    monkeypatch.setattr(capture, "import_snapshot", restore_false_empty_inference)
    with pytest.raises(AssertionError):
        test_zero_rows_do_not_silently_mean_empty_pool(synthetic_capture, "empty_output")


def test_imported_bundle_binds_shared_truth_and_exact_measurement_inputs(synthetic_capture, tmp_path, capsys):
    from test_goal5c_contracts import measurement, truth_catalog

    bundle = capture.import_snapshot(*synthetic_capture)
    contract, truth = measurement(), truth_catalog()
    truth["truths"][0]["query_id"] = "q_synthetic"
    for mapping in truth["example_mappings"]:
        example = next(item for item in bundle["examples"] if item["source_type"] == mapping["source_type"])
        mapping["example_id"], mapping["query_id"] = example["example_id"], example["query_id"]
    truth["representation_assessments"] = []
    contract["query_cohort"] = [{"query_id": "q_synthetic", "intent_family_id": "synthetic-family", "split": "discovery"}]
    chosen = next(item for item in bundle["raw_events"]
                  if item["captured"]["source_type"] == "publication" and item["captured"]["attempt"] == 1)
    representation = next(item for item in bundle["representations"] if chosen["raw_event_id"] in item["raw_event_ids"])
    contract["observations"] = [{
        "query_id": "q_synthetic", "example_id": representation["example_id"], "attempt": 1,
        "role": "candidate", "execution_id": chosen["execution_id"],
        "representation_id": representation["representation_id"], "raw_event_id": chosen["raw_event_id"],
    }]
    contract["capture_bundle_sha256"] = sha256_json(bundle)
    contract["truth_catalog_sha256"] = sha256_json(truth)
    validate_measurement_inputs(contract, bundle, truth)
    assert len(truth["truths"]) == 1 and len(truth["example_mappings"]) == 2
    paths = {name: tmp_path / (name + ".json") for name in ("measurement", "bundle", "truth")}
    for name, value in (("measurement", contract), ("bundle", bundle), ("truth", truth)):
        paths[name].write_bytes(canonical_json_bytes(value))
    args = ["validate-inputs", "--measurement", str(paths["measurement"]),
            "--bundle", str(paths["bundle"]), "--truth", str(paths["truth"])]
    assert main(args) == 0
    bundle["representations"][0]["score_text"] += " tampered"
    paths["bundle"].write_bytes(canonical_json_bytes(bundle))
    assert main(args) == 2
    assert QUERY not in capsys.readouterr().out
