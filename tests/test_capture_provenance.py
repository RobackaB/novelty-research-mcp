"""Origin provenance must survive dedupe, ranking and SQLite persistence.

patent_search calls each provider once per query variant. A candidate's
query_variant must be the variant that actually produced THAT occurrence, not
the normalised query, and provider must be the provider that returned it.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import tools.patent_search as patent_search
import tools.research_session as rs
from tools.decision_capture import CollectorContext, DecisionCollector

QUERY = "smart door lock controlled by a mobile application with access codes"


def _candidate(number: str, title: str, provider: str) -> patent_search.PatentCandidate:
    return patent_search.PatentCandidate(
        title=title,
        url=f"https://patents.google.com/patent/{number}/en",
        patent_number=number,
        snippet="Smart door lock unlocked by a mobile application using access codes.",
        provider=provider,
    )


@pytest.fixture
def two_variants_two_providers(monkeypatch):
    """Each provider returns a distinct candidate per variant it is called with."""
    seen: list[str] = []

    async def alpha(_client, variant, _max_results=10):
        seen.append(variant)
        return [_candidate(f"USA{len(seen)}B2", f"Alpha lock {variant[:18]}", "alpha_provider")]

    async def beta(_client, variant, _max_results=10):
        return [_candidate(f"USB{abs(hash(variant)) % 97}B2", f"Beta lock {variant[:18]}", "beta_provider")]

    async def empty(_client, _variant, _max_results=10):
        return []

    monkeypatch.setattr(patent_search, "_google_patents_xhr_search", alpha)
    monkeypatch.setattr(patent_search, "_tavily_patent_search", beta)
    monkeypatch.setattr(patent_search, "_exa_patent_search", empty)
    monkeypatch.setattr(patent_search, "_wipo_patentscope_search", empty)
    patent_search._PATENT_SEARCH_CACHE.clear()
    return seen


def test_more_than_one_variant_is_actually_executed(two_variants_two_providers):
    """The fixture is only meaningful if the real code issues several variants."""
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    asyncio.run(patent_search.patent_search(QUERY, 6, _collector=collector))
    assert len(set(two_variants_two_providers)) > 1, (
        f"only one variant executed: {two_variants_two_providers}"
    )


def test_each_event_records_the_variant_that_produced_it(two_variants_two_providers):
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    asyncio.run(patent_search.patent_search(QUERY, 6, _collector=collector))
    # Across all captured stages, not just one: dedupe removes some occurrences
    # before ranking, so a single stage may legitimately see one variant.
    assert collector.events, f"no events: {collector.events}"
    variants = {e["query_variant"] for e in collector.events}
    assert len(variants) > 1, (
        f"every event recorded the same variant, so origin was substituted: {variants}"
    )
    assert all(v in two_variants_two_providers for v in variants if v), variants
    # the normalised query must not stand in for every executed variant
    assert variants != {QUERY}, "normalized_query substituted for real provenance"


def test_each_event_records_the_provider_that_returned_it(two_variants_two_providers):
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    asyncio.run(patent_search.patent_search(QUERY, 6, _collector=collector))
    # _origin_of reports the provider REGISTRY name -- the call that actually
    # executed -- which is stronger provenance than a candidate's self-declared
    # provider field.
    providers = {e["provider"] for e in collector.events}
    assert {"google_patents_xhr", "tavily"} <= providers, providers


def test_origin_values_survive_sqlite_persistence(two_variants_two_providers, temp_db):
    collector = DecisionCollector(
        context=CollectorContext(session_id="s", run_id="r", source_type="patent", attempt=1)
    )
    asyncio.run(patent_search.patent_search(QUERY, 6, _collector=collector))
    rs._persist_decision_events(collector)

    with sqlite3.connect(rs._db_path()) as conn:
        rows = conn.execute(
            "SELECT provider, query_variant, decision_query FROM evaluation_candidate_decisions"
        ).fetchall()
    assert rows, "no persisted anchor rows"
    assert {"google_patents_xhr", "tavily"} <= {r[0] for r in rows}, rows
    # decision_query is the scorer's input and is legitimately the same for all
    assert len({r[2] for r in rows}) == 1
    # the executed variant must not have been replaced by the scorer's input
    assert any(r[1] and r[1] != r[2] for r in rows), f"variant collapsed into decision_query: {rows}"


def test_unknown_origin_stays_empty_rather_than_guessed():
    """A candidate with no recorded origin must not inherit the normalised query."""
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    patent_search._rank(QUERY, [_candidate("US9B2", "Orphan lock", "x")], 6,
                        _collector=collector, _origins=None)
    anchors = [e for e in collector.events if e["decision_stage"] == "domain_anchor"]
    assert anchors
    assert anchors[0]["query_variant"] == "", anchors[0]


# --- publication fallback: the ArXiv argument is the executed query -----------

def _expected_search_queries(pubs, query: str) -> list[str]:
    """Recompute the search variants exactly as publications_search does."""
    variants = pubs.section_query_variants(
        query, "PUBLICATION_QUERIES", max_variants=2,
        legacy_marker="publication search queries",
    )
    # Mirrors the production condition exactly: a section that yields only the
    # raw query is treated as absent and the fallback variants are used instead.
    if variants == [(query or "").strip()]:
        variants = pubs._fallback_publication_queries(query, max_variants=2)
    return variants


PUB_QUERY = "smart door lock controlled by a mobile application with access codes"


def _arxiv_block() -> str:
    """A production-format block the publication parser recognises."""
    import tools.publications_search as pubs

    # Production format, but reporting arxiv as the source. _crossref_block
    # emits its own "SOURCE: Crossref" line and _extract_block_provider takes the
    # first match, so the marker has to be replaced rather than appended.
    block = pubs._crossref_block(
        "Smart door lock access control study",
        "A. Author",
        "2023",
        "Smart door locks controlled by a mobile application using access codes.",
        "https://doi.org/10.1000/lock",
        7.0,
    )
    return block.replace("SOURCE: Crossref", "SOURCE: arxiv")


@pytest.fixture
def rate_limited_publication_fallback(monkeypatch):
    """Force the rate-limited branch and record ArXiv's actual argument."""
    import tools.publications_search as pubs

    recorded: dict[str, str] = {}

    async def semantic_scholar_rate_limited(*_a, **_k):
        return [], False, True

    async def empty(*_a, **_k):
        return []

    async def fake_arxiv(query, _max_results):
        recorded["arxiv_argument"] = query
        return _arxiv_block()

    monkeypatch.setattr(pubs, "_semantic_scholar_blocks", semantic_scholar_rate_limited)
    for name in ("_crossref_blocks_safe", "_openalex_blocks_safe", "_pubmed_blocks_safe",
                 "_alpha_blocks_safe", "_arxiv_blocks_safe"):
        monkeypatch.setattr(pubs, name, empty)
    monkeypatch.setattr(pubs, "arxiv_search", fake_arxiv)
    return recorded


def test_fallback_records_the_executed_arxiv_query_not_the_scoring_query(
    rate_limited_publication_fallback,
):
    import tools.publications_search as pubs

    collector = DecisionCollector(context=CollectorContext(source_type="publication"))
    asyncio.run(pubs.publications_search(PUB_QUERY, 5, _collector=collector))

    executed = rate_limited_publication_fallback.get("arxiv_argument")
    assert executed, "the rate-limited fallback never called arxiv_search"

    events = [e for e in collector.events if e["decision_stage"] == "provider_filter"]
    assert events, f"no fallback events captured: {collector.events}"
    event = events[0]

    assert event["query_variant"] == executed, (
        f"query_variant {event['query_variant']!r} is not the executed ArXiv "
        f"argument {executed!r}"
    )
    assert event["provider"] == "arxiv", event
    # The scoring query is the joined relevance_query, unchanged by the fix.
    expected_scoring_query = " ".join(_expected_search_queries(pubs, PUB_QUERY))
    assert event["decision_query"] == expected_scoring_query, (
        f"decision_query {event['decision_query']!r} is not the unchanged scoring input"
    )
    assert event["decision_query"] != event["query_variant"], event


def test_fallback_provenance_survives_persistence(rate_limited_publication_fallback, temp_db):
    import tools.publications_search as pubs

    collector = DecisionCollector(
        context=CollectorContext(session_id="s", run_id="r", source_type="publication", attempt=1)
    )
    asyncio.run(pubs.publications_search(PUB_QUERY, 5, _collector=collector))
    rs._persist_decision_events(collector)

    executed = rate_limited_publication_fallback["arxiv_argument"]
    with sqlite3.connect(rs._db_path()) as conn:
        rows = conn.execute(
            "SELECT query_variant, decision_query, provider FROM "
            "evaluation_candidate_decisions WHERE decision_stage = 'provider_filter'"
        ).fetchall()
    assert rows, "no persisted fallback rows"
    assert rows[0][0] == executed, f"executed query lost in storage: {rows}"
    assert rows[0][2] == "arxiv"
    expected_scoring_query = " ".join(_expected_search_queries(pubs, PUB_QUERY))
    assert rows[0][1] == expected_scoring_query, (
        f"persisted decision_query {rows[0][1]!r} is not the unchanged scoring input"
    )
    assert rows[0][0] != rows[0][1], "executed query collapsed into the scoring query"


# --- non-document metadata must never become a labelling example --------------

def test_provider_marker_header_is_captured_but_not_dataset_eligible():
    """The STATUS/QUERY header scores like a candidate; it is not a document.

    Reproduced on the base commit too: prepend_markers emits the header as its
    own block, the splitter scores it, and it echoes the query so it scores well.
    Production filtering is unchanged -- only labelling eligibility differs.
    """
    import tools.publications_search as pubs
    from tools.result_contract import NormalizedResult, prepend_markers

    header_and_body = prepend_markers(
        NormalizedResult(status="ok", completed=True, reliable_no_results=False, query=PUB_QUERY),
        _arxiv_block(),
    )
    collector = DecisionCollector(context=CollectorContext(source_type="publication"))
    pubs._filter_publication_text(PUB_QUERY, header_and_body, 5, _collector=collector)

    by_eligibility = {e["dataset_eligible"] for e in collector.events}
    assert collector.events, "nothing captured"
    headers = [e for e in collector.events if "STATUS:" in e["score_text"]]
    documents = [e for e in collector.events if "STATUS:" not in e["score_text"]]
    assert headers, "the marker header block was not scored, so the fixture is wrong"
    assert all(e["dataset_eligible"] is False for e in headers), headers
    assert documents and all(e["dataset_eligible"] is True for e in documents), documents
    assert by_eligibility == {True, False}


def test_marker_header_eligibility_survives_persistence(temp_db):
    import tools.publications_search as pubs
    from tools.result_contract import NormalizedResult, prepend_markers

    collector = DecisionCollector(
        context=CollectorContext(session_id="s", source_type="publication", attempt=1)
    )
    pubs._filter_publication_text(
        PUB_QUERY,
        prepend_markers(
            NormalizedResult(status="ok", completed=True, reliable_no_results=False, query=PUB_QUERY),
            _arxiv_block(),
        ),
        5,
        _collector=collector,
    )
    rs._persist_decision_events(collector)
    with sqlite3.connect(rs._db_path()) as conn:
        rows = conn.execute(
            "SELECT dataset_eligible, score_text FROM evaluation_candidate_decisions"
        ).fetchall()
    assert rows
    assert any(r[0] == 0 and "STATUS:" in r[1] for r in rows), rows
    assert any(r[0] == 1 and "STATUS:" not in r[1] for r in rows), rows
