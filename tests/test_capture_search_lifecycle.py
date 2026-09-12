"""Candidate lifecycle coverage and controls for patent and web decisions."""

from __future__ import annotations

import json

import httpx
import pytest

import tools.patent_search as patents
import tools.publications_search as publications
import tools.web_search as web
from tools.decision_capture import CollectorContext, DecisionCollector, capture_scope

QUERY = "smart door lock controlled by a mobile application with access codes"


class RaisingCollector:
    def __init__(self):
        self.calls = 0

    def record(self, **fields):
        self.calls += 1
        raise RuntimeError("collector unavailable")


def collector(source):
    return DecisionCollector(context=CollectorContext(source_type=source, query_fingerprint="query"))


def candidate(title="lock", number="US1B2"):
    return patents.PatentCandidate(title=title, snippet="", patent_number=number,
                                   url=f"https://patents.google.com/patent/{number}/en",
                                   provider="google_patents_xhr")


@pytest.mark.parametrize("score, threshold, reason, keep", [
    (4.0, 2.8, "accepted", True),
    (2.4, 2.2, "relaxed_threshold_applied", True),
    (1.7, 1.5, "floor_applied", True),
    (1.4, 1.5, "below_threshold", False),
])
def test_patent_threshold_tiers_describe_actual_selection(monkeypatch, score, threshold, reason, keep):
    # Control the numeric score, not the threshold or the production selection.
    monkeypatch.setattr(patents, "_score", lambda *a, **kw: score)
    enabled = collector("patent")
    ranked, returned_threshold = patents._rank(QUERY, [candidate()], 3, True, _collector=enabled)
    events = [e for e in enabled.events if e["decision_stage"] == "threshold_filter"]
    event = next(e for e in events if e["threshold_at_decision"] == threshold)
    assert (event["score_at_decision"], event["decision_reason"], event["retained"]) == (score, reason, keep)
    assert bool(ranked) is keep
    # Production's historic threshold note stays 2.8 even when floor is used.
    if threshold == 1.5:
        assert returned_threshold == 2.8


def test_patent_threshold_capture_negative_control(monkeypatch):
    from test_capture_coverage import test_patent_threshold_rejection_records_the_real_score_and_evaluated_tiers

    monkeypatch.setattr(patents, "_record_threshold", lambda *a, **kw: None)
    with pytest.raises(AssertionError):
        test_patent_threshold_rejection_records_the_real_score_and_evaluated_tiers()


async def test_patent_low_confidence_search_forwards_capture_and_preserves_output(monkeypatch):
    async def provider(*args):
        return [candidate("mobile")]

    async def unavailable(*args):
        raise patents.ProviderUnavailable("not configured")

    monkeypatch.setattr(patents, "_google_patents_xhr_search", provider)
    for name in ("_tavily_patent_search", "_exa_patent_search", "_wipo_patentscope_search"):
        monkeypatch.setattr(patents, name, unavailable)
    enabled, raising = collector("patent"), RaisingCollector()
    outputs = []
    for sink in (None, enabled, raising):
        patents._PATENT_SEARCH_CACHE.clear()
        outputs.append(await patents.patent_search(QUERY, 3, _collector=sink))
    assert outputs[0] == outputs[1] == outputs[2]
    assert json.loads(outputs[0])["results"]
    floor = [e for e in enabled.events if e["decision_reason"] == "floor_applied"]
    assert floor and all(e["threshold_at_decision"] == 1.5 for e in floor)
    assert all(e["provider"] == "google_patents_xhr" and e["query_variant"] for e in floor)
    assert raising.calls > 0


def test_patent_provider_dedupe_and_cap_have_structural_provenance(monkeypatch):
    monkeypatch.setattr(patents, "PROVIDER_CANDIDATE_CAP", 1)
    enabled = collector("patent")
    first, duplicate, last = candidate(), candidate(), candidate("mobile", "US2B2")
    with capture_scope(patents._PROVIDER_CAPTURE, (enabled, QUERY, "tavily", "executed variant")):
        assert patents._provider_candidates([first, duplicate, last]) == [first]
    assert [(e["decision_stage"], e["retained"]) for e in enabled.events] == [
        ("dedupe", None), ("truncation", None)]
    assert all(e["provider"] == "tavily" and e["query_variant"] == "executed variant"
               for e in enabled.events)


def test_patent_origin_lookup_failure_preserves_ranked_output(caplog):
    class BrokenOrigins(dict):
        def get(self, *args):
            raise RuntimeError("capture sidecar unavailable")

    expected, expected_threshold = patents._rank(QUERY, [candidate("smart lock")], 3)
    enabled = collector("patent")
    actual, actual_threshold = patents._rank(
        QUERY, [candidate("smart lock")], 3, _collector=enabled,
        _origins=BrokenOrigins(occupied=True),
    )
    assert (actual, actual_threshold) == (expected, expected_threshold)
    assert enabled.events and "patent origin capture failed" in caplog.text
    assert all(e["query_variant"] == "" for e in enabled.events)


def mock_web(monkeypatch, mode="partial"):
    rows = [(f"https://example.com/{i}", f"Smart door lock {i}", QUERY) for i in range(3)]

    async def provider(*args):
        return rows

    monkeypatch.setattr(web, "SEARCH_PROVIDERS", (("fixture", provider),))
    monkeypatch.setattr(web, "section_query_variants", lambda *a, **kw: [QUERY])
    monkeypatch.setattr(web, "build_corpus_idf", lambda *a: {"lock": 2.0})
    original_score = web.evidence_score

    def score(query, text, kind, idf=None):
        if idf:
            return 1.0 if mode == "empty" or text.startswith("Smart door lock 0\n") else 6.0
        return original_score(query, text, kind, idf=idf)

    # Real is_relevant and ordering run; control only corpus/score inputs to
    # reach both merged branches without tuning production thresholds.
    monkeypatch.setattr(web, "evidence_score", score)
    original_relevant = web.is_relevant

    def relevant(query, text, kind, threshold=None, idf=None):
        if idf:
            return score(query, text, kind, idf) >= threshold
        return original_relevant(query, text, kind, threshold=threshold)

    monkeypatch.setattr(web, "is_relevant", relevant)
    return rows


@pytest.mark.parametrize("mode", ["partial", "empty"])
async def test_web_merged_rejections_restoration_and_limit_are_observed(monkeypatch, mode):
    rows = mock_web(monkeypatch, mode)
    enabled, raising = collector("web"), RaisingCollector()
    outputs = [await web.web_search(QUERY, 1, _collector=sink) for sink in (None, enabled, raising)]
    assert outputs[0] == outputs[1] == outputs[2]
    merged = [e for e in enabled.events if e["decision_stage"] == "merged_rerank"]
    assert len(merged) == len(rows)
    assert merged[0]["retained"] is False and merged[0]["score_at_decision"] == 1.0
    assert merged[0]["threshold_at_decision"] == web.MIN_WEB_RERANK_SCORE == 5.0
    assert all(e["provider"] == "fixture" and e["query_variant"] == QUERY for e in merged)
    restored = [e for e in enabled.events if e["decision_stage"] == "fill_back"]
    assert len(restored) == (3 if mode == "empty" else 0)
    assert all(e["decision_reason"] == "restored_after_empty_rerank" and e["retained"] for e in restored)
    truncated = [e for e in enabled.events if e["decision_stage"] == "truncation"]
    assert len(truncated) == (2 if mode == "empty" else 1)
    assert all(e["retained"] is None for e in truncated)
    assert raising.calls > 0


async def test_web_merged_hook_removal_is_detected(monkeypatch):
    monkeypatch.setattr(web, "_record_merged_web", lambda *a, **kw: None)
    with pytest.raises(AssertionError):
        await test_web_merged_rejections_restoration_and_limit_are_observed(monkeypatch, "partial")


def test_web_salient_rejection_is_not_a_score_rejection():
    enabled = collector("web")
    web._record_merged_web(enabled, ("https://e.com", "unrelated", "text", 4.0),
                           QUERY, {}, "merged_rerank", "below_threshold", False, {"lock": 5.0})
    event = enabled.events[0]
    assert event["decision_reason"] == "no_discriminative_term"
    assert event["score_at_decision"] is None and event["threshold_at_decision"] is None


async def test_web_capture_only_score_failure_preserves_results(monkeypatch, caplog):
    mock_web(monkeypatch)
    expected = await web.web_search(QUERY, 1)
    actual_score = web.evidence_score

    def score(*args, **kwargs):
        if kwargs.get("idf") and args[1].startswith("Smart door lock 0\n"):
            raise RuntimeError("observation unavailable")
        return actual_score(*args, **kwargs)

    monkeypatch.setattr(web, "evidence_score", score)
    assert await web.web_search(QUERY, 1, _collector=collector("web")) == expected
    assert "web merged capture failed" in caplog.text


def test_publication_overlap_rejection_does_not_call_diagnostic_scorer(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("scorer must not run after an overlap rejection")

    monkeypatch.setattr(publications, "evidence_score", unavailable)
    enabled = collector("publication")
    for sink in (None, enabled, RaisingCollector()):
        assert publications._filter_publication_text(
            QUERY, "Seismic earthquake waves", 3, _collector=sink
        ) == ""
    assert enabled.events[0]["decision_reason"] == "below_overlap_gate"
    assert enabled.events[0]["score_at_decision"] is None


async def test_web_rejection_observer_failure_cannot_change_retrieval(monkeypatch, caplog):
    async def provider(*args):
        return [("https://example.com/irrelevant", "Seismic waves", "earthquake")]

    monkeypatch.setattr(web, "SEARCH_PROVIDERS", (("fixture", provider),))
    monkeypatch.setattr(web, "section_query_variants", lambda *a, **kw: [QUERY])
    disabled = await web.web_search(QUERY, 1)
    # Model a valid rejection followed by failure only in capture's repeated
    # overlap calculation; production gate keeps its real behavior and result.
    actual_overlap = web._query_overlap_match
    calls = []

    def overlap(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("diagnostic overlap failed")
        return actual_overlap(*args)

    monkeypatch.setattr(web, "_query_overlap_match", overlap)
    assert await web.web_search(QUERY, 1, _collector=collector("web")) == disabled
    assert len(calls) == 2
    assert "web provider capture failed" in caplog.text


@pytest.mark.parametrize("provider_name, function, key, snippet_key", [
    ("google_custom_search", "_google_cse_query", "items", "snippet"),
    ("tavily", "_tavily_web_query", "results", "content"),
    ("exa", "_exa_web_query", "results", "text"),
])
async def test_real_web_provider_url_gates_capture_rejected_documents(
    monkeypatch, provider_name, function, key, snippet_key,
):
    monkeypatch.setattr(web, "_google_cse_credentials", lambda: ("test", "test"))
    monkeypatch.setenv("TAVILY_API_KEY", "test")
    monkeypatch.setenv("EXA_API_KEY", "test")
    url_key = "link" if provider_name == "google_custom_search" else "url"
    data = {key: [
        {url_key: "https://example.com/article", "title": "Article", snippet_key: QUERY},
        {url_key: "https://patents.google.com/patent/US1/en", "title": "Patent", snippet_key: QUERY},
    ]}

    async def response(*args, **kwargs):
        return httpx.Response(200, json=data, request=httpx.Request("GET", "https://fixture.test"))

    monkeypatch.setattr(httpx.AsyncClient, "get", response)
    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    enabled, raising = collector("web"), RaisingCollector()
    outputs = []
    async with httpx.AsyncClient() as client:
        for sink in (None, enabled, raising):
            with capture_scope(web._PROVIDER_CAPTURE, (sink, provider_name)):
                outputs.append(await getattr(web, function)(client, QUERY, 5))
    assert outputs[0] == outputs[1] == outputs[2] and len(outputs[0]) == 1
    assert [e["decision_reason"] for e in enabled.events] == ["accepted", "url_policy_rejected"]
    assert all(e["provider"] == provider_name and e["query_variant"] == QUERY for e in enabled.events)
    assert all(e["retained"] is None for e in enabled.events)
    assert enabled.events[1]["score_text"] == f"Patent\n{QUERY}"
    assert raising.calls == 2
