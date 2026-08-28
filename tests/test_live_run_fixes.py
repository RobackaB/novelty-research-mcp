"""Testy opráv vyplývajúcich z hĺbkovej analýzy reálneho behu."""

from __future__ import annotations

import json

import tools.jina_reader as jina
import tools.research_session as rs
import tools.web_evidence_pack as wep


# --- Priorita 2: Jina API kľúč ------------------------------------------------

def test_jina_headers_without_key(monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    headers = jina._jina_headers()
    assert "Authorization" not in headers
    assert "User-Agent" in headers


def test_jina_headers_with_key(monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", "secret-key")
    headers = jina._jina_headers()
    assert headers["Authorization"] == "Bearer secret-key"


async def test_fetch_via_jina_sends_auth_header(monkeypatch):
    captured = {}

    class _FakeResponse:
        text = "page content"

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, headers=None, **kwargs):
            captured["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setenv("JINA_API_KEY", "secret-key")
    monkeypatch.setattr(jina.httpx, "AsyncClient", _FakeClient)
    await jina.fetch_via_jina("https://example.com")
    assert captured["headers"]["Authorization"] == "Bearer secret-key"


# --- Priorita 3: web_evidence_pack chybové hlásenia ---------------------------

def test_errors_from_markers_parses_error_lines():
    search_output = (
        "STATUS: PARTIAL_FAILURE\nERROR_COUNT: 2\n"
        "ERROR: google_custom_search_web_search_failure - HTTP 429 rate limited\n"
        "ERROR: tavily_web_search_failure - connection reset\n"
    )
    errors = wep._errors_from_markers(search_output)
    assert len(errors) == 2
    assert errors[0]["type"] == "google_custom_search_web_search_failure"
    assert "rate limited" in errors[0]["message"]


def test_errors_from_markers_empty_when_no_error_lines():
    assert wep._errors_from_markers("STATUS: OK\nCOMPLETED: TRUE") == []


async def test_web_evidence_pack_surfaces_real_error_reason(monkeypatch):
    search_output = (
        "STATUS: PARTIAL_FAILURE\nCOMPLETED: FALSE\nRELIABLE_NO_RESULTS: FALSE\nERROR_COUNT: 1\n"
        "ERROR: google_custom_search_web_search_failure - HTTP 429 rate limited\n\n"
        "No results were found."
    )

    async def fake_search(query, max_results=5):
        return search_output

    monkeypatch.setattr(wep, "web_search", fake_search)
    payload = json.loads(await wep.web_evidence_pack("query with no candidates parsed"))
    assert payload["errors"], "reálny dôvod chyby má byť v errors, nie len generická hláška"
    assert payload["errors"][0]["type"] == "google_custom_search_web_search_failure"
    assert any("google_custom_search_web_search_failure" in w for w in payload["warnings"])
    assert not any(w == "Search provider reported partial errors; see server logs or raw web_search output." for w in payload["warnings"])


# --- Priorita 4: dôležitosťou vedený výber variantu dotazu --------------------

def test_query_variants_keep_function_atoms_over_positional_slicing():
    atomic = [
        {"category": "object_or_form_factor", "label": "system for detecting anomalies in application logs"},
        {"category": "function", "label": "processes events in real time"},
        {"category": "object_or_form_factor", "label": "machine learning to recognize unusual patterns"},
        {"category": "object_or_form_factor", "label": "automatically creates incident"},
        {"category": "function", "label": "notifies administrator"},
    ]
    variants = rs._query_variants_from_atoms("original query text", ["term"], atomic)
    tail_variant = variants[-1]
    assert "real time" in tail_variant, "funkčný prvok sa nesmie stratiť pri skracovaní variantu"
    assert "notifies administrator" in tail_variant


def test_query_variants_low_priority_category_trimmed_first():
    # Veľa object_or_form_factor atómov + jeden function atóm; pri orezaní na
    # limit tokenov musí function atóm prežiť dlhšie ako generické atómy.
    atomic = [{"category": "object_or_form_factor", "label": "core subject device"}]
    atomic += [
        {"category": "object_or_form_factor", "label": f"generic filler element number {i} words here"}
        for i in range(10)
    ]
    atomic.append({"category": "function", "label": "notifies administrator immediately"})
    variants = rs._query_variants_from_atoms("q", ["term"], atomic)
    tail_variant = variants[-1]
    assert "notifies administrator" in tail_variant


def test_query_variants_from_atoms_no_labels_falls_back():
    variants = rs._query_variants_from_atoms("clean query text", ["clean", "query", "text"], [])
    assert variants[0] == "clean query text"
    assert len(variants) >= 1


# --- Priorita 6: orezanie warnings v ACK a zjednotenie retry akcií -----------

def test_truncated_warnings_keeps_all_items_but_shortens_text():
    long_text = "CLAIM1: " + ("word " * 200)
    result = rs._truncated_warnings([long_text, "short warning"])
    assert len(result) == 2
    assert len(result[0]) <= 240
    assert result[1] == "short warning"


def test_truncated_warnings_drops_falsy_items():
    assert rs._truncated_warnings(["", None, "kept"]) == ["kept"]


def test_save_evidence_ack_warnings_are_truncated(temp_db):
    session_id = json.loads(rs.research_session_start("query"))["session_id"]
    long_warning = "Patent fetch did not verify https://x: " + ("PATENT_NUMBER: X " * 100)
    pack = {
        "source_type": "web",
        "status": "partial_failure",
        "completed": False,
        "reliable_no_results": False,
        "hits": [],
        "errors": [],
        "warnings": [long_warning],
    }
    response = json.loads(rs.research_session_save_evidence(session_id, "web", pack, query="query", attempt=1))
    assert len(response["warnings"][0]) <= 240
    assert response["warning_count"] == 1


def test_checklist_has_no_duplicate_recommended_next_actions_key(temp_db):
    session_id = json.loads(rs.research_session_start("query"))["session_id"]
    checklist = json.loads(rs.research_session_checklist(session_id))
    assert "recommended_next_actions" not in checklist
    assert "retry_actions" in checklist


def test_plan_next_reads_from_retry_actions(temp_db):
    session_id = json.loads(rs.research_session_start("query"))["session_id"]
    plan = json.loads(rs.research_session_plan_next(session_id))
    assert plan["status"] == "ok"
    assert len(plan["plans"]) == 3  # patent, publication, web all missing -> 3 retry actions
