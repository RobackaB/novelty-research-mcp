"""Testy čistenia textu výstupu podľa defektov z reálneho reportu."""

from __future__ import annotations

import json

from tools.output_cleaner import (
    collapse_repeats,
    strip_boilerplate,
    strip_leading_title,
    unescape_entities,
)
from tools.user_answer import (
    _sanitize_display_text,
    _sanitize_summary,
    _summary_carries_evidence,
    build_user_answer_payload,
)
from tools.relevance import tokens


# --- HTML entity -------------------------------------------------------------

def test_unescape_handles_double_encoding():
    assert unescape_entities("AI &amp; SIEM") == "AI & SIEM"
    assert unescape_entities("AI &amp;amp; SIEM") == "AI & SIEM"
    assert unescape_entities("plain text") == "plain text"
    assert unescape_entities("") == ""


# --- Zopakované výrazy -------------------------------------------------------

def test_collapse_pipe_repeat_from_page_title():
    assert collapse_repeats("Rootly | Rootly Anomaly Scoring Engine") == "Rootly Anomaly Scoring Engine"


def test_collapse_immediate_word_repeat():
    assert collapse_repeats("Product Product") == "Product"
    assert collapse_repeats("Product Product Product") == "Product"


def test_collapse_leaves_normal_text_intact():
    assert collapse_repeats("anomaly detection in application logs") == "anomaly detection in application logs"


# --- Navigačný a metadátový balast -------------------------------------------

def test_strip_repository_metadata():
    text = "logmind AI-powered log anomaly detection CLI - Stars: 0 - Forks: 0 - Watchers: 3"
    cleaned = strip_boilerplate(text)
    assert "Stars" not in cleaned
    assert "Forks" not in cleaned
    assert "log anomaly detection CLI" in cleaned


def test_strip_markdown_repository_header():
    assert strip_boilerplate("# Repository: owner/name real content here").startswith("owner/name")
    assert strip_boilerplate("## Section title text").startswith("Section title text")


def test_strip_navigation_calls_to_action():
    text = "Anomaly engine is available now. Click here to check it out. Subscribe now"
    cleaned = strip_boilerplate(text)
    assert "Click here" not in cleaned
    assert "available now" not in cleaned
    assert "Anomaly engine" in cleaned


def test_strip_boilerplate_keeps_technical_sentence():
    text = "The system detects anomalies in application logs and notifies the administrator."
    assert strip_boilerplate(text) == text.strip(" .,;:|-–—")


# --- Duplicitný názov na začiatku súhrnu -------------------------------------

def test_strip_leading_title_removes_duplicate():
    title = "Detecting Anomalies in Application Logs"
    summary = "Detecting Anomalies in Application Logs This paper presents a method."
    assert strip_leading_title(summary, title) == "This paper presents a method."


def test_strip_leading_title_ignores_short_or_absent_title():
    assert strip_leading_title("Some summary text", "AB") == "Some summary text"
    assert strip_leading_title("Some summary text", "Different Title Here") == "Some summary text"


# --- Skutočné reťazce z reálneho reportu -------------------------------------

def test_real_report_github_entry_is_cleaned():
    title = _sanitize_display_text(
        "GitHub - PhilipLykov/LogPulseAI: AI-Powered Log Intelligence &amp; SIEM Platform", 220
    )
    summary = _sanitize_summary(
        "# Repository: PhilipLykov/LogPulseAI AI-Powered Log Intelligence &amp; SIEM Platform "
        "- Stars: 12 - Forks: 3 - Watchers: 1",
        360,
        "en",
        title=title,
    )
    assert "&amp;" not in title and "&" in title
    assert "# Repository:" not in summary
    assert "Stars:" not in summary
    assert "Log Intelligence" in summary


def test_real_report_rootly_entry_is_cleaned():
    title = _sanitize_display_text("Rootly | Rootly Anomaly Scoring Engine Flags Outliers in Real Time", 220)
    summary = _sanitize_summary(
        "Rootly | Rootly Anomaly Scoring Engine Flags Outliers in Real Time 2025 Top People "
        "Making the World More Reliable is available now. Click here to check it out. Product Product",
        360,
        "en",
        title=title,
    )
    assert title == "Rootly Anomaly Scoring Engine Flags Outliers in Real Time"
    assert "Click here" not in summary
    assert "Product Product" not in summary
    assert not summary.startswith("Rootly Anomaly Scoring Engine")


# --- Súhrn bez dôkaznej hodnoty ----------------------------------------------

def test_summary_without_query_terms_is_dropped():
    query_tokens = tokens("anomaly detection in application logs")
    assert _summary_carries_evidence("Top People Making the World More Reliable", query_tokens) is False
    assert _summary_carries_evidence("Detects anomalies in system logs", query_tokens) is True


def test_summary_check_passes_when_no_query_tokens():
    assert _summary_carries_evidence("anything at all", None) is True
    assert _summary_carries_evidence("anything at all", set()) is True


def test_report_omits_meaningless_summary_but_keeps_hit():
    pack = {
        "source_type": "merged_evidence_pack",
        "query": "anomaly detection in application logs",
        "overall_status": "complete",
        "patents": {"source_type": "patent", "status": "ok", "completed": True,
                    "reliable_no_results": True, "hits": [], "errors": [], "warnings": []},
        "publications": {"source_type": "publication", "status": "ok", "completed": True,
                         "reliable_no_results": True, "hits": [], "errors": [], "warnings": []},
        "web": {
            "source_type": "web", "status": "ok", "completed": True, "reliable_no_results": False,
            "hits": [{
                "title": "Rootly Anomaly Scoring Engine",
                "url": "https://example.com/rootly",
                "evidence_level": "fetched_excerpt",
                "summary": "2025 Top People Making the World More Reliable Product",
                "verified_url": True,
                "relevance": "direct",
                "relevance_score": 7.0,
            }],
            "errors": [], "warnings": [],
        },
        "flags": {}, "warnings": [], "errors": [],
    }
    answer = build_user_answer_payload(
        merged_pack=pack, original_query="anomaly detection in application logs"
    )["user_answer"]
    assert "Rootly Anomaly Scoring Engine" in answer, "nález sa má naďalej zobraziť"
    assert "Top People Making the World" not in answer, "bezobsažný súhrn sa nemá zobraziť"
    assert "https://example.com/rootly" in answer
