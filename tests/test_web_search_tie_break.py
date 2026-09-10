"""Guard: web result ordering must not depend on dict insertion order.

`_ordered_results` previously keyed on (-score, _rank_domain(url)). `_rank_domain`
returns (rank, domain), so two different URLs on the same domain with the same
score produced an identical sort key and fell through to `merged_results`
insertion order -- which is the order providers happened to return them in.

That is observable twice over: it sets the displayed order of results, and at the
`max_results` cut boundary it decides which results appear at all.

`merged_results` is keyed by URL, so appending the URL makes the key total: no two
entries can compare equal. The URL is an identity component, not a ranking one.
"""

from __future__ import annotations

from tools.web_search import _ordered_results, _rank_domain

# Same domain, same score, different URLs: every earlier key component ties.
ALPHA = ("https://example.com/alpha", "Alpha result", "snippet a", 5.0)
BRAVO = ("https://example.com/bravo", "Bravo result", "snippet b", 5.0)


def _merged(rows):
    return {row[0]: row for row in rows}


def test_the_tie_is_actually_reachable():
    """Without a genuine key collision this guard would pass against broken code."""
    assert ALPHA[3] == BRAVO[3]
    assert _rank_domain(ALPHA[0]) == _rank_domain(BRAVO[0])


def test_order_is_stable_under_insertion_order():
    forward = _ordered_results(_merged([ALPHA, BRAVO]), 10)
    reversed_order = _ordered_results(_merged([BRAVO, ALPHA]), 10)
    assert [row[0] for row in forward] == [row[0] for row in reversed_order]


def test_which_result_survives_the_cut_is_stable():
    """At the cut boundary the dependency decides inclusion, not just order."""
    forward = _ordered_results(_merged([ALPHA, BRAVO]), 1)
    reversed_order = _ordered_results(_merged([BRAVO, ALPHA]), 1)
    assert [row[0] for row in forward] == [row[0] for row in reversed_order]


def test_non_tied_ordering_is_unchanged():
    """Score and domain rank must still dominate the URL component."""
    low = ("https://example.com/zzz-low", "Low", "s", 1.0)
    high = ("https://example.com/aaa-high", "High", "s", 9.0)
    gov = ("https://agency.gov/mid", "Gov", "s", 5.0)
    com = ("https://example.com/mid", "Com", "s", 5.0)
    assert [r[0] for r in _ordered_results(_merged([low, high]), 10)] == [high[0], low[0]]
    # _rank_domain puts .gov (0) ahead of a plain .com (2) at equal score
    assert [r[0] for r in _ordered_results(_merged([com, gov]), 10)] == [gov[0], com[0]]


def test_the_key_is_total_for_distinct_urls():
    """merged_results is keyed by URL, so no two entries can compare equal."""
    rows = [ALPHA, BRAVO, ("https://example.com/charlie", "C", "s", 5.0)]
    keys = {(-r[3], _rank_domain(r[0]), r[0]) for r in rows}
    assert len(keys) == len(rows)
