"""Tests of the shared matching of requirements against evidence text."""

from __future__ import annotations

from tools.requirement_match import (
    atom_coverage,
    blob_tokens,
    covers_all_atoms,
    requirement_match_strength,
    stem_requirement_token,
)


def test_stem_normalization_table():
    assert stem_requirement_token("unlocking") == "unlock"
    assert stem_requirement_token("logged") == "log"
    assert stem_requirement_token("verification") == "verify"


def test_stem_suffix_rules():
    assert stem_requirement_token("codes") == "code"
    assert stem_requirement_token("batches") == "batch"
    assert stem_requirement_token("cat") == "cat"


def test_blob_tokens_include_stems():
    tokens = blob_tokens("The lock logs entries")
    assert "logs" in tokens
    assert "log" in tokens
    assert "entry" in tokens or "entrie" in tokens


def test_requirement_match_strength_levels():
    text = "A smart door lock with a mobile application that logs entry history."
    assert requirement_match_strength(["mobile", "application"], text) == "full"
    assert requirement_match_strength(["mobile", "fingerprint"], text) == "partial"
    assert requirement_match_strength(["quantum", "sensor"], text) == "none"
    assert requirement_match_strength([], text) == "none"


def test_requirement_match_strength_stemming_variants():
    # "unlocked" in the text must cover the "unlocking" requirement.
    assert requirement_match_strength(["unlocking"], "The door is unlocked remotely") == "full"


def test_atom_coverage_fraction():
    atoms = [
        {"terms": ["mobile", "application"]},
        {"terms": ["entry", "history"]},
        {"terms": ["fingerprint"]},
    ]
    text = "Mobile application controlled lock recording entry history."
    coverage, matched = atom_coverage(text, atoms)
    assert matched == 2
    assert coverage == round(2 / 3, 4)


def test_atom_coverage_empty_inputs():
    assert atom_coverage("", [{"terms": ["a"]}]) == (0.0, 0)
    assert atom_coverage("text", []) == (0.0, 0)
    assert atom_coverage("text", None) == (0.0, 0)


def test_covers_all_atoms():
    atoms = [{"terms": ["mobile", "application"]}, {"terms": ["access", "codes"]}]
    full_text = "Mobile application generating temporary access codes."
    partial_text = "Mobile application for the lock."
    assert covers_all_atoms(full_text, atoms) is True
    assert covers_all_atoms(partial_text, atoms) is False
    assert covers_all_atoms(full_text, []) is False
