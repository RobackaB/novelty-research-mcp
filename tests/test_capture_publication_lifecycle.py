"""Exercise publication capture below the real provider response parsers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from xml.sax.saxutils import escape

import httpx
import pytest

import tools.arxiv_search as arxiv_module
import tools.publications_search as pubs
from tools.decision_capture import CollectorContext, DecisionCollector, query_fingerprint


QUERY = "smart door lock mobile application access codes"
PROVIDERS = ("semanticscholar", "crossref", "openalex", "pubmed", "alphaxiv", "arxiv")


def collector(query=QUERY):
    return DecisionCollector(context=CollectorContext(
        source_type="publication", query_fingerprint=query_fingerprint(query),
    ))


def records(query=QUERY):
    return [
        {"title": f"{query} study", "abstract": f"{query}. Tested system architecture.", "doi": "10.1234/KEEP", "id": "2401.00001"},
        {"title": "Ancient pottery catalog", "abstract": "Clay excavation artifacts and ceramic restoration.", "doi": "10.1234/DROP", "id": "2401.00002"},
    ]


def install_responses(monkeypatch, enabled, *, by_query=False, alpha_text=False, distinct=False):
    """Replace transport only, retaining provider parsing, filtering and rendering."""
    calls = []

    def rows(provider, query):
        calls.append((provider, query))
        result = records(query if by_query else QUERY) if provider in enabled else []
        if distinct:
            for row in result:
                row["title"] += " " + provider
                row["doi"] += "/" + provider
        return result

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params):
            await asyncio.sleep(0)
            if "semanticscholar" in url:
                data = {"data": [dict(title=r["title"], abstract=r["abstract"], externalIds={"DOI": r["doi"]}, url=f"https://www.semanticscholar.org/paper/{r['id']}", authors=[], year=2024) for r in rows("semanticscholar", params["query"])]}
            elif "crossref" in url:
                data = {"message": {"items": [dict(title=[r["title"]], abstract=f"<jats:p>{r['abstract']}</jats:p>", DOI=r["doi"], author=[], published={"date-parts": [[2024]]}) for r in rows("crossref", params["query"])]}}
            elif "openalex" in url:
                data = {"results": [dict(title=r["title"], abstract_inverted_index={word: [i for i, w in enumerate(r["abstract"].split()) if w == word] for word in r["abstract"].split()}, doi=r["doi"], id=f"https://openalex.org/{r['id']}", publication_year=2024) for r in rows("openalex", params["search"])]}
            elif "esearch" in url:
                self.pubmed_rows = rows("pubmed", params["term"])
                data = {"esearchresult": {"idlist": [str(i + 1) for i, _ in enumerate(self.pubmed_rows)]}}
            elif "efetch" in url:
                xml = "<PubmedArticleSet>" + "".join(
                    f"<PubmedArticle><MedlineCitation><PMID>{i + 1}</PMID><Article><ArticleTitle>{escape(r['title'])}</ArticleTitle><Abstract><AbstractText>{escape(r['abstract'])}</AbstractText></Abstract></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType='doi'>{r['doi']}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>"
                    for i, r in enumerate(self.pubmed_rows)
                ) + "</PubmedArticleSet>"
                return httpx.Response(200, text=xml, request=httpx.Request("GET", url))
            else:
                raise AssertionError(f"Unexpected network request: {url}")
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    async def alpha(query):
        await asyncio.sleep(0)
        result = rows("alphaxiv", query)
        if alpha_text:
            return [f"Title: {r['title']}\nAbstract: {r['abstract']}\nURL: https://arxiv.org/abs/{r['id']}v2" for r in result]
        return [{"papers": [dict(title=r["title"], abstract=r["abstract"], arxivId=r["id"] + "v2") for r in result]}]

    def arxiv(query, max_results):
        return [SimpleNamespace(title=r["title"], summary=r["abstract"], published=datetime(2024, 1, 1), authors=[], entry_id=f"https://arxiv.org/abs/{r['id']}v2", pdf_url=f"https://arxiv.org/pdf/{r['id']}v2") for r in rows("arxiv", query)]

    monkeypatch.setattr(pubs.httpx, "AsyncClient", Client)
    monkeypatch.setattr(pubs, "alphaxiv_discover_papers", alpha)
    monkeypatch.setattr(arxiv_module, "_run_search", arxiv)
    return calls


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_provider_filters_capture_exact_scored_text_and_identity(monkeypatch, provider):
    calls = install_responses(monkeypatch, {provider})
    scored = []
    original_score = pubs.evidence_score

    def observe_score(query, text, source_type, **kwargs):
        value = original_score(query, text, source_type, **kwargs)
        scored.append((query, text, value))
        return value

    monkeypatch.setattr(pubs, "evidence_score", observe_score)
    capture = collector()
    output = await pubs.publications_search(QUERY, 5, _collector=capture)
    gates = [e for e in capture.events if e["decision_stage"] == "provider_filter"]
    assert len(gates) == 2
    assert {e["retained"] for e in gates} == {True, False}
    assert all(e["provider"] == provider for e in gates)
    assert all((provider, e["query_variant"]) in calls for e in gates)
    assert all((e["decision_query"], e["score_text"], e["score_at_decision"]) in scored for e in gates)
    assert all(e["dataset_eligible"] for e in gates)
    expected_ids = {"arxiv:2401.00001", "arxiv:2401.00002"} if provider in {"arxiv", "alphaxiv"} else {"doi:10.1234/keep", "doi:10.1234/drop"}
    assert {e["candidate_identity"] for e in gates} == expected_ids
    accepted = next(e for e in gates if e["retained"])
    rejected = next(e for e in gates if not e["retained"])
    assert accepted["decision_reason"] == "accepted"
    assert rejected["decision_reason"] == "below_overlap_gate"
    assert accepted["threshold_at_decision"] == 3.5
    assert rejected["threshold_at_decision"] is None
    assert accepted["title"] in output and rejected["title"] not in output
    later = [e for e in capture.events if e["decision_stage"] == "truncation"]
    assert len(later) == 1 and later[0]["example_id"] == accepted["example_id"]
    assert later[0]["provider"] == provider and later[0]["query_variant"] == accepted["query_variant"]


async def test_alpha_text_response_records_rejected_documents(monkeypatch):
    install_responses(monkeypatch, {"alphaxiv"}, alpha_text=True)
    capture = collector()
    await pubs.publications_search(QUERY, 5, _collector=capture)
    gates = [e for e in capture.events if e["decision_stage"] == "provider_filter"]
    assert len(gates) == 2 and {e["retained"] for e in gates} == {True, False}
    assert {e["score_text"] for e in gates} == {f"{r['title']} {r['abstract']}" for r in records()}
    assert {e["candidate_identity"] for e in gates} == {"arxiv:2401.00001", "arxiv:2401.00002"}


async def test_cross_provider_dedupe_keeps_origin_and_shared_example_id(monkeypatch):
    install_responses(monkeypatch, {"semanticscholar", "crossref", "pubmed", "openalex"})
    capture = collector()
    await pubs.publications_search(QUERY, 5, _collector=capture)
    merged = [e for e in capture.events if e["decision_stage"] == "dedupe" and e["payload"]["scope"] == "merged"]
    assert len(merged) == 4
    assert {e["provider"] for e in merged} == {"semanticscholar", "crossref", "pubmed", "openalex"}
    assert [e["decision_reason"] for e in merged] == ["accepted", "duplicate_of", "duplicate_of", "duplicate_of"]
    assert len({e["example_id"] for e in merged}) == 1
    assert all(e["retained"] is None for e in merged)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_provider_dedupe_reports_actual_second_variant(monkeypatch, provider):
    install_responses(monkeypatch, {provider})
    variants = [QUERY, QUERY + " architecture"]
    monkeypatch.setattr(pubs, "section_query_variants", lambda *args, **kwargs: variants)
    capture = collector()
    await pubs.publications_search(QUERY, 5, _collector=capture)
    duplicates = [e for e in capture.events if e["decision_stage"] == "dedupe" and e["decision_reason"] == "duplicate_of" and e["payload"]["scope"] == "provider"]
    assert duplicates
    assert all(e["provider"] == provider and e["query_variant"] == variants[1] for e in duplicates)
    assert all(e["decision_query"] == " ".join(variants) for e in duplicates)
    first = [e for e in capture.events if e["decision_stage"] == "dedupe" and e["decision_reason"] == "accepted" and e["payload"]["scope"] == "provider"]
    assert {e["example_id"] for e in duplicates} <= {e["example_id"] for e in first}


async def test_global_limit_observes_dropped_candidates_without_relevance_verdict(monkeypatch):
    install_responses(monkeypatch, {"semanticscholar", "crossref", "pubmed", "openalex"}, distinct=True)
    capture = collector()
    output = await pubs.publications_search(QUERY, 2, _collector=capture)
    selected = [e for e in capture.events if e["decision_stage"] == "truncation"]
    assert len(selected) >= 3
    assert sum(e["decision_reason"] == "accepted" for e in selected) == 2
    assert any(e["decision_reason"] == "beyond_limit" for e in selected)
    assert all(e["retained"] is None for e in selected)
    for event in selected:
        assert (event["title"] in output) == (event["decision_reason"] == "accepted")
        gates = [e for e in capture.events if e["decision_stage"] == "provider_filter" and e["example_id"] == event["example_id"]]
        assert len(gates) == 1
        assert gates[0]["provider"] == event["provider"]
        assert gates[0]["query_variant"] == event["query_variant"]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_raising_and_disabled_collector_preserve_provider_output(monkeypatch, provider):
    calls = install_responses(monkeypatch, {provider})

    class Broken:
        def record(self, **fields):
            raise RuntimeError("capture unavailable")

    outputs, schedules = [], []
    for sink in (None, collector(), Broken()):
        calls.clear()
        outputs.append(await pubs.publications_search(QUERY, 5, _collector=sink))
        schedules.append(sorted(calls))
    assert outputs[0] == outputs[1] == outputs[2]
    assert schedules[0] == schedules[1] == schedules[2]


async def test_concurrent_searches_isolate_collector_and_reset_context(monkeypatch):
    install_responses(monkeypatch, {"crossref"}, by_query=True)
    other_query = "solar photovoltaic panel battery charging inverter"
    left, right = collector(), collector(other_query)
    await asyncio.gather(pubs.publications_search(QUERY, 5, _collector=left), pubs.publications_search(other_query, 5, _collector=right))
    assert left.events and right.events
    assert {e["decision_query"] for e in left.events} == {QUERY}
    assert {e["decision_query"] for e in right.events} == {other_query}
    assert {e["query_variant"] for e in left.events} == {QUERY}
    assert {e["query_variant"] for e in right.events} == {other_query}
    assert {e["example_id"] for e in left.events}.isdisjoint(e["example_id"] for e in right.events)
    assert pubs._PUBLICATION_CAPTURE.get() is None
    counts = len(left.events), len(right.events)
    await pubs.publications_search(QUERY, 5)
    assert counts == (len(left.events), len(right.events))


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_removing_real_provider_gate_hook_is_detected(monkeypatch, provider):
    install_responses(monkeypatch, {provider})

    async def exercise():
        capture = collector()
        output = await pubs.publications_search(QUERY, 5, _collector=capture)
        return output, [e for e in capture.events if e["decision_stage"] == "provider_filter"]

    before, events = await exercise()
    assert len(events) == 2 and any(not e["retained"] for e in events)
    monkeypatch.setattr(pubs, "_record_provider_gate", lambda *args, **kwargs: None)
    after, missing = await exercise()
    assert before == after
    with pytest.raises(AssertionError):
        assert len(missing) == 2 and any(not e["retained"] for e in missing)


def test_fillback_and_limit_trace_share_ids_with_actual_rerank_decisions(monkeypatch):
    capture = collector()
    blocks = [(7.0, pubs._crossref_block(f"Unrelated pottery study {i}", "", "2024", "Clay ceramic artifacts", f"https://doi.org/10.1234/{i}", 7.0)) for i in range(6)]
    with pubs.capture_scope(pubs._PUBLICATION_CAPTURE, (capture, {})):
        for _, block in blocks:
            pubs._remember_publication(block, "crossref", "provider executed query")
        reranked, removed = pubs._rerank_with_corpus_idf(blocks, QUERY)
        pubs._capture_selection(reranked, 1, QUERY)
    assert len(reranked) == 3 and removed == 3
    rejected = [e for e in capture.events if e["decision_stage"] == "merged_rerank"]
    restored = [e for e in capture.events if e["decision_stage"] == "fill_back"]
    selected = [e for e in capture.events if e["decision_stage"] == "truncation"]
    assert len(rejected) == 6 and all(e["retained"] is False for e in rejected)
    assert len(restored) == 3 and all(e["retained"] is True for e in restored)
    assert all(e["decision_reason"] == "restored_to_meet_minimum" for e in restored)
    assert [e["decision_reason"] for e in selected] == ["accepted", "beyond_limit", "beyond_limit"]
    assert {e["example_id"] for e in restored} == {e["example_id"] for e in selected}
    assert {e["example_id"] for e in restored} <= {e["example_id"] for e in rejected}
    assert all(e["provider"] == "crossref" and e["query_variant"] == "provider executed query" for e in capture.events)
    assert all(e["retained"] is None for e in selected)


async def test_missing_doi_keeps_original_text_identity_across_rendering(monkeypatch):
    base_records = records

    def missing_doi(query=QUERY):
        result = base_records(query)
        for row in result:
            row["doi"] = ""
            row["abstract"] += " Related work cites https://doi.org/10.9999/not-this-paper."
        return result

    monkeypatch.setattr(__import__(__name__), "records", missing_doi)
    install_responses(monkeypatch, {"crossref"})
    capture = collector()
    await pubs.publications_search(QUERY, 5, _collector=capture)
    gate = next(e for e in capture.events if e["decision_stage"] == "provider_filter" and e["retained"])
    later = [e for e in capture.events if e["decision_stage"] == "truncation"]
    assert gate["candidate_identity_kind"] == "text_hash"
    assert len(later) == 1
    assert later[0]["score_text"] != gate["score_text"]
    assert later[0]["example_id"] == gate["example_id"]
    assert later[0]["candidate_identity_kind"] == "text_hash"


async def test_missing_identity_sidecar_negative_control(monkeypatch):
    original = pubs._remember_publication

    def lose_identity(block, provider, executed_query, **kwargs):
        return original(block, provider, executed_query)

    monkeypatch.setattr(pubs, "_remember_publication", lose_identity)
    with pytest.raises(AssertionError):
        await test_missing_doi_keeps_original_text_identity_across_rendering(monkeypatch)


def test_cited_doi_cannot_override_document_url():
    block = ("Publication title returned by Semantic Scholar: **Paper**.\n"
             "Abstract excerpt: cites https://doi.org/10.9999/someone-else\n"
             "DOI URL constructed for this publication: Not available.\n"
             "Semantic Scholar URL for this publication: https://semanticscholar.org/paper/ours.\n")
    fields = pubs._publication_capture_fields(block)
    assert fields["canonical_id"] == ""
    assert fields["url"] == "https://semanticscholar.org/paper/ours"
