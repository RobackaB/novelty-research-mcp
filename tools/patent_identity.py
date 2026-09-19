"""Identity and evidence-promotion gates for patent verification.

Verification is independent of discovery: a candidate found by Exa may be
verified through a structured lookup, the official PDF or the Reader backend.
Whatever the route, the content must be shown to belong to the requested
publication before it can strengthen that candidate's evidence level, and it
must never create a second candidate for the same document.

Two separate gates:

* identity -- does this content belong to the requested publication?
* content  -- does it structurally contain claims or an abstract?

Both must pass before evidence is promoted above ``fetched_excerpt``.
"""

from __future__ import annotations

import re
from typing import Final, Literal

IdentityBasis = Literal[
    "structured_lookup",
    "official_pdf_url",
    "canonical_url",
    "normalised_number_in_content",
    "none",
]

# Ordered strongest first. A basis earlier in this tuple outranks a later one.
IDENTITY_BASIS_ORDER: Final[tuple[IdentityBasis, ...]] = (
    "structured_lookup",
    "normalised_number_in_content",
    "official_pdf_url",
    "canonical_url",
)

# Evidence levels this module may assign, weakest first. Used only to enforce
# the no-downgrade rule; the global multipliers in result_contract are untouched.
_PROMOTION_ORDER: Final[tuple[str, ...]] = (
    "provider_error",
    "fetch_failed",
    "fetch_timeout",
    "citation_only",
    "unverified",
    "search_snippet_only",
    "fetched_excerpt",
    "abstract_verified",
    "claim_verified",
)


def exact_publication_identity(value: str) -> str:
    """Reduce a publication number to an EXACT identity key.

    Formatting is normalised -- "US 10,762,444 B2", "US-10762444-B2" and
    "US10762444B2" are one publication -- but the kind code is PRESERVED.

    US10762444A1 (application) and US10762444B2 (granted patent) are different
    documents with different claims, and EP...A1 and EP...B1 likewise. Treating
    them as interchangeable would let an application's text be credited as
    verification of a granted patent. Family/base identity, which deliberately
    ignores kind codes for deduplication, is a separate concept: see
    family_base_identity.
    """
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not raw or raw == "UNKNOWN":
        return ""
    return raw


def family_base_identity(value: str) -> str:
    """Reduce a publication number to a kind-code-insensitive base key.

    Provided for callers that genuinely want family-level grouping. It is NOT
    used for verification identity or enrichment. The existing dedupe policy in
    patent_search is unchanged by this module.
    """
    raw = exact_publication_identity(value)
    if not raw:
        return ""
    return re.sub(r"([A-Z]{2}\d{4,})[A-Z]\d?$", r"\1", raw)


# Back-compat alias kept intentionally narrow: enrichment and verification must
# call exact_publication_identity explicitly.
normalise_publication_number = exact_publication_identity


_PUBLICATION_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:US|EP|WO|GB|DE|FR|CA|AU|JP|CN|KR|RU|IN|TW|ES|IT|BR|MX|HK|SG|CH|AT|BE|NL|SE|FI|DK)"
    r"[ \t-]*\d[\d,./]*(?:[ \t]+\d{3,})*[ \t-]*(?:[A-Z]\d{0,2})?(?![A-Za-z0-9])",
    re.I,
)


def publication_in_header(content: str) -> str:
    """Read the first publication identifier in document front matter.

    A requested publication mentioned only in another patent's citations or in
    Reader's URL envelope cannot establish document identity.
    """
    text = str(content or "")
    marker = re.search(r"(?im)^Markdown Content:\s*", text)
    if marker:
        text = text[marker.end():]
    text = re.sub(r"(?im)^URL Source:.*$", "", text)
    text = re.split(r"(?im)^\s*(?:references cited|patent citations|similar documents)\b", text)[0]
    match = _PUBLICATION_TOKEN.search(text[:1600])
    return exact_publication_identity(match.group()) if match else ""


def content_contains_publication(content: str, patent_number: str) -> bool:
    """Match publication identity in document front matter, including kind code."""
    target = exact_publication_identity(patent_number)
    return bool(target and publication_in_header(content) == target)


def resolve_identity(
    *,
    patent_number: str,
    content: str = "",
    structured_lookup_number: str = "",
    official_pdf_url: str = "",
    canonical_url: str = "",
) -> IdentityBasis:
    """Return the strongest identity basis available, or "none".

    Exact structured identity and exact retrieved-content identity outrank URL
    locators. Family/base URL matches describe request provenance only and
    cannot support strong evidence promotion.
    """
    target = exact_publication_identity(patent_number)
    if not target:
        return "none"
    if exact_publication_identity(structured_lookup_number) == target:
        return "structured_lookup"
    if content_contains_publication(content, patent_number):
        return "normalised_number_in_content"
    # URL-derived bases are matched on the kind-code-insensitive base, because
    # patentimages filenames frequently omit the kind code (".../US10762444.pdf"
    # for US10762444B2). These bases assert only which document was REQUESTED --
    # the URL was obtained for this publication -- and are never sufficient to
    # promote retrieved content on their own. Content matching below stays exact.
    base = family_base_identity(patent_number)
    if official_pdf_url and base and base in family_base_identity(official_pdf_url):
        return "official_pdf_url"
    if canonical_url and base and base in family_base_identity(canonical_url):
        return "canonical_url"
    return "none"


# Plain-text headings retain their line structure, including Reader Markdown
# and patent PDF INID labels. A numbered list by itself is not a claims section.
_CLAIMS_SECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:what\s+is\s+claimed(?:\s+is)?|we\s+claim|i\s+claim|the\s+invention\s+claimed\s+is)\s*:", re.I),
    re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?claims?[ \t]*(?:\(\d+\))?[ \t]*:?[ \t]*$"),
)
_ABSTRACT_SECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\(57\)[ \t]*)?abstract(?: of the disclosure)?[ \t]*:?[ \t]*(?:$|(?<=:))"),
)
_SECTION_END = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:abstract|claims?(?:[ \t]*\(\d+\))?|description|"
    r"background(?: of the invention)?|summary(?: of the invention)?|references cited|"
    r"patent citations|similar documents|legal events|external links)[ \t]*:?[ \t]*$"
)
_MIN_CLAIM_WORDS: Final = 25
_MIN_ABSTRACT_WORDS: Final = 20


def section_unavailable(text: str, section: str) -> bool:
    """Reject section-load notices in HTML, PDF and Reader text."""
    # PDF and Reader can wrap a notice across lines; preserve the source text
    # elsewhere and normalize whitespace only for this availability check.
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^1\s*[.)]\s*", "", text)
    return bool(re.match(
        rf"(?:no (?:first )?{section}\b|(?:the )?{section}(?: of this patent)? "
        r"(?:are|is|was|were|could)\b.{0,80}\b(?:unavailable|not available|not be loaded|not loaded|missing)\b)",
        text, re.I,
    ))


def _section(content: str, patterns: tuple[re.Pattern[str], ...], minimum: int, *, claims: bool = False) -> str:
    """Extract bounded section text; later unrelated sections cannot supply length."""
    text = content or ""
    for pattern in patterns:
        for match in pattern.finditer(text):
            tail = text[match.end():]
            end = _SECTION_END.search(tail)
            body = tail[:end.start()] if end else tail
            if len(body.split()) < minimum:
                continue
            if section_unavailable(body, "claims?" if claims else "abstract"):
                continue
            # A claims heading must lead to a numbered claim, not prose about
            # claims or navigation. Explicit legal openers are also accepted.
            if claims and not re.search(r"\bclaim(?:ed)?\s*(?:is)?\s*:", match.group(), re.I):
                if not re.match(r"\s*1\s*[.)]\s+\S", body):
                    continue
            start = match.start() if claims else match.end()
            return text[start:match.end() + len(body)].strip()
    return ""


def extract_claims_section(content: str) -> str:
    """Retain the identified claims-bearing extraction without paraphrasing."""
    return _section(content, _CLAIMS_SECTION_PATTERNS, _MIN_CLAIM_WORDS, claims=True)


def extract_abstract_section(content: str) -> str:
    """Retain an actual, bounded abstract section without consuming later text."""
    return _section(content, _ABSTRACT_SECTION_PATTERNS, _MIN_ABSTRACT_WORDS)


def recognised_claims_section(content: str) -> bool:
    return bool(extract_claims_section(content))


def recognised_abstract_section(content: str) -> bool:
    return bool(extract_abstract_section(content))


def promote_evidence_level(current: str, candidate: str) -> str:
    """Return the stronger of two evidence levels.

    A later, weaker fallback must never overwrite claim_verified or
    abstract_verified obtained earlier in the chain. Unknown levels are treated
    as weakest so an unexpected value cannot silently win.
    """
    def rank(level: str) -> int:
        try:
            return _PROMOTION_ORDER.index(str(level or "").strip().lower())
        except ValueError:
            return -1

    return candidate if rank(candidate) > rank(current) else current


def evidence_level_for_content(
    *,
    content: str,
    identity: IdentityBasis,
    min_words: int = 60,
) -> str:
    """Decide the level a piece of retrieved content may support.

    Conservative by construction. Without identity nothing is promoted, and
    substantive text alone reaches only fetched_excerpt.
    """
    text = content or ""
    if identity == "none" or len(text.split()) < min_words:
        return "search_snippet_only"
    if identity not in {"structured_lookup", "normalised_number_in_content"}:
        return "fetched_excerpt"
    if recognised_claims_section(text):
        return "claim_verified"
    if recognised_abstract_section(text):
        return "abstract_verified"
    return "fetched_excerpt"
