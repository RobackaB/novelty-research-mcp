"""Testy tvorby používateľskej odpovede."""

from __future__ import annotations

import json

from tools.user_answer import (
    _detect_language,
    _pseudo_atoms_from_requirements,
    _sanitize_summary,
    _trim_to_words,
    build_user_answer_payload,
)


def _source(source_type, hits, status="ok", completed=True, reliable_no_results=False):
    return {
        "source_type": source_type,
        "status": status,
        "completed": completed,
        "reliable_no_results": reliable_no_results,
        "hits": hits,
        "errors": [],
        "warnings": [],
    }


def _merged(patent_hits=None, publication_hits=None, web_hits=None):
    return {
        "source_type": "merged_evidence_pack",
        "query": "smart door lock mobile application",
        "overall_status": "complete",
        "patents": _source("patent", patent_hits or []),
        "publications": _source("publication", publication_hits or [], reliable_no_results=not publication_hits),
        "web": _source("web", web_hits or [], reliable_no_results=not web_hits),
        "flags": {},
        "warnings": [],
        "errors": [],
    }


def _strong_patent_hit():
    return {
        "title": "Smart door lock with mobile application",
        "url": "https://patents.google.com/patent/US1234567B2/en",
        "patent_number": "US1234567B2",
        "evidence_level": "claim_verified",
        "summary": "A smart door lock unlocked by a mobile application with temporary access codes and entry history.",
        "verified_url": True,
        "relevance": "focused",
        "relevance_score": 8.0,
    }


def test_payload_structure_and_verdict():
    payload = build_user_answer_payload(
        merged_pack=json.dumps(_merged(patent_hits=[_strong_patent_hit()])),
        original_query="smart door lock mobile application",
    )
    assert payload["source_type"] == "research_session_user_answer"
    assert payload["verdict"] == "close_prior_art"
    assert payload["confidence"] in {"low", "medium", "high"}
    assert payload["word_count"] > 0
    assert payload["source_counts"]["patent"] == 1
    assert "## Summary" in payload["user_answer"]
    assert "US1234567B2" in payload["user_answer"]


def test_payload_language_slovak_for_non_ascii_query():
    payload = build_user_answer_payload(
        merged_pack=_merged(patent_hits=[_strong_patent_hit()]),
        original_query="inteligentný dverový zámok",
    )
    assert payload["language"] == "sk"
    assert "## Zhrnutie" in payload["user_answer"]


def test_payload_language_from_envelope_non_english():
    payload = build_user_answer_payload(
        merged_pack=_merged(),
        original_query="plain ascii query",
        query_envelope={"language": "non_english"},
    )
    assert payload["language"] == "sk"


def test_detect_language_variants():
    assert _detect_language("hello", {"language": "en"}) == "en"
    assert _detect_language("hello", {"language": "non_english"}) == "sk"
    assert _detect_language("zámok", None) == "sk"
    assert _detect_language("lock", None) == "en"


def test_per_requirement_table_rendered_for_atomic_requirements():
    envelope = {
        "language": "en",
        "critical_requirements": ["mobile application unlock", "entry history"],
        "critical_requirements_atomic": [
            {"category": "function", "label": "mobile application unlock", "terms": ["mobile", "application"]},
            {"category": "function", "label": "entry history", "terms": ["entry", "history"]},
        ],
    }
    payload = build_user_answer_payload(
        merged_pack=_merged(patent_hits=[_strong_patent_hit()]),
        original_query="smart door lock mobile application entry history",
        query_envelope=envelope,
    )
    answer = payload["user_answer"]
    assert "Element-by-element coverage" in answer
    assert "| Element | Status |" in answer
    statuses = {row["status"] for row in payload["critical_requirement_statuses"]}
    assert statuses <= {"verified", "partially_indicated", "not_verified"}
    assert any(row["status"] == "verified" for row in payload["critical_requirement_statuses"])


def test_pseudo_atoms_used_when_atomic_missing():
    envelope = {
        "language": "en",
        "critical_requirements": ["mobile application unlock"],
        "critical_requirements_atomic": [],
    }
    payload = build_user_answer_payload(
        merged_pack=_merged(patent_hits=[_strong_patent_hit()]),
        original_query="smart door lock mobile application",
        query_envelope=envelope,
    )
    assert payload["critical_requirement_statuses"], "pseudo-atomy majú vyplniť tabuľku pokrytia"
    assert "Element-by-element coverage" in payload["user_answer"]


def test_pseudo_atoms_builder():
    atoms = _pseudo_atoms_from_requirements(["mobile application unlock", ""])
    assert len(atoms) == 1
    assert atoms[0]["label"] == "mobile application unlock"
    assert atoms[0]["terms"]


def test_sanitize_summary_drops_tables_and_non_ascii():
    assert _sanitize_summary("| a | b | c |", 100, "en") == ""
    # Prevažne ne-ASCII text (nad 40 % znakov) sa v anglickom výstupe zahodí.
    assert _sanitize_summary("智能门锁系统分析研究报告 " * 5, 200, "en") == ""
    # Slovenčina s diakritikou je pod prahom a zostáva zachovaná.
    assert _sanitize_summary("čisto slovenský text", 200, "en") == "čisto slovenský text"
    assert _sanitize_summary("plain summary", 100, "en") == "plain summary"


def test_trim_to_words_limits_length():
    text = "\n".join(["word " * 50] * 10)
    trimmed = _trim_to_words(text, 60)
    assert len(trimmed.split()) <= 61  # +1 pre pripojené "..."


def test_failed_pack_yields_partial_retrieval():
    payload = build_user_answer_payload(merged_pack="not json at all", original_query="query")
    assert payload["verdict"] == "partial_retrieval"
    assert payload["confidence"] == "low"
