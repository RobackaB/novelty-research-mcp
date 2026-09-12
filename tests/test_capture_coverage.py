"""Behavioural coverage for the newly instrumented patent sites."""

from __future__ import annotations

import tools.patent_search as patent_search
from tools.decision_capture import CollectorContext, DecisionCollector

QUERY = "smart door lock controlled by a mobile application with access codes"


def _candidate(number: str, title: str, snippet: str) -> patent_search.PatentCandidate:
    return patent_search.PatentCandidate(
        title=title, url=f"https://patents.google.com/patent/{number}/en",
        patent_number=number, snippet=snippet, provider="google_patents_xhr",
    )


def test_patent_threshold_rejection_records_the_real_score_and_evaluated_tiers():
    """A real sparse candidate clears the anchor but fails both score tiers."""
    weak = _candidate("US1B2", "lock", "")
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    ranked, _ = patent_search._rank(QUERY, [weak], 6, _collector=collector)
    assert ranked == []
    anchors = [e for e in collector.events if e["decision_stage"] == "domain_anchor"]
    assert anchors, "anchor stage must always be recorded"
    assert anchors[0]["retained"] is True
    rejected = [e for e in collector.events if e["decision_stage"] == "threshold_filter"]
    assert [(e["threshold_at_decision"], e["score_at_decision"], e["retained"])
            for e in rejected] == [(2.8, 1.4, False), (2.2, 1.4, False)]
    assert all(e["score_text"] == "lock " for e in rejected)


def test_patent_truncation_is_structural():
    """Removal by the display limit is not a relevance verdict."""
    candidates = [
        _candidate(f"US{i}B2", f"Smart door lock mobile application variant {i}",
                   "Smart door lock controlled by a mobile application with access codes.")
        for i in range(1, 5)
    ]
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    ranked, _ = patent_search._rank(QUERY, candidates, 2, _collector=collector)
    assert len(ranked) == 2, ranked
    truncated = [e for e in collector.events if e["decision_stage"] == "truncation"]
    assert truncated, f"truncation not captured: {[e['decision_stage'] for e in collector.events]}"
    assert truncated[0]["decision_reason"] == "beyond_limit"
    assert truncated[0]["retained"] is None, "truncation must not claim a relevance verdict"
