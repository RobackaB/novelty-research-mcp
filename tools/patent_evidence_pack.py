"""Spracovanie patentových dôkazov pre finálny prieskum."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ._hit_sort import sort_hits_by_relevance
from .output_cleaner import trim_words
from .patent_fetch import patent_fetch
from .patent_search import patent_search
from .query_normalize import clean_tool_query
from .relevance import discriminative_tokens, evidence_score, subject_anchors, tokens
from .requirement_match import atom_coverage
from .source_verify import verify_sources


_CLAIM_COVERAGE_FOCUSED_THRESHOLD = 0.5
_VERIFIED_PATENT_EVIDENCE_LEVELS = frozenset({"claim_verified", "abstract_verified", "verified_metadata"})
_EXACT_CANDIDATE_EVIDENCE_LEVELS = frozenset({"claim_verified", "abstract_verified"})


def _compute_claim_coverage(
    claim_text: str, atomic_requirements: list[dict[str, Any]] | None
) -> tuple[float, int]:
    """Vypočíta, koľko častí dotazu je pokrytých textom patentových nárokov."""
    return atom_coverage(claim_text or "", atomic_requirements)


_COMBINATION_PATTERNS = (
    re.compile(r"\bcombin(?:ing|e|ed)\b.*\binto (?:one|a single|the same)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:integrat(?:ing|e|ed)|merg(?:ing|e|ed))\b.*\b(?:device|unit|product)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\ball-in-one\b", re.IGNORECASE),
    re.compile(r"\bsingle (?:handheld|portable|wearable) (?:device|unit)\b", re.IGNORECASE),
)


def _query_is_combination_pattern(query: str) -> bool:
    """Zistí, či dotaz opisuje kombináciu viacerých prvkov v jednom riešení."""
    text = query or ""
    return any(pattern.search(text) for pattern in _COMBINATION_PATTERNS)


def _patent_relevance(
    query: str,
    title: str,
    summary: str,
    claim_coverage: float = 0.0,
    evidence_level: str = "",
) -> str:
    """Určí orientačnú relevanciu patentového nálezu voči dotazu."""
    query_terms = discriminative_tokens(query)
    if not query_terms:
        if claim_coverage >= _CLAIM_COVERAGE_FOCUSED_THRESHOLD:
            return "focused"
        return "adjacent"
    text_terms = tokens(f"{title} {summary}")
    shared = query_terms & text_terms
    anchor_terms = subject_anchors(query_terms)
    has_anchor = not anchor_terms or bool(anchor_terms & text_terms)
    coverage = len(shared) / max(len(query_terms), 1)
    if has_anchor and coverage >= 0.55 and len(shared) >= min(3, len(query_terms)):
        return "focused"
    if (
        str(evidence_level or "").lower() in _VERIFIED_PATENT_EVIDENCE_LEVELS
        and has_anchor
        and len(shared) >= min(4, len(query_terms))
        and coverage >= 0.30
    ):
        return "focused"
    if claim_coverage >= _CLAIM_COVERAGE_FOCUSED_THRESHOLD:
        return "focused"
    if _query_is_combination_pattern(query) and coverage >= 0.5:
        return "focused"
    if anchor_terms and not has_anchor and coverage < 0.55:
        return "generic"
    if shared:
        return "adjacent"
    return "generic"

VERIFY_RE = re.compile(
    r"Verification status for URL:\s*(https?://\S+)\s+returned\s+(ALIVE|BROKEN)\b",
    flags=re.I,
)

_URL_PATENT_ID_RE = re.compile(
    r"/patent/([A-Z]{2}\d+[A-Z]?\d?)(?:/|\b)",
    flags=re.IGNORECASE,
)
_TITLE_LEADING_ID_RE = re.compile(
    r"^\s*([A-Z]{2}\d{4,}[A-Z]?\d?)\s*[-–—:]\s*",
    flags=re.IGNORECASE,
)
_GOOGLE_PATENTS_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*Google\s*Patents?\s*$",
    flags=re.IGNORECASE,
)


def _patent_id_from_url(url: str) -> str:
    """Vytiahne patentové číslo z URL adresy, ak sa v nej nachádza."""
    match = _URL_PATENT_ID_RE.search(url or "")
    return match.group(1).upper() if match else ""


def _normalize_patent_identity(
    raw_title: str, raw_patent_number: str, url: str
) -> tuple[str, str]:
    """Zjednotí názov patentu a patentové číslo podľa URL adresy."""
    title = (raw_title or "").strip()
    patent_number = (raw_patent_number or "").strip().upper()
    url_id = _patent_id_from_url(url)
    title = _GOOGLE_PATENTS_SUFFIX_RE.sub("", title).strip()
    leading = _TITLE_LEADING_ID_RE.match(title)
    leading_id = leading.group(1).upper() if leading else ""
    canonical = url_id or patent_number or leading_id
    if leading_id and canonical and leading_id != canonical:
        title = title[leading.end():].strip()
    if not patent_number or (canonical and patent_number != canonical):
        patent_number = canonical or patent_number or "Unknown"
    if not title:
        title = patent_number or "Untitled patent"
    return title, patent_number


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
    """Prevedie hodnotu EVIDENCE_LEVEL z patent_fetch na jednotnú úroveň dôkazu."""
    raw = _field(fetch_output, "EVIDENCE_LEVEL").upper()
    return {
        "CLAIM_VERIFIED": "claim_verified",
        "ABSTRACT_VERIFIED": "abstract_verified",
        "FETCH_TIMEOUT": "fetch_timeout",
        "FETCH_FAILED": "fetch_failed",
        "FETCH_BLOCKED": "fetch_failed",
    }.get(raw, "fetch_failed" if "TOOL_ERROR:" in (fetch_output or "") else "search_snippet_only")


def _fetch_summary(fetch_output: str) -> str:
    """Vytvorí krátke zhrnutie z výsledku patent_fetch."""
    claim = _field(fetch_output, "CLAIM1")
    abstract = _field(fetch_output, "ABSTRACT")
    parts = []
    if claim and "No claim excerpt" not in claim and "No first claim" not in claim:
        parts.append(f"Claim evidence: {claim}")
    if abstract and "No abstract excerpt" not in abstract and "No abstract section" not in abstract:
        parts.append(f"Abstract evidence: {abstract}")
    return trim_words(" ".join(parts), 90)


def _fetch_attempt_log(fetch_output: str) -> list[dict[str, object]]:
    """Načíta diagnostický záznam pokusov z výsledku patent_fetch."""
    raw = _field(fetch_output, "ATTEMPT_LOG_JSON")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _clean_hit_text(value: Any) -> str:
    """Očistí textové pole nálezu od zvyškov vloženého JSON obsahu."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for marker in (
        '\\"verified_url\\":',
        '"verified_url":',
        "\\n      \\\"verified_url\\\"",
        '\n      "verified_url"',
        '\\"evidence_level\\":',
        '"evidence_level":',
        "\\n      \\\"evidence_level\\\"",
        '\n      "evidence_level"',
        '\\"title\\":',
        '"title":',
        "\\n    {",
        "\n    {",
        '\\"url\\":',
        '"url":',
    ):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
            break
    return text.strip(" ,{}[]'\"\\")


def _worse_status(base_status: str, warnings: list[str], verification_failed: bool) -> str:
    """Zhorší stav evidence packu, ak nastali varovania alebo zlyhalo overenie."""
    if base_status == "failed":
        return "failed"
    if base_status == "partial_failure" or warnings or verification_failed:
        return "partial_failure"
    return "ok"


def _completed(search_completed: bool, status: str) -> bool:
    """Určí, či možno evidence pack považovať za dokončený."""
    return bool(search_completed and status == "ok")


async def patent_evidence_pack(
    query: str,
    max_results: int = 10,
    max_fetches: int = 6,
    fetch_timeout_ms: int = 18000,
    atomic_requirements: list[dict[str, Any]] | None = None,
    english_query: str = "",
) -> str:
    """Vyhľadá a spracuje patentové dôkazy pre zadaný dotaz."""
    warnings: list[str] = []
    verification_failed = False
    query = clean_tool_query(query)
    relevance_query = (english_query or "").strip() or query
    search_query = relevance_query
    try:
        search_raw = await patent_search(query=search_query, max_results=max_results)
        search_payload = json.loads(search_raw)
    except Exception as exc:
        return json.dumps(
            {
                "source_type": "patent",
                "status": "failed",
                "completed": False,
                "reliable_no_results": False,
                "hits": [],
                "errors": [{"type": "patent_search_failed", "message": str(exc)}],
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )

    candidates: list[dict[str, Any]] = list(search_payload.get("results") or [])
    top_score = float(candidates[0].get("score") or 0.0) if candidates else 0.0
    base_ceiling = 6 if top_score >= 5.0 else 3
    fetch_limit = max(0, min(max_fetches, base_ceiling, len(candidates)))
    selected_for_fetch = [item for item in candidates[:fetch_limit] if item.get("url")]
    fetch_outputs: list[str] = []
    if selected_for_fetch:
        per_fetch_ceiling = max(5000, min(fetch_timeout_ms, 18000))
        outer_ceiling_s = max(6.0, (per_fetch_ceiling * 2) / 1000.0)

        async def _bounded_patent_fetch(url: str, pdf_url: str) -> str:
            """Načíta detail patentu s dodatočným časovým limitom volania."""
            try:
                return await asyncio.wait_for(
                    patent_fetch(url=url, timeout_ms=per_fetch_ceiling, pdf_url=pdf_url),
                    timeout=outer_ceiling_s,
                )
            except asyncio.TimeoutError:
                return (
                    f"TOOL_ERROR: patent_fetch\n"
                    f"REASON: outer fetch ceiling {outer_ceiling_s:.1f}s exceeded.\n"
                    f"URL: {url}\n"
                    "STATUS: FAILED\n"
                    "EVIDENCE_LEVEL: FETCH_FAILED"
                )

        fetched = await asyncio.gather(
            *[
                _bounded_patent_fetch(
                    str(item.get("url") or ""), str(item.get("pdf_url") or "")
                )
                for item in selected_for_fetch
            ],
            return_exceptions=True,
        )
        for item, result in zip(selected_for_fetch, fetched):
            if isinstance(result, Exception):
                text = f"TOOL_ERROR: patent_fetch\nREASON: {result}\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"
                warnings.append(trim_words(text, 50))
                fetch_outputs.append(text)
            else:
                fetch_outputs.append(str(result))

    final_urls = [str(item.get("url") or "").strip() for item in candidates if item.get("url")]
    verification = ""
    if final_urls:
        try:
            verification = await verify_sources(final_urls, max_urls=len(final_urls))
            if re.search(r"^STATUS:\s*FAILED\s*$", verification, flags=re.I | re.M):
                verification_failed = True
                warnings.append("URL verification failed; verified_url values remain false unless ALIVE was returned.")
        except Exception as exc:
            verification_failed = True
            warnings.append(f"URL verification failed: {exc}")
    verified = _verification_map(verification)

    hits: list[dict[str, Any]] = []
    for index, item in enumerate(candidates):
        fetch_output = fetch_outputs[index] if index < len(fetch_outputs) else ""
        evidence_level = _fetch_level(fetch_output) if fetch_output else "search_snippet_only"
        fetched_summary = _fetch_summary(fetch_output) if fetch_output else ""
        snippet = trim_words(_clean_hit_text(item.get("snippet")), 70)
        summary_parts = []
        if snippet:
            summary_parts.append(f"Search snippet: {snippet}")
        if fetched_summary:
            summary_parts.append(fetched_summary)
        if fetch_output and "STATUS: BLOCKED" in fetch_output:
            warnings.append(
                f"Patent fetch for {item.get('url')} was blocked by the source's "
                "anti-automation protection; claim/abstract could not be verified."
            )
        elif fetch_output and evidence_level in {"fetch_timeout", "fetch_failed"}:
            warnings.append(f"Patent fetch did not verify {item.get('url')}: {trim_words(fetch_output, 40)}")
        url = _clean_hit_text(item.get("url") or "")
        provider = _clean_hit_text(_field(fetch_output, "PROVIDER") or item.get("provider") or search_payload.get("provider") or "")
        attempt_log = _fetch_attempt_log(fetch_output)
        summary = trim_words(_clean_hit_text(" ".join(summary_parts)), 120)
        norm_title, norm_number = _normalize_patent_identity(
            item.get("title") or "",
            item.get("patent_number") or "",
            url,
        )
        claim1_text = _field(fetch_output, "CLAIM1") if fetch_output else ""
        claims_text = _field(fetch_output, "CLAIMS_TEXT") if fetch_output else ""
        abstract_text = _field(fetch_output, "ABSTRACT") if fetch_output else ""
        description_text = _field(fetch_output, "DESCRIPTION_TEXT") if fetch_output else ""
        # Tokeny celého oficiálneho PDF (plný text nárokov aj opisu vynálezu).
        coverage_tokens = _field(fetch_output, "COVERAGE_TOKENS") if fetch_output else ""
        coverage_parts = [
            part
            for part in (claim1_text, claims_text, abstract_text, description_text)
            if part and "No claim excerpt" not in part and "No first claim" not in part
            and "No abstract" not in part
        ]
        if coverage_tokens:
            coverage_parts.append(coverage_tokens)
        coverage_text = " ".join(coverage_parts)
        claim_coverage, claim_atom_match_count = _compute_claim_coverage(
            coverage_text, atomic_requirements
        )
        exact_candidate = bool(
            atomic_requirements
            and claim_coverage >= 1.0
            and evidence_level in _EXACT_CANDIDATE_EVIDENCE_LEVELS
        )
        hits.append(
            {
                "title": _clean_hit_text(norm_title),
                "url": url,
                "patent_number": _clean_hit_text(norm_number),
                "evidence_level": evidence_level,
                "summary": summary,
                "verified_url": bool(verified.get(url, False)),
                "relevance": _patent_relevance(
                    relevance_query,
                    _clean_hit_text(norm_title),
                    summary,
                    claim_coverage=claim_coverage,
                    evidence_level=evidence_level,
                ),
                "claim_coverage": claim_coverage,
                "claim_atom_match_count": claim_atom_match_count,
                "exact_combination_candidate_found": exact_candidate,
                "relevance_score": evidence_score(
                    relevance_query,
                    f"{norm_title} {summary}",
                    "PATENT",
                    evidence_level,
                ),
                "provider": provider,
                "attempt_log": attempt_log,
            }
        )
    hits = sort_hits_by_relevance(hits)

    status = _worse_status(str(search_payload.get("status") or "failed"), warnings, verification_failed)
    payload = {
        "source_type": "patent",
        "status": status,
        "completed": _completed(bool(search_payload.get("completed")), status),
        "reliable_no_results": bool(search_payload.get("reliable_no_results")),
        "hits": hits,
        "errors": list(search_payload.get("errors") or []),
        "warnings": list(search_payload.get("notes") or []) + warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
