"""Patent-number enrichment: recover an official PDF URL for any candidate.

Only google_patents_xhr returns a `pdf` path, so a candidate discovered by
Tavily, Exa or WIPO previously reached patent_fetch with pdf_url empty and could
be verified only through the HTML page -- the one path that gets blocked.
Discovery and verification are independent; enrichment must strengthen the SAME
candidate rather than create a second one.
"""

from __future__ import annotations

import asyncio

import pytest

import tools.patent_search as ps

NUMBER = "US10762444B2"
PDF_PATH = "aa/bb/US10762444.pdf"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload, record=None):
        self._payload = payload
        self.record = record if record is not None else []

    async def get(self, url, params=None, headers=None):
        self.record.append((url, params))
        return _Response(self._payload)


def _payload(number: str, pdf: str = PDF_PATH):
    return {
        "results": {
            "cluster": [
                {"result": [{"patent": {
                    "publication_number": number,
                    "pdf": pdf,
                    "assignee": "Example Corp",
                    "filing_date": "2018-01-01",
                }}]}
            ]
        }
    }


def _exa_candidate() -> ps.PatentCandidate:
    return ps.PatentCandidate(
        title="Irrigation controller",
        url=f"https://patents.google.com/patent/{NUMBER}/en",
        patent_number=NUMBER,
        snippet="Soil moisture irrigation control.",
        provider="exa",
    )


def test_candidate_without_pdf_url_is_enriched_in_place():
    candidate = _exa_candidate()
    assert candidate.pdf_url == ""
    result = asyncio.run(ps.enrich_candidate_by_number(_Client(_payload(NUMBER)), candidate))
    assert result is candidate, "enrichment must not create a second candidate"
    assert result.pdf_url.endswith(PDF_PATH)
    assert result.provider == "exa", "discovery provider must be preserved"


def test_enrichment_rejects_a_different_publication():
    """A near-miss result must not lend its PDF to this candidate."""
    candidate = _exa_candidate()
    asyncio.run(ps.enrich_candidate_by_number(_Client(_payload("US9111111B1")), candidate))
    assert candidate.pdf_url == ""


def test_enrichment_accepts_a_differently_formatted_number():
    candidate = _exa_candidate()
    asyncio.run(ps.enrich_candidate_by_number(_Client(_payload("US 10,762,444 B2")), candidate))
    assert candidate.pdf_url.endswith(PDF_PATH)


def test_candidate_that_already_has_a_pdf_url_is_not_re_fetched():
    candidate = _exa_candidate()
    candidate.pdf_url = "https://patentimages.storage.googleapis.com/x/y.pdf"
    client = _Client(_payload(NUMBER))
    asyncio.run(ps.enrich_candidate_by_number(client, candidate))
    assert client.record == [], "no lookup should be issued"


def test_enrichment_is_fail_open():
    class _Boom:
        async def get(self, *_a, **_k):
            raise RuntimeError("blocked")

    candidate = _exa_candidate()
    result = asyncio.run(ps.enrich_candidate_by_number(_Boom(), candidate))
    assert result is candidate and result.pdf_url == ""
