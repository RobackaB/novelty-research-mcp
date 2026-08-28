"""Testy vylepšení vyhľadávania: expanzia dotazov, XHR provider, arXiv, korohorácia."""

from __future__ import annotations

import json

import tools.publications_search as ps
import tools.research_session as rs
from tools.patent_search import _google_patents_xhr_search
from tools.publications_search import _relevant_abstract_excerpt
from tools.query_expansion import applicable_synonyms, synonym_query_variants
from tools.user_answer import _mark_cross_source_corroboration, build_user_answer_payload


# --- Expanzia dotazov -------------------------------------------------------

def test_synonym_variants_for_acronym():
    variants = synonym_query_variants("IoT device for anomaly detection", max_variants=3)
    assert variants
    joined = " ".join(variants).lower()
    assert "internet of things" in joined or "outlier detection" in joined


def test_synonym_variants_preserve_rest_of_query():
    variants = synonym_query_variants("smart door lock with mobile application", max_variants=2)
    assert variants
    assert all("door lock" in variant.lower() for variant in variants)


def test_synonym_variants_empty_for_no_matches():
    assert synonym_query_variants("underwater basket weaving techniques") == []
    assert synonym_query_variants("") == []


def test_applicable_synonyms_listed():
    synonyms = applicable_synonyms("machine learning for predictive maintenance")
    lowered = {synonym.lower() for synonym in synonyms}
    assert "ml" in lowered
    assert "condition-based maintenance" in lowered


def test_envelope_contains_synonyms_and_expanded_variants():
    envelope = rs._build_query_envelope(
        "smart door lock with a mobile application using machine learning"
    )
    assert envelope["synonyms"], "synonyms pole už nemá byť prázdne"
    patent_variants = envelope["query_variants"]["patent"]
    assert any("ml" in variant.lower().split() or "intelligent" in variant.lower() for variant in patent_variants), patent_variants


# --- Google Patents XHR provider -------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.requested = None

    async def get(self, url, params=None, headers=None):
        self.requested = (url, params)
        return _FakeResponse(self._payload)


async def test_google_patents_xhr_parses_results():
    payload = {
        "results": {
            "cluster": [
                {
                    "result": [
                        {"patent": {
                            "publication_number": "US1234567B2",
                            "title": " Smart <b>lock</b> ",
                            "snippet": "A smart lock with mobile app control.",
                        }},
                        {"patent": {
                            "publication_number": "WO2021110453A1",
                            "title": "Smart lock having an electromechanical key",
                            "snippet": "Locking mechanism configured to switch.",
                        }},
                        {"patent": {"publication_number": "", "title": "no number"}},
                    ]
                }
            ]
        }
    }
    client = _FakeClient(payload)
    candidates = await _google_patents_xhr_search(client, "smart lock", 10)
    assert len(candidates) == 2
    first = candidates[0]
    assert first.patent_number == "US1234567B2"
    assert first.url == "https://patents.google.com/patent/US1234567B2/en"
    assert first.provider == "google_patents_xhr"
    assert "lock" in first.title.lower()
    assert client.requested[0].endswith("/xhr/query")


async def test_google_patents_xhr_empty_payload():
    client = _FakeClient({"results": {}})
    assert await _google_patents_xhr_search(client, "query", 10) == []


# --- arXiv paralelný provider -----------------------------------------------

async def test_arxiv_blocks_adapter(monkeypatch):
    arxiv_output = (
        "STATUS: OK\nCOMPLETED: TRUE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 0\n\n"
        "Publication title returned by ArXiv: **Smart lock security with mobile applications**.\n"
        "Publication year returned by ArXiv: 2023.\n"
        "Authors listed for this publication result: Jane Doe.\n"
        "Abstract excerpt from this publication record: We analyze smart door locks controlled "
        "by mobile applications with temporary access codes.\n"
        "ArXiv URL for this publication: https://arxiv.org/abs/2301.00001.\n"
        "Direct PDF download link for the paper: https://arxiv.org/pdf/2301.00001.\n"
        "SOURCE: ArXiv"
    )

    async def fake_arxiv_search(query, max_results=5):
        return arxiv_output

    monkeypatch.setattr(ps, "arxiv_search", fake_arxiv_search)
    blocks = await ps._arxiv_blocks_safe(
        ["smart door lock mobile application access codes"],
        "smart door lock mobile application access codes",
        5,
    )
    assert len(blocks) == 1
    score, block = blocks[0]
    assert score > 0
    assert "SOURCE: ArXiv" in block
    assert "Local rerank score" in block
    # Blok musí byť kompatibilný s deduplikáciou podľa názvu.
    _doi, title_key = ps._publication_dedupe_keys(block)
    assert title_key == "smart lock security with mobile applications"


async def test_arxiv_blocks_adapter_failure_is_empty(monkeypatch):
    async def fake_arxiv_search(query, max_results=5):
        raise RuntimeError("network down")

    monkeypatch.setattr(ps, "arxiv_search", fake_arxiv_search)
    assert await ps._arxiv_blocks_safe(["q"], "q", 5) == []


# --- Query-focused výňatky abstraktov ---------------------------------------

def test_relevant_abstract_excerpt_picks_matching_sentence():
    abstract = (
        "This paper surveys many unrelated industrial topics in great depth and detail. "
        "It also reviews historical developments of manufacturing over decades of research. "
        "Finally, we present a smart door lock controlled by a mobile application with access codes."
    )
    excerpt = _relevant_abstract_excerpt(
        abstract, "smart door lock mobile application access codes", max_words=20
    )
    assert "smart door lock" in excerpt.lower()
    assert "historical developments" not in excerpt.lower()


def test_relevant_abstract_excerpt_short_abstract_untouched():
    abstract = "Short abstract about locks."
    assert _relevant_abstract_excerpt(abstract, "locks", max_words=60) == abstract


# --- Kros-zdrojová korohorácia ----------------------------------------------

def _source(source_type, hits):
    return {
        "source_type": source_type,
        "status": "ok",
        "completed": True,
        "reliable_no_results": not hits,
        "hits": hits,
        "errors": [],
        "warnings": [],
    }


def _corroborated_pack():
    patent_hit = {
        "title": "Smart lock patent",
        "url": "https://patents.google.com/patent/US1234567B2/en",
        "patent_number": "US1234567B2",
        "evidence_level": "claim_verified",
        "summary": "Smart lock with mobile app.",
        "verified_url": True,
        "relevance": "focused",
        "relevance_score": 8.0,
    }
    web_hit = {
        "title": "Google Patents page found via web",
        "url": "https://patents.google.com/patent/US1234567B1/en",
        "patent_number": "US1234567B1",
        "evidence_level": "fetched_excerpt",
        "summary": "Same patent found through web search.",
        "verified_url": True,
        "relevance": "direct",
        "relevance_score": 7.0,
    }
    return {
        "source_type": "merged_evidence_pack",
        "query": "smart lock",
        "overall_status": "complete",
        "patents": _source("patent", [patent_hit]),
        "publications": _source("publication", []),
        "web": _source("web", [web_hit]),
        "flags": {},
        "warnings": [],
        "errors": [],
    }


def test_cross_source_corroboration_detects_same_patent_family():
    pack = _corroborated_pack()
    labels = _mark_cross_source_corroboration(pack)
    assert labels, "patent a web zdieľajú rovnaké patentové číslo (bez kind kódu)"
    assert any("patent" in label and "web" in label for label in labels)
    assert pack["patents"]["hits"][0].get("cross_source_corroborated") is True
    assert pack["web"]["hits"][0].get("cross_source_corroborated") is True


def test_cross_source_corroboration_absent_for_distinct_documents():
    pack = _corroborated_pack()
    pack["web"]["hits"][0]["url"] = "https://example.com/product"
    pack["web"]["hits"][0].pop("patent_number")
    assert _mark_cross_source_corroboration(pack) == []


def test_payload_and_answer_include_corroboration():
    payload = build_user_answer_payload(
        merged_pack=_corroborated_pack(),
        original_query="smart lock",
    )
    assert payload["corroborated_documents"], payload
    answer = payload["user_answer"]
    assert "Cross-source corroboration" in answer
    assert "corroborated across sources" in answer
