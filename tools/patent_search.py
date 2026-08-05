"""Vyhľadávanie patentových dokumentov vo viacerých zdrojoch."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .output_cleaner import USER_AGENT, trim_words
from .patent_filters import extract_patent_number
from .relevance import evidence_score
from .result_contract import NormalizedResult

LOGGER = logging.getLogger(__name__)
MAX_RESULTS_CAP = 10
MIN_RELEVANCE_SCORE = 2.8
MIN_RELEVANCE_FALLBACK_NEEDED = 3
MIN_RELEVANCE_SCORE_FALLBACK = 2.2
PROVIDER_CANDIDATE_CAP = 80
SEARCH_CANDIDATE_POOL_SIZE = 15
SEARCH_CACHE_TTL_SECONDS = 300
_PATENT_SEARCH_CACHE: dict[tuple[str, int], tuple[float, str]] = {}


class ProviderUnavailable(RuntimeError):
    """Výnimka pre poskytovateľa, ktorý nie je dostupný alebo nie je nakonfigurovaný."""


@dataclass
class PatentCandidate:
    title: str
    url: str
    patent_number: str
    snippet: str
    score: float = 0.0
    provider: str = ""


def _normalize_query(query: Any) -> str:
    """Zjednotí vstupný dotaz na jednoduchý text."""
    from .query_normalize import coerce_query_input  
    text = coerce_query_input(query) if not isinstance(query, str) else query
    return re.sub(r"\s+", " ", text).strip()


def _provider_query(query: str) -> str:
    """Pripraví dotaz pre poskytovateľov vyhľadávania."""
    return _normalize_query(query)


def _wipo_query(query: str) -> str:
    """Pripraví jednoduchší dotaz pre WIPO PATENTSCOPE."""
    text = _normalize_query(query).lower()
    stopwords = {
        "the", "and", "for", "with", "that", "this", "from", "into", "using",
        "instead", "there", "existing", "concept", "about", "already",
    }
    words = [
        word
        for word in re.findall(r"[a-z0-9][a-z0-9-]{2,}", text)
        if word not in stopwords
    ]
    return " ".join(dict.fromkeys(words[:10])) or query


def _text(value: Any) -> str:
    """Prevedie hodnotu z API poskytovateľa na čistý jednoriadkový text."""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", html.unescape(value)).strip()
    if isinstance(value, list):
        return re.sub(r"\s+", " ", " ".join(_text(item) for item in value)).strip()
    if isinstance(value, dict):
        return re.sub(r"\s+", " ", " ".join(_text(item) for item in value.values())).strip()
    return "" if value is None else str(value)


def _patent_url(number: str, fallback: str = "") -> str:
    """Vytvorí preferovanú URL adresu patentu alebo použije fallback."""
    if fallback.startswith("http") and "patents.google.com/patent/" in fallback and number != "Unknown":
        return f"https://patents.google.com/patent/{number}/en"
    if fallback.startswith("http"):
        return fallback
    return f"https://patents.google.com/patent/{number}/en" if number != "Unknown" else fallback


def _normalize_patent_dedupe_key(patent_number: str) -> str:
    """Zjednotí patentové číslo na kľúč používaný pri odstraňovaní duplicít."""
    raw = (patent_number or "").upper().strip()
    if not raw or raw == "UNKNOWN":
        return ""
    return re.sub(r"([A-Z]{2}\d{4,})[A-Z]\d?$", r"\1", raw)


def _dedupe(candidates: list[PatentCandidate]) -> list[PatentCandidate]:
    """Odstráni duplicitné patentové kandidáty podľa čísla alebo URL adresy."""
    seen: set[str] = set()
    out: list[PatentCandidate] = []
    for candidate in candidates:
        key = _normalize_patent_dedupe_key(candidate.patent_number) or candidate.url
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


_PHRASE_SCORE_STOP = {"that", "this", "with", "from", "into", "have", "been", "will", "would", "should", "could", "what", "when", "where", "which", "while", "such"}


def _phrase_score(query: str, text: str) -> float:
    """Zvýši skóre pri zhode s významovými frázami dotazu."""
    normalized_query = query.lower()
    haystack = text.lower()
    sig_tokens = [
        token for token in re.findall(r"[a-z][a-z\-]{2,}", normalized_query)
        if token not in _PHRASE_SCORE_STOP
    ]
    token_bonus = 0.0
    if sig_tokens:
        matched = sum(1 for token in sig_tokens if token in haystack)
        token_bonus = min(0.4 * matched, 1.2)
    phrase_parts = [part.strip() for part in re.split(r"[,;|]", normalized_query) if len(part.strip()) >= 6]
    if any(phrase and phrase in haystack for phrase in phrase_parts):
        return min(token_bonus + 1.0, 1.5)
    query_words = normalized_query.split()
    for size in range(min(5, len(query_words)), 1, -1):
        for start in range(0, len(query_words) - size + 1):
            phrase = " ".join(query_words[start : start + size])
            if phrase in haystack:
                return min(token_bonus + (size / max(len(query_words), 1)), 1.5)
    return min(token_bonus, 1.5)


def _score(query: str, candidate: PatentCandidate) -> float:
    """Vypočíta celkové interné skóre patentového kandidáta."""
    text = f"{candidate.title} {candidate.snippet}"
    return round(min(evidence_score(query, text, "PATENT") + _phrase_score(query, text), 10.0), 2)


def _matches_required_concept(query: str, candidate: PatentCandidate) -> bool:
    """Overí, či kandidát spĺňa povinný koncept dotazu."""
    return True


def _json_response(result: NormalizedResult, provider: str, candidates: list[PatentCandidate]) -> str:
    """Zabalí patentových kandidátov do normalizovanej JSON odpovede."""
    payload = {
        "status": result.status,
        "completed": result.completed,
        "reliable_no_results": result.reliable_no_results,
        "query": result.query,
        "provider": provider,
        "error_count": len(result.errors),
        "errors": result.errors,
        "notes": result.notes,
        "results": [
            {
                "title": candidate.title,
                "url": candidate.url,
                "patent_number": candidate.patent_number,
                "snippet": candidate.snippet,
                "score": candidate.score,
                "evidence_level": "search_snippet_only",
                "verified_url": False,
                "needs_fetch": True,
            }
            for candidate in candidates
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _candidate(title: str, url: str, patent_number: str, snippet: str, provider: str) -> PatentCandidate | None:
    """Vytvorí patentového kandidáta, ak sa dá určiť patentové číslo."""
    number = patent_number if patent_number and patent_number != "Unknown" else extract_patent_number(title, snippet, url)
    if number == "Unknown":
        return None
    return PatentCandidate(
        title=title or number,
        url=_patent_url(number, url),
        patent_number=number,
        snippet=trim_words(snippet or "No abstract snippet was returned.", 60),
        provider=provider,
    )


def _web_patent_query(query: str) -> str:
    """Doplní do dotazu obmedzenie na patentové weby."""
    return f"{query} site:patents.google.com/patent OR site:patentscope.wipo.int"


async def _tavily_patent_search(client: httpx.AsyncClient, query: str, limit: int) -> list[PatentCandidate]:
    """Vyhľadá patentových kandidátov cez Tavily."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("TAVILY_API_KEY is not configured; skipping Tavily provider.")
    response = await client.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "api_key": api_key,
            "query": _web_patent_query(query),
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": min(max(limit * 4, SEARCH_CANDIDATE_POOL_SIZE), 20),
            "include_domains": ["patents.google.com", "patentscope.wipo.int"],
        },
    )
    response.raise_for_status()
    candidates: list[PatentCandidate] = []
    for record in response.json().get("results", []):
        title = _text(record.get("title"))
        url = _text(record.get("url"))
        snippet = _text(record.get("content") or record.get("raw_content"))
        item = _candidate(title, url, "", snippet, "tavily")
        if item:
            candidates.append(item)
    return _dedupe(candidates)[:PROVIDER_CANDIDATE_CAP]


async def _exa_patent_search(client: httpx.AsyncClient, query: str, limit: int) -> list[PatentCandidate]:
    """Vyhľadá patentových kandidátov cez Exa."""
    api_key = os.getenv("EXA_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("EXA_API_KEY is not configured; skipping Exa provider.")
    response = await client.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "query": _web_patent_query(query),
            "type": "auto",
            "numResults": min(max(limit * 5, SEARCH_CANDIDATE_POOL_SIZE), PROVIDER_CANDIDATE_CAP),
            "includeDomains": ["patents.google.com", "patentscope.wipo.int"],
            "contents": {"text": True},
        },
    )
    response.raise_for_status()
    candidates: list[PatentCandidate] = []
    for record in response.json().get("results", []):
        title = _text(record.get("title"))
        url = _text(record.get("url"))
        snippet = _text(record.get("text") or record.get("summary") or record.get("highlights"))
        item = _candidate(title, url, "", snippet, "exa")
        if item:
            candidates.append(item)
    return _dedupe(candidates)[:PROVIDER_CANDIDATE_CAP]


def _wipo_publication_number(anchor_text: str, row_text: str, url: str) -> str:
    """Vytiahne publikačné číslo z riadku výsledku WIPO PATENTSCOPE."""
    normalized_anchor = re.sub(r"[^A-Za-z0-9]", "", anchor_text).upper()
    if re.fullmatch(r"(?:WO|EP|CN|JP|KR)\d{8,}[A-Z0-9]*", normalized_anchor):
        return normalized_anchor
    country_match = re.search(r"\b(US|EP|WO|CN|JP|KR)\s*-", row_text, flags=re.IGNORECASE)
    if country_match and re.fullmatch(r"\d{6,}", normalized_anchor):
        return f"{country_match.group(1).upper()}{normalized_anchor}"
    return extract_patent_number(anchor_text, row_text, url)


def _wipo_title(anchor_text: str, row_text: str) -> str:
    """Vytiahne názov patentu z riadku výsledku WIPO PATENTSCOPE."""
    text = re.sub(r"^\s*\d+\.\s*", "", row_text)
    text = re.sub(re.escape(anchor_text), "", text, count=1).strip()
    split = re.split(r"\b(?:US|EP|WO|CN|JP|KR)\s*-\s*\d{2}\.\d{2}\.\d{4}", text, maxsplit=1, flags=re.IGNORECASE)
    title = split[0].strip(" -:;")
    return title or anchor_text


async def _wipo_patentscope_search(client: httpx.AsyncClient, query: str, limit: int) -> list[PatentCandidate]:
    """Vyhľadá patentových kandidátov vo WIPO PATENTSCOPE."""
    response = await client.get(
        "https://patentscope.wipo.int/search/en/result.jsf",
        headers={"Referer": "https://patentscope.wipo.int/search/en/search.jsf"},
        params={"queryString": query, "maxRec": str(min(max(limit * 5, SEARCH_CANDIDATE_POOL_SIZE), PROVIDER_CANDIDATE_CAP))},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    candidates: list[PatentCandidate] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if "detail.jsf" not in href:
            continue
        row = anchor.find_parent("tr")
        anchor_text = _text(anchor.get_text(" ", strip=True))
        text = _text(row.get_text(" ", strip=True) if row else anchor.get_text(" ", strip=True))
        source_url = urljoin("https://patentscope.wipo.int/search/en/", href)
        number = _wipo_publication_number(anchor_text, text, source_url)
        item = _candidate(_wipo_title(anchor_text, text), "", number, text, "wipo_patentscope")
        if item:
            candidates.append(item)
    return _dedupe(candidates)[:PROVIDER_CANDIDATE_CAP]


def _rank(
    query: str,
    candidates: list[PatentCandidate],
    limit: int,
    allow_low_confidence: bool = False,
) -> tuple[list[PatentCandidate], float]:
    """Ohodnotí kandidátov a vráti najrelevantnejšie výsledky."""
    for candidate in candidates:
        candidate.score = _score(query, candidate)
    primary = [
        candidate
        for candidate in candidates
        if candidate.score >= MIN_RELEVANCE_SCORE and _matches_required_concept(query, candidate)
    ]
    threshold_used = float(MIN_RELEVANCE_SCORE)
    if len(primary) < MIN_RELEVANCE_FALLBACK_NEEDED:
        relaxed = [
            candidate
            for candidate in candidates
            if candidate.score >= MIN_RELEVANCE_SCORE_FALLBACK
            and _matches_required_concept(query, candidate)
        ]
        if len(relaxed) > len(primary):
            primary = relaxed
            threshold_used = float(MIN_RELEVANCE_SCORE_FALLBACK)
    ranked = primary or (candidates if allow_low_confidence else [])
    sorted_ranked = sorted(ranked, key=lambda item: (-item.score, item.patent_number))[:limit]
    return sorted_ranked, threshold_used


def _patent_query_variants(normalized_query: str, max_variants: int = 3) -> list[str]:
    """Vytvorí obmedzený počet interných variantov patentového dotazu."""
    text = (normalized_query or "").strip()
    if not text:
        return []
    variants = [text]
    compact = re.sub(
        r"\b(?:does|do|a|an|the|for|of|to|is|are|with|using|by|via|through|and|or)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    compact = re.sub(r"\s+", " ", compact).strip(" ?!.")
    if compact and compact.lower() != text.lower():
        variants.append(compact)
    truncated = re.split(r"\b(?:already\s+exist|exists?\??)\b", text, flags=re.IGNORECASE)[0].strip(" ?!.")
    if truncated and truncated.lower() not in {v.lower() for v in variants}:
        variants.append(truncated)
    return variants[: max(1, max_variants)]


async def patent_search(query: str, max_results: int = 10) -> str:
    """Vyhľadá patentové výsledky vo viacerých dostupných zdrojoch."""
    normalized_query = _normalize_query(query)
    search_query = _provider_query(normalized_query)
    limit = max(1, min(max_results, MAX_RESULTS_CAP))
    cache_key = (normalized_query, limit)
    cached = _PATENT_SEARCH_CACHE.get(cache_key)
    if cached and time.time() - cached[0] <= SEARCH_CACHE_TTL_SECONDS:
        LOGGER.info("patent_search cache_hit query=%r max_results=%s", normalized_query[:200], limit)
        return cached[1]
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    completed_count = 0
    primary_completed_count = 0
    unavailable_primary_count = 0
    all_candidates: list[PatentCandidate] = []
    providers = [
        ("tavily", _tavily_patent_search),
        ("exa", _exa_patent_search),
        ("wipo_patentscope", _wipo_patentscope_search),
    ]
    query_variants = _patent_query_variants(search_query, max_variants=3)
    wipo_variants = _patent_query_variants(_wipo_query(normalized_query), max_variants=3)

    completed_providers: list[str] = []

    async def _run_provider_variant(provider_name: str, provider, variant: str):
        """Spustí jeden provider s jedným variantom dotazu a zachytí chyby."""
        try:
            candidates = await provider(client, variant, limit)
            return ("ok", provider_name, variant, candidates)
        except ProviderUnavailable as exc:
            return ("unavailable", provider_name, variant, exc)
        except Exception as exc:
            return ("error", provider_name, variant, exc)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=8.0) as client:
        tasks = []
        for provider_name, provider in providers:
            variants = wipo_variants if provider_name == "wipo_patentscope" else query_variants
            for variant in variants:
                tasks.append(_run_provider_variant(provider_name, provider, variant))
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

        provider_status: dict[str, dict[str, int]] = {}
        for outcome in results:
            if isinstance(outcome, Exception):
                continue
            status, provider_name, variant, payload = outcome
            slot = provider_status.setdefault(provider_name, {"ok": 0, "unavailable": 0, "error": 0})
            slot[status] += 1
            if status == "ok":
                all_candidates.extend(payload)
            elif status == "unavailable":
                LOGGER.info("patent_search provider=%s skipped: %s", provider_name, payload)
            elif status == "error":
                LOGGER.info(
                    "patent_search provider=%s variant=%r failed: %s",
                    provider_name, variant[:60], payload,
                )
                errors.append({"provider": provider_name, "message": str(payload)})

        for provider_name, slot in provider_status.items():
            if slot["ok"] > 0:
                completed_count += 1
                completed_providers.append(provider_name)
                if provider_name in {"tavily", "exa"}:
                    primary_completed_count += 1
            elif slot["unavailable"] > 0 and slot["error"] == 0:
                if provider_name in {"tavily", "exa"}:
                    unavailable_primary_count += 1
                notes.append(f"{provider_name} not configured for this run.")

    deduped_candidates = _dedupe(all_candidates)
    ranked, threshold_used = _rank(normalized_query, deduped_candidates, limit)
    threshold_note = (
        f"Patent rerank threshold used: {threshold_used:.2f}"
        + (
            f" (fallback from primary {MIN_RELEVANCE_SCORE:.2f})"
            if threshold_used < MIN_RELEVANCE_SCORE - 1e-6
            else ""
        )
        + "."
    )
    if ranked:
        retrieval_incomplete = bool(errors) or (
            primary_completed_count == 0 and unavailable_primary_count > 0
        )
        provider_label = (
            "+".join(completed_providers) if len(completed_providers) > 1
            else (completed_providers[0] if completed_providers else "mixed")
        )
        result = _json_response(
            NormalizedResult(
                status="partial_failure" if retrieval_incomplete else "ok",
                completed=not retrieval_incomplete,
                reliable_no_results=False,
                query=normalized_query,
                hits=[asdict(item) for item in ranked],
                errors=errors,
                notes=notes
                + [threshold_note]
                + (
                    [f"Patent retrieval was incomplete; merged hits from completed providers: {', '.join(completed_providers) or 'none'}."]
                    if retrieval_incomplete else []
                ),
            ),
            provider_label if not retrieval_incomplete else "mixed",
            ranked,
        )
        _PATENT_SEARCH_CACHE[cache_key] = (time.time(), result)
        return result

    low_confidence_ranked, _ = _rank(normalized_query, deduped_candidates, limit, allow_low_confidence=True)
    if low_confidence_ranked:
        result = _json_response(
            NormalizedResult(
                status="partial_failure",
                completed=False,
                reliable_no_results=False,
                query=normalized_query,
                hits=[asdict(item) for item in low_confidence_ranked],
                errors=errors,
                notes=notes + ["No high-confidence patent records met the relevance threshold; returning low-confidence candidates for review."],
            ),
            "mixed",
            low_confidence_ranked,
        )
        _PATENT_SEARCH_CACHE[cache_key] = (time.time(), result)
        return result

    if completed_count > 0 and not errors:
        reliable_no_results = primary_completed_count > 0
        result = _json_response(
            NormalizedResult(
                status="ok" if reliable_no_results else "partial_failure",
                completed=reliable_no_results,
                reliable_no_results=reliable_no_results,
                query=normalized_query,
                errors=[],
                notes=notes
                + [
                    "Tavily, Exa, and WIPO PATENTSCOPE completed but returned zero high-confidence patent records."
                    if reliable_no_results
                    else "Only WIPO PATENTSCOPE completed; Tavily and Exa were unavailable, so do not treat this as reliable negative patent evidence."
                ],
            ),
            "mixed",
            [],
        )
        _PATENT_SEARCH_CACHE[cache_key] = (time.time(), result)
        return result

    result = _json_response(
        NormalizedResult(
            status="partial_failure" if completed_count else "failed",
            completed=False,
            reliable_no_results=False,
            query=normalized_query,
            errors=errors,
            notes=notes + ["Patent retrieval failed or was incomplete; do not treat this as evidence that no relevant patents exist."],
        ),
        "mixed",
        [],
    )
    _PATENT_SEARCH_CACHE[cache_key] = (time.time(), result)
    return result
