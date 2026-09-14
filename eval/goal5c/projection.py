"""Exact, reviewed text projections for synthetic blinded evidence packets.

The residual checks recognize generated metadata, not arbitrary semantic leaks.
Human review of every visible field remains mandatory. False positives fail closed:
omit the affected line or withhold the item rather than rewriting its evidence.
"""

from __future__ import annotations

import re
import unicodedata

from .contracts import ContractError


PROJECTION_SEPARATOR = "\n[…]\n"

# Production wrappers in publications_search, web_search and result_contract,
# plus capture/evidence fields that are never part of an assessor packet.
_METADATA = re.compile(
    r"local\s+rerank\s+score|patent\s+rerank\s+threshold|"
    r"publication\s+(?:title|year)\s+returned\s+by|"
    r"result\s+title\s+returned\s+by\s+search|"
    r"(?:semantic\s+scholar|alphaxiv|pubmed|openalex|crossref|arxiv)\s+url\s+for|"
    r"(?:web[_\s]+search[_\s]+provider|publication\s+provider\s+counts)|"
    r"provider\s+dominance\s+warning|corpus-relative\s+rerank|"
    r"(?:merged\s+hits\s+from|completed)\s+(?:web\s+)?providers|"
    r"\b(?:provider|scorer|score(?:[_\s-]+at[_\s-]+decision)?|relevance[_\s-]+score|"
    r"threshold(?:[_\s-]+at[_\s-]+decision)?|retained|rejected|accepted|"
    r"decision[_\s-]+(?:reason|stage)|reason|source(?:[_\s-]+(?:type|stage))?|"
    r"retry|attempt(?:[_\s-]+number)?|rank(?:ing)?|grade|verdict|"
    r"status|analysis|coverage[_\s]+tokens|decomposition|"
    r"evidence[_\s]+(?:status|level|quality)|reliable[_\s]+no[_\s]+results|"
    r"error(?:[_\s]+count)?|note|query|completed|dataset[_\s]+eligible)\b[\s\"'*`]*[:=]",
    re.IGNORECASE,
)


def _validate_text(text: str) -> None:
    if type(text) is not str:
        raise ContractError("invalid projection text")
    # LF and CRLF are supported; other control/format characters can conceal
    # field names or boundaries and must not bypass the visible-field review.
    for index, character in enumerate(text):
        category = unicodedata.category(character)
        if (category in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                and character not in "\n\t\r"):
            raise ContractError("unsupported projection character")
        if character == "\r" and (index + 1 == len(text) or text[index + 1] != "\n"):
            raise ContractError("unsupported projection line ending")


def validate_visible_text(text: str) -> None:
    """Reject known residual system metadata without modifying visible text.

    Apply to titles, topic cards and projected content alike. This conservative
    lexical check does not certify semantic blinding or privacy.
    """
    _validate_text(text)
    if _METADATA.search(unicodedata.normalize("NFKC", text)):
        raise ContractError("visible text contains system metadata")


def _render_gap(gap: str) -> str:
    """Preserve source whitespace; mark only omissions containing other text."""
    return PROJECTION_SEPARATOR if gap.strip(" \t\r\n") else gap


def project_text(text: str, spans: list[list[int]]) -> str:
    """Select ordered, nonoverlapping complete lines by Unicode character index.

    Ranges are half-open. A range starts at zero or immediately after LF and ends
    at EOF, immediately before LF (before CR for CRLF), or immediately after LF.
    Whole-line selection prevents retaining just a metadata value after deleting
    its label. Touching spans and whitespace-only gaps coalesce, preserving
    exact source separators. Only gaps containing other text use the omission
    separator, so splitting equivalent selections cannot fabricate omissions.
    An empty span list explicitly projects no evidence.
    """
    _validate_text(text)
    if type(spans) is not list:
        raise ContractError("invalid projection spans")
    pieces: list[str] = []
    previous_end = -1
    for span in spans:
        if (type(span) is not list or len(span) != 2
                or any(type(value) is not int for value in span)):
            raise ContractError("invalid projection span")
        start, end = span
        if not (0 <= start < end <= len(text)) or start < previous_end:
            raise ContractError("invalid projection span bounds")
        if start != 0 and text[start - 1] != "\n":
            raise ContractError("projection must select complete lines")
        if end != len(text) and not (
            text[end - 1] == "\n"
            or text[end:end + 2] == "\r\n"
            or (text[end] == "\n" and text[end - 1] != "\r")
        ):
            raise ContractError("projection must select complete lines")
        piece = text[start:end]
        validate_visible_text(piece)
        if previous_end >= 0:
            pieces.append(_render_gap(text[previous_end:start]))
        pieces.append(piece)
        previous_end = end
    result = "".join(pieces)
    validate_visible_text(result)
    return result
