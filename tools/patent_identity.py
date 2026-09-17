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
    "official_pdf_url",
    "canonical_url",
    "normalised_number_in_content",
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


def content_contains_publication(content: str, patent_number: str) -> bool:
    """Check whether the returned content carries the requested publication number.

    Matching is done on the normalised form of every number-shaped token in the
    content, so a differently formatted rendering of the same publication still
    matches. Fuzzy title similarity is deliberately not accepted anywhere: two
    unrelated patents routinely share a title.
    """
    target = exact_publication_identity(patent_number)
    if not target:
        return False
    for token in re.findall(r"[A-Za-z]{2}[\s\-]?[\d,\s]{4,}[A-Za-z]?\d?", content or ""):
        if exact_publication_identity(token) == target:
            return True
    return False


def resolve_identity(
    *,
    patent_number: str,
    content: str = "",
    structured_lookup_number: str = "",
    official_pdf_url: str = "",
    canonical_url: str = "",
) -> IdentityBasis:
    """Return the strongest identity basis available, or "none".

    Ordered exactly as the review requires: a structured lookup tied to the
    requested publication is strongest, then an official document URL obtained
    for it, then the canonical URL, then the normalised number appearing in the
    returned content.
    """
    target = exact_publication_identity(patent_number)
    if not target:
        return "none"
    if exact_publication_identity(structured_lookup_number) == target:
        return "structured_lookup"
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
    if content_contains_publication(content, patent_number):
        return "normalised_number_in_content"
    return "none"


# Structural claims openers. The bare word "claims" is deliberately absent: it
# occurs in ordinary prose ("the applicant claims", "claims processing") and is
# not evidence that a claims section was retrieved.
_CLAIMS_SECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bwhat\s+is\s+claimed\s+is\b", re.I),
    re.compile(r"\bwe\s+claim\b", re.I),
    re.compile(r"\bi\s+claim\b", re.I),
    re.compile(r"\bthe\s+invention\s+claimed\s+is\b", re.I),
    re.compile(r"\bhaving\s+thus\s+described\s+the\s+invention[^.]{0,80}claim", re.I),
    # A numbered claim opener on its own line, e.g. "1. A soil moisture sensor..."
    re.compile(r"(?m)^\s*1\s*[.)]\s+(?:A|An|The)\s+\w", re.I),
    re.compile(r"(?m)^\s*claims?\s*:?\s*$", re.I),
)

_ABSTRACT_SECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?m)^\s*abstract\s*:?\s*$", re.I),
    re.compile(r"\babstract\s+of\s+the\s+disclosure\b", re.I),
    re.compile(r"(?m)^\s*abstract\s*[:\-]\s*\S", re.I),
)

_MIN_CLAIM_WORDS: Final = 25
_MIN_ABSTRACT_WORDS: Final = 20


def recognised_claims_section(content: str) -> bool:
    """Structurally recognise a claims section, not merely the word "claims".

    Requires a claims opener AND enough following text to be claim-bearing, so
    a passing mention such as "the patent claims priority" cannot promote a
    document to claim_verified.
    """
    text = content or ""
    for pattern in _CLAIMS_SECTION_PATTERNS:
        match = pattern.search(text)
        if match and len(text[match.end():].split()) >= _MIN_CLAIM_WORDS:
            return True
    return False


def recognised_abstract_section(content: str) -> bool:
    """Structurally recognise an abstract section with substantive text after it."""
    text = content or ""
    for pattern in _ABSTRACT_SECTION_PATTERNS:
        match = pattern.search(text)
        if match and len(text[match.end():].split()) >= _MIN_ABSTRACT_WORDS:
            return True
    return False


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
    if recognised_claims_section(text):
        return "claim_verified"
    if recognised_abstract_section(text):
        return "abstract_verified"
    return "fetched_excerpt"

def extract_claims_section(content: str) -> str:
    """Return the recognised claims text verbatim, or "" if none is recognised.

    Nothing is fabricated or paraphrased: the text returned is the slice of the
    document that follows a structural claims opener. It exists so a
    claim_verified result can be audited against the text it was promoted on.
    """
    text = content or ""
    for pattern in _CLAIMS_SECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            tail = text[match.start():].strip()
            if len(tail.split()) >= _MIN_CLAIM_WORDS:
                return tail
    return ""


def extract_abstract_section(content: str) -> str:
    """Return the recognised abstract text verbatim, or "" if none is recognised."""
    text = content or ""
    for pattern in _ABSTRACT_SECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            tail = text[match.end():].strip()
            if len(tail.split()) >= _MIN_ABSTRACT_WORDS:
                return tail
    return ""
