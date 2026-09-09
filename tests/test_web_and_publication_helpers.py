"""Tests of the web and publication search helpers."""

from __future__ import annotations

from tools.publications_search import (
    _fallback_publication_queries,
    _publication_dedupe_keys,
    _parse_pubmed_efetch_xml,
    _reconstruct_openalex_abstract,
)
from tools.result_contract import parse_status_marker
from tools.web_evidence_pack import _is_unsupported_evidence_url, _parse_candidates
from tools.web_search import (
    _canonical_source_url,
    _is_low_value_url,
    _looks_like_binary,
    _mdpi_url_to_doi,
    web_search,
)


def test_canonical_source_url_strips_tracking():
    url = "https://example.com/page?utm_source=x&utm_medium=y&id=42&fbclid=abc"
    assert _canonical_source_url(url) == "https://example.com/page?id=42"


def test_canonical_source_url_keeps_normal_params():
    assert _canonical_source_url("https://example.com/a?x=1") == "https://example.com/a?x=1"


def test_is_low_value_url_search_pages():
    assert _is_low_value_url("https://www.google.com/search?q=abc")
    assert _is_low_value_url("https://example.com/search/results")
    assert not _is_low_value_url("https://example.com/products/sl-100")


def test_is_low_value_url_marketplace_rules():
    assert _is_low_value_url("https://www.amazon.com/s?k=smart+lock")
    assert not _is_low_value_url("https://www.amazon.com/dp/B00TEST123")


def test_mdpi_url_to_doi():
    assert _mdpi_url_to_doi("https://www.mdpi.com/1424-8220/23/4/2075") == "10.3390/s23042075"
    assert _mdpi_url_to_doi("https://example.com/article") == ""


def test_looks_like_binary():
    assert _looks_like_binary("\x00\x01\x02" * 200)
    assert not _looks_like_binary("Plain readable text " * 50)


async def test_web_search_requires_query():
    result = await web_search("")
    assert parse_status_marker(result) == "failed"
    assert "query is required" in result


def test_parse_web_candidates_from_search_output():
    output = (
        "Result title returned by search: **Product A**.\n"
        "Source page URL for this result: https://example.com/a\n"
        "Local rerank score: 6.5/10.\n"
        "Short summary snippet from search: Summary A\n"
        "\n"
        "Result title returned by search: **Spec sheet**.\n"
        "Source page URL for this result: https://example.com/spec.pdf\n"
        "Local rerank score: 5.0/10.\n"
        "Short summary snippet from search: PDF datasheet snippet"
    )
    candidates, skipped = _parse_candidates(output)
    assert len(candidates) == 2
    assert candidates[0].url == "https://example.com/a"
    assert candidates[0].score == 6.5
    assert candidates[1].no_fetch is True
    assert skipped == []


def test_is_unsupported_evidence_url():
    assert _is_unsupported_evidence_url("https://x.com/file.pdf")
    assert _is_unsupported_evidence_url("https://x.com/file.PDF?download=1")
    assert not _is_unsupported_evidence_url("https://x.com/page.html")


def test_fallback_publication_queries_strip_fillers():
    variants = _fallback_publication_queries("does a smart door lock with mobile app already exist")
    assert variants
    assert "does" not in variants[0].split()
    assert "smart" in variants[0]


def test_publication_dedupe_keys():
    block = (
        "Publication title returned by Crossref: **Smart Lock Study**.\n"
        "DOI URL constructed for this publication: https://doi.org/10.1000/XYZ.\n"
    )
    doi_key, title_key = _publication_dedupe_keys(block)
    assert doi_key == "10.1000/xyz"
    assert title_key == "smart lock study"


def test_reconstruct_openalex_abstract():
    inverted = {"lock": [1], "Smart": [0], "system": [2]}
    assert _reconstruct_openalex_abstract(inverted) == "Smart lock system"
    assert _reconstruct_openalex_abstract(None) == ""
    assert _reconstruct_openalex_abstract({}) == ""


def test_parse_pubmed_efetch_xml():
    xml_text = """<?xml version=\"1.0\"?>
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>12345678</PMID>
          <Article>
            <Journal><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue></Journal>
            <ArticleTitle>Smart lock security analysis</ArticleTitle>
            <Abstract><AbstractText Label="BACKGROUND">Locks are studied.</AbstractText></Abstract>
            <AuthorList>
              <Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author>
            </AuthorList>
          </Article>
        </MedlineCitation>
        <PubmedData>
          <ArticleIdList>
            <ArticleId IdType="pubmed">12345678</ArticleId>
            <ArticleId IdType="doi">10.1000/abc</ArticleId>
          </ArticleIdList>
        </PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
    """
    records = _parse_pubmed_efetch_xml(xml_text)
    assert len(records) == 1
    record = records[0]
    assert record["title"] == "Smart lock security analysis"
    assert record["abstract"] == "BACKGROUND: Locks are studied."
    assert record["year"] == "2021"
    assert record["doi"] == "10.1000/abc"
    assert record["pmid"] == "12345678"
    assert record["authors"] == "Jane Doe"


def test_parse_pubmed_efetch_xml_malformed():
    assert _parse_pubmed_efetch_xml("<not-xml") == []
