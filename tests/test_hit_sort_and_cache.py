"""Tests of hit ordering and the TTL cache."""

from __future__ import annotations

from tools._hit_sort import sort_hits_by_relevance
from tools._ttl_cache import TTLCache


def test_sort_hits_focused_before_adjacent():
    hits = [
        {"title": "b", "relevance": "adjacent", "relevance_score": 9.0},
        {"title": "a", "relevance": "focused", "relevance_score": 5.0},
        {"title": "c", "relevance": "generic", "relevance_score": 10.0},
    ]
    ordered = sort_hits_by_relevance(hits)
    assert [hit["title"] for hit in ordered] == ["a", "b", "c"]


def test_sort_hits_score_breaks_ties():
    hits = [
        {"title": "low", "relevance": "focused", "relevance_score": 2.0},
        {"title": "high", "relevance": "focused", "relevance_score": 8.0},
    ]
    ordered = sort_hits_by_relevance(hits)
    assert ordered[0]["title"] == "high"


def test_sort_hits_tolerates_bad_score():
    hits = [
        {"title": "bad", "relevance": "focused", "relevance_score": "oops"},
        {"title": "good", "relevance": "focused", "relevance_score": 1.0},
    ]
    ordered = sort_hits_by_relevance(hits)
    assert ordered[0]["title"] == "good"


def test_ttl_cache_set_get():
    cache: TTLCache[str, str] = TTLCache(ttl_seconds=60, max_entries=4)
    cache.set("a", "1")
    assert cache.get("a") == "1"
    assert cache.get("missing") is None


def test_ttl_cache_expiry():
    cache: TTLCache[str, str] = TTLCache(ttl_seconds=0, max_entries=4)
    cache.set("a", "1")
    assert cache.get("a") is None


def test_ttl_cache_eviction_of_oldest():
    cache: TTLCache[int, int] = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set(1, 1)
    cache.set(2, 2)
    cache.set(3, 3)
    assert len(cache) == 2
    assert cache.get(1) is None
    assert cache.get(3) == 3
