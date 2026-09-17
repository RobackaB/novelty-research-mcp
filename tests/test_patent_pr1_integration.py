"""Orchestration-boundary tests for PR 1.

Helper-level tests passed while enrichment was never invoked in production and
while a successful HTML fetch discarded stronger Reader evidence. These exercise
the real functions end to end so that class of gap cannot recur.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import tools.patent_fetch as pf
import tools.patent_search as ps
from tools.patent_identity import (
    exact_publication_identity,
    extract_claims_section,
    family_base_identity,
)

NUMBER = "US10762444B2"
URL = f"https://patents.google.com/patent/{NUMBER}/en"
PDF_PATH = "aa/bb/US10762444.pdf"

CLAIMS = (
    "United States Patent US 10,762,444 B2\n\n"
    "What is claimed is:\n"
    "1. An irrigation controller comprising a soil moisture sensor, a wireless "
    "transmitter arranged to send measurements to a control unit, and a valve "
    "actuated according to a stored watering schedule, wherein a scheduled "
    "watering is skipped when measured moisture exceeds a threshold."
) * 4

ABSTRACT_ONLY = (
    "US 10,762,444 B2\n\nAbstract\n"
    "An irrigation system measuring soil moisture and operating a valve on a "
    "schedule stored in a wireless control unit. " * 8
)


# --- 1 & 2: enrichment runs in the production path and reaches patent_fetch ---

def _exa_candidate() -> ps.PatentCandidate:
    return ps.PatentCandidate(
        title="Irrigation controller with soil moisture sensor",
        url=URL,
        patent_number=NUMBER,
        snippet="Soil moisture irrigation control with wireless valve scheduling.",
        provider="exa",
    )


class _EnrichClient:
    """Answers the number lookup with a matching publication carrying a PDF path."""

    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append(params)

        class _R:
            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return {"results": {"cluster": [{"result": [{"patent": {
                    "publication_number": NUMBER,
                    "pdf": PDF_PATH,
                }}]}]}}

        return _R()


def test_retained_candidate_is_enriched_through_the_production_hook():
    """_enrich_ranked_candidates is what patent_search actually calls."""
    candidate = _exa_candidate()
    assert candidate.pdf_url == ""
    client = _EnrichClient()
    result = asyncio.run(ps._enrich_ranked_candidates(client, [candidate]))
    assert client.calls, "production hook issued no lookup"
    assert result[0] is candidate, "must enrich in place, not create a candidate"
    assert candidate.pdf_url.endswith(PDF_PATH)


def test_enriched_pdf_url_is_serialised_so_it_reaches_patent_fetch():
    """The pack reads pdf_url off the serialised search payload."""
    import json

    candidate = _exa_candidate()
    asyncio.run(ps._enrich_ranked_candidates(_EnrichClient(), [candidate]))
    from tools.result_contract import NormalizedResult

    payload = json.loads(
        ps._json_response(
            NormalizedResult(status="ok", completed=True, reliable_no_results=False, query="q"),
            "exa",
            [candidate],
        )
    )
    assert payload["results"][0]["pdf_url"].endswith(PDF_PATH)


def test_candidates_already_carrying_a_pdf_url_are_not_looked_up():
    candidate = _exa_candidate()
    candidate.pdf_url = "https://patentimages.storage.googleapis.com/x/y.pdf"
    client = _EnrichClient()
    asyncio.run(ps._enrich_ranked_candidates(client, [candidate]))
    assert client.calls == []


# --- 3 & 4: no-downgrade holds in the real patent_fetch ----------------------

def _run_fetch(monkeypatch, *, jina_text: str, html_level: str | None, pdf_url: str = ""):
    """Drive the real patent_fetch with the PDF path failing and HTML controlled."""
    async def no_pdf(*_a, **_k):
        return ""

    async def jina(_url, timeout_s=20.0):
        return jina_text

    monkeypatch.setattr(pf, "_patent_pdf_fetch", no_pdf)
    monkeypatch.setattr(pf, "fetch_via_jina", jina)

    # PatentFetchProvider is a frozen dataclass, so the tuple is replaced.
    if html_level is None:
        async def html_fetch(_url, _timeout):
            raise pf.BotBlockedError("blocked")
    else:
        async def html_fetch(_url, _timeout):
            return ("<html></html>", "irrelevant")

        monkeypatch.setattr(
            pf, "_extract_fields",
            lambda *_a, **_k: f"PATENT_NUMBER: {NUMBER}\nEVIDENCE_LEVEL: {html_level.upper()}\n",
        )

    monkeypatch.setattr(
        pf, "PATENT_FETCH_PROVIDERS",
        (pf.PatentFetchProvider("google_patents", html_fetch),),
    )

    pf._PATENT_FETCH_CACHE.clear()
    return asyncio.run(pf.patent_fetch(URL, 10000, pdf_url=pdf_url))


def test_strong_reader_evidence_is_not_downgraded_by_later_html(monkeypatch):
    """Reader PDF claim_verified must survive an HTML abstract_verified result."""
    out = _run_fetch(
        monkeypatch, jina_text=CLAIMS, html_level="abstract_verified",
        pdf_url=f"https://patentimages.storage.googleapis.com/{PDF_PATH}",
    )
    assert pf._evidence_level_of(out) == "claim_verified", out[:200]


def test_stronger_html_evidence_upgrades_weaker_reader_evidence(monkeypatch):
    """The rule is strongest-wins, not Reader-wins."""
    out = _run_fetch(
        monkeypatch, jina_text=ABSTRACT_ONLY, html_level="claim_verified",
        pdf_url=f"https://patentimages.storage.googleapis.com/{PDF_PATH}",
    )
    assert pf._evidence_level_of(out) == "claim_verified"


# --- 5 & 6: exact identity ----------------------------------------------------

def test_a1_does_not_validate_b2():
    assert exact_publication_identity("US10762444A1") != exact_publication_identity("US10762444B2")
    assert pf._jina_evidence(CLAIMS.replace("B2", "A1"), URL, "", "US10762444B2") == ("", "")


def test_ep_a1_does_not_validate_ep_b1():
    assert exact_publication_identity("EP1234567A1") != exact_publication_identity("EP1234567B1")


def test_family_identity_still_ignores_kind_codes():
    """Dedupe semantics are unchanged and remain a separate concept."""
    assert family_base_identity("US10762444A1") == family_base_identity("US10762444B2")


def test_formatting_only_variants_match():
    for variant in ("US 10,762,444 B2", "US-10762444-B2", "us10762444b2"):
        assert exact_publication_identity(variant) == exact_publication_identity(NUMBER)


# --- 7: substantive evidence retained ----------------------------------------

def test_claim_verified_retains_actual_claim_text(monkeypatch):
    out = _run_fetch(
        monkeypatch, jina_text=CLAIMS, html_level=None,
        pdf_url=f"https://patentimages.storage.googleapis.com/{PDF_PATH}",
    )
    assert pf._evidence_level_of(out) == "claim_verified"
    claim_line = next(l for l in out.splitlines() if l.startswith("CLAIM1:"))
    assert "What is claimed is" in claim_line, f"claim text not retained in CLAIM1: {claim_line}"
    assert "No first claim was extracted" not in claim_line
    assert extract_claims_section(CLAIMS)


# --- 8: sentinel secret containment ------------------------------------------

def test_api_key_never_appears_in_fetch_output_or_attempt_log(monkeypatch):
    sentinel = "jina_SENTINEL_SECRET_do_not_leak_9f1c"
    monkeypatch.setenv("JINA_API_KEY", sentinel)
    out = _run_fetch(
        monkeypatch, jina_text=CLAIMS, html_level=None,
        pdf_url=f"https://patentimages.storage.googleapis.com/{PDF_PATH}",
    )
    assert sentinel not in out, "the API key leaked into the fetch output"
    assert "Authorization" not in out
    from tools.jina_reader import _jina_headers

    assert _jina_headers()["Authorization"] == f"Bearer {sentinel}"


def test_enrichment_runs_inside_patent_search_itself(monkeypatch):
    """Drives patent_search end to end; disabling the hook must fail this.

    An isolated call to _enrich_ranked_candidates cannot prove the production
    path invokes it -- that was the original defect.
    """
    lookups: list = []

    async def provider(_client, _variant, _max_results=10):
        return [_exa_candidate()]

    async def empty(_client, _variant, _max_results=10):
        return []

    async def fake_enrich(_client, candidate, timeout_s=12.0):
        lookups.append(candidate.patent_number)
        candidate.pdf_url = f"https://patentimages.storage.googleapis.com/{PDF_PATH}"
        return candidate

    monkeypatch.setattr(ps, "_google_patents_xhr_search", provider)
    for name in ("_tavily_patent_search", "_exa_patent_search", "_wipo_patentscope_search"):
        monkeypatch.setattr(ps, name, empty)
    monkeypatch.setattr(ps, "enrich_candidate_by_number", fake_enrich)
    ps._PATENT_SEARCH_CACHE.clear()

    out = asyncio.run(ps.patent_search("soil moisture irrigation valve schedule", 5))
    assert lookups == [NUMBER], f"patent_search did not enrich retained candidates: {lookups}"
    assert PDF_PATH in out, "the enriched pdf_url did not reach the serialised output"
