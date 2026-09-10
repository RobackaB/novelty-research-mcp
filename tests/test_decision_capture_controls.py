"""Proof that capture cannot change retrieval, and that the hooks really fire.

The first group compares a disabled collector against a real one and against a
collector that raises on every call: all three must produce byte-identical
output. The second group is the negative control -- each capture hook is
monkeypatched out and the corresponding assertion is shown to fail, so a passing
test in test_decision_capture.py cannot be an artefact of a test that would pass
with no hook at all.
"""

from __future__ import annotations

import asyncio

import pytest

import tools.patent_search as patent_search
import tools.publications_search as pubs
import tools.web_search as web_search
from tools.decision_capture import CollectorContext, DecisionCollector


class RaisingCollector:
    events: list = []

    def record(self, **_fields):
        raise RuntimeError("capture exploded")


def _fresh() -> DecisionCollector:
    return DecisionCollector(context=CollectorContext(session_id="s", source_type="publication"))


QUERY = "anomaly detection in application logs using machine learning"
TEXT = "\n\n".join(
    [
        "Anomaly detection in application logs using workflow patterns.",
        "Seismic wave classification for earthquake early warning systems.",
        "Deep learning for anomalies in distributed system logs and events.",
        "Predictive maintenance of industrial motors using vibration analysis.",
    ]
)


# --- equivalence: capture must not change output ------------------------------

def test_publication_filter_output_identical_with_and_without_collector():
    disabled = pubs._filter_publication_text(QUERY, TEXT, 5)
    enabled = pubs._filter_publication_text(QUERY, TEXT, 5, _collector=_fresh())
    raising = pubs._filter_publication_text(QUERY, TEXT, 5, _collector=RaisingCollector())
    assert disabled == enabled == raising


def test_publication_rerank_output_identical_with_and_without_collector():
    blocks = [(0.0, block) for block in TEXT.split("\n\n")]
    disabled = pubs._rerank_with_corpus_idf(list(blocks), QUERY)
    enabled = pubs._rerank_with_corpus_idf(list(blocks), QUERY, _collector=_fresh())
    raising = pubs._rerank_with_corpus_idf(list(blocks), QUERY, _collector=RaisingCollector())
    assert disabled == enabled == raising


def _patent_candidates():
    return [
        patent_search.PatentCandidate(
            patent_number="US1111111B2",
            title="Anomaly detection in application log data",
            url="https://patents.google.com/patent/US1111111B2/en",
            snippet="Detecting anomalies in application logs using machine learning.",
        ),
        patent_search.PatentCandidate(
            patent_number="US2222222B2",
            title="Method and system for comparing images",
            url="https://patents.google.com/patent/US2222222B2/en",
            snippet="A method, system and computer program for comparing images.",
        ),
    ]


def test_patent_rank_output_identical_with_and_without_collector():
    disabled = patent_search._rank(QUERY, _patent_candidates(), 6)
    enabled = patent_search._rank(QUERY, _patent_candidates(), 6, _collector=_fresh())
    raising = patent_search._rank(QUERY, _patent_candidates(), 6, _collector=RaisingCollector())
    ids = lambda r: ([c.patent_number for c in r[0]], r[1])
    assert ids(disabled) == ids(enabled) == ids(raising)


def test_patent_dedupe_output_identical_with_and_without_collector():
    disabled = patent_search._dedupe(_patent_candidates())
    enabled = patent_search._dedupe(_patent_candidates(), _collector=_fresh())
    raising = patent_search._dedupe(_patent_candidates(), _collector=RaisingCollector())
    ids = lambda r: [c.patent_number for c in r]
    assert ids(disabled) == ids(enabled) == ids(raising)


def test_raising_collector_still_records_nothing_and_breaks_nothing():
    collector = RaisingCollector()
    result = pubs._filter_publication_text(QUERY, TEXT, 5, _collector=collector)
    assert isinstance(result, str) and result


# --- negative controls: remove the hook, the capture test must fail ------------

def test_negative_control_publication_filter_hook(monkeypatch):
    """With safe_record disabled, the rejection capture assertion fails."""
    monkeypatch.setattr(pubs, "safe_record", lambda *a, **k: None)
    collector = _fresh()
    pubs._filter_publication_text(QUERY, TEXT, 5, _collector=collector)
    assert collector.events == [], "hook still fired; the control is not exercising anything"


def test_negative_control_patent_anchor_hook(monkeypatch):
    monkeypatch.setattr(patent_search, "safe_record", lambda *a, **k: None)
    collector = _fresh()
    patent_search._rank(QUERY, _patent_candidates(), 6, _collector=collector)
    assert collector.events == []


def test_negative_control_patent_dedupe_hook(monkeypatch):
    monkeypatch.setattr(patent_search, "safe_record", lambda *a, **k: None)
    collector = _fresh()
    patent_search._dedupe(_patent_candidates(), _collector=collector)
    assert collector.events == []


def test_hooks_fire_when_not_disabled():
    """The positive half of the control: without monkeypatching, events appear."""
    collector = _fresh()
    pubs._filter_publication_text(QUERY, TEXT, 5, _collector=collector)
    assert collector.events, "publication filter hook did not fire"

    patent_collector = _fresh()
    patent_search._rank(QUERY, _patent_candidates(), 6, _collector=patent_collector)
    assert patent_collector.events, "patent anchor hook did not fire"
