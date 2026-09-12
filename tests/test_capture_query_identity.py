"""Capture provenance follows stored structure and the original query only."""

from __future__ import annotations

import copy
import hashlib
import json
import logging

import pytest

import tools.decision_capture as dc
import tools.research_session as rs

QUERY = "smart door lock controlled by mobile application"


def _atoms():
    # Use the actual schema producer so schema drift cannot hide in a fixture.
    envelope = rs._build_query_envelope(QUERY)
    atoms = envelope["critical_requirements_atomic"]
    assert len(atoms) > 1 and len(atoms[0]["terms"]) > 1
    return atoms


@pytest.mark.parametrize("field,value", [
    ("category", "constraint"),
    ("label", "different requirement"),
    ("terms", ["different", "terms"]),
    ("too_broad_for_element_retry", True),
])
def test_structural_field_drift_changes_envelope_hash(field, value):
    atoms = _atoms()
    changed = copy.deepcopy(atoms)
    assert changed[0][field] != value
    changed[0][field] = value
    assert dc.query_envelope_hash(atoms) != dc.query_envelope_hash(changed)


def test_envelope_hash_ignores_mapping_order_and_non_schema_metadata():
    atoms = _atoms()
    reordered = [dict(reversed(list(atom.items()))) for atom in atoms]
    for atom in reordered:
        atom["diagnostic_note"] = "not an atomic requirement field"
    assert dc.query_envelope_hash(atoms) == dc.query_envelope_hash(reordered)


@pytest.mark.parametrize("order", ["atoms", "terms"])
def test_envelope_hash_preserves_order_used_by_query_generation(order):
    atoms = _atoms()
    changed = copy.deepcopy(atoms)
    if order == "atoms":
        changed.reverse()
        assert rs._query_variants_from_atoms(QUERY, [], atoms) != rs._query_variants_from_atoms(
            QUERY, [], changed,
        )
    else:
        changed[0]["category"] = atoms[0]["category"] = "function"
        changed[0]["terms"].reverse()
        assert rs._web_query_variants([], "lock", atoms) != rs._web_query_variants(
            [], "lock", changed,
        )
    assert dc.query_envelope_hash(atoms) != dc.query_envelope_hash(changed)


@pytest.mark.parametrize("invalid", [{}, "[]", ["label"], [{"label": "partial"}]])
def test_malformed_atoms_cannot_produce_a_valid_envelope_hash(invalid):
    with pytest.raises(ValueError):
        dc.query_envelope_hash(invalid)


@pytest.mark.parametrize("envelope", [None, "not json", "{}", "null", "[]",
                                          '{"critical_requirements_atomic":null}'])
def test_unavailable_envelope_differs_from_explicit_empty_decomposition(temp_db, envelope):
    session_id = json.loads(rs.research_session_start(QUERY))["session_id"]
    if envelope is not None:
        with rs._connect() as conn:
            conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                         (envelope, session_id))
    collector = rs._new_decision_collector(session_id, "run", "web", 1)
    assert collector is not None
    assert collector.context.query_envelope_hash == ""
    assert collector.context.query_fingerprint == dc.query_fingerprint(QUERY)
    with rs._connect() as conn:
        conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                     ('{"critical_requirements_atomic":[]}', session_id))
    empty = rs._new_decision_collector(session_id, "run", "web", 1)
    assert empty.context.query_envelope_hash == dc.query_envelope_hash([])
    assert empty.context.query_envelope_hash
    assert empty.context.query_fingerprint == collector.context.query_fingerprint
    for item in (collector, empty):
        item.record(decision_stage="provider_filter", decision_reason="accepted", retained=True)
        rs._persist_decision_events(item)
    with rs._connect() as conn:
        hashes = [row[0] for row in conn.execute(
            "SELECT query_envelope_hash FROM evaluation_candidate_decisions ORDER BY id",
        )]
    assert hashes == ["", empty.context.query_envelope_hash]


@pytest.mark.parametrize("stored_query", [None, "", " \t\n", "\u00a0"])
def test_unavailable_original_query_disables_capture_with_diagnostic(temp_db, caplog, stored_query):
    session_id = "rs_missing_provenance"
    if stored_query is not None:
        rs.research_session_start(QUERY, session_id=session_id)
        with rs._connect() as conn:
            conn.execute("UPDATE research_sessions SET original_query=? WHERE session_id=?",
                         (stored_query, session_id))
    with caplog.at_level(logging.WARNING, logger="tools.research_session"):
        collector = rs._new_decision_collector(session_id, "retry-run", "web", 2)
    assert collector is None
    assert any("stored original query unavailable" in record.message for record in caplog.records)
    assert all(QUERY not in record.message and session_id not in record.message
               for record in caplog.records)


@pytest.mark.parametrize("query", [
    "  Mixed\tWHITESPACE\nQuery  ", "\uff33\uff4d\uff41\uff52\uff54\u00a0lock",
    "Straße STRASSE", "ΟΣ ος σ", "İ I ı", "e\u0301 É", "",
])
def test_query_normalization_matches_session_identity(query):
    normalized = rs.normalize_query_for_hash(query)
    assert dc.normalize_query_for_hash(query) == normalized
    assert dc.query_fingerprint(query) == hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    assert dc.query_fingerprint(query).startswith(rs.query_hash(query))


def test_casefold_only_equivalences_do_not_merge_original_queries():
    assert dc.query_fingerprint("Straße") != dc.query_fingerprint("STRASSE")
    assert dc.query_fingerprint("ος") != dc.query_fingerprint("οσ")


def test_label_only_hash_negative_control(monkeypatch):
    def label_only(atoms):
        return hashlib.sha256("\x1f".join(sorted(
            dc.normalize_query_for_hash(atom["label"]) for atom in atoms
        )).encode("utf-8")).hexdigest()[:32]

    monkeypatch.setattr(dc, "query_envelope_hash", label_only)
    with pytest.raises(AssertionError):
        test_structural_field_drift_changes_envelope_hash("terms", ["different", "terms"])


def test_casefold_normalization_negative_control(monkeypatch):
    original = dc.normalize_query_for_hash
    monkeypatch.setattr(dc, "normalize_query_for_hash", lambda query: original(query).casefold())
    with pytest.raises(AssertionError):
        test_query_normalization_matches_session_identity("Straße")


def test_unavailable_envelope_negative_control(monkeypatch, temp_db):
    original = dc.query_envelope_hash
    monkeypatch.setattr(dc, "query_envelope_hash", lambda atoms: original(atoms or []))
    with pytest.raises(AssertionError):
        test_unavailable_envelope_differs_from_explicit_empty_decomposition(temp_db, None)


@pytest.mark.parametrize("stored_query", [None, ""])
def test_attempt_query_fallback_negative_control(monkeypatch, temp_db, caplog, stored_query):
    original = rs._build_decision_collector

    def restore_fallback(*args, **kwargs):
        collector = original(*args, **kwargs)
        if collector is None:
            return dc.DecisionCollector(context=dc.CollectorContext(
                query_fingerprint=dc.query_fingerprint("attempt query substituted for original"),
            ))
        return collector

    monkeypatch.setattr(rs, "_build_decision_collector", restore_fallback)
    with pytest.raises(AssertionError):
        test_unavailable_original_query_disables_capture_with_diagnostic(
            temp_db, caplog, stored_query,
        )
