"""Vyhľadávanie a načítanie webových stránok."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .chromium_scraper import fetch_page_html
from .jina_reader import fetch_via_jina
from .output_cleaner import BLOCKED_WEB_DOMAINS, USER_AGENT, clean_output, format_error, trim_words
from .relevance import evidence_score, is_relevant, tokens
from .result_contract import NormalizedResult, prepend_markers
from .search_bounds import section_query_variants

PATENT_EVIDENCE_DOMAINS = {
    "patents.google.com",
    "patents.justia.com",
    "patentsencyclopedia.com",
    "thinkstruct.com",
    "data.epo.org",
    "freepatentsonline.com",
    "www.freepatentsonline.com",
    "patents.com",
    "www.patents.com",
    "lens.org",
    "www.lens.org",
    "espacenet.com",
    "worldwide.espacenet.com",
    "patentscope.wipo.int",
}
TEXT_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/xml", "text/xml")
FETCH_TOTAL_TIMEOUT_MS = 14000
MIN_EXTRACTED_WORDS = 12
FETCH_CONTENT_WORDS = 450
SEARCH_CANDIDATE_POOL_SIZE = 15
MIN_WEB_RERANK_SCORE = 5.0
SEARCH_PAGE_DOMAINS = {
    "google.com",
    "www.google.com",
    "bing.com",
    "www.bing.com",
    "duckduckgo.com",
    "www.duckduckgo.com",
    "search.yahoo.com",
}
MARKETPLACE_DOMAINS = {
    "alibaba.com",
    "amazon.com",
    "ebay.com",
    "etsy.com",
    "walmart.com",
}
MARKETPLACE_PRODUCT_PATHS = (
    re.compile(r"^/dp/[A-Z0-9]+(?:/|$)", re.IGNORECASE),                  
    re.compile(r"^/gp/product/[A-Z0-9]+(?:/|$)", re.IGNORECASE),          
    re.compile(r"^/itm/", re.IGNORECASE),                                  
    re.compile(r"^/listing/\d+", re.IGNORECASE),                           
    re.compile(r"^/ip/", re.IGNORECASE),                                   
    re.compile(r"^/product-detail/", re.IGNORECASE),                       
)
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "srsltid"}

class WebSearchProviderUnavailable(RuntimeError):
    """Výnimka pre webového poskytovateľa, ktorý nie je dostupný alebo nakonfigurovaný."""

def _rank_domain(url: str) -> tuple[int, str]:
    """Vráti prioritu domény pri radení webových výsledkov."""
    domain = urlsplit(url).netloc.lower()
    if domain in PATENT_EVIDENCE_DOMAINS or domain.endswith(".patents.google.com"):
        return (3, domain)
    if domain.endswith(".gov") or domain.endswith(".edu"):
        return (0, domain)
    if any(token in domain for token in ("docs.", "developer.", "support.", "github.io")):
        return (1, domain)
    return (2, domain)


def _extract_main_text(html: str) -> tuple[str, str]:
    """Získa hlavný text a najlepší dostupný dátum z HTML stránky."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("nav, header, footer, aside, script, style, form, noscript, .ads, .cookie-banner"):
        node.decompose()
    date = ""
    for selector in ("meta[property='article:published_time']", "meta[name='pubdate']", "time[datetime]"):
        node = soup.select_one(selector)
        if node:
            date = (node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)).strip()
            break
    main = soup.select_one("main, article, [role='main']") or soup
    lines = []
    for raw in main.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line.split()) < 6:
            continue
        if re.match(r"^(Menu|Skip|Cart|Sign in|Subscribe)\b", line, flags=re.IGNORECASE):
            continue
        lines.append(line)
    return date or "Unknown", "\n".join(lines)

def _query_terms(query: str) -> set[str]:
    """Získa významové slová z dotazu pre webové filtrovanie."""
    stopwords = {
        "about",
        "after",
        "before",
        "from",
        "have",
        "into",
        "that",
        "their",
        "this",
        "with",
        "using",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9-]{3,}", (query or "").lower())
        if token not in stopwords
    }

def _compact_content(text: str, query: str = "", max_words: int = FETCH_CONTENT_WORDS) -> str:
    """Skráti obsah stránky na vety najrelevantnejšie k dotazu."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    terms = _query_terms(query)
    if not terms:
        return trim_words(text, max_words)
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]
    scored = []
    for index, sentence in enumerate(sentences):
        lower = sentence.lower()
        score = sum(1 for term in terms if term in lower)
        if score:
            scored.append((score, index, sentence))
    if not scored:
        return trim_words(text, max_words)
    scored.sort(key=lambda row: (-row[0], row[1]))
    selected_indexes = sorted({index for _score, index, _sentence in scored[:8]})
    excerpt = " ".join(sentences[index] for index in selected_indexes)
    return trim_words(excerpt, max_words)

def _clean_jina_content(text: str) -> str:
    """Odstráni navigačný šum z textu získaného cez Jina reader."""
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if re.match(r"^\* \[", line):
            continue
        if re.match(r"^(Skip to content|Log in|Cart|View cart|Check out|Continue shopping)\b", line, flags=re.I):
            continue
        lines.append(line)
    return "\n".join(lines)

def _is_error_document(text: str) -> bool:
    """Zistí, či načítaný text vyzerá ako chybová stránka."""
    compact = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if not compact:
        return False
    return bool(re.search(r"\b(?:403 forbidden|404 not found|target url returned error 403|target url returned error 404)\b", compact))

def _has_meaningful_content(text: str) -> bool:
    """Overí, či text obsahuje aspoň minimálny počet slov."""
    return len((text or "").split()) >= MIN_EXTRACTED_WORDS

def _is_access_limited(text: str) -> bool:
    """Zistí, či stránka naznačuje captcha alebo blokovanie prístupu."""
    return bool(re.search(r"captcha|access denied|forbidden|temporarily blocked|automated queries", text or "", re.I))

def _is_low_value_url(url: str) -> bool:
    """Zistí, či adresa smeruje na vyhľadávaciu alebo málo užitočnú stránku."""
    parsed = urlsplit(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if domain in SEARCH_PAGE_DOMAINS and path.startswith("/search"):
        return True
    if domain.endswith("google.com") and path.startswith("/search"):
        return True
    if re.search(r"/(?:search|results)(?:/|$)", path):
        return True
    is_marketplace = any(domain == blocked or domain.endswith(f".{blocked}") for blocked in MARKETPLACE_DOMAINS)
    if is_marketplace:
        if any(pattern.search(parsed.path) for pattern in MARKETPLACE_PRODUCT_PATHS):
            return False
        return True
    if path.rstrip("/").endswith("/s") and query_keys & {"k", "q", "query", "search"}:
        return True
    return False

def _canonical_source_url(url: str) -> str:
    """Odstráni sledovacie parametre dotazu zo zdrojovej URL adresy."""
    parsed = urlsplit(url.strip().rstrip(".,;"))
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith(TRACKING_QUERY_PREFIXES)
    ]
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(filtered_query, doseq=True),
        "",
    ))

def _query_overlap_match(query: str, text: str) -> bool:
    """Overí, či text obsahuje dostatok výrazov z dotazu."""
    query_tokens = tokens(query)
    text_tokens = tokens(text)
    if not query_tokens or not text_tokens:
        return False
    overlap = query_tokens & text_tokens
    required = max(1, min(5, (len(query_tokens) + 1) // 2))
    return len(overlap) >= required

def _web_rerank_score(query: str, title: str, snippet: str) -> float:
    """Vypočíta lokálne skóre webového výsledku."""
    block = f"{title}\n{snippet}"
    return evidence_score(query, block, "WEB")

def _passes_web_rerank(query: str, title: str, snippet: str, min_score: float = MIN_WEB_RERANK_SCORE) -> bool:
    """Overí, či webový výsledok spĺňa internú hranicu relevancie."""
    block = f"{title}\n{snippet}"
    if not _query_overlap_match(query, block):
        return False
    return _web_rerank_score(query, title, snippet) >= min_score

def _format_fetch_result(url: str, date: str, content: str, status: str, query: str = "") -> str:
    """Vytvorí štandardný textový výstup pre úspešné načítanie webovej stránky."""
    compact = _compact_content(content, query=query)
    if not _has_meaningful_content(compact):
        return _unsupported_fetch("No meaningful text content was extracted from the page within the fetch budget.", url)
    return clean_output(
        f"SOURCE_URL: {url} was the page requested.\n"
        f"DATE: {date} was the best date found.\n"
        f"CONTENT: {compact}\n"
        f"STATUS: {status}\n"
        "EVIDENCE_LEVEL: FULLTEXT_VERIFIED"
    )

_MDPI_ISSN_TO_CODE: dict[str, str] = {
    "1424-8220": "s",            
    "2076-3417": "app",         
    "1996-1944": "ma",           
    "2079-9292": "electronics",
    "2227-9032": "healthcare",
    "2073-4360": "polymers",
    "2306-5354": "bioengineering",
    "2306-5729": "data",
    "2076-2615": "ani",          
    "2079-7737": "biology",
    "2304-8158": "foods",
    "2071-1050": "su",           
    "2073-4344": "catalysts",
    "1660-4601": "ijerph",
}
_MDPI_URL_RE = re.compile(
    r"https?://(?:www\.)?mdpi\.com/(?P<issn>\d{4}-\d{3}[\dX])/(?P<vol>\d+)/(?P<issue>\d+)/(?P<article>\d+)",
    flags=re.IGNORECASE,
)

def _mdpi_url_to_doi(url: str) -> str:
    """Pokúsi sa previesť URL adresu vedeckého článku na DOI."""
    match = _MDPI_URL_RE.search(url or "")
    if not match:
        return ""
    code = _MDPI_ISSN_TO_CODE.get(match.group("issn"))
    if not code:
        return ""
    vol = int(match.group("vol"))
    issue = int(match.group("issue"))
    article = int(match.group("article"))
    return f"10.3390/{code}{vol:02d}{issue:02d}{article:04d}"

async def _crossref_metadata_fallback(doi: str, timeout_ms: int) -> str:
    """Získa názov a abstrakt z Crossref podľa DOI ako záložný zdroj."""
    if not doi:
        return ""
    timeout_s = max(3.0, min(8.0, timeout_ms / 2000))
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=timeout_s
        ) as client:
            response = await client.get(f"https://api.crossref.org/works/{doi}")
            if response.status_code >= 400:
                return ""
            payload = response.json() if hasattr(response, "json") else {}
    except Exception:
        return ""
    message = (payload or {}).get("message") or {}
    title_values = message.get("title") or []
    title = str(title_values[0]) if title_values else ""
    abstract = re.sub(r"<[^>]+>", " ", str(message.get("abstract") or ""))
    abstract = re.sub(r"\s+", " ", abstract).strip()
    if not (title or abstract):
        return ""
    return f"{title}\n\n{abstract}".strip()

async def _wayback_snapshot_text(url: str, timeout_ms: int) -> str:
    """Pokúsi sa načítať text stránky z najbližšej dostupnej archivovanej snímky."""
    avail_timeout = max(2.0, min(5.0, timeout_ms / 4000))
    snapshot_url = ""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=avail_timeout
        ) as client:
            response = await client.get(
                "https://archive.org/wayback/available", params={"url": url}
            )
            payload = response.json() if hasattr(response, "json") else {}
            closest = ((payload or {}).get("archived_snapshots") or {}).get("closest") or {}
            if not closest:
                return ""
            available = closest.get("available")
            if available is False or (isinstance(available, str) and available.lower() == "false"):
                return ""
            snapshot_url = str(closest.get("url") or "").strip()
            status = str(closest.get("status") or "200").strip()
            if not snapshot_url:
                return ""
            if status and not status.startswith("2"):
                return ""
    except Exception:
        return ""

    fetch_timeout = max(3.0, min(8.0, timeout_ms / 2000))
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=fetch_timeout
        ) as client:
            response = await client.get(snapshot_url)
            if response.status_code >= 400:
                return ""
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if content_type and not any(
                content_type.startswith(allowed) for allowed in TEXT_CONTENT_TYPES
            ):
                return ""
            if _looks_like_binary(response.text):
                return ""
            return response.text
    except Exception:
        return ""

async def _fetch_with_budget(url: str, timeout_ms: int, query: str = "") -> str:
    """Načíta webovú stránku viacerými záložnými spôsobmi v rámci časového limitu."""
    if _is_low_value_url(url):
        return _unsupported_fetch("URL is a low-value search or listing page and was not fetched.", url)
    html = ""
    fail_reason = "No meaningful text content was extracted from the page within the fetch budget."
    http_timeout = max(2.0, min(5.0, timeout_ms / 4000))
    saw_403 = False
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=http_timeout
        ) as client:
            response = await client.get(url)
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            final_url = str(getattr(response, "url", ""))
            if response.status_code == 403:
                saw_403 = True
                fail_reason = "HTTP 403 while fetching page."
            elif response.status_code >= 400:
                return _unsupported_fetch(f"HTTP {response.status_code} while fetching page.", url)
            elif re.search(r"(?:^|/)(?:error)?404(?:\.|/|$)|not[-_]?found", final_url, flags=re.IGNORECASE):
                return _unsupported_fetch(f"Final URL appears to be an error page: {final_url}.", url)
            elif content_type and not any(content_type.startswith(allowed) for allowed in TEXT_CONTENT_TYPES):
                return _unsupported_fetch(f"Unsupported non-text content type {content_type}.", url)
            elif _looks_like_binary(response.text):
                return _unsupported_fetch("Fetched content appears to be binary or undecodable text.", url)
            elif not _is_access_limited(response.text):
                date, content = _extract_main_text(response.text)
                if _has_meaningful_content(content):
                    return _format_fetch_result(url, date, content, "OK", query=query)
                html = response.text
    except Exception as exc:
        fail_reason = f"Live fetch failed: {exc}."

    if saw_403:
        doi = _mdpi_url_to_doi(url)
        if doi:
            metadata = await _crossref_metadata_fallback(doi, timeout_ms)
            if metadata:
                return _format_fetch_result(url, "Unknown", metadata, "CROSSREF_METADATA", query=query)

    jina_timeout = max(2.0, min(5.0, timeout_ms / 4000))
    try:
        content = _clean_jina_content(await fetch_via_jina(url, timeout_s=jina_timeout))
        if _is_error_document(content):
            return _unsupported_fetch("Fallback reader returned an error document instead of source content.", url)
        if _has_meaningful_content(content) and not _looks_like_binary(content):
            return _format_fetch_result(url, "Unknown", content, "JINA_FALLBACK", query=query)
    except Exception:
        pass

    chromium_timeout_ms = max(3000, min(6000, timeout_ms // 3))
    try:
        html = await fetch_page_html(url, timeout_ms=chromium_timeout_ms)
    except Exception:
        html = html or ""

    if html:
        date, content = _extract_main_text(html)
        if _has_meaningful_content(content):
            return _format_fetch_result(url, date, content, "OK", query=query)

    snapshot_html = await _wayback_snapshot_text(url, timeout_ms)
    if snapshot_html:
        date, content = _extract_main_text(snapshot_html)
        if _has_meaningful_content(content):
            return _format_fetch_result(url, date, content, "WAYBACK_FALLBACK", query=query)

    return _unsupported_fetch(fail_reason, url)

def _google_cse_credentials() -> tuple[str, str]:
    """Načíta prihlasovacie údaje pre Google Custom Search z premenných prostredia."""
    api_key = (
        os.getenv("GOOGLE_CSE_API_KEY")
        or os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY")
        or os.getenv("CUSTOM_SEARCH_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )
    search_engine_id = (
        os.getenv("GOOGLE_CSE_ID")
        or os.getenv("GOOGLE_CSE_CX")
        or os.getenv("GOOGLE_CUSTOM_SEARCH_ENGINE_ID")
        or os.getenv("GOOGLE_CUSTOM_SEARCH_CX")
        or os.getenv("CUSTOM_SEARCH_ENGINE_ID")
        or os.getenv("GOOGLE_CX")
    )
    if not api_key or not search_engine_id:
        raise WebSearchProviderUnavailable(
            "Google Custom Search is not configured; set GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID."
        )
    return api_key, search_engine_id

def _extract_snippet(value: object) -> str:
    """Zjednotí úryvok výsledku vyhľadávania na čistý text."""
    if isinstance(value, str) and value.strip():
        return re.sub(r"\s+", " ", value).strip()
    return "No snippet was returned."

def _keep_web_result(url: str, query: str) -> bool:
    """Rozhodne, či sa webový výsledok môže ponechať na ďalšie spracovanie."""
    domain = urlsplit(url).netloc.lower()
    if not domain:
        return False
    if _is_low_value_url(url):
        return False
    if any(domain.endswith(blocked) for blocked in BLOCKED_WEB_DOMAINS):
        return False
    if domain in PATENT_EVIDENCE_DOMAINS and "patent" not in query.lower():
        return False
    return True

def _raise_for_search_status(response: httpx.Response, provider_name: str) -> None:
    """Vyhodí chybu pri neúspešnej alebo neúplnej odpovedi vyhľadávača."""
    if response.status_code == 202:
        raise RuntimeError(f"{provider_name} returned 202 Accepted; search retrieval is incomplete.")
    response.raise_for_status()

async def _google_cse_query(client: httpx.AsyncClient, query: str, limit: int) -> list[tuple[str, str, str]]:
    """Vyhľadá webové výsledky cez Google Custom Search."""
    api_key, search_engine_id = _google_cse_credentials()
    response = await client.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": api_key,
            "cx": search_engine_id,
            "q": query,
            "num": max(1, min(limit, 10)),
        },
    )
    _raise_for_search_status(response, "Google Custom Search")
    payload = response.json()
    results = []
    for item in payload.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        url = _canonical_source_url(str(item.get("link") or ""))
        if not url or not _keep_web_result(url, query):
            continue
        results.append((
            url,
            str(item.get("title") or "Untitled result").strip(),
            _extract_snippet(item.get("snippet")),
        ))
    return results

async def _tavily_web_query(client: httpx.AsyncClient, query: str, limit: int) -> list[tuple[str, str, str]]:
    """Vyhľadá webové výsledky cez Tavily."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise WebSearchProviderUnavailable("Tavily is not configured; set TAVILY_API_KEY.")
    response = await client.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max(1, min(limit, 10)),
            "include_answer": False,
            "include_raw_content": False,
        },
    )
    _raise_for_search_status(response, "Tavily")
    payload = response.json()
    results = []
    for item in payload.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        url = _canonical_source_url(str(item.get("url") or ""))
        if not url or not _keep_web_result(url, query):
            continue
        results.append((
            url,
            str(item.get("title") or "Untitled result").strip(),
            _extract_snippet(item.get("content") or item.get("snippet")),
        ))
    return results

async def _exa_web_query(client: httpx.AsyncClient, query: str, limit: int) -> list[tuple[str, str, str]]:
    """Vyhľadá webové výsledky cez Exa."""
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        raise WebSearchProviderUnavailable("Exa is not configured; set EXA_API_KEY.")
    response = await client.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key},
        json={
            "query": query,
            "type": "auto",
            "numResults": max(1, min(limit, 20)),
            "contents": {"text": True},
        },
    )
    _raise_for_search_status(response, "Exa")
    payload = response.json()
    results = []
    for item in payload.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        url = _canonical_source_url(str(item.get("url") or ""))
        if not url or not _keep_web_result(url, query):
            continue
        title = str(item.get("title") or "Untitled result").strip()
        snippet = item.get("text") or item.get("summary") or item.get("snippet")
        results.append((url, title, _extract_snippet(snippet)))
    return results

SearchProvider = Callable[[httpx.AsyncClient, str, int], Awaitable[list[tuple[str, str, str]]]]

SEARCH_PROVIDERS: tuple[tuple[str, SearchProvider], ...] = (
    ("google_custom_search", _google_cse_query),
    ("tavily", _tavily_web_query),
    ("exa", _exa_web_query),
)

def _looks_like_binary(text: str) -> bool:
    """Zistí, či načítaný obsah vyzerá ako binárny alebo poškodený text."""
    sample = text[:2000]
    if not sample:
        return False
    controls = sum(1 for char in sample if ord(char) < 32 and char not in "\n\r\t")
    replacement = sample.count("\ufffd")
    return (controls + replacement) / max(len(sample), 1) > 0.02

def _canonical_fetch_url(url: str) -> str:
    """Prevedie niektoré priame súborové odkazy na vhodnejšiu stránku zdroja."""
    match = re.search(r"https://patents\.google\.com/patent/([A-Z]{2}\d+[A-Z0-9]*)\.pdf\b", url, flags=re.IGNORECASE)
    if match:
        return f"https://patents.google.com/patent/{match.group(1).upper()}/en"
    return url

def _unsupported_fetch(reason: str, url: str) -> str:
    """Vytvorí štandardný chybový výstup pre nepodporené načítanie stránky."""
    return "\n".join([
        f"TOOL_ERROR: web_fetch",
        f"REASON: {reason}",
        f"URL: {url}",
        "STATUS: FAILED",
        "EVIDENCE_LEVEL: FETCH_FAILED",
    ])

async def web_search(query: Any, max_results: int = 5) -> str:
    """Vyhľadá webové výsledky a vráti očistené záznamy."""
    try:
        from .query_normalize import coerce_query_input
        if not isinstance(query, str):
            query = coerce_query_input(query)
        if not (query or "").strip():
            return prepend_markers(
                NormalizedResult(
                    status="failed",
                    completed=False,
                    reliable_no_results=False,
                    errors=[{"type": "web_search_missing_query", "message": "query is required"}],
                    notes=["A malformed tool call is not evidence that no relevant web sources exist."],
                ),
                "TOOL_ERROR: web_search\nREASON: query is required.\nWeb search did not run; no reliable negative web conclusion can be made.",
            )
        search_queries = section_query_variants(query, "WEB_QUERIES", max_variants=2, legacy_marker="web search queries")
        relevance_query = " ".join(search_queries)
        max_provider_results = max(SEARCH_CANDIDATE_POOL_SIZE, max_results * 3, 5)
        provider_errors: list[dict[str, str]] = []
        provider_notes: list[str] = []
        provider_completed_without_hits: list[str] = []

        merged_results: dict[str, tuple[str, str, str, float]] = {}
        merged_failures: list[Exception] = []
        completed_provider_names: list[str] = []

        async def _run_web_provider_variant(
            provider_name: str, provider: SearchProvider, variant: str, client: httpx.AsyncClient,
        ) -> tuple[str, str, str, object]:
            """Spustí jedného webového poskytovateľa pre jeden variant dotazu."""
            try:
                results = await provider(client, variant, max_provider_results)
                return ("ok", provider_name, variant, results)
            except WebSearchProviderUnavailable as exc:
                return ("unavailable", provider_name, variant, exc)
            except Exception as exc:
                return ("error", provider_name, variant, exc)

        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20.0) as client:
            tasks: list = []
            for provider_name, provider in SEARCH_PROVIDERS:
                for variant in search_queries:
                    tasks.append(_run_web_provider_variant(provider_name, provider, variant, client))
            outcomes = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

            provider_status: dict[str, dict[str, list]] = {}
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    merged_failures.append(outcome)
                    continue
                status, provider_name, variant, payload = outcome
                slot = provider_status.setdefault(
                    provider_name, {"ok": 0, "unavailable_excs": [], "errors": []}
                )
                if status == "ok":
                    slot["ok"] += 1
                    for url, title, snippet in payload:
                        if url in merged_results:
                            continue
                        score = _web_rerank_score(relevance_query, title, snippet)
                        if not _passes_web_rerank(relevance_query, title, snippet):
                            continue
                        if not is_relevant(relevance_query, f"{title}\n{snippet}", "WEB", threshold=MIN_WEB_RERANK_SCORE):
                            continue
                        merged_results[url] = (url, title, snippet, score)
                elif status == "unavailable":
                    slot["unavailable_excs"].append(payload)
                elif status == "error":
                    slot["errors"].append(payload)
                    merged_failures.append(payload)
                    provider_errors.append(
                        {"type": f"{provider_name}_web_search_failure", "message": str(payload)}
                    )

            for provider_name, _provider in SEARCH_PROVIDERS:
                slot = provider_status.get(provider_name)
                if not slot:
                    continue
                if slot["ok"] > 0:
                    completed_provider_names.append(provider_name)
                elif slot["unavailable_excs"] and not slot["errors"]:
                    provider_notes.append(f"{provider_name} unavailable: {slot['unavailable_excs'][0]}")

        if merged_results:
            ranked = sorted(
                merged_results.values(),
                key=lambda row: (-row[3], _rank_domain(row[0])),
            )[: max(1, min(max_results, 10))]
            text = "\n\n".join(
                "\n".join(
                    [
                        f"Result title returned by search: **{title}**.",
                        f"Source page URL for this result: {url}",
                        f"Local rerank score: {score}/10.",
                        f"Short summary snippet from search: {trim_words(snippet, 30)}",
                    ]
                )
                for url, title, snippet, score in ranked
            )
            status = "partial_failure" if provider_errors else "ok"
            completed = not provider_errors
            provider_label = (
                "+".join(completed_provider_names) if len(completed_provider_names) > 1
                else (completed_provider_names[0] if completed_provider_names else "mixed")
            )
            body = f"WEB_SEARCH_PROVIDER: {provider_label}\n\n{text}"
            return prepend_markers(
                NormalizedResult(
                    status=status,
                    completed=completed,
                    reliable_no_results=False,
                    query=relevance_query,
                    hits=[url for url, _title, _snippet, _score in ranked],
                    errors=provider_errors,
                    notes=provider_notes + (
                        [f"Merged hits from web providers: {', '.join(completed_provider_names) or 'none'}."]
                        if completed_provider_names else []
                    ),
                ),
                clean_output(body),
            )
        if completed_provider_names and not provider_errors:
            provider_completed_without_hits.extend(completed_provider_names)
        if completed_provider_names and provider_errors:
            return prepend_markers(
                NormalizedResult(
                    status="partial_failure",
                    completed=False,
                    reliable_no_results=False,
                    query=relevance_query,
                    errors=provider_errors,
                    notes=[
                        *provider_notes,
                        f"Completed providers without hits: {', '.join(completed_provider_names)}.",
                        "Absence of relevant web hits is not reliable negative evidence.",
                    ],
                ),
                "Web search incomplete; no reliable negative web conclusion can be made.",
            )

        if provider_completed_without_hits and not provider_errors:
            return prepend_markers(
                NormalizedResult(
                    status="ok",
                    completed=True,
                    reliable_no_results=True,
                    query=relevance_query,
                    notes=[*provider_notes, f"Completed providers: {', '.join(provider_completed_without_hits)}."],
                ),
                "No clearly relevant web results matched the requested query.",
            )
        return prepend_markers(
            NormalizedResult(
                status="failed" if provider_errors else "partial_failure",
                completed=False,
                reliable_no_results=False,
                query=relevance_query,
                errors=provider_errors or [{"type": "web_search_unavailable", "message": "No API-backed web search provider is configured."}],
                notes=[
                    *provider_notes,
                    "Configured web retrieval failed or no API-backed provider was available; do not treat this as no-results evidence.",
                ],
            ),
            "TOOL_ERROR: web_search\nREASON: API-backed web retrieval did not complete.\nWeb search incomplete; no reliable negative web conclusion can be made.",
        )
    except Exception as exc:
        return format_error("web_search", str(exc))

async def web_fetch(url: str, timeout_ms: int = FETCH_TOTAL_TIMEOUT_MS, query: str = "") -> str:
    """Načíta webovú stránku a vráti očistený textový obsah."""
    try:
        url = _canonical_fetch_url(url)
        budget_ms = max(3000, min(timeout_ms, FETCH_TOTAL_TIMEOUT_MS))
        try:
            return await asyncio.wait_for(_fetch_with_budget(url, budget_ms, query=query), timeout=budget_ms / 1000)
        except TimeoutError:
            return _unsupported_fetch(f"Timed out after {budget_ms} ms while fetching page.", url)
    except Exception as exc:
        return format_error("web_fetch", str(exc), url=url) + "\nSTATUS: FAILED\nEVIDENCE_LEVEL: FETCH_FAILED"