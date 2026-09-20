"""Real writer -> pack -> search -> provider-boundary query contracts."""

import importlib
import json
from pathlib import Path

import pytest

from tools import research_session as rs
from tools.decision_capture import query_fingerprint
from test_capture_pack_equivalence import (
    QUERY, _mock_patent_io, _mock_publication_io, _mock_web_io,
)

ORIGINAL = "Inteligentný dverový zámok ovládaný mobilnou aplikáciou"
RETRY = "smart door lock mobile application remote revocation"
CASES = ("english_initial", "multilingual_initial", "english_retry",
         "multilingual_retry", "element_retry", "missing_first", "auto_retry")


def _instrument(monkeypatch, source):
    pack = importlib.import_module(f"tools.{source}_evidence_pack")
    search_name = "publications_search" if source == "publication" else source + "_search"
    search = importlib.import_module("tools." + search_name)
    {"patent": _mock_patent_io, "publication": _mock_publication_io,
     "web": _mock_web_io}[source](monkeypatch)
    calls = {"search": [], "provider": [], "scoring": []}
    real_search = getattr(pack, search_name)

    async def search_spy(*args, **kwargs):
        calls["search"].append(kwargs.copy())
        return await real_search(*args, **kwargs)

    monkeypatch.setattr(pack, search_name, search_spy)
    real_score = pack.evidence_score

    def score_spy(query, *args, **kwargs):
        calls["scoring"].append(query)
        return real_score(query, *args, **kwargs)

    monkeypatch.setattr(pack, "evidence_score", score_spy)
    if source == "patent":
        search._PATENT_SEARCH_CACHE.clear()
        provider = search._google_patents_xhr_search

        async def provider_spy(client, query, *args, **kwargs):
            calls["provider"].append(query)
            return await provider(client, query, *args, **kwargs)

        monkeypatch.setattr(search, "_google_patents_xhr_search", provider_spy)
    elif source == "web":
        provider = search.SEARCH_PROVIDERS[0][1]

        async def provider_spy(client, query, *args, **kwargs):
            calls["provider"].append(query)
            rows = await provider(client, query, *args, **kwargs)
            return [(url, title, snippet + " Remote revocation of access codes.")
                    for url, title, snippet in rows]

        monkeypatch.setattr(search, "SEARCH_PROVIDERS", (("stub_provider", provider_spy),))
    else:
        provider = search._crossref_blocks_safe

        async def provider_spy(queries, relevance_query, *args, **kwargs):
            calls["provider"].extend(queries)
            return await provider(queries, relevance_query, *args, **kwargs)

        monkeypatch.setattr(search, "_crossref_blocks_safe", provider_spy)
    return calls, pack


def _prepare(source, case):
    multilingual = case != "english_initial" and case != "english_retry"
    original = ORIGINAL if multilingual else QUERY
    translation = QUERY if multilingual else ""
    session = json.loads(rs.research_session_start(original))["session_id"]
    rs.research_session_understand_query(session, english_query=translation)
    if case == "missing_first":
        other = next(s for s in rs.SOURCE_TYPES if s != source)
        for attempt in (1, 2):
            rs.research_session_save_evidence(session, other, {
                "source_type": other, "status": "failed", "completed": False, "hits": [],
            }, query=original, attempt=attempt)
    retry = case in {"english_retry", "multilingual_retry", "element_retry", "auto_retry"}
    with rs._connect() as conn:
        envelope = json.loads(rs._session_row(conn, session)["query_envelope_json"])
        envelope["query_variants"][source] = [original, RETRY]
        # Explicit synthetic backend plan; parser behavior is not under test.
        envelope["critical_requirements_atomic"] = [{
            "category": "function", "label": "remote revocation",
            "terms": ["remote", "revocation"], "too_broad_for_element_retry": False,
        }]
        conn.execute("UPDATE research_sessions SET query_envelope_json=? WHERE session_id=?",
                     (json.dumps(envelope), session))
        conn.commit()
    if retry:
        rs.research_session_save_evidence(session, source, {
            "status": "ok", "completed": True, "source_type": source,
            "hits": [{"title": "Smart door lock", "summary": QUERY,
                      "url": "https://example.org/lock", "relevance": "direct",
                      "evidence_level": "fetched_excerpt" if case == "element_retry" else "search_snippet_only"}],
        }, query=original, attempt=1)
    first = json.loads(rs.research_session_checklist(session))["retry_actions"]
    second = json.loads(rs.research_session_checklist(session))["retry_actions"]
    assert first == second
    action = next(a for a in first if a["source_type"] == source)
    if case == "element_retry":
        assert action["element_guided"] is True
    assert action["attempt_no"] == (2 if retry else 1)
    return session, original, translation, action


async def _exercise(monkeypatch, source, case, *, replace_query=False, omit_translation=False):
    calls, pack = _instrument(monkeypatch, source)
    session, original, translation, action = _prepare(source, case)
    with rs._connect() as conn:
        original_envelope = rs._session_row(conn, session)["query_envelope_json"]
    query = original if replace_query else action["query"]
    passed_translation = "" if omit_translation else translation
    ack = json.loads(await getattr(rs, source + "_evidence_to_session")(
        session, query, english_query=passed_translation,
        attempt_no=0 if case == "auto_retry" else action["attempt_no"],
    ))
    assert ack["status"] == "written", ack
    expected = translation if action["attempt_no"] == 1 and translation else action["query"]
    effective_search = calls["search"][0].get("english_query") or calls["search"][0]["query"]
    if source == "publication" and action["attempt_no"] == 1 and translation:
        assert effective_search == pack._publication_search_query(expected, ""), "execution query mismatch"
    else:
        assert effective_search == expected, "execution query mismatch"
    assert calls["provider"], "real search must reach a provider boundary"
    if action["attempt_no"] > 1:
        assert any("revocation" in q for q in calls["provider"]), calls
    assert calls["scoring"], f"real evidence scoring must execute: {calls}"
    score_query = pack._publication_search_query(expected, "") if source == "publication" else expected
    assert set(calls["scoring"]) == {score_query}
    assert ack["english_query_received"] == bool(passed_translation)
    used = bool(passed_translation) and action["attempt_no"] == 1
    assert ack["english_query_used_for_search"] == used
    assert ack["english_query_used_for_scoring"] == used
    with rs._connect() as conn:
        row = conn.execute("SELECT * FROM evidence_results WHERE session_id=? AND source_type=? AND attempt=?",
                           (session, source, action["attempt_no"])).fetchone()
        assert row["query"] == action["query"]
        assert row["query_hash"] == rs.query_hash(action["query"])
        assert rs._session_row(conn, session)["original_query"] == original
        assert rs._session_row(conn, session)["query_envelope_json"] == original_envelope
        events = conn.execute("SELECT query_fingerprint FROM evaluation_candidate_decisions WHERE session_id=? AND source_type=? AND attempt=?",
                              (session, source, action["attempt_no"])).fetchall()
        assert events, "real search capture must persist events"
        assert {e["query_fingerprint"] for e in events} == {query_fingerprint(original)}
        if action["attempt_no"] > 1:
            assert "__english_query" not in json.loads(row["normalized_json"])
    return session, ack, calls


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
@pytest.mark.parametrize("case", CASES)
async def test_query_execution_storage_and_capture_agree(temp_db, monkeypatch, source, case):
    await _exercise(monkeypatch, source, case)


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
async def test_flowise_retry_without_translation_uses_backend_query(temp_db, monkeypatch, source):
    await _exercise(monkeypatch, source, "multilingual_retry", omit_translation=True)


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
async def test_duplicate_ack_does_not_claim_translation_was_executed(temp_db, monkeypatch, source):
    session, _, calls = await _exercise(monkeypatch, source, "multilingual_retry")
    before = len(calls["provider"])
    ack = json.loads(await getattr(rs, source + "_evidence_to_session")(
        session, RETRY, english_query=QUERY, attempt_no=2,
    ))
    assert ack["status"] == "duplicate_noop"
    assert ack["english_query_received"] is True
    assert ack["english_query_used_for_search"] is False
    assert ack["english_query_used_for_scoring"] is False
    assert len(calls["provider"]) == before


@pytest.mark.parametrize("source", rs.SOURCE_TYPES)
@pytest.mark.parametrize("defect", ["stale_translation", "original_query"])
async def test_restored_execution_defect_fails_real_path(temp_db, monkeypatch, source, defect):
    if defect == "stale_translation":
        monkeypatch.setattr(rs, "_attempt_english_query", lambda attempt, english: english)
    with pytest.raises(AssertionError, match="execution query mismatch"):
        await _exercise(monkeypatch, source, "multilingual_retry", replace_query=defect == "original_query")


def _supervisor_prompt():
    workflow = json.loads((Path(__file__).resolve().parents[1] / "flowise_architecture/Flowise_agent.json").read_text(encoding="utf-8"))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    return next(s for s in strings(workflow) if '<system_prompt name="Supervisor">' in s)


def _assert_prompt_contract(prompt):
    assert "EXACT retry_actions[].query" in prompt
    assert 'english_query=""' in prompt
    assert "missing-source action with attempt_no=1" in prompt
    assert "Never invent, rewrite or retranslate a retry query" in prompt
    assert "original session query remains immutable" in prompt
    assert "english_query_used_for_search=false" in prompt
    assert "Only checklist retry_actions authorize further attempts" in prompt
    assert "Pass it byte-for-byte to all three writers as query, regardless of language" not in prompt
    assert "every retry writer call must include the same non-empty" not in prompt


def test_flowise_query_and_ack_contract():
    _assert_prompt_contract(_supervisor_prompt())


def test_restored_flowise_hard_rule_fails_contract():
    broken = _supervisor_prompt() + "\nPass it byte-for-byte to all three writers as query, regardless of language."
    with pytest.raises(AssertionError):
        _assert_prompt_contract(broken)
