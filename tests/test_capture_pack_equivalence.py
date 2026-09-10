"""P8: pack equivalence through the real pack -> search -> hook path.

External I/O is mocked *below* the instrumented search functions, not by
replacing them, so `publications_search`, `patent_search` and `web_search` run
for real and their capture hooks execute. That is what makes the equivalence
claim meaningful: an earlier version of this file stubbed the search functions
wholesale, and every test still passed with collector forwarding removed.

Each source compares the exact serialized output four ways -- pinned base,
capture disabled, capture enabled, raising collector -- and asserts that the
enabled run actually emitted events and that the raising collector actually had
`record` called on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib

import pytest

import tools.patent_evidence_pack as pep
import tools.patent_search as patent_search
import tools.publication_evidence_pack as pub
import tools.publications_search as pubs
import tools.web_evidence_pack as wep
import tools.web_search as web_search
from tools.decision_capture import CollectorContext, DecisionCollector

QUERY = "smart door lock controlled by a mobile application with access codes"
VERIFY_OK = "URL_STATUS: https://example.com/a -> ok\n"


class CountingRaisingCollector:
    """Raises on every record, and counts that it was actually called."""

    def __init__(self) -> None:
        self.attempts = 0
        self.events: list = []

    def record(self, **_fields):
        self.attempts += 1
        raise RuntimeError("capture exploded")


def _collector(source_type: str) -> DecisionCollector:
    return DecisionCollector(
        context=CollectorContext(session_id="s", run_id="r", source_type=source_type, attempt=1)
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Pinned output generated from the PRE-CAPTURE base commit with these exact
# fixtures. A same-tree fourth run is not a baseline: it moves whenever the
# patched implementation moves, so common drift across all modes goes unnoticed.
_BASELINE_PATH = pathlib.Path(__file__).parent / "fixtures" / "pack_baseline_cbfd5ba.json"
BASELINE = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
BASE_COMMIT = "cbfd5babe2ba7f041c963384bdaa06a874cc6f39"


def test_baseline_fixture_records_its_provenance():
    assert BASELINE["_provenance"]["base_commit"] == BASE_COMMIT
    assert set(BASELINE["packs"]) == {"patent", "publication", "web"}


# --- mocks placed BELOW the instrumented search functions ---------------------

def _mock_patent_io(monkeypatch):
    """Replace the four provider queries, leaving _dedupe/_rank/_score real."""
    async def provider(_client, _variant, _max_results=10):
        return [
            patent_search.PatentCandidate(
                title="Smart door lock with mobile application",
                url="https://patents.google.com/patent/US1111111B2/en",
                patent_number="US1111111B2",
                snippet="Smart door lock unlocked by a mobile application using access codes.",
                provider="google_patents_xhr",
            ),
            patent_search.PatentCandidate(
                title="Smart door lock with mobile application",
                url="https://patents.google.com/patent/US3333333B2/en",
                patent_number="US3333333B2",
                snippet="Duplicate family member of the same smart door lock application.",
                provider="google_patents_xhr",
            ),
            patent_search.PatentCandidate(
                title="Method and system for comparing images",
                url="https://patents.google.com/patent/US2222222B2/en",
                patent_number="US2222222B2",
                snippet="A method, system and computer program for comparing images.",
                provider="google_patents_xhr",
            ),
        ]

    async def empty(_client, _variant, _max_results=10):
        return []

    monkeypatch.setattr(patent_search, "_google_patents_xhr_search", provider)
    monkeypatch.setattr(patent_search, "_tavily_patent_search", empty)
    monkeypatch.setattr(patent_search, "_exa_patent_search", empty)
    monkeypatch.setattr(patent_search, "_wipo_patentscope_search", empty)

    async def fake_fetch(url, timeout_ms=18000, pdf_url=""):
        return (
            "PATENT_NUMBER: US1111111B2 was identified from the page.\n"
            "CLAIM1: 1. A smart door lock comprising a mobile application and access codes.\n"
            "ABSTRACT: A smart door lock unlocked by a mobile application.\n"
            "STATUS: OK was returned for this extraction.\n"
            "EVIDENCE_LEVEL: CLAIM_VERIFIED\nPROVIDER: google_patents\n"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pep, "patent_fetch", fake_fetch)
    monkeypatch.setattr(pep, "verify_sources", fake_verify)


def _mock_web_io(monkeypatch):
    """Replace SEARCH_PROVIDERS, leaving the merge/gate/rerank path real."""
    async def provider(_client, _query, _max_results):
        return [
            ("https://example.com/a", "Smart door lock product page",
             "Smart door lock with mobile application control and access codes."),
            ("https://example.com/a", "Duplicate smart lock page",
             "Smart door lock mobile application access codes duplicate copy."),
            ("https://example.com/off", "Quarterly earnings report",
             "Financial results for the fiscal year with revenue figures."),
        ]

    monkeypatch.setattr(web_search, "SEARCH_PROVIDERS", (("stub_provider", provider),))

    async def fake_fetch(url, timeout_ms=14000, query=""):
        return (
            "CONTENT: Smart door lock controlled by a mobile application with access codes.\n"
            "STATUS: OK\nEVIDENCE_LEVEL: FETCHED_EXCERPT\n"
        )

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(wep, "web_fetch", fake_fetch)
    monkeypatch.setattr(wep, "verify_sources", fake_verify)


def _mock_publication_io(monkeypatch):
    """Replace the provider block runners, leaving filter/rerank/dedup real.

    Two contract details matter. _semantic_scholar_blocks returns
    (blocks, errored, rate_limited), so a generic [] stub breaks tuple unpacking
    and silently diverts into fallback code. And the parser only recognises
    production-format blocks, so the fixture builds them with _crossref_block.
    """
    def _block(title: str, doi: str, abstract: str, score: float) -> str:
        return pubs._crossref_block(title, "A. Author", "2023", abstract,
                                    f"https://doi.org/{doi}", score)

    async def fake_crossref(_search_queries, _relevance_query, _max_results, *_a, **_k):
        return [
            (7.0, _block("Smart door lock access control study", "10.1000/lock",
                         "Smart door locks controlled by a mobile application using access codes.", 7.0)),
            (6.0, _block("Seismic wave classification", "10.1000/seis",
                         "Seismic wave classification for earthquake early warning systems.", 6.0)),
            # A third block is required: build_corpus_idf returns {} below
            # _MIN_CORPUS_FOR_IDF, so the rerank would exit before its hook.
            (5.5, _block("Mobile credential door access study", "10.1000/cred",
                         "Mobile application credentials for door access with temporary codes.", 5.5)),
        ]

    async def empty(*_a, **_k):
        return []

    async def empty_semantic_scholar(*_a, **_k):
        return [], False, False

    monkeypatch.setattr(pubs, "_crossref_blocks_safe", fake_crossref)
    monkeypatch.setattr(pubs, "_semantic_scholar_blocks", empty_semantic_scholar)
    for name in ("_openalex_blocks_safe", "_pubmed_blocks_safe", "_alpha_blocks_safe",
                 "_arxiv_blocks_safe"):
        monkeypatch.setattr(pubs, name, empty)

    async def fake_fetch(url, timeout_s=12.0):
        return ("TITLE: Smart door lock access control study\n"
                "ABSTRACT: Smart door locks controlled by a mobile application.\n"
                "STATUS: OK\nEVIDENCE_LEVEL: ABSTRACT_VERIFIED\n")

    async def fake_verify(urls, max_urls=20):
        return VERIFY_OK

    monkeypatch.setattr(pub, "publication_fetch", fake_fetch)
    monkeypatch.setattr(pub, "verify_sources", fake_verify)


CASES = [
    ("patent", _mock_patent_io, lambda **kw: pep.patent_evidence_pack(query=QUERY, **kw)),
    ("publication", _mock_publication_io, lambda **kw: pub.publication_evidence_pack(query=QUERY, **kw)),
    ("web", _mock_web_io, lambda **kw: wep.web_evidence_pack(query=QUERY, **kw)),
]


@pytest.mark.parametrize("source_type, mock_io, call", CASES, ids=[c[0] for c in CASES])
def test_pack_output_identical_across_all_capture_modes(source_type, mock_io, call, monkeypatch):
    mock_io(monkeypatch)

    def run(**kw):
        # patent_search memoises results in a module-level TTL cache. Without
        # clearing it, only the first run executes the scoring path and every
        # later run is served from cache, so capture would appear to emit
        # nothing and the equivalence assertion would be vacuous.
        patent_search._PATENT_SEARCH_CACHE.clear()
        return asyncio.run(call(**kw))

    base = BASELINE["packs"][source_type]            # pinned pre-capture output
    disabled = run(_collector=None)
    collector = _collector(source_type)
    enabled = run(_collector=collector)
    raising = CountingRaisingCollector()
    raised = run(_collector=raising)

    assert disabled == base, (
        f"{source_type} diverged from the pinned base {BASE_COMMIT[:8]}: "
        f"{_digest(base)} vs {_digest(disabled)}"
    )
    assert enabled == base, f"enabled diverged: {_digest(base)} vs {_digest(enabled)}"
    assert raised == base, f"raising diverged: {_digest(base)} vs {_digest(raised)}"

    # Equivalence must not pass because capture never ran.
    assert collector.events, f"{source_type}: capture enabled but no events emitted"
    assert raising.attempts > 0, f"{source_type}: raising collector was never called"


@pytest.mark.parametrize("source_type, mock_io, call", CASES, ids=[c[0] for c in CASES])
def test_removing_forwarding_would_be_detected(source_type, mock_io, call, monkeypatch):
    """The emission assertion is what catches broken forwarding.

    Simulated by passing a collector the pack cannot forward -- if forwarding
    were removed, this is exactly the state the enabled run would be in.
    """
    mock_io(monkeypatch)
    patent_search._PATENT_SEARCH_CACHE.clear()
    unforwarded = _collector(source_type)
    # no _collector argument: hooks cannot possibly fire
    asyncio.run(call())
    assert unforwarded.events == [], "sanity: an unpassed collector must stay empty"
