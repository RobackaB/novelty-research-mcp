"""PR 1: patent identity gate, Reader fallback and conservative promotion.

Reader is used as an alternate legitimate fetch backend, never as a means of
circumventing access controls. Every test here pins a rule that stops Reader
output being credited as stronger evidence than it actually is.
"""

from __future__ import annotations

import asyncio

import pytest

import tools.patent_fetch as pf
from tools.patent_identity import (
    evidence_level_for_content,
    normalise_publication_number,
    promote_evidence_level,
    recognised_abstract_section,
    recognised_claims_section,
    resolve_identity,
)

URL = "https://patents.google.com/patent/US10762444B2/en"
PDF_URL = "https://patentimages.storage.googleapis.com/aa/bb/US10762444.pdf"
NUMBER = "US10762444B2"

CLAIMS_TEXT = (
    "United States Patent US 10,762,444 B2\n\n"
    "What is claimed is:\n"
    "1. An irrigation controller comprising a soil moisture sensor, a wireless "
    "transmitter arranged to send measurements to a control unit, and a valve "
    "actuated according to a stored watering schedule, wherein a scheduled "
    "watering is skipped when the measured moisture exceeds a threshold value."
)

ABSTRACT_TEXT = (
    "US 10,762,444 B2\n\n"
    "Abstract\n"
    "An irrigation system measures soil moisture with a sensor and transmits the "
    "measurement wirelessly to a control unit which operates a valve according to "
    "a schedule stored in the unit."
)

# Carries the patent number and exceeds the word threshold on purpose: identity
# and length both pass, so only the block-page check can reject it.
BLOCK_PAGE = (
    "US 10,762,444 B2 patents.google.com\n"
    "Our systems have detected unusual traffic from your computer network. "
    "your computer or network may be sending automated queries. "
    "Please try your request again later. " * 20
)


# --- identity ----------------------------------------------------------------

def test_normalised_identity_accepted_despite_formatting_differences():
    for variant in ("US 10,762,444 B2", "US-10762444-B2", "us10762444b2"):
        assert normalise_publication_number(variant) == normalise_publication_number(NUMBER)


def test_identity_basis_is_ordered_strongest_first():
    assert resolve_identity(
        patent_number=NUMBER, structured_lookup_number=NUMBER, canonical_url=URL
    ) == "structured_lookup"
    assert resolve_identity(patent_number=NUMBER, official_pdf_url=PDF_URL) == "official_pdf_url"
    assert resolve_identity(patent_number=NUMBER, canonical_url=URL) == "canonical_url"
    assert resolve_identity(
        patent_number=NUMBER, content=CLAIMS_TEXT
    ) == "normalised_number_in_content"


def test_fuzzy_title_similarity_is_not_identity_proof():
    """A shared title must never establish identity."""
    same_title_other_patent = (
        "US 9,999,999 B1\nIrrigation controller with soil moisture sensor\n"
        "What is claimed is:\n1. A different irrigation apparatus comprising a "
        "moisture probe and a solenoid valve operated on a timer schedule."
    )
    assert resolve_identity(patent_number=NUMBER, content=same_title_other_patent) == "none"


def test_content_for_a_different_patent_is_not_promoted():
    """Substantive claims text for another publication must not be promoted.

    Long enough and structurally a claims section, so only the identity gate can
    reject it. The canonical URL is deliberately for a third publication, so no
    identity basis can succeed.
    """
    other = (CLAIMS_TEXT.replace("10,762,444", "9,111,111") + " ") * 6
    level, validated = pf._jina_evidence(
        other, "https://patents.google.com/patent/US8000000B1/en", "", "US8000000B1"
    )
    assert level == "" and validated == ""


# --- claims / abstract recognition -------------------------------------------

def test_unrelated_prose_containing_the_word_claims_is_not_claim_verified():
    """The existing PDF marker list contains the bare word "claims"."""
    prose = (
        "US 10,762,444 B2. The applicant claims priority from an earlier filing. "
        "Insurance claims processing is unrelated to this document. " * 6
    )
    assert not recognised_claims_section(prose)
    level, _ = pf._jina_evidence(prose, URL, "", NUMBER)
    assert level != "claim_verified"


def test_structural_claims_section_is_recognised():
    assert recognised_claims_section(CLAIMS_TEXT)


def test_structural_abstract_section_is_recognised():
    assert recognised_abstract_section(ABSTRACT_TEXT)
    assert not recognised_claims_section(ABSTRACT_TEXT)


def test_claims_opener_without_following_text_is_not_enough():
    assert not recognised_claims_section("US 10,762,444 B2\nWhat is claimed is:\n")


# --- promotion ordering -------------------------------------------------------

def test_strong_evidence_cannot_be_downgraded_by_a_weaker_fallback():
    assert promote_evidence_level("claim_verified", "fetched_excerpt") == "claim_verified"
    assert promote_evidence_level("abstract_verified", "search_snippet_only") == "abstract_verified"
    assert promote_evidence_level("fetched_excerpt", "claim_verified") == "claim_verified"
    assert promote_evidence_level("", "fetched_excerpt") == "fetched_excerpt"


def test_substantive_text_alone_reaches_only_fetched_excerpt():
    body = "US 10,762,444 B2 " + ("irrigation valve schedule moisture sensor wireless " * 30)
    assert evidence_level_for_content(
        content=body, identity="canonical_url"
    ) == "fetched_excerpt"


def test_no_identity_means_no_promotion():
    assert evidence_level_for_content(
        content=CLAIMS_TEXT, identity="none"
    ) == "search_snippet_only"


# --- Reader backend behaviour -------------------------------------------------

def test_block_page_from_reader_is_rejected():
    level, validated = pf._jina_evidence(BLOCK_PAGE, URL, "", NUMBER)
    assert level == "" and validated == ""


def test_reader_claims_content_becomes_claim_verified():
    level, validated = pf._jina_evidence(CLAIMS_TEXT * 3, URL, PDF_URL, NUMBER)
    assert level == "claim_verified"
    assert validated


def test_reader_failure_is_fail_open(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("reader unavailable")

    monkeypatch.setattr(pf, "fetch_via_jina", boom)
    assert asyncio.run(pf._jina_patent_fetch(URL, 10000)) == ""


def test_missing_api_key_still_attempts_and_sends_no_authorization(monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    from tools.jina_reader import _jina_headers

    headers = _jina_headers()
    assert "Authorization" not in headers


def test_api_key_never_appears_in_the_reader_url():
    """The key travels in a header only, so it cannot leak into a persisted URL."""
    import inspect

    from tools.jina_reader import fetch_via_jina

    source = inspect.getsource(fetch_via_jina)
    assert "r.jina.ai/{url}" in source.replace('f"', "").replace('"', "")
    assert "api_key" not in source
