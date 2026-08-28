"""Vyhľadávanie patentových dokumentov vo viacerých zdrojoch."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ._ttl_cache import TTLCache
from .output_cleaner import USER_AGENT, trim_words
from .patent_filters import extract_patent_number
from .relevance import build_corpus_idf, evidence_score
from .result_contract import NormalizedResult

LOGGER = logging.getLogger(__name__)
MAX_RESULTS_CAP = 10
MIN_RELEVANCE_SCORE = 2.8
MIN_RELEVANCE_FALLBACK_NEEDED = 3
MIN_RELEVANCE_SCORE_FALLBACK = 2.2
MIN_RELEVANCE_FLOOR = 1.5
PROVIDER_CANDIDATE_CAP = 80
SEARCH_CANDIDATE_POOL_SIZE = 15
SEARCH_CACHE_TTL_SECONDS = 300
_PATENT_SEARCH_CACHE: TTLCache[tuple[str, int], str] = TTLCache(
    ttl_seconds=SEARCH_CACHE_TTL_SECONDS, max_entries=64
)


class ProviderUnavailable(RuntimeError):
    """Výnimka pre poskytovateľa, ktorý nie je dostupný alebo nie je nakonfigurovaný."""


# Provideri, ktorých úspešné dokončenie bez nálezov je spoľahlivý negatívny signál.
_PRIMARY_PATENT_PROVIDERS = frozenset({"google_patents_xhr", "tavily", "exa"})

# Google hostuje oficiálne PDF patentov na samostatnom storage buckete, ktorý
# nepodlieha ochrane proti automatizovaným požiadavkam ako patents.google.com.
PATENT_PDF_BASE_URL = "https://patentimages.storage.googleapis.com/"


@dataclass
class PatentCandidate:
    title: str
    url: str
    patent_number: str
    snippet: str
    score: float = 0.0
    provider: str = ""
    # Oficiálne PDF patentu na Google storage; na rozdiel od HTML stránky
    # nepodlieha ochrane proti automatizovaným požiadavkam a obsahuje plný
    # text nárokov aj opisu vynálezu.
    pdf_url: str = ""
    assignee: str = ""
    filing_date: str = ""
    grant_date: str = ""


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


def _score(query: str, candidate: PatentCandidate, idf: dict[str, float] | None = None) -> float:
    """Vypočíta celkové interné skóre patentového kandidáta."""
    text = f"{candidate.title} {candidate.snippet}"
    return round(
        min(evidence_score(query, text, "PATENT", idf=idf) + _phrase_score(query, text), 10.0), 2
    )


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
                "pdf_url": candidate.pdf_url,
                "assignee": candidate.assignee,
                "filing_date": candidate.filing_date,
                "grant_date": candidate.grant_date,
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
            "include_domains": [
                "patents.google.com",
                "patentscope.wipo.int",
                "patents.justia.com",
                "worldwide.espacenet.com",
            ],
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
            "includeDomains": [
                "patents.google.com",
                "patentscope.wipo.int",
                "patents.justia.com",
                "worldwide.espacenet.com",
            ],
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


async def _google_patents_xhr_search(client: httpx.AsyncClient, query: str, limit: int) -> list[PatentCandidate]:
    """Vyhľadá patentových kandidátov cez natívne Google Patents JSON rozhranie.

    Rozhranie nevyžaduje API kľúč, takže patentové vyhľadávanie funguje
    aj bez nakonfigurovaného Tavily alebo Exa.
    """
    response = await client.get(
        "https://patents.google.com/xhr/query",
        params={"url": f"q=({query})", "exp": ""},
        headers={"Referer": "https://patents.google.com/"},
    )
    response.raise_for_status()
    payload = response.json()
    clusters = ((payload.get("results") or {}).get("cluster")) or []
    candidates: list[PatentCandidate] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        for item in cluster.get("result") or []:
            if not isinstance(item, dict):
                continue
            patent = item.get("patent") or {}
            if not isinstance(patent, dict):
                continue
            number = re.sub(r"[^A-Z0-9]", "", _text(patent.get("publication_number")).upper())
            if not number:
                continue
            title = _text(patent.get("title"))
            snippet = _text(patent.get("snippet") or patent.get("abstract"))
            pdf_path = _text(patent.get("pdf")).lstrip("/")
            candidates.append(
                PatentCandidate(
                    title=title or number,
                    url=f"https://patents.google.com/patent/{number}/en",
                    patent_number=number,
                    snippet=trim_words(snippet or "No abstract snippet was returned.", 60),
                    provider="google_patents_xhr",
                    pdf_url=f"{PATENT_PDF_BASE_URL}{pdf_path}" if pdf_path else "",
                    assignee=_text(patent.get("assignee")),
                    filing_date=_text(patent.get("filing_date")),
                    grant_date=_text(patent.get("grant_date") or patent.get("publication_date")),
                )
            )
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


_WIPO_CLASSIFICATION_RE = re.compile(
    r"\bInt\.?\s*Class\b.*?(?=\bAppl\.?\s*No\b|\bApplicant\b|\bInventor\b|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_WIPO_FIELD_RE = re.compile(
    r"\b(?:Appl\.?\s*No|Applicant|Inventor|Agent|Priority\s*Data|Publication\s*Date)\b\s*:?\s*",
    flags=re.IGNORECASE,
)
_WIPO_ROW_PREFIX_RE = re.compile(r"^\s*\d+\.\s*\d{6,}\s*", flags=re.IGNORECASE)


def _wipo_snippet(row_text: str) -> str:
    """Vytvorí snippet z riadku výsledkov WIPO PATENTSCOPE.

    Riadok tabuľky neobsahuje abstrakt, ale poradové číslo, názov, dátum a
    najmä celý rozpis medzinárodného patentového triedenia. Ten je zložený zo
    všeobecných technických slov ("recognising patterns", "computing", "data"),
    ktoré pri hodnotení relevancie spôsobovali falošné zhody s ľubovoľným
    technickým dotazom. Klasifikácia a formulárové polia sa preto odstraňujú;
    ak po očistení nezostane nič vecné, snippet sa nevytvára vôbec.
    """
    text = _WIPO_ROW_PREFIX_RE.sub("", str(row_text or ""))
    text = _WIPO_CLASSIFICATION_RE.sub(" ", text)
    text = _WIPO_FIELD_RE.sub(" ", text)
    text = re.sub(r"\b[A-Z]{2}\s*-\s*\d{2}\.\d{2}\.\d{4}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .;,-")
    return text if len(text.split()) >= 5 else ""


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
        item = _candidate(
            _wipo_title(anchor_text, text), "", number, _wipo_snippet(text), "wipo_patentscope"
        )
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
    # Vzácnosť termínov sa počíta nad celou množinou kandidátov, aby termíny
    # spoločné pre všetky patenty nezvyšovali skóre tematicky vzdialených.
    corpus_idf = build_corpus_idf([f"{c.title} {c.snippet}" for c in candidates])
    for candidate in candidates:
        candidate.score = _score(query, candidate, idf=corpus_idf)
    primary = [
        candidate
        for candidate in candidates
        if candidate.score >= MIN_RELEVANCE_SCORE
    ]
    threshold_used = float(MIN_RELEVANCE_SCORE)
    if len(primary) < MIN_RELEVANCE_FALLBACK_NEEDED:
        relaxed = [
            candidate
            for candidate in candidates
            if candidate.score >= MIN_RELEVANCE_SCORE_FALLBACK
        ]
        if len(relaxed) > len(primary):
            primary = relaxed
            threshold_used = float(MIN_RELEVANCE_SCORE_FALLBACK)
    if primary:
        ranked = primary
    elif allow_low_confidence:
        # Aj v núdzovom režime sa vracajú len kandidáti s aspoň minimálnym
        # prekryvom s dotazom. Bez tejto hranice sa do reportu dostávali
        # patenty so skóre 0, ktoré s dotazom nesúviseli vôbec.
        ranked = [c for c in candidates if c.score >= MIN_RELEVANCE_FLOOR]
    else:
        ranked = []
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
    if cached is not None:
        LOGGER.info("patent_search cache_hit query=%r max_results=%s", normalized_query[:200], limit)
        return cached
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    completed_count = 0
    primary_completed_count = 0
    unavailable_primary_count = 0
    all_candidates: list[PatentCandidate] = []
    providers = [
        ("google_patents_xhr", _google_patents_xhr_search),
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
                if provider_name in _PRIMARY_PATENT_PROVIDERS:
                    primary_completed_count += 1
            elif slot["unavailable"] > 0 and slot["error"] == 0:
                if provider_name in _PRIMARY_PATENT_PROVIDERS:
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
        _PATENT_SEARCH_CACHE.set(cache_key, result)
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
        _PATENT_SEARCH_CACHE.set(cache_key, result)
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
                    f"Patent providers completed ({', '.join(completed_providers) or 'none'}) but returned zero high-confidence patent records."
                    if reliable_no_results
                    else "No primary patent provider completed; do not treat this as reliable negative patent evidence."
                ],
            ),
            "mixed",
            [],
        )
        _PATENT_SEARCH_CACHE.set(cache_key, result)
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
    _PATENT_SEARCH_CACHE.set(cache_key, result)
    return result
