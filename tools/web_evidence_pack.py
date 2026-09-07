"""Processing of web evidence for the final research report."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from typing import Any

from ._hit_sort import sort_hits_by_relevance
from .output_cleaner import trim_words
from .patent_filters import extract_patent_number
from .pdf_fetch import pdf_fetch_text
from .query_normalize import clean_tool_query
from .relevance import evidence_score, subject_anchors
from .requirement_match import atom_coverage, unique_coverage_tokens
from .result_contract import parse_error_count, parse_reliable_no_results_marker, parse_status_marker
from .source_verify import verify_sources
from .web_search import _compact_content, web_fetch, web_search

_ATOM_COVERAGE_DIRECT_THRESHOLD = 0.5

_PATENT_LIKE_DOMAINS = (
    "freepatentsonline.com",
    "patentsencyclopedia.com",
    "patsnap.com",
    "patents.com",
    "patents.justia.com",
    "lens.org",
    "espacenet.com",
    "patentscope.wipo.int",
    "patents.google.com",
)

def _is_patent_like_url(url: str) -> bool:
    """Determine whether a URL looks like a patent source."""
    lower = (url or "").lower()
    return any(domain in lower for domain in _PATENT_LIKE_DOMAINS)

def _normalize_patent_like_url(url: str, title: str = "", snippet: str = "") -> tuple[str, str]:
    """Convert a patent web link into a canonical Google Patents URL where possible."""
    number = extract_patent_number(url, title, snippet)
    if number and number != "Unknown":
        return f"https://patents.google.com/patent/{number}/en", number
    return url, ""

URL_RE = re.compile(r"Source page URL for this result:\s*(https?://\S+)")
TITLE_RE = re.compile(r"Result title returned by search:\s*\*\*(.*?)\*\*\.?", flags=re.S)
SCORE_RE = re.compile(r"Local rerank score:\s*([0-9.]+)\s*/\s*10", flags=re.I)
SNIPPET_RE = re.compile(r"Short summary snippet from search:\s*(.*)", flags=re.S)
UNSUPPORTED_EVIDENCE_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
)
QUERY_STOPWORDS = {
    "about",
    "already",
    "because",
    "does",
    "from",
    "have",
    "into",
    "that",
    "this",
    "using",
    "with",
}

@dataclass
class WebCandidate:
    title: str
    url: str
    score: float
    snippet: str
    patent_number: str = ""  
    no_fetch: bool = False  

def _parse_candidates(search_output: str) -> tuple[list[WebCandidate], list[str]]:
    """Extract the web candidates from the search tool's text output."""
    candidates: list[WebCandidate] = []
    skipped_unsupported: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", search_output or ""):
        url_match = URL_RE.search(block)
        if not url_match:
            continue
        url = url_match.group(1).rstrip(".,;")
        if url in seen:
            continue
        seen.add(url)
        no_fetch = _is_unsupported_evidence_url(url)
        title_match = TITLE_RE.search(block)
        score_match = SCORE_RE.search(block)
        snippet_match = SNIPPET_RE.search(block)
        title_str = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "Untitled result"
        snippet_str = trim_words(snippet_match.group(1).strip() if snippet_match else "", 60)
        canonical_url = url
        patent_number = ""
        if _is_patent_like_url(url):
            canonical_url, patent_number = _normalize_patent_like_url(url, title_str, snippet_str)
        has_real_title = bool(title_str) and title_str != "Untitled result"
        if no_fetch and not (snippet_str or has_real_title):
            skipped_unsupported.append(url)
            continue
        candidates.append(
            WebCandidate(
                title=title_str,
                url=canonical_url,
                score=float(score_match.group(1)) if score_match else 0.0,
                snippet=snippet_str,
                patent_number=patent_number,
                no_fetch=no_fetch,
            )
        )
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates, skipped_unsupported

_PDF_SKIPPED_OUTPUT = "SKIPPED: non-text evidence format is kept as snippet-backed evidence only."


async def _pdf_fetch_output(url: str, timeout_ms: int, query: str) -> str:
    """Download a PDF document and return output in the same format as web_fetch.

    If the text cannot be extracted, the document remains snippet-backed
    evidence, as in the original behaviour.
    """
    timeout_s = max(8.0, min((timeout_ms or 14000) / 1000 * 2, 30.0))
    text = await pdf_fetch_text(url, timeout_s=timeout_s)
    if not text:
        return _PDF_SKIPPED_OUTPUT
    compact = _compact_content(text, query=query)
    if not compact:
        return _PDF_SKIPPED_OUTPUT
    analysis = _compact_content(text, query=query, max_words=900, max_sentences=24)
    lines = [
        f"SOURCE_URL: {url} was the PDF document requested.",
        "DATE: Unknown was the best date found.",
        f"CONTENT: {compact}",
        "STATUS: PDF_TEXT",
        "EVIDENCE_LEVEL: FULLTEXT_VERIFIED",
    ]
    if analysis and analysis != compact:
        lines.append(f"ANALYSIS: {analysis}")
    token_line = unique_coverage_tokens(text)
    if token_line:
        lines.append(f"COVERAGE_TOKENS: {token_line}")
    return "\n".join(lines)

def _fetch_succeeded(fetch_output: str) -> bool:
    """Determine whether fetching the page produced verified full-text content."""
    return "EVIDENCE_LEVEL: FULLTEXT_VERIFIED" in (fetch_output or "") and "TOOL_ERROR:" not in (fetch_output or "")

def _is_unsupported_evidence_url(url: str) -> bool:
    """Determine whether a URL points to a file type this module does not handle."""
    lower = (url or "").lower().split("?", 1)[0]
    return lower.endswith(UNSUPPORTED_EVIDENCE_EXTENSIONS)

def _query_terms(query: str) -> set[str]:
    """Extract the meaningful words of a query for local filtering of web hits."""
    terms = set()
    for token in re.findall(r"[a-z0-9][a-z0-9-]{3,}", (query or "").lower()):
        if token not in QUERY_STOPWORDS:
            terms.add(token)
    return terms

def _query_coverage(query: str, text: str) -> int:
    """Count how many of the query's meaningful words occur in a text."""
    lower = (text or "").lower()
    return sum(1 for term in _query_terms(query) if term in lower)

def _has_product_anchor(query: str, text: str) -> bool:
    """Check whether a text contains at least one head term from the query."""
    anchor_terms = subject_anchors(_query_terms(query))
    if not anchor_terms:
        return True
    lower = (text or "").lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lower) for term in anchor_terms)

_CONCRETE_ARTIFACT_PATTERNS = (
    r"\b(?:US|EP|WO|CN|JP|KR|DE|FR|GB|RU)\s?\d{4,}\s?[A-Z]?\d?\b",  
    r"\b10\.\d{4,}\/[\w.\-/()]+",                                    
    r"\bdoi\s*[:\-]\s*10\.",
    r"\b(?:ISO|IEC|ANSI|IEEE|ASTM|NIST|EN|DIN|JIS)\s?\d+",          
    r"\b(?:RFC|FIPS|PEP)\s?\d+",
    r"\b(?:patent|application|publication)\s+(?:no|number)\b",
    r"\b(?:datasheet|spec\s?sheet|manual|firmware|service\s+manual)\b",
    r"\bversion\s+v?\d+(?:\.\d+)*\b",
    r"\bmodel\s+(?:no\.?|number)?\s*[A-Z0-9-]{3,}\b",
    r"\b(?:released|published|filed|issued|granted)\s+(?:on\s+)?\d{4}\b",
    r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b",                              
)
_CONCRETE_ARTIFACT_RE = re.compile("|".join(_CONCRETE_ARTIFACT_PATTERNS), re.IGNORECASE)

def _has_concrete_artifact(text: str) -> bool:
    """Determine whether a text contains a concrete identifier or technical item."""
    return bool(_CONCRETE_ARTIFACT_RE.search(text or ""))

def _required_direct_coverage(query: str) -> int:
    """Determine the minimum number of covered query words for direct web evidence."""
    terms = _query_terms(query)
    return max(3, min(6, (len(terms) + 1) // 2))

def _relevance_label(query: str, evidence_text: str, score: float, fetch_ok: bool) -> str:
    """Assign a relevance label to web evidence from its concreteness and requirement coverage."""
    terms = _query_terms(query)
    has_artifact = _has_concrete_artifact(evidence_text or "")
    if not terms:
        if fetch_ok and score >= 7 and has_artifact:
            return "direct"
        return "adjacent"
    required = _required_direct_coverage(query)
    coverage = _query_coverage(query, evidence_text)
    if fetch_ok and score >= 7 and coverage >= required and has_artifact:
        return "direct"
    if (not fetch_ok) and score >= 7 and coverage >= required + 2 and has_artifact:
        return "direct"
    return "adjacent"

def _registrable_domain(url: str) -> str:
    """Return a simplified URL domain for grouping evidence from the same source."""
    match = re.match(r"https?://([^/]+)", (url or "").lower())
    host = match.group(1) if match else ""
    return host[4:] if host.startswith("www.") else host

def _apply_collective_directness(hits: list[dict], query: str) -> None:
    """Mark a domain's best hit as direct evidence when the requirements are confirmed across its pages."""
    if not hits or any(hit.get("relevance") == "direct" for hit in hits):
        return
    groups: dict[str, list[dict]] = {}
    for hit in hits:
        domain = _registrable_domain(str(hit.get("url") or ""))
        if domain:
            groups.setdefault(domain, []).append(hit)
    required = _required_direct_coverage(query)
    for domain_hits in groups.values():
        if len(domain_hits) < 2:
            continue
        if not any(hit.get("evidence_level") == "fetched_excerpt" for hit in domain_hits):
            continue
        combined = " ".join(
            f"{hit.get('title') or ''} {hit.get('summary') or ''}" for hit in domain_hits
        )
        if _query_coverage(query, combined) < required:
            continue
        if not _has_concrete_artifact(combined):
            continue
        best = max(
            domain_hits,
            key=lambda hit: (hit.get("evidence_level") == "fetched_excerpt", float(hit.get("relevance_score") or 0.0)),
        )
        best["relevance"] = "direct"
        return

def _minimum_hit_coverage(query: str) -> int:
    """Determine the minimum number of query words needed to keep a web hit."""
    terms = _query_terms(query)
    if len(terms) >= 7:
        return 3
    if len(terms) >= 4:
        return 2
    return 1 if terms else 0

def _should_keep_hit(query: str, evidence_text: str, title: str = "", url: str = "") -> bool:
    """Decide whether a web hit contains enough concrete query elements."""
    terms = _query_terms(query)
    if not terms:
        return bool(evidence_text.strip())
    combined = " ".join(filter(None, [evidence_text, title, url]))
    if not _has_product_anchor(query, combined):
        return False
    return _query_coverage(query, combined) >= _minimum_hit_coverage(query)

def _extract_fetch_excerpt(fetch_output: str) -> str:
    """Extract a short excerpt from the web fetch tool's output."""
    match = re.search(r"CONTENT:\s*(.*?)\nSTATUS:", fetch_output or "", flags=re.S)
    return trim_words(match.group(1), 80) if match else ""


_ANALYSIS_RE = re.compile(r"^ANALYSIS:\s*(.*)$", flags=re.M)
_COVERAGE_TOKENS_RE = re.compile(r"^COVERAGE_TOKENS:\s*(.*)$", flags=re.M)


def _extract_analysis_text(fetch_output: str) -> str:
    """Extract the extended analysis text from the fetch tool's output."""
    match = _ANALYSIS_RE.search(fetch_output or "")
    return match.group(1).strip() if match else ""


def _extract_coverage_tokens(fetch_output: str) -> str:
    """Extract the whole-page tokens used to compute requirement coverage."""
    match = _COVERAGE_TOKENS_RE.search(fetch_output or "")
    return match.group(1).strip() if match else ""


def _verification_map(verification: str) -> dict[str, bool]:
    """Convert the URL verification result into an availability map."""
    out: dict[str, bool] = {}
    pattern = re.compile(r"Verification status for URL:\s*(https?://\S+)\s+returned\s+(ALIVE|BROKEN)\b", flags=re.I)
    for url, status in pattern.findall(verification or ""):
        out[url.rstrip(".,;")] = status.upper() == "ALIVE"
    return out


def _errors_from_markers(search_output: str) -> list[dict[str, str]]:
    """Read the structured errors out of the web search status markers."""
    errors = []
    for error_type, message in re.findall(r"^ERROR:\s*([^-]+?)\s*-\s*(.*?)$", search_output or "", flags=re.I | re.M):
        errors.append({"type": error_type.strip(), "message": trim_words(message.strip(), 40)})
    return errors

def _status(search_output: str, hits: list[dict], warnings: list[str], errors: list[str]) -> str:
    """Determine the final status of the web evidence pack."""
    search_status = parse_status_marker(search_output)
    if search_status == "failed":
        return "failed"
    if errors or warnings or search_status == "partial_failure":
        return "partial_failure"
    return "ok" if hits else "failed"

async def web_evidence_pack(
    query: str,
    max_results: int = 10,
    max_fetches: int = 5,
    timeout_ms: int = 14000,
    english_query: str = "",
    atomic_requirements: list[dict[str, Any]] | None = None,
) -> str:
    """Search for and process web evidence for the given query."""
    warnings: list[str] = []
    errors: list[str] = []
    hits: list[dict] = []
    try:
        query = clean_tool_query(query)
        relevance_query = (english_query or "").strip() or query
        search_query = relevance_query
        search_output = await web_search(query=search_query, max_results=max_results)
        if parse_status_marker(search_output) == "failed":
            return json.dumps(
                {
                    "source_type": "web",
                    "status": "failed",
                    "completed": False,
                    "reliable_no_results": False,
                    "hits": [],
                    "warnings": [],
                    "errors": [search_output],
                },
                ensure_ascii=False,
                indent=2,
            )

        candidates, skipped_unsupported = _parse_candidates(search_output)
        for skipped_url in skipped_unsupported:
            warnings.append(
                f"Skipped unsupported non-text evidence URL {skipped_url}: extension is not text/HTML."
            )
        selected = candidates[: max(0, min(max_fetches, 8))]
        fetch_outputs = await asyncio.gather(
            *[
                _pdf_fetch_output(item.url, timeout_ms, search_query) if item.no_fetch
                else web_fetch(url=item.url, timeout_ms=timeout_ms, query=search_query)
                for item in selected
            ],
            return_exceptions=True,
        )

        for candidate, fetched in zip(selected, fetch_outputs):
            fetched_text = str(fetched) if not isinstance(fetched, Exception) else f"TOOL_ERROR: web_fetch\nREASON: {fetched}"
            fetch_ok = _fetch_succeeded(fetched_text)
            if candidate.no_fetch and not fetch_ok:
                warnings.append(
                    f"Non-text evidence URL {candidate.url} kept as snippet-backed evidence only; document text was not extracted."
                )
            elif not fetch_ok:
                warnings.append(f"Fetch failed for {candidate.url}: {trim_words(fetched_text, 40)}")
            excerpt = _extract_fetch_excerpt(fetched_text) if fetch_ok else ""
            has_excerpt = bool(excerpt.strip())
            summary_parts = []
            if candidate.snippet:
                summary_parts.append(f"Search snippet: {candidate.snippet}")
            if excerpt:
                summary_parts.append(f"Fetched excerpt: {excerpt}")
            if not summary_parts:
                if candidate.patent_number or candidate.title:
                    summary_parts.append(
                        f"Search snippet: {candidate.title or candidate.url}"
                    )
                else:
                    continue
            summary = trim_words(" ".join(summary_parts), 100)
            if not candidate.patent_number and not _should_keep_hit(
                relevance_query, summary, candidate.title, candidate.url
            ):
                warnings.append(
                    f"Discarded weak generic web hit {candidate.url}: it did not mention enough requested constraints."
                )
                continue
            excerpt_backed = fetch_ok and has_excerpt
            evidence_level = "fetched_excerpt" if excerpt_backed else "search_snippet_only"
            hit_record: dict[str, object] = {
                "title": candidate.title,
                "url": candidate.url,
                "evidence_level": evidence_level,
                "summary": summary,
                "verified_url": False,
                "relevance": _relevance_label(relevance_query, summary, candidate.score, excerpt_backed),
                "relevance_score": evidence_score(
                    relevance_query,
                    f"{candidate.title} {summary}",
                    "WEB",
                    evidence_level,
                ),
            }
            if candidate.patent_number:
                hit_record["patent_number"] = candidate.patent_number
                hit_record["is_patent_like"] = True
            if atomic_requirements:
                analysis_text = _extract_analysis_text(fetched_text) if fetch_ok else ""
                coverage_tokens = _extract_coverage_tokens(fetched_text) if fetch_ok else ""
                coverage_blob = " ".join(
                    part
                    for part in (candidate.title, candidate.snippet, excerpt, analysis_text, coverage_tokens)
                    if part
                )
                coverage, matched = atom_coverage(coverage_blob, atomic_requirements)
                hit_record["atom_coverage"] = coverage
                hit_record["atom_match_count"] = matched
                if excerpt_backed and coverage >= _ATOM_COVERAGE_DIRECT_THRESHOLD:
                    hit_record["relevance"] = "direct"
                hit_record["exact_combination_candidate_found"] = bool(
                    excerpt_backed and coverage >= 1.0
                )
            hits.append(hit_record)

        verification = await verify_sources([hit["url"] for hit in hits], max_urls=len(hits)) if hits else ""
        verified = _verification_map(verification)
        for hit in hits:
            hit["verified_url"] = bool(verified.get(hit["url"], False))
        hits = sort_hits_by_relevance(hits)
        _apply_collective_directness(hits, relevance_query)

        reliable_no_results = parse_reliable_no_results_marker(search_output)
        payload = {
            "source_type": "web",
            "status": _status(search_output, hits, warnings, errors),
            "completed": parse_status_marker(search_output) != "failed",
            "reliable_no_results": bool(reliable_no_results) if reliable_no_results is not None else False,
            "hits": hits,
            "warnings": warnings,
            "errors": errors or _errors_from_markers(search_output),
        }
        if parse_error_count(search_output) and not payload["errors"]:
            payload["warnings"].append("Search provider reported partial errors; see server logs or raw web_search output.")
        elif payload["errors"]:
            payload["warnings"].append(
                "Search provider errors: "
                + "; ".join(f"{err['type']}: {err['message']}" for err in payload["errors"][:3])
            )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {
                "source_type": "web",
                "status": "failed",
                "completed": False,
                "reliable_no_results": False,
                "hits": [],
                "warnings": warnings,
                "errors": [str(exc)],
            },
            ensure_ascii=False,
            indent=2,
        )