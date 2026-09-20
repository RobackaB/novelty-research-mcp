"""The web evidence pack must preserve upstream completeness semantics.

`completed` was derived as `parse_status_marker(search_output) != "failed"`, so
any non-failed status counted as complete. A partial_failure search that
explicitly reported COMPLETED: FALSE surfaced from the pack as completed=true,
losing the one signal that says retrieval was incomplete.

Completeness now comes from the search's own COMPLETED marker. Fetch outcomes
downgrade the pack's status but never redefine search completeness.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import tools.web_evidence_pack as wep
from tools.result_contract import NormalizedResult, prepend_markers

QUERY = "smart door lock controlled by a mobile application with access codes"

BODY = (
    "Result title returned by search: **Smart lock product page**.\n"
    "Source page URL for this result: https://example.com/a\n"
    "Local rerank score: 6.0/10.\n"
    "Short summary snippet from search: Smart door lock with mobile application "
    "control and temporary access codes.\n"
)

PROVIDER_ERROR = {"type": "web_provider_error", "message": "tavily: transport failure"}


def _search_output(status, completed, *, hits=(BODY,), errors=(), reliable=False):
    return prepend_markers(
        NormalizedResult(
            status=status, completed=completed, reliable_no_results=reliable,
            query=QUERY, hits=list(hits), errors=list(errors),
        ),
        "\n\n".join(hits) if hits else "No web results matched the query.",
    )


def _install(monkeypatch, search_output, *, fetch_fails=False):
    async def search(query, max_results=5, **_kw):
        return search_output

    async def fetch(url, timeout_ms=14000, query=""):
        if fetch_fails:
            raise RuntimeError("page fetch failed")
        # _fetch_succeeded requires FULLTEXT_VERIFIED; anything else counts as a
        # failed fetch and would add a warning, masking what these tests assert.
        return ("CONTENT: Smart door lock controlled by a mobile application "
                "with temporary access codes.\nSTATUS: OK\n"
                "EVIDENCE_LEVEL: FULLTEXT_VERIFIED\n")

    async def verify(urls, max_urls=20):
        return "URL_STATUS: https://example.com/a -> ok\n"

    monkeypatch.setattr(wep, "web_search", search)
    monkeypatch.setattr(wep, "web_fetch", fetch)
    monkeypatch.setattr(wep, "verify_sources", verify)


def _pack() -> dict:
    return json.loads(asyncio.run(wep.web_evidence_pack(query=QUERY)))


# --- partial search -----------------------------------------------------------

def test_partial_search_with_usable_hits_stays_incomplete(monkeypatch):
    _install(monkeypatch, _search_output("partial_failure", False))
    payload = _pack()
    assert payload["status"] == "partial_failure"
    assert payload["completed"] is False, "upstream completed=false was discarded"
    assert payload["hits"], "usable hits were dropped"


def test_partial_search_preserves_the_structured_error(monkeypatch):
    _install(monkeypatch, _search_output("partial_failure", False, errors=[PROVIDER_ERROR]))
    payload = _pack()
    assert payload["status"] == "partial_failure"
    assert payload["completed"] is False
    assert payload["errors"], "structured search error was lost"
    assert any("tavily" in json.dumps(e) for e in payload["errors"])


# --- healthy / failed / no-results --------------------------------------------

def test_healthy_search_remains_complete(monkeypatch):
    _install(monkeypatch, _search_output("ok", True))
    payload = _pack()
    assert payload["status"] == "ok"
    assert payload["completed"] is True
    assert payload["hits"]


def test_failed_search_remains_incomplete(monkeypatch):
    _install(monkeypatch, _search_output("failed", False, hits=()))
    payload = _pack()
    assert payload["status"] == "failed"
    assert payload["completed"] is False


def test_reliable_no_results_semantics_are_unchanged(monkeypatch):
    _install(monkeypatch, _search_output("ok", True, hits=(), reliable=True))
    payload = _pack()
    assert payload["completed"] is True
    assert payload["reliable_no_results"] is True
    assert payload["hits"] == []


# --- fetch outcomes must not redefine search completeness ---------------------

def test_fetch_failure_after_a_complete_search_keeps_completed_true(monkeypatch):
    """A failed page fetch downgrades status but not search completeness."""
    _install(monkeypatch, _search_output("ok", True), fetch_fails=True)
    payload = _pack()
    assert payload["completed"] is True, "a fetch warning redefined search completeness"
    assert payload["status"] in {"ok", "partial_failure"}


def test_fetch_failure_does_not_rescue_an_incomplete_search(monkeypatch):
    _install(monkeypatch, _search_output("partial_failure", False), fetch_fails=True)
    payload = _pack()
    assert payload["completed"] is False


# --- propagation through the session writer -----------------------------------

def test_partial_completeness_propagates_through_the_session(monkeypatch, temp_db):
    import tools.research_session as rs

    _install(monkeypatch, _search_output("partial_failure", False, errors=[PROVIDER_ERROR]))
    session = json.loads(rs.research_session_start(QUERY))["session_id"]
    ack = json.loads(asyncio.run(rs.web_evidence_to_session(session_id=session, query=QUERY)))
    assert ack.get("status") != "failed", ack

    checklist = json.loads(rs.research_session_checklist(session))
    web = next(c for c in checklist["source_checks"] if c["source_type"] == "web")
    assert web["completed"] is False, f"completeness lost through SQLite: {web}"


# --- negative control ---------------------------------------------------------

def test_restored_status_derived_completed_breaks_the_partial_case(monkeypatch):
    """The historical expression: any non-failed status counted as complete."""
    from tools.result_contract import parse_status_marker

    output = _search_output("partial_failure", False)
    historical = parse_status_marker(output) != "failed"
    assert historical is True, "the old expression should call this complete"

    _install(monkeypatch, output)
    assert _pack()["completed"] is False, "the fix must disagree with the old expression"
