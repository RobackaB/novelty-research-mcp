"""Spracovanie publikačných dôkazov pre finálny prieskum."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from ._hit_sort import sort_hits_by_relevance
from .output_cleaner import trim_words
from .publication_fetch import publication_fetch
from .publications_search import publications_search
from .query_normalize import clean_tool_query
from .relevance import discriminative_tokens, evidence_score, subject_anchors, tokens
from .result_contract import (
    parse_completed_marker,
    parse_error_count,
    parse_reliable_no_results_marker,
    parse_status_marker,
)
from .source_verify import verify_sources

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
    """Očistí URL adresu od koncovej interpunkcie."""
    return (url or "").strip().rstrip(".,;")


def _doi_from_url(url: str) -> str:
    """Vytiahne DOI identifikátor z DOI URL adresy."""
    match = re.search(r"https?://doi\.org/(.+)", url or "", flags=re.I)
    return match.group(1).rstrip(".,;") if match else ""


def _parse_candidates(search_output: str) -> list[PublicationCandidate]:
    """Vytiahne kandidátske publikácie z textového výstupu vyhľadávania."""
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
    """Prevedie výsledok overenia URL adries na slovník dostupnosti."""
    out: dict[str, bool] = {}
    for url, status in VERIFY_RE.findall(verification or ""):
        out[url.rstrip(".,;")] = status.upper() == "ALIVE"
    return out


def _field(text: str, label: str) -> str:
    """Získa hodnotu konkrétneho poľa z textového výstupu fetch nástroja."""
    match = re.search(rf"^{re.escape(label)}:\s*(.*?)$", text or "", flags=re.I | re.M)
    return match.group(1).strip() if match else ""


def _fetch_level(fetch_output: str) -> str:
    """Prevedie hodnotu EVIDENCE_LEVEL z publication_fetch na jednotnú úroveň dôkazu."""
    raw = _field(fetch_output, "EVIDENCE_LEVEL").upper()
    if raw == "ABSTRACT_VERIFIED":
        return "abstract_verified"
    if raw == "FETCH_FAILED" or "TOOL_ERROR:" in (fetch_output or ""):
        return "fetch_failed"
    return "search_snippet_only"


def _fetch_summary(fetch_output: str) -> str:
    """Vytvorí krátke zhrnutie z výsledku publication_fetch."""
    title = _field(fetch_output, "TITLE")
    abstract = _field(fetch_output, "ABSTRACT")
    parts = []
    if title and title != "Unknown was extracted from the publication page.":
        parts.append(f"Fetched title: {title}")
    if abstract:
        parts.append(f"Fetched abstract: {abstract}")
    return trim_words(" ".join(parts), 90)


def _errors_from_markers(search_output: str) -> list[dict[str, str]]:
    """Získa chyby zo stavových markerov vyhľadávania publikácií."""
    errors = []
    for error_type, message in re.findall(r"^ERROR:\s*([^-]+?)\s*-\s*(.*?)$", search_output or "", flags=re.I | re.M):
        errors.append({"type": error_type.strip(), "message": message.strip()})
    return errors


def _publication_relevance(query: str, title: str, summary: str) -> str:
    """Určí základnú relevanciu publikácie voči dotazu."""
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
    """Pripraví kratší publikačný vyhľadávací dotaz."""
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
) -> str:
    """Vyhľadá a spracuje publikačné dôkazy pre zadaný dotaz."""
    warnings: list[str] = []
    verification_failed = False
    query = clean_tool_query(query)
    english_query = (english_query or "").strip()
    relevance_query = _publication_search_query(query, english_query)
    try:
        search_output = await publications_search(
            query=query, max_results=max_results, english_query=relevance_query if english_query else ""
        )
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
        if fetch_output and evidence_level == "fetch_failed":
            warnings.append(f"Publication fetch did not verify {candidate.url}: {trim_words(fetch_output, 40)}")
            if candidate.summary and len(candidate.summary.split()) >= 12:
                evidence_level = "verified_metadata"
        summary_parts = [f"Search evidence: {candidate.summary}"]
        if fetched_summary:
            summary_parts.append(fetched_summary)
        summary = trim_words(" ".join(summary_parts), 120)
        relevance = _publication_relevance(relevance_query, candidate.title, summary)
        hits.append(
            {
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
        )

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
