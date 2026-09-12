"""Processing of publication evidence for the final research report."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ._hit_sort import sort_hits_by_relevance
from .output_cleaner import USER_AGENT, trim_words
from .pdf_fetch import pdf_fetch_text
from .publication_fetch import publication_fetch
from .publications_search import publications_search
from .query_normalize import clean_tool_query
from .relevance import discriminative_tokens, evidence_score, subject_anchors, tokens
from .requirement_match import atom_coverage, unique_coverage_tokens
from .result_contract import (
    parse_completed_marker,
    parse_error_count,
    parse_reliable_no_results_marker,
    parse_status_marker,
)
from .source_verify import verify_sources

_ATOM_COVERAGE_FOCUSED_THRESHOLD = 0.5
_VERIFIED_PUBLICATION_EVIDENCE_LEVELS = frozenset(
    {"abstract_verified", "verified_metadata", "fetched_excerpt"}
)
_PUBLICATION_FULLTEXT_MAX = 3
_UNPAYWALL_TIMEOUT_S = 8.0
_UNPAYWALL_DEFAULT_EMAIL = "research-server@flowise-mcp.local"

_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", re.IGNORECASE)


def _direct_pdf_url(candidate: "PublicationCandidate") -> str:
    """Determine the direct full-text PDF URL without a network lookup (arXiv, .pdf)."""
    url = candidate.url or ""
    arxiv_match = _ARXIV_ABS_RE.search(url)
    if arxiv_match:
        return f"https://arxiv.org/pdf/{arxiv_match.group(1)}"
    if url.lower().split("?", 1)[0].endswith(".pdf"):
        return url
    return ""


async def _unpaywall_pdf_url(doi: str) -> str:
    """Look up an open-access PDF link for a DOI through the free Unpaywall API.

    No API key is needed, but Unpaywall's policy requires a contact e-mail in the
    request. Placeholder addresses of the form *@example.com are rejected, so a
    private .local domain is used instead. Any error, or a closed-access record,
    returns an empty string.
    """
    clean_doi = (doi or "").strip().strip("/")
    if not clean_doi:
        return ""
    email = os.getenv("UNPAYWALL_EMAIL", "").strip() or _UNPAYWALL_DEFAULT_EMAIL
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=_UNPAYWALL_TIMEOUT_S
        ) as client:
            response = await client.get(
                f"https://api.unpaywall.org/v2/{clean_doi}", params={"email": email}
            )
            if response.status_code >= 400:
                return ""
            data = response.json()
    except Exception:
        return ""
    if not isinstance(data, dict) or not data.get("is_oa"):
        return ""
    best = data.get("best_oa_location") or {}
    if not isinstance(best, dict):
        return ""
    pdf_url = str(best.get("url_for_pdf") or "").strip()
    return pdf_url


async def _resolve_pdf_url(candidate: "PublicationCandidate") -> str:
    """Determine a publication's full-text PDF URL directly or via Unpaywall by DOI."""
    direct = _direct_pdf_url(candidate)
    if direct:
        return direct
    if candidate.doi:
        return await _unpaywall_pdf_url(candidate.doi)
    return ""

TITLE_RE = re.compile(r"(?:Publication|Paper) title[^:]*:\s*\*\*(.*?)\*\*\.?", flags=re.I | re.S)
ABSTRACT_RE = re.compile(r"Abstract excerpt[^:]*:\s*(.*?)(?:\n(?:DOI|Semantic Scholar|ArXiv|Paper landing page|SOURCE|Local rerank)|$)", flags=re.I | re.S)
DOI_RE = re.compile(r"DOI URL[^:]*:\s*(https?://\S+|Not available)\.?", flags=re.I)
S2_RE = re.compile(r"Semantic Scholar URL[^:]*:\s*(https?://\S+|Not available)\.?", flags=re.I)
ARXIV_RE = re.compile(r"(?:ArXiv URL|AlphaXiv URL|Paper landing page on ArXiv)[^:]*:\s*(https?://\S+)\.?", flags=re.I)
VERIFY_RE = re.compile(
    r"Verification status for URL:\s*(https?://\S+)\s+returned\s+(ALIVE|BROKEN)\b",
    flags=re.I,
)
PUBLICATION_QUERY_STOP_TERMS = {
    "does", "concept", "already", "exist", "exists", "using", "with", "that",
    "for", "and", "the", "this", "from", "into", "device", "system", "method",
}

@dataclass
class PublicationCandidate:
    title: str
    url: str
    doi: str
    summary: str


def _clean_url(url: str) -> str:
    """Strip trailing punctuation from a URL."""
    return (url or "").strip().rstrip(".,;")


def _doi_from_url(url: str) -> str:
    """Extract the DOI identifier from a DOI URL."""
    match = re.search(r"https?://doi\.org/(.+)", url or "", flags=re.I)
    return match.group(1).rstrip(".,;") if match else ""


def _parse_candidates(search_output: str) -> list[PublicationCandidate]:
    """Extract the candidate publications from the search tool's text output."""
    candidates: list[PublicationCandidate] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", search_output or ""):
        title_match = TITLE_RE.search(block)
        if not title_match:
            continue
        doi_match = DOI_RE.search(block)
        s2_match = S2_RE.search(block)
        arxiv_match = ARXIV_RE.search(block)
        doi_url = _clean_url(doi_match.group(1)) if doi_match and doi_match.group(1) != "Not available" else ""
        s2_url = _clean_url(s2_match.group(1)) if s2_match and s2_match.group(1) != "Not available" else ""
        arxiv_url = _clean_url(arxiv_match.group(1)) if arxiv_match else ""
        url = doi_url or s2_url or arxiv_url
        if not url or url in seen:
            continue
        seen.add(url)
        abstract_match = ABSTRACT_RE.search(block)
        candidates.append(
            PublicationCandidate(
                title=re.sub(r"\s+", " ", title_match.group(1)).strip(),
                url=url,
                doi=_doi_from_url(doi_url),
                summary=trim_words(abstract_match.group(1).strip() if abstract_match else block, 90),
            )
        )
    return candidates


def _verification_map(verification: str) -> dict[str, bool]:
    """Convert the URL verification result into an availability map."""
    out: dict[str, bool] = {}
    for url, status in VERIFY_RE.findall(verification or ""):
        out[url.rstrip(".,;")] = status.upper() == "ALIVE"
    return out


def _field(text: str, label: str) -> str:
    """Read the value of a specific field from a fetch tool's text output."""
    match = re.search(rf"^{re.escape(label)}:\s*(.*?)$", text or "", flags=re.I | re.M)
    return match.group(1).strip() if match else ""


def _fetch_level(fetch_output: str) -> str:
    """Map the EVIDENCE_LEVEL value from publication_fetch onto the shared evidence level."""
    raw = _field(fetch_output, "EVIDENCE_LEVEL").upper()
    if raw == "ABSTRACT_VERIFIED":
        return "abstract_verified"
    if raw == "FETCH_FAILED" or "TOOL_ERROR:" in (fetch_output or ""):
        return "fetch_failed"
    return "search_snippet_only"


def _fetch_summary(fetch_output: str) -> str:
    """Build a short summary from a publication_fetch result."""
    title = _field(fetch_output, "TITLE")
    abstract = _field(fetch_output, "ABSTRACT")
    parts = []
    if title and title != "Unknown was extracted from the publication page.":
        parts.append(f"Fetched title: {title}")
    if abstract:
        parts.append(f"Fetched abstract: {abstract}")
    return trim_words(" ".join(parts), 90)


def _errors_from_markers(search_output: str) -> list[dict[str, str]]:
    """Read the errors out of the publication search status markers."""
    errors = []
    for error_type, message in re.findall(r"^ERROR:\s*([^-]+?)\s*-\s*(.*?)$", search_output or "", flags=re.I | re.M):
        errors.append({"type": error_type.strip(), "message": message.strip()})
    return errors


def _publication_relevance(query: str, title: str, summary: str) -> str:
    """Determine a publication's baseline relevance to the query."""
    query_terms = discriminative_tokens(query)
    if not query_terms:
        return "adjacent"
    text_terms = tokens(f"{title} {summary}")
    shared = query_terms & text_terms
    anchor_terms = subject_anchors(query_terms)
    has_anchor = not anchor_terms or bool(anchor_terms & text_terms)
    coverage = len(shared) / max(len(query_terms), 1)
    if has_anchor and coverage >= 0.55 and len(shared) >= min(3, len(query_terms)):
        return "focused"
    if anchor_terms and not has_anchor and coverage < 0.55:
        return "generic"
    if shared:
        return "adjacent"
    return "generic"


def _publication_search_query(query: str, english_query: str) -> str:
    """Build a shorter publication search query."""
    base = (english_query or "").strip() or query
    raw_terms = re.findall(r"[a-z0-9][a-z0-9-]{2,}", base.lower())
    compact: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        normalized = term.strip("-")
        if not normalized or normalized in PUBLICATION_QUERY_STOP_TERMS:
            continue
        if normalized.endswith("s") and len(normalized) > 4:
            normalized = normalized[:-1]
        if normalized not in seen:
            seen.add(normalized)
            compact.append(normalized)
    return " ".join(compact[:12]) or base


async def publication_evidence_pack(
    query: str,
    max_results: int = 10,
    max_fetches: int = 4,
    fetch_timeout_s: float = 12.0,
    english_query: str = "",
    atomic_requirements: list[dict[str, Any]] | None = None,
    *,
    _collector: Any = None,
) -> str:
    """Search for and process publication evidence for the given query."""
    # Passed only when capture is active, so with capture off the call is
    # byte-identical to before and existing stubs keep working unchanged.
    _capture_kwargs = {"_collector": _collector} if _collector is not None else {}
    warnings: list[str] = []
    verification_failed = False
    query = clean_tool_query(query)
    english_query = (english_query or "").strip()
    relevance_query = _publication_search_query(query, english_query)
    try:
        search_output = await publications_search(
            query=query, max_results=max_results, english_query=relevance_query if english_query else ""
        , **_capture_kwargs)
    except Exception as exc:
        return json.dumps(
            {
                "source_type": "publication",
                "status": "failed",
                "completed": False,
                "reliable_no_results": False,
                "hits": [],
                "errors": [{"type": "publications_search_failed", "message": str(exc)}],
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )

    search_status = parse_status_marker(search_output) or "failed"
    search_completed = bool(parse_completed_marker(search_output))
    reliable_no_results = bool(parse_reliable_no_results_marker(search_output))
    errors = _errors_from_markers(search_output)
    if parse_error_count(search_output):
        warnings.append("Publication search reported provider errors; do not treat missing papers as negative evidence.")
    if search_status == "failed":
        return json.dumps(
            {
                "source_type": "publication",
                "status": "failed",
                "completed": False,
                "reliable_no_results": False,
                "hits": [],
                "errors": errors or [{"type": "publications_search_failed", "message": trim_words(search_output, 80)}],
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )

    candidates = _parse_candidates(search_output)
    fetch_limit = max(0, min(max_fetches, 6, len(candidates)))
    fetch_outputs: list[str] = []
    if fetch_limit:
        fetched = await asyncio.gather(
            *[publication_fetch(url=candidate.url, timeout_s=fetch_timeout_s) for candidate in candidates[:fetch_limit]],
            return_exceptions=True,
        )
        for item in fetched:
            if isinstance(item, Exception):
                text = f"TOOL_ERROR: publication_fetch\nREASON: {item}\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"
                warnings.append(trim_words(text, 50))
                fetch_outputs.append(text)
            else:
                fetch_outputs.append(str(item))

    # Full texts of freely available PDFs (arXiv, direct .pdf links, or open-access
    # PDFs found through Unpaywall by DOI), so requirement coverage is measured
    # against the whole article rather than the abstract alone.
    fulltext_by_index: dict[int, str] = {}
    if atomic_requirements:
        resolved_urls = await asyncio.gather(
            *[_resolve_pdf_url(candidate) for candidate in candidates[:fetch_limit]],
            return_exceptions=True,
        )
        pdf_targets = [
            (index, url)
            for index, url in enumerate(resolved_urls)
            if isinstance(url, str) and url
        ][:_PUBLICATION_FULLTEXT_MAX]
        if pdf_targets:
            pdf_texts = await asyncio.gather(
                *[
                    pdf_fetch_text(url, timeout_s=max(10.0, fetch_timeout_s * 2))
                    for _index, url in pdf_targets
                ],
                return_exceptions=True,
            )
            for (index, _url), text in zip(pdf_targets, pdf_texts):
                if isinstance(text, Exception) or not text:
                    continue
                fulltext_by_index[index] = str(text)

    verification = ""
    if candidates:
        try:
            verification = await verify_sources([candidate.url for candidate in candidates], max_urls=len(candidates))
            if re.search(r"^STATUS:\s*FAILED\s*$", verification, flags=re.I | re.M):
                verification_failed = True
                warnings.append("URL verification failed; verified_url values remain false unless ALIVE was returned.")
        except Exception as exc:
            verification_failed = True
            warnings.append(f"URL verification failed: {exc}")
    verified = _verification_map(verification)

    hits: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates):
        fetch_output = fetch_outputs[index] if index < len(fetch_outputs) else ""
        evidence_level = _fetch_level(fetch_output) if fetch_output else "search_snippet_only"
        fetched_summary = _fetch_summary(fetch_output) if fetch_output else ""
        fulltext = fulltext_by_index.get(index, "")
        if fetch_output and evidence_level == "fetch_failed":
            warnings.append(f"Publication fetch did not verify {candidate.url}: {trim_words(fetch_output, 40)}")
            if candidate.summary and len(candidate.summary.split()) >= 12:
                evidence_level = "verified_metadata"
        if fulltext and evidence_level in {"search_snippet_only", "fetch_failed"}:
            evidence_level = "fetched_excerpt"
        summary_parts = [f"Search evidence: {candidate.summary}"]
        if fetched_summary:
            summary_parts.append(fetched_summary)
        summary = trim_words(" ".join(summary_parts), 120)
        relevance = _publication_relevance(relevance_query, candidate.title, summary)
        hit_record: dict[str, object] = {
            "title": candidate.title,
            "url": candidate.url,
            "doi": candidate.doi,
            "evidence_level": evidence_level,
            "summary": summary,
            "verified_url": bool(verified.get(candidate.url, False)),
            "relevance": relevance,
            "relevance_score": evidence_score(
                relevance_query,
                f"{candidate.title} {summary}",
                "PUBLICATION",
                evidence_level,
            ),
        }
        if fulltext:
            hit_record["fulltext_analyzed"] = True
            hit_record["fulltext_word_count"] = len(fulltext.split())
        if atomic_requirements:
            fulltext_tokens = unique_coverage_tokens(fulltext) if fulltext else ""
            coverage_blob = " ".join(
                part
                for part in (candidate.title, candidate.summary, fetched_summary, fulltext_tokens)
                if part
            )
            coverage, matched = atom_coverage(coverage_blob, atomic_requirements)
            hit_record["atom_coverage"] = coverage
            hit_record["atom_match_count"] = matched
            evidence_verified = evidence_level in _VERIFIED_PUBLICATION_EVIDENCE_LEVELS
            if evidence_verified and coverage >= _ATOM_COVERAGE_FOCUSED_THRESHOLD:
                hit_record["relevance"] = "focused"
            hit_record["exact_combination_candidate_found"] = bool(
                evidence_verified and coverage >= 1.0
            )
        hits.append(hit_record)

    focused_hits = [hit for hit in hits if hit.get("relevance") == "focused"]
    hits = sort_hits_by_relevance(hits)
    if hits and not focused_hits:
        warnings.append(
            "Publication hits were weak or adjacent to the query; do not treat them as strong publication prior-art evidence."
        )

    status = (
        "failed"
        if search_status == "failed"
        else "partial_failure"
        if search_status == "partial_failure" or warnings or verification_failed or (hits and not focused_hits)
        else "ok"
    )
    payload = {
        "source_type": "publication",
        "status": status,
        "completed": bool(search_completed and status == "ok"),
        "reliable_no_results": reliable_no_results,
        "hits": hits,
        "errors": errors,
        "warnings": warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
