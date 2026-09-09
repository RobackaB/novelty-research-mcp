"""Tests of the patent filters and the bound on search variants."""

from __future__ import annotations

from tools.patent_filters import extract_patent_number, is_valid_patent_result
from tools.search_bounds import compact_query_variants, patent_search_plan, section_query_variants


def test_extract_patent_number_from_url():
    assert extract_patent_number("https://patents.google.com/patent/US1234567B2/en") == "US1234567B2"


def test_extract_patent_number_from_title():
    assert extract_patent_number("Smart lock", "EP2345678A1 granted") == "EP2345678A1"


def test_extract_patent_number_unknown():
    assert extract_patent_number("no patent here") == "Unknown"


def test_is_valid_patent_result_accepts_google_detail():
    assert is_valid_patent_result(
        "Smart door lock",
        "US1234567B2",
        "https://patents.google.com/patent/US1234567B2/en",
        "A smart lock with app control",
    )


def test_is_valid_patent_result_rejects_portal_root():
    assert not is_valid_patent_result("Google Patents", "US1234567B2", "https://patents.google.com/", "")


def test_is_valid_patent_result_rejects_unknown_domain():
    assert not is_valid_patent_result("Smart lock", "US1234567B2", "https://example.com/patent/US1234567B2", "")


def test_is_valid_patent_result_rejects_search_ui_text():
    assert not is_valid_patent_result(
        "Results / page",
        "US1234567B2",
        "https://patents.google.com/patent/US1234567B2/en",
        "group by deduplicate by sort by",
    )


def test_section_query_variants_from_section():
    plan_text = "PATENT_QUERIES:\n- smart lock app\n- door lock codes\nWEB_QUERIES:\n- other"
    variants = section_query_variants(plan_text, "PATENT_QUERIES", max_variants=2)
    assert variants == ["smart lock app", "door lock codes"]


def test_section_query_variants_plain_text_fallback():
    variants = section_query_variants("plain query text", "PATENT_QUERIES", max_variants=2)
    assert variants == ["plain query text"]


def test_compact_query_variants_deduplicates():
    plan_text = "PATENT_QUERIES:\n- smart lock\n- Smart Lock\n- smart lock"
    assert compact_query_variants(plan_text, max_variants=3) == ["smart lock"]


def test_patent_search_plan_bounded():
    plan = patent_search_plan("query", max_variants=2, max_domains=3, max_requests=4)
    assert len(plan) <= 4
    assert all(len(item) == 2 for item in plan)
