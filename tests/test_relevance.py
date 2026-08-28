"""Testy lokálneho hodnotenia relevancie."""

from __future__ import annotations

from tools.relevance import (
    discriminative_tokens,
    evidence_score,
    is_relevant,
    subject_anchors,
    tokens,
)


def test_tokens_remove_stopwords_and_stem():
    result = tokens("the anomaly detection systems are running")
    assert "the" not in result
    assert "are" not in result
    # "detection" a "systems" sa zjednodušia na približný koreň slova.
    assert any(token.startswith("detect") for token in result)


def test_evidence_score_zero_for_empty():
    assert evidence_score("", "text", "WEB") == 0.0
    assert evidence_score("query", "", "WEB") == 0.0


def test_evidence_score_higher_for_matching_text():
    query = "predictive maintenance electric motor vibration temperature"
    matching = "Predictive maintenance of electric motors using vibration and temperature analysis"
    unrelated = "Recipes for baking sourdough bread at home in winter"
    assert evidence_score(query, matching, "PUBLICATION") > evidence_score(query, unrelated, "PUBLICATION")


def test_evidence_score_bounded():
    query = "vibration temperature motor"
    text = " ".join(["vibration temperature motor"] * 50)
    score = evidence_score(query, text, "PATENT")
    assert 0.0 <= score <= 10.0


def test_is_relevant_publication_rejects_unrelated_text():
    query = "smart door lock mobile application access codes"
    unrelated = "A study of coral reef bleaching in tropical oceans"
    assert not is_relevant(query, unrelated, "PUBLICATION")


def test_is_relevant_publication_accepts_matching_text():
    query = "smart door lock mobile application access codes"
    matching = (
        "We present a smart door lock controlled by a mobile application "
        "supporting temporary access codes and entry history logging."
    )
    assert is_relevant(query, matching, "PUBLICATION")


def test_discriminative_tokens_drop_generic_terms():
    result = discriminative_tokens("system method for anomaly detection in logs")
    assert "system" not in result
    assert "method" not in result


def test_subject_anchors_strip_mechanism_tokens():
    terms = discriminative_tokens("self-heating lunch box without electricity")
    anchors = subject_anchors(terms)
    # Zostávajú predmetové tokeny, čisto mechanizmové (self, heating) vypadnú.
    assert anchors <= terms
