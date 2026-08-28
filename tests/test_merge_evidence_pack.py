"""Testy zlučovania dôkazov do spoločného wrapperu."""

from __future__ import annotations

import json

from tools.merge_evidence_pack import merge_evidence_pack


def _source(source_type: str, hits=None, status="ok", completed=True, reliable_no_results=False):
    return {
        "source_type": source_type,
        "status": status,
        "completed": completed,
        "reliable_no_results": reliable_no_results,
        "hits": hits if hits is not None else [],
        "errors": [],
        "warnings": [],
    }


def _hit(title="Hit", url="https://example.com"):
    return {
        "title": title,
        "url": url,
        "evidence_level": "search_snippet_only",
        "summary": "Summary text",
        "verified_url": False,
    }


def test_merge_three_sources_complete():
    merged = json.loads(
        merge_evidence_pack(
            patent_evidence_json=_source("patent", [_hit("P")]),
            publication_evidence_json=_source("publication", [_hit("Q")]),
            web_evidence_json=_source("web", [_hit("W")]),
            original_query="smart lock",
        )
    )
    assert merged["source_type"] == "merged_evidence_pack"
    assert merged["overall_status"] == "complete"
    assert merged["query"] == "smart lock"
    assert merged["flags"]["has_patent_evidence"] is True
    assert merged["flags"]["has_web_evidence"] is True


def test_merge_missing_inputs_failed():
    merged = json.loads(merge_evidence_pack(original_query="q"))
    assert merged["overall_status"] == "failed"
    assert merged["patents"]["status"] == "failed"
    assert any(err.get("type") == "missing_input" for err in merged["patents"]["errors"])


def test_merge_accepts_json_strings():
    merged = json.loads(
        merge_evidence_pack(
            patent_evidence_json=json.dumps(_source("patent", [_hit()])),
            original_query="q",
        )
    )
    assert merged["patents"]["status"] == "ok"
    assert len(merged["patents"]["hits"]) == 1


def test_merge_can_claim_no_only_for_reliable_empty_source():
    merged = json.loads(
        merge_evidence_pack(
            patent_evidence_json=_source("patent", [], reliable_no_results=True),
            publication_evidence_json=_source("publication", [_hit()]),
            web_evidence_json=_source("web", [], status="failed", completed=False),
            original_query="q",
        )
    )
    assert merged["flags"]["may_claim_no_patents"] is True
    assert merged["flags"]["may_claim_no_publications"] is False
    assert merged["flags"]["may_claim_no_web"] is False


def test_merge_legacy_text_bundle():
    bundle = (
        "ORIGINAL_QUERY: smart lock\n"
        f"PATENT_EVIDENCE_JSON: {json.dumps(_source('patent', [_hit()]))}\n"
        f"PUBLICATION_EVIDENCE_JSON: {json.dumps(_source('publication'))}\n"
        f"WEB_EVIDENCE_JSON: {json.dumps(_source('web'))}\n"
    )
    merged = json.loads(merge_evidence_pack(evidence_bundle=bundle))
    assert merged["query"] == "smart lock"
    assert merged["patents"]["status"] == "ok"
    assert len(merged["patents"]["hits"]) == 1


def test_merge_repairs_url_whitespace():
    hit = _hit(url="https://exa mple.com/page")
    merged = json.loads(
        merge_evidence_pack(patent_evidence_json=_source("patent", [hit]), original_query="q")
    )
    assert merged["patents"]["hits"][0]["url"] == "https://example.com/page"


def test_merge_invalid_status_normalized_to_failed():
    source = _source("web", [_hit()], status="banana")
    merged = json.loads(merge_evidence_pack(web_evidence_json=source, original_query="q"))
    assert merged["web"]["status"] == "failed"
