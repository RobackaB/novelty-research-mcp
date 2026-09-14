"""Synthetic projection regressions for embedded production metadata."""

import pytest

from eval.goal5c import projection
from eval.goal5c.contracts import ContractError
from eval.goal5c.projection import PROJECTION_SEPARATOR, project_text, validate_visible_text


def test_projection_preserves_exact_unicode_evidence_and_order():
    text = "Local rerank score: 9.5/10.\nČlánok 🧪\nProvider: synthetic\nExact  abstract.\n"
    first = text.index("Článok")
    second = text.index("Exact")
    assert project_text(text, [[first, text.index("\n", first)], [second, len(text)]]) == (
        "Článok 🧪" + PROJECTION_SEPARATOR + "Exact  abstract.\n"
    )


@pytest.mark.parametrize("text", ["", "Provider: synthetic", "Local rerank score: 1/10"])
def test_empty_projection_explicitly_omits_all_text(text):
    assert project_text(text, []) == ""


@pytest.mark.parametrize("text", ["Title", "Title\nAbstract", "Title\r\nAbstract\r\n"])
def test_whole_document_without_metadata_is_unchanged(text):
    assert project_text(text, [[0, len(text)]]) == text


@pytest.mark.parametrize("end", [5, 7])
def test_complete_crlf_line_may_include_or_exclude_terminator(end):
    text = "Title\r\nAbstract"
    assert project_text(text, [[0, end]]) == text[:end]


@pytest.mark.parametrize("line", [
    "Local rerank score: 8.0/10.",
    "Local rerank score for this publication: 8.0/10.",
    "Patent rerank threshold used: 2.80",
    "Publication title returned by Semantic Scholar: **Example**.",
    "Publication year returned by PubMed: 2020.",
    "Publication title returned by AlphaXiv: **Example**.",
    "Publication title returned by Crossref: **Example**.",
    "Publication title returned by OpenAlex: **Example**.",
    "Semantic Scholar URL for this publication: https://example.invalid/1.",
    "WEB_SEARCH_PROVIDER: mixed",
    "SOURCE: AlphaXiv",
    "SOURCE: PubMed",
    "SOURCE: OpenAlex",
    "Result title returned by search: **Example**.",
    "Publication provider counts: pubmed=1.",
    "Provider dominance warning: 'openalex' supplied 3/3 selected blocks",
    "Corpus-relative rerank dropped 3 candidate(s).",
    "Merged hits from web providers: synthetic",
    "Completed providers without hits: synthetic",
    "STATUS: OK",
    "ANALYSIS: adequate",
    "COVERAGE_TOKENS: widget",
    '"provider": "synthetic"',
    "**retained**: true",
    "rejected=true",
    "accepted: false",
    "relevance_score: 3.0",
    "score_at_decision: 4.0",
    "threshold_at_decision: 3.0",
    "decision_reason: below_threshold",
    "decision_stage: provider_filter",
    "source_type: web",
    "source-stage: gate",
    "rank: 1",
    "scorer: generic",
    "retry: 2",
    "attempt_number=2",
    "grade: high",
    "verdict: relevant",
    "evidence_level: snippet",
    "reliable_no_results: 1",
    "dataset_eligible: true",
    "Ｐｒｏｖｉｄｅｒ： synthetic",
])
def test_visible_metadata_is_rejected_in_content_titles_and_topic_cards(line):
    with pytest.raises(ContractError, match="system metadata"):
        validate_visible_text(line)
    with pytest.raises(ContractError, match="system metadata"):
        project_text(line, [[0, len(line)]])


@pytest.mark.parametrize("text,spans", [
    ("Provider: synthetic", [[10, 19]]),
    ("Title\nLocal rerank score: 9.0/10.", [[25, 32]]),
    ("Title and extra", [[0, 5]]),
    ("Title\r\nAbstract", [[0, 6]]),
    ("Title\r\nAbstract", [[6, 15]]),
])
def test_partial_line_projection_cannot_strip_metadata_labels(text, spans):
    with pytest.raises(ContractError):
        project_text(text, spans)


@pytest.mark.parametrize("spans", [
    None, (), {}, [[False, 5]], [[0, True]], [[0.0, 5]], [[0]],
    [(0, 5)], [[-1, 5]], [[0, 100]], [[3, 2]], [[0, 0]],
    [[6, 14], [0, 5]], [[0, 14], [6, 14]],
])
def test_invalid_ranges_fail_without_coercion(spans):
    with pytest.raises(ContractError):
        project_text("Title\nAbstract", spans)


@pytest.mark.parametrize("text", [
    None, 123, "provi\u200bder: synthetic", "score\x00: 2", "Title\rHidden",
    "Title\u2028score: 2", "Title\u2029provider: hidden", "Title\ud800",
])
def test_hidden_boundaries_and_controls_fail_closed(text):
    with pytest.raises(ContractError):
        validate_visible_text(text)
    with pytest.raises(ContractError):
        project_text(text, [])


def test_scientific_words_without_system_metadata_are_allowed():
    text = "Rank deficiency and threshold dynamics\nWe compare a score distribution."
    validate_visible_text(text)
    assert project_text(text, [[0, len(text)]]) == text


def test_projection_errors_do_not_echo_payload():
    with pytest.raises(ContractError) as error:
        project_text("provider: PRIVATE_SENTINEL", [[0, 26]])
    assert "PRIVATE_SENTINEL" not in str(error.value)


def test_negative_control_removed_metadata_guard_breaks_leak_oracle(monkeypatch):
    """Restoring an unchecked projection causes the rejection oracle to fail."""
    text = "Local rerank score for this publication: 9.0/10."

    def rejection_oracle():
        with pytest.raises(ContractError, match="system metadata"):
            project_text(text, [[0, len(text)]])

    rejection_oracle()
    monkeypatch.setattr(projection, "validate_visible_text", lambda text: None)
    with pytest.raises(pytest.fail.Exception):
        rejection_oracle()
