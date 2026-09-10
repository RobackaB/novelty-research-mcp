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


def test_patent_threshold_filter_hook_exists_but_has_no_behavioural_control():
    """KNOWN GAP: threshold_filter is wired but not behaviourally proven.

    A behavioural test needs a candidate that clears _shares_discriminative_term
    yet scores below MIN_RELEVANCE_SCORE (2.8). Corpus IDF weighting inflates
    sparse candidates -- the weakest fixture tried still scored 4.38 -- so no such
    fixture was found. Rather than a conditional assertion that passes when the
    hook is removed, the gap is recorded here and in the coverage matrix.

    What IS asserted: the anchor stage is always recorded, so the surrounding
    instrumentation is live.
    """
    strong = _candidate("US1B2", "Smart door lock mobile application",
                        "Smart door lock controlled by a mobile application with access codes.")
    collector = DecisionCollector(context=CollectorContext(source_type="patent"))
    patent_search._rank(QUERY, [strong], 6, _collector=collector)
    anchors = [e for e in collector.events if e["decision_stage"] == "domain_anchor"]
    assert anchors, "anchor stage must always be recorded"
    assert anchors[0]["retained"] is True


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
