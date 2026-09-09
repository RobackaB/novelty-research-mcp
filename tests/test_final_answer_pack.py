"""Tests of building the final debug answer."""

from __future__ import annotations

import json

from tools.final_answer_pack import final_answer_pack, normalize_final_response


def _merged(patent_hits=None, publication_hits=None, web_hits=None, overall="complete"):
    def _source(source_type, hits):
        return {
            "source_type": source_type,
            "status": "ok",
            "completed": True,
            "reliable_no_results": not hits,
            "hits": hits or [],
            "errors": [],
            "warnings": [],
        }

    return {
        "source_type": "merged_evidence_pack",
        "query": "smart lock",
        "overall_status": overall,
        "patents": _source("patent", patent_hits),
        "publications": _source("publication", publication_hits),
        "web": _source("web", web_hits),
        "flags": {},
        "warnings": [],
        "errors": [],
    }


def _hit(title="Hit", url="https://example.com", evidence_level="abstract_verified"):
    return {
        "title": title,
        "url": url,
        "evidence_level": evidence_level,
        "summary": "Summary of evidence",
        "verified_url": True,
        "relevance": "focused",
    }


def test_final_answer_contains_all_sections():
    answer = final_answer_pack(_merged(patent_hits=[_hit("Patent A")]), original_query="smart lock")
    for heading in (
        "## 1. Direct Answer",
        "## 2. Strongest Patent Evidence",
        "## 3. Strongest Publication Evidence",
        "## 4. Strongest Web Evidence",
        "## 5. Important Gaps Or Incomplete Retrieval",
        "## 6. Patentability Risk Assessment",
        "## 7. Sources",
    ):
        assert heading in answer
    assert "Patent A" in answer
    assert "Patent hits: 1." in answer


def test_final_answer_accepts_json_string():
    answer = final_answer_pack(json.dumps(_merged(web_hits=[_hit("Web A")])), original_query="q")
    assert "Web A" in answer


def test_final_answer_malformed_pack_is_conservative():
    answer = final_answer_pack("totally not a pack", original_query="q")
    assert "cannot support a novelty" in answer
    assert "`failed`" in answer


def test_normalize_final_response_unwraps_text_content():
    wrapped = {"content": [{"type": "text", "text": "final text"}]}
    assert normalize_final_response(wrapped) == "final text"
    assert normalize_final_response("plain") == "plain"
    assert normalize_final_response(None) == ""
