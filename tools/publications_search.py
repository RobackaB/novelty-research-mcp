"""Vyhľadávanie odborných publikácií vo viacerých zdrojoch."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Any

import httpx

from .arxiv_search import arxiv_search
from .output_cleaner import USER_AGENT, clean_output, format_error, trim_words
from .relevance import evidence_score, is_relevant
from .result_contract import NormalizedResult, prepend_markers
from .search_bounds import section_query_variants

FAILURE_TERMS = ("tool_error:", "429", "rate limit", "too many requests", "timed out", "timeout", "failed because")
PUBLICATION_CANDIDATE_POOL_SIZE = 15
MIN_PUBLICATION_RERANK_SCORE = 3.5
ABSTRACT_WORD_LIMIT = 60
ALPHA_CLI_TIMEOUT_S = 30.0
QUERY_FILLER_RE = re.compile(
    r"\b(?:does|do|a|an|the|for|of|to|is|are|in|on|by|with|as|at|or|and|"
    r"je|su|sú|sa|na|do|vo|ako|the|that|this)\b",
    flags=re.I,
)


def _publication_failure(reason: str) -> str:
    """Vytvorí odpoveď pri úplnom zlyhaní publikačného vyhľadávania."""
    return prepend_markers(
        NormalizedResult(
            status="failed",
            completed=False,
            reliable_no_results=False,
            errors=[{"type": "publication_search_failed", "message": reason}],
            notes=["Do not treat this as evidence that no relevant publications exist."],
        ),
        "\n".join([
            "TOOL_ERROR: publications_search",
            f"REASON: publication search failed or was incomplete: {reason}",
            "STATUS: INCOMPLETE_RETRIEVAL",
            "EVIDENCE: Do not treat this as evidence that no relevant publications exist.",
        ]),
    )


def _publication_partial(reason: str, body: str) -> str:
    """Vytvorí odpoveď pre neúplné publikačné výsledky."""
    return prepend_markers(
        NormalizedResult(
            status="partial_failure",
            completed=False,
            reliable_no_results=False,
            errors=[{"type": "publication_search_partial", "message": reason}],
            notes=["Fallback results are partial; do not treat missing publications as negative evidence."],
        ),
        body,
    )


def _looks_failed(text: str) -> bool:
    """Zistí, či text obsahuje známky chyby alebo limitu poskytovateľa."""
    lower = (text or "").lower()
    return any(term in lower for term in FAILURE_TERMS)


def _filter_publication_text(query: str, text: str, max_results: int) -> str:
    """Prefiltruje textové bloky publikácií podľa lokálneho skóre relevancie."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]
    ranked = [
        (evidence_score(query, block, "PUBLICATION"), block)
        for block in blocks
        if is_relevant(query, block, "PUBLICATION", threshold=MIN_PUBLICATION_RERANK_SCORE)
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    return "\n\n".join(block for _score, block in ranked[:max(1, min(max_results, 20))])


def _fallback_publication_queries(query: str, max_variants: int = 2) -> list[str]:
    """Vytvorí jednoduché záložné varianty publikačného dotazu."""
    normalized = (query or "").lower()
    compact = re.sub(r"[^a-z0-9+\- ]+", " ", normalized)
    compact = QUERY_FILLER_RE.sub(" ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return [query.strip()]
    variants = [compact]
    from .relevance import subject_anchors as _subject_anchors 
    subject_only = " ".join(
        token
        for token in compact.split()
        if token in _subject_anchors(set(compact.split()))
    ).strip()
    if subject_only and subject_only != compact and len(subject_only.split()) >= 2:
        variants.append(subject_only)
    return variants[:max(1, max_variants)]


def _alpha_executable() -> str:
    """Nájde cestu k AlphaXiv CLI nástroju, ak je dostupný."""
    configured = os.getenv("ALPHA_CLI_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("alpha")
    return found or ""


async def _run_alpha_command(query: str, mode: str = "semantic") -> str:
    """Spustí AlphaXiv CLI vyhľadávanie a vráti jeho textový výstup."""
    executable = _alpha_executable()
    if not executable:
        return ""
    process = await asyncio.create_subprocess_exec(
        executable,
        "search",
        "--mode",
        mode,
        query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=ALPHA_CLI_TIMEOUT_S)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return ""
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        return ""
    return output or error


def _iter_alpha_items(value: object) -> list[dict[str, object]]:
    """Nájde položky publikácií v rôznych tvaroch AlphaXiv výstupu."""
    if isinstance(value, list):
        items: list[dict[str, object]] = []
        for entry in value:
            items.extend(_iter_alpha_items(entry))
        return items
    if not isinstance(value, dict):
        return []
    for key in ("papers", "results", "data", "items"):
        nested = value.get(key)
        if isinstance(nested, (list, dict)):
            return _iter_alpha_items(nested)
    title = value.get("title") or value.get("paperTitle") or value.get("name")
    if title:
        return [value]
    return []


def _alpha_url(item: dict[str, object]) -> str:
    """Vytiahne URL adresu publikácie z položky AlphaXiv výstupu."""
    for key in ("url", "alphaUrl", "alphaXivUrl", "arxivUrl", "pdfUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    arxiv_id = str(item.get("arxivId") or item.get("arxiv_id") or item.get("id") or "").strip()
    if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", arxiv_id):
        return f"https://arxiv.org/abs/{arxiv_id}"
    return ""


def _format_alpha_item(item: dict[str, object], relevance_query: str) -> tuple[float, str] | None:
    """Prevedie jednu AlphaXiv položku na skórovaný textový blok."""
    title = str(item.get("title") or item.get("paperTitle") or item.get("name") or "").strip()
    if not title:
        return None
    abstract = str(item.get("abstract") or item.get("summary") or item.get("description") or "").strip()
    url = _alpha_url(item)
    year = str(item.get("year") or item.get("published") or item.get("publicationYear") or "Unknown")
    authors_value = item.get("authors") or item.get("author")
    if isinstance(authors_value, list):
        authors = ", ".join(str(author.get("name") if isinstance(author, dict) else author) for author in authors_value[:5])
    else:
        authors = str(authors_value or "Unknown")
    score_text = f"{title} {abstract}"
    score = evidence_score(relevance_query, score_text, "PUBLICATION")
    if not is_relevant(relevance_query, score_text, "PUBLICATION", threshold=MIN_PUBLICATION_RERANK_SCORE):
        return None
    lines = [
        f"Publication title returned by AlphaXiv: **{title}**.",
        f"Publication year returned by AlphaXiv: {year[:4] if year != 'Unknown' else year}.",
        f"Authors listed for this publication result: {authors or 'Unknown'}.",
    ]
    if abstract:
        lines.append(f"Abstract excerpt from this publication record: {trim_words(abstract, ABSTRACT_WORD_LIMIT)}")
    if url:
        lines.append(f"AlphaXiv URL for this publication: {url}.")
    lines.extend([
        f"Local rerank score for this publication: {score}/10.",
        "SOURCE: AlphaXiv",
    ])
    return score, "\n".join(lines)


def _alpha_text_blocks(output: str, relevance_query: str, max_results: int) -> list[tuple[float, str]]:
    """Spracuje textový AlphaXiv výstup na skórované bloky publikácií."""
    blocks: list[tuple[float, str]] = []
    for raw_block in [block.strip() for block in re.split(r"\n\s*\n", output or "") if block.strip()]:
        title_match = re.search(r"(?:^|\n)\s*(?:[-*]\s*)?(?:title|paper title|publication title)[^:\n]*:\s*\**(.+?)\**\.?\s*(?:\n|$)", raw_block, re.I)
        if not title_match:
            continue
        abstract_match = re.search(r"(?:abstract|summary)[^:\n]*:\s*(.+?)(?:\n(?:url|arxiv|doi|authors?|year|source)|$)", raw_block, re.I | re.S)
        url_match = re.search(r"https?://(?:www\.)?(?:alphaxiv\.org|arxiv\.org|doi\.org)/\S+", raw_block, re.I)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip(" *.:-")
        abstract = re.sub(r"\s+", " ", abstract_match.group(1)).strip() if abstract_match else raw_block
        score = evidence_score(relevance_query, f"{title} {abstract}", "PUBLICATION")
        if not is_relevant(relevance_query, f"{title} {abstract}", "PUBLICATION", threshold=MIN_PUBLICATION_RERANK_SCORE):
            continue
        lines = [
            f"Publication title returned by AlphaXiv: **{title}**.",
            f"Abstract excerpt from this publication record: {trim_words(abstract, ABSTRACT_WORD_LIMIT)}",
        ]
        if url_match:
            lines.append(f"AlphaXiv URL for this publication: {url_match.group(0).rstrip('.,;')}.")
        lines.extend([
            f"Local rerank score for this publication: {score}/10.",
            "SOURCE: AlphaXiv",
        ])
        blocks.append((score, "\n".join(lines)))
        if len(blocks) >= max_results:
            break
    return blocks


async def _alpha_search_blocks(search_queries: list[str], relevance_query: str, max_results: int, seen: set[str]) -> list[tuple[float, str]]:
    """Vyhľadá publikácie cez AlphaXiv a vráti neduplicitné skórované bloky."""
    blocks: list[tuple[float, str]] = []
    for search_query in search_queries:
        output = await _run_alpha_command(search_query, "semantic")
        if not output:
            continue
        parsed_blocks: list[tuple[float, str]] = []
        try:
            parsed = json.loads(output)
            for item in _iter_alpha_items(parsed):
                formatted = _format_alpha_item(item, relevance_query)
                if formatted:
                    parsed_blocks.append(formatted)
        except json.JSONDecodeError:
            parsed_blocks = _alpha_text_blocks(output, relevance_query, max_results)
        for score, block in parsed_blocks:
            key = block.lower()
            if key in seen:
                continue
            seen.add(key)
            blocks.append((score, block))
        if len(blocks) >= max_results:
            break
    return blocks


def _low_confidence_no_results_query(query: str, search_queries: list[str]) -> bool:
    """Určí, či je záver bez publikačných nálezov málo spoľahlivý."""
    text = f"{query} {' '.join(search_queries)}".lower()
    terms = re.findall(r"[a-z0-9][a-z0-9-]{2,}", text)
    return (
        bool(re.search(r"[^\x00-\x7f]", text))
        or len(set(terms)) >= 14
    )


async def _crossref_search(query: str, limit: int) -> list[tuple[str, str, str, str, str]]:
    """Vyhľadá publikácie v Crossref a vráti základné údaje s abstraktom."""
    params = {
        "query": query,
        "rows": min(max(1, limit) * 2, 20),
        "select": "DOI,title,abstract,author,published",
        "filter": "has-abstract:true",
    }
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        response = await client.get("https://api.crossref.org/works", params=params)
        response.raise_for_status()

    results: list[tuple[str, str, str, str, str]] = []
    for item in (response.json().get("message") or {}).get("items") or []:
        title_values = item.get("title") or []
        title = title_values[0] if title_values else "Untitled"
        abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if not abstract:
            continue
        authors = ", ".join(
            f"{author.get('given', '')} {author.get('family', '')}".strip()
            for author in (item.get("author") or [])[:5]
        )
        date_parts = (item.get("published") or {}).get("date-parts") or [[]]
        year = str(date_parts[0][0]) if date_parts and date_parts[0] else "Unknown"
        doi = item.get("DOI") or ""
        doi_url = f"https://doi.org/{doi}" if doi else "Not available"
        results.append((title, authors, year, abstract, doi_url))
    return results


def _crossref_block(title: str, authors: str, year: str, abstract: str, doi_url: str, score: float) -> str:
    """Vytvorí textový blok pre jeden výsledok z Crossref."""
    return "\n".join([
        f"Publication title returned by Crossref: **{title}**.",
        f"Publication year returned by Crossref: {year}.",
        f"Authors listed for this publication result: {authors or 'Unknown'}.",
        f"Abstract excerpt from this publication record: {trim_words(abstract, ABSTRACT_WORD_LIMIT)}",
        f"DOI URL constructed for this publication: {doi_url}.",
        f"Local rerank score for this publication: {score}/10.",
        "SOURCE: Crossref",
    ])


def _parse_pubmed_efetch_xml(xml_text: str) -> list[dict[str, str]]:
    """Načíta XML odpoveď z PubMedu a vytiahne z nej údaje o článkoch."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return []
    out: list[dict[str, str]] = []
    for article in root.findall(".//PubmedArticle"):
        title_node = article.find(".//Article/ArticleTitle")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        abstract_parts: list[str] = []
        for abstract_text in article.findall(".//Article/Abstract/AbstractText"):
            label = (abstract_text.get("Label") or "").strip()
            text = "".join(abstract_text.itertext()).strip()
            if not text:
                continue
            abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(abstract_parts).strip()
        year_node = article.find(".//Article/Journal/JournalIssue/PubDate/Year")
        if year_node is None:
            year_node = article.find(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
        year = ""
        if year_node is not None:
            year_text = "".join(year_node.itertext()).strip()
            year_match = re.search(r"\d{4}", year_text)
            year = year_match.group(0) if year_match else year_text
        pmid_node = article.find(".//PMID")
        if pmid_node is None:
            pmid_node = article.find(".//ArticleIdList/ArticleId[@IdType='pubmed']")
        pmid = "".join(pmid_node.itertext()).strip() if pmid_node is not None else ""
        doi = ""
        for article_id in article.findall(".//ArticleIdList/ArticleId"):
            if (article_id.get("IdType") or "").lower() == "doi":
                doi = "".join(article_id.itertext()).strip()
                break
        author_names: list[str] = []
        for author in article.findall(".//Article/AuthorList/Author"):
            last_name_node = author.find("LastName")
            first_name_node = author.find("ForeName")
            if first_name_node is None:
                first_name_node = author.find("Initials")
            last = last_name_node.text.strip() if last_name_node is not None and last_name_node.text else ""
            first = first_name_node.text.strip() if first_name_node is not None and first_name_node.text else ""
            full = f"{first} {last}".strip()
            if full:
                author_names.append(full)
            if len(author_names) >= 5:
                break
        if title:
            out.append(
                {
                    "title": title,
                    "abstract": abstract,
                    "year": year,
                    "doi": doi,
                    "pmid": pmid,
                    "authors": ", ".join(author_names),
                }
            )
    return out


def _pubmed_block(
    title: str,
    authors: str,
    year: str,
    abstract: str,
    doi: str,
    pmid: str,
    score: float,
) -> str:
    """Vytvorí textový blok pre jeden výsledok z PubMedu."""
    doi_url = f"https://doi.org/{doi}" if doi else "Not available"
    pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "Not available"
    return "\n".join(
        [
            f"Publication title returned by PubMed: **{title}**.",
            f"Publication year returned by PubMed: {year or 'Unknown'}.",
            f"Authors listed for this publication result: {authors or 'Unknown'}.",
            f"Abstract excerpt from this publication record: {trim_words(abstract, ABSTRACT_WORD_LIMIT)}",
            f"DOI URL constructed for this publication: {doi_url}.",
            f"PubMed URL for this publication: {pubmed_url}.",
            f"Local rerank score for this publication: {score}/10.",
            "SOURCE: PubMed",
        ]
    )


async def _pubmed_search_records(query: str, limit: int) -> list[dict[str, str]]:
    """Vyhľadá publikácie v PubMede a načíta ich detailné XML záznamy."""
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("PUBMED_API_KEY", "").strip()
    esearch_params: dict[str, str] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(min(max(1, limit) * 2, 20)),
        "sort": "relevance",
    }
    efetch_params: dict[str, str] = {
        "db": "pubmed",
        "retmode": "xml",
    }
    if api_key:
        esearch_params["api_key"] = api_key
        efetch_params["api_key"] = api_key
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=15.0
    ) as client:
        try:
            esearch_response = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=esearch_params,
            )
            esearch_response.raise_for_status()
            esearch_payload = esearch_response.json()
        except Exception:
            return []
        ids = (
            ((esearch_payload or {}).get("esearchresult") or {}).get("idlist") or []
        )
        ids = [str(pmid).strip() for pmid in ids if str(pmid).strip()]
        if not ids:
            return []
        efetch_params["id"] = ",".join(ids)
        try:
            efetch_response = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params=efetch_params,
            )
            efetch_response.raise_for_status()
        except Exception:
            return []
        return _parse_pubmed_efetch_xml(efetch_response.text)


async def _pubmed_blocks(
    search_queries: list[str],
    relevance_query: str,
    max_results: int,
    seen: set[str],
) -> list[tuple[float, str]]:
    """Vyberie relevantné PubMed záznamy a prevedie ich na textové bloky."""
    blocks: list[tuple[float, str]] = []
    for search_query in search_queries:
        records = await _pubmed_search_records(search_query, max_results)
        for record in records:
            title = record.get("title") or ""
            abstract = record.get("abstract") or ""
            if not title or not abstract:
                continue
            doi = record.get("doi") or ""
            pmid = record.get("pmid") or ""
            key = (
                f"doi:{doi.lower()}" if doi else (f"pmid:{pmid}" if pmid else f"title:{title.lower()}")
            )
            if key in seen:
                continue
            seen.add(key)
            score = evidence_score(relevance_query, f"{title} {abstract}", "PUBLICATION")
            if not is_relevant(
                relevance_query,
                f"{title} {abstract}",
                "PUBLICATION",
                threshold=MIN_PUBLICATION_RERANK_SCORE,
            ):
                continue
            blocks.append(
                (
                    score,
                    _pubmed_block(
                        title,
                        record.get("authors") or "",
                        record.get("year") or "",
                        abstract,
                        doi,
                        pmid,
                        score,
                    ),
                )
            )
        if len(blocks) >= max_results:
            break
    return blocks


async def _pubmed_blocks_safe(
    search_queries: list[str], relevance_query: str, max_results: int
) -> list[tuple[float, str]]:
    """Bezpečne spustí PubMed vyhľadávanie a pri chybe vráti prázdny zoznam."""
    try:
        return await _pubmed_blocks(search_queries, relevance_query, max_results, set())
    except Exception:
        return []


def _reconstruct_openalex_abstract(inverted: object) -> str:
    """Zrekonštruuje abstrakt z invertovaného indexu, ktorý vracia OpenAlex."""
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indices in inverted.items():
        if not isinstance(word, str):
            continue
        if not isinstance(indices, list):
            continue
        for index in indices:
            try:
                positions.append((int(index), word))
            except (TypeError, ValueError):
                continue
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _index, word in positions)


def _openalex_block(
    title: str,
    authors: str,
    year: str,
    abstract: str,
    doi_url: str,
    work_url: str,
    score: float,
) -> str:
    """Vytvorí textový blok pre jeden výsledok z OpenAlex."""
    return "\n".join(
        [
            f"Publication title returned by OpenAlex: **{title}**.",
            f"Publication year returned by OpenAlex: {year}.",
            f"Authors listed for this publication result: {authors or 'Unknown'}.",
            f"Abstract excerpt from this publication record: {trim_words(abstract, ABSTRACT_WORD_LIMIT)}",
            f"DOI URL constructed for this publication: {doi_url}.",
            f"OpenAlex URL for this publication: {work_url or 'Not available'}.",
            f"Local rerank score for this publication: {score}/10.",
            "SOURCE: OpenAlex",
        ]
    )


async def _openalex_search(query: str, limit: int) -> list[tuple[str, str, str, str, str, str]]:
    """Vyhľadá publikácie v OpenAlex a vráti základné údaje s abstraktom."""
    params = {
        "search": query,
        "per_page": min(max(1, limit) * 2, 25),
        "select": "id,title,publication_year,authors_count,authorships,doi,abstract_inverted_index",
        "mailto": "research-server@example.com",
    }
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        response = await client.get("https://api.openalex.org/works", params=params)
        response.raise_for_status()

    out: list[tuple[str, str, str, str, str, str]] = []
    for item in (response.json().get("results") or []):
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if not title:
            continue
        abstract = _reconstruct_openalex_abstract(item.get("abstract_inverted_index"))
        if not abstract:
            continue
        year = str(item.get("publication_year") or "Unknown")
        doi = str(item.get("doi") or "")
        if doi.startswith("https://doi.org/"):
            doi_url = doi
        elif doi:
            doi_url = f"https://doi.org/{doi.lstrip('/')}"
        else:
            doi_url = "Not available"
        work_url = str(item.get("id") or "")
        authors_list: list[str] = []
        for authorship in (item.get("authorships") or [])[:5]:
            if isinstance(authorship, dict):
                author = authorship.get("author") or {}
                name = str(author.get("display_name") or "").strip()
                if name:
                    authors_list.append(name)
        authors = ", ".join(authors_list)
        out.append((title, authors, year, abstract, doi_url, work_url))
    return out


async def _openalex_blocks(
    search_queries: list[str],
    relevance_query: str,
    max_results: int,
    seen: set[str],
) -> list[tuple[float, str]]:
    """Vyberie relevantné OpenAlex záznamy a prevedie ich na textové bloky."""
    blocks: list[tuple[float, str]] = []
    for search_query in search_queries:
        try:
            results = await _openalex_search(search_query, max_results)
        except Exception:
            continue
        for title, authors, year, abstract, doi_url, work_url in results:
            key = doi_url.lower() if doi_url != "Not available" else title.lower()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            score = evidence_score(relevance_query, f"{title} {abstract}", "PUBLICATION")
            if not is_relevant(
                relevance_query,
                f"{title} {abstract}",
                "PUBLICATION",
                threshold=MIN_PUBLICATION_RERANK_SCORE,
            ):
                continue
            blocks.append((score, _openalex_block(title, authors, year, abstract, doi_url, work_url, score)))
        if len(blocks) >= max_results:
            break
    return blocks


async def _openalex_blocks_safe(
    search_queries: list[str], relevance_query: str, max_results: int
) -> list[tuple[float, str]]:
    """Bezpečne spustí OpenAlex vyhľadávanie a pri chybe vráti prázdny zoznam."""
    try:
        return await _openalex_blocks(search_queries, relevance_query, max_results, set())
    except Exception:
        return []


async def _crossref_blocks(
    search_queries: list[str],
    relevance_query: str,
    max_results: int,
    seen: set[str],
) -> list[tuple[float, str]]:
    """Vyberie relevantné Crossref záznamy a prevedie ich na textové bloky."""
    blocks: list[tuple[float, str]] = []
    for search_query in search_queries:
        for title, authors, year, abstract, doi_url in await _crossref_search(search_query, max_results):
            key = doi_url.lower() if doi_url != "Not available" else title.lower()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            score = evidence_score(relevance_query, f"{title} {abstract}", "PUBLICATION")
            if not is_relevant(
                relevance_query,
                f"{title} {abstract}",
                "PUBLICATION",
                threshold=MIN_PUBLICATION_RERANK_SCORE,
            ):
                continue
            blocks.append((score, _crossref_block(title, authors, year, abstract, doi_url, score)))
        if len(blocks) >= max_results:
            break
    return blocks


_DOI_BLOCK_RE = re.compile(r"DOI URL[^:]*:\s*https?://doi\.org/(\S+)", flags=re.I)
_TITLE_BLOCK_RE = re.compile(r"Publication title[^:]*:\s*\*\*(.*?)\*\*\.?", flags=re.I | re.S)


_PROVIDER_QUALITY_MULTIPLIERS = {
    "semanticscholar": 1.0,
    "pubmed": 1.0,
    "openalex": 0.95,
    "crossref": 0.85,
    "alphaxiv": 0.80,
}
_PROVIDER_SOURCE_RE = re.compile(r"^SOURCE:\s*(.+?)\s*$", flags=re.IGNORECASE | re.MULTILINE)
_PUB_DOMINANCE_RATIO = 0.7


def _extract_block_provider(block: str) -> str:
    """Vytiahne názov poskytovateľa z textového bloku publikácie."""
    match = _PROVIDER_SOURCE_RE.search(block or "")
    if not match:
        return ""
    name = match.group(1).strip().lower()
    return re.sub(r"\s+", "", name)


def _publication_dedupe_keys(block: str) -> tuple[str, str]:
    """Vytvorí kľúče na deduplikáciu podľa DOI alebo názvu."""
    doi_match = _DOI_BLOCK_RE.search(block)
    doi_key = ""
    if doi_match:
        doi_key = doi_match.group(1).strip().rstrip(".,;").lower()
    title_match = _TITLE_BLOCK_RE.search(block)
    title_key = ""
    if title_match:
        normalized_title = re.sub(r"\s+", " ", title_match.group(1)).strip().lower()
        if normalized_title:
            title_key = normalized_title
    return doi_key, title_key


async def _semantic_scholar_blocks(
    client: httpx.AsyncClient,
    search_queries: list[str],
    relevance_query: str,
    max_results: int,
) -> tuple[list[tuple[float, str]], bool, bool]:
    """Vyhľadá publikácie v Semantic Scholar a vráti zoradené textové bloky."""
    blocks: list[tuple[float, str]] = []
    seen: set[str] = set()
    errored = False
    rate_limited = False
    for search_query in search_queries:
        params = {
            "query": search_query,
            "limit": max(PUBLICATION_CANDIDATE_POOL_SIZE, min(max_results * 3, 20)),
            "fields": "title,authors,year,abstract,externalIds,url",
        }
        try:
            response = await client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search", params=params
            )
        except httpx.HTTPError:
            errored = True
            continue
        if response.status_code == 429:
            rate_limited = True
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            errored = True
            continue
        for item in response.json().get("data", []):
            abstract = (item.get("abstract") or "").strip()
            if not abstract:
                continue
            doi = (item.get("externalIds") or {}).get("DOI")
            key = str(doi or item.get("url") or item.get("title") or "")
            if key and key.lower() in seen:
                continue
            if key:
                seen.add(key.lower())
            authors = ", ".join(author.get("name", "Unknown") for author in item.get("authors", []))
            doi_url = f"https://doi.org/{doi}" if doi else "Not available"
            block = "\n".join(
                [
                    f"Publication title returned by Semantic Scholar: **{item.get('title', 'Untitled')}**.",
                    f"Publication year returned by Semantic Scholar: {item.get('year', 'Unknown')}.",
                    f"Authors listed for this publication result: {authors or 'Unknown'}.",
                    f"Abstract excerpt from this publication record: {trim_words(abstract, 60)}",
                    f"DOI URL constructed for this publication: {doi_url}.",
                    f"Semantic Scholar URL for this publication: {item.get('url', 'Not available')}.",
                ]
            )
            score = evidence_score(relevance_query, block, "PUBLICATION")
            if not is_relevant(
                relevance_query, block, "PUBLICATION", threshold=MIN_PUBLICATION_RERANK_SCORE
            ):
                continue
            block = f"Local rerank score for this publication: {score}/10.\n{block}"
            blocks.append((score, block))
    return blocks, errored, rate_limited


async def _crossref_blocks_safe(
    search_queries: list[str], relevance_query: str, max_results: int
) -> list[tuple[float, str]]:
    """Bezpečne spustí Crossref vyhľadávanie a pri chybe vráti prázdny zoznam."""
    try:
        return await _crossref_blocks(search_queries, relevance_query, max_results, set())
    except Exception:
        return []


async def _alpha_blocks_safe(
    search_queries: list[str], relevance_query: str, max_results: int
) -> list[tuple[float, str]]:
    """Bezpečne spustí AlphaXiv vyhľadávanie a pri chybe vráti prázdny zoznam."""
    try:
        return await _alpha_search_blocks(search_queries, relevance_query, max_results, set())
    except Exception:
        return []


async def publications_search(
    query: Any, max_results: int = 5, english_query: str = ""
) -> str:
    """Vyhľadá odborné publikácie vo viacerých dostupných zdrojoch."""
    from .query_normalize import coerce_query_input
    if not isinstance(query, str):
        query = coerce_query_input(query)
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    try:
        english_query_text = (english_query or "").strip()
        if english_query_text:
            query = english_query_text
        search_queries = section_query_variants(query, "PUBLICATION_QUERIES", max_variants=2, legacy_marker="publication search queries")
        if search_queries == [(query or "").strip()]:
            search_queries = _fallback_publication_queries(query, max_variants=2)
        relevance_query = " ".join(search_queries)

        provider_errors: list[str] = []

        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0) as client:
            outcomes = await asyncio.gather(
                _semantic_scholar_blocks(client, search_queries, relevance_query, max_results),
                _crossref_blocks_safe(search_queries, relevance_query, max_results),
                _alpha_blocks_safe(search_queries, relevance_query, max_results),
                _pubmed_blocks_safe(search_queries, relevance_query, max_results),
                _openalex_blocks_safe(search_queries, relevance_query, max_results),
                return_exceptions=True,
            )

        s2_outcome, cr_outcome, ax_outcome, pubmed_outcome, openalex_outcome = outcomes
        s2_blocks: list[tuple[float, str]] = []
        primary_errored = False
        rate_limited = False
        if isinstance(s2_outcome, BaseException):
            primary_errored = True
            provider_errors.append(f"semantic_scholar: {s2_outcome}")
        else:
            s2_blocks, primary_errored, rate_limited = s2_outcome
            if primary_errored:
                provider_errors.append("semantic_scholar: HTTP error during one or more variants")

        cr_blocks: list[tuple[float, str]] = []
        if isinstance(cr_outcome, BaseException):
            provider_errors.append(f"crossref: {cr_outcome}")
        else:
            cr_blocks = cr_outcome

        ax_blocks: list[tuple[float, str]] = []
        if isinstance(ax_outcome, BaseException):
            provider_errors.append(f"alphaxiv: {ax_outcome}")
        else:
            ax_blocks = ax_outcome

        pubmed_blocks: list[tuple[float, str]] = []
        if isinstance(pubmed_outcome, BaseException):
            provider_errors.append(f"pubmed: {pubmed_outcome}")
        else:
            pubmed_blocks = pubmed_outcome

        openalex_blocks: list[tuple[float, str]] = []
        if isinstance(openalex_outcome, BaseException):
            provider_errors.append(f"openalex: {openalex_outcome}")
        else:
            openalex_blocks = openalex_outcome

        merged: list[tuple[float, str]] = []
        seen_dois: set[str] = set()
        seen_titles: set[str] = set()
        for block_list in (s2_blocks, cr_blocks, pubmed_blocks, openalex_blocks, ax_blocks):
            for score, block in block_list:
                doi_key, title_key = _publication_dedupe_keys(block)
                if doi_key and doi_key in seen_dois:
                    continue
                if title_key and title_key in seen_titles:
                    continue
                if doi_key:
                    seen_dois.add(doi_key)
                if title_key:
                    seen_titles.add(title_key)
                merged.append((score, block))

        def _sort_score(item: tuple[float, str]) -> float:
            """Upraví skóre výsledku podľa kvality poskytovateľa."""
            score, block = item
            provider = _extract_block_provider(block)
            multiplier = _PROVIDER_QUALITY_MULTIPLIERS.get(provider, 1.0)
            return float(score) * multiplier
        merged.sort(key=_sort_score, reverse=True)
        selected_blocks = merged[: max(1, min(max_results, 20))]
        text = "\n\n".join(block for _score, block in selected_blocks)
        provider_counts: dict[str, int] = {}
        for _score, block in selected_blocks:
            name = _extract_block_provider(block) or "unknown"
            provider_counts[name] = provider_counts.get(name, 0) + 1
        dominance_note = ""
        if selected_blocks:
            top_provider, top_count = max(provider_counts.items(), key=lambda kv: kv[1])
            if top_count / len(selected_blocks) > _PUB_DOMINANCE_RATIO:
                dominance_note = (
                    f"Provider dominance warning: '{top_provider}' supplied "
                    f"{top_count}/{len(selected_blocks)} selected blocks — other "
                    "providers may be rate-limited or degraded (fallback dominance)."
                )

        if text:
            partial = bool(provider_errors) or (rate_limited and not s2_blocks)
            status = "partial_failure" if partial else "ok"
            notes: list[str] = []
            if rate_limited and not s2_blocks:
                notes.append("Semantic Scholar returned 429; using fallback publication providers.")
            if provider_errors:
                notes.append(
                    "Some publication providers errored; merged hits from remaining providers: "
                    + "; ".join(provider_errors[:3])
                )
            if dominance_note:
                notes.append(dominance_note)
            if provider_counts:
                provenance = ", ".join(
                    f"{name}={count}" for name, count in sorted(provider_counts.items())
                )
                notes.append(f"Publication provider counts: {provenance}.")
            return prepend_markers(
                NormalizedResult(
                    status=status,
                    completed=not partial,
                    reliable_no_results=False,
                    query=relevance_query,
                    hits=[block for _score, block in selected_blocks],
                    notes=notes,
                ),
                clean_output(text),
            )

        if rate_limited:
            try:
                fallback = await arxiv_search(search_queries[0], max_results)
            except Exception:
                fallback = ""
            if _looks_failed(fallback):
                return _publication_failure(
                    "Semantic Scholar returned 429 and Crossref/AlphaXiv/ArXiv fallbacks also failed or were rate-limited."
                )
            filtered = _filter_publication_text(relevance_query, fallback, max_results) if fallback else ""
            if not filtered:
                return _publication_partial(
                    "Semantic Scholar returned 429 and Crossref/AlphaXiv/ArXiv fallbacks yielded no usable relevant records.",
                    "Publication search incomplete; Crossref/ArXiv fallbacks yielded no usable relevant records. Do not treat this as evidence that no relevant publications exist.",
                )
            return _publication_partial(
                "Semantic Scholar returned 429; ArXiv fallback supplied partial relevant records.",
                "Semantic Scholar rate limit was reached, so ArXiv fallback results are returned instead.\n"
                f"{filtered}",
            )

        if primary_errored or provider_errors:
            return _publication_partial(
                "All publication providers either errored or returned no usable records: "
                + "; ".join(provider_errors[:3]),
                "Publication search incomplete; one or more providers errored and no usable records were returned. Do not treat this as evidence that no relevant publications exist.",
            )
        if _low_confidence_no_results_query(query, search_queries):
            return _publication_partial(
                "Publication providers returned no hits for a low-confidence or multilingual query; alphaXiv may be unavailable.",
                "Publication search incomplete; no usable publication records were returned for the expanded query. Do not treat this as evidence that no relevant publications exist.",
            )
        return prepend_markers(
            NormalizedResult(status="ok", completed=True, reliable_no_results=True, query=relevance_query),
            "No clearly relevant publication results with abstracts matched the requested query.",
        )
    except Exception as exc:
        try:
            search_queries = section_query_variants(query, "PUBLICATION_QUERIES", max_variants=2, legacy_marker="publication search queries")
            if search_queries == [(query or "").strip()]:
                search_queries = _fallback_publication_queries(query, max_variants=2)
            relevance_query = " ".join(search_queries)
            try:
                crossref_blocks = await _crossref_blocks(search_queries, relevance_query, max_results, set())
            except Exception:
                crossref_blocks = []
            if crossref_blocks:
                crossref_blocks.sort(key=lambda item: item[0], reverse=True)
                selected_blocks = crossref_blocks[:max(1, min(max_results, 20))]
                return _publication_partial(
                    f"Semantic Scholar failed ({exc}); Crossref supplied partial relevant records.",
                    clean_output("\n\n".join(block for _score, block in selected_blocks)),
                )
            try:
                alpha_blocks = await _alpha_search_blocks(search_queries, relevance_query, max_results, set())
            except Exception:
                alpha_blocks = []
            if alpha_blocks:
                alpha_blocks.sort(key=lambda item: item[0], reverse=True)
                selected_blocks = alpha_blocks[:max(1, min(max_results, 20))]
                return _publication_partial(
                    f"Semantic Scholar failed ({exc}); AlphaXiv supplied partial relevant records.",
                    clean_output("\n\n".join(block for _score, block in selected_blocks)),
                )
            fallback = await arxiv_search(query, max_results)
            if _looks_failed(fallback):
                return _publication_failure(f"Semantic Scholar failed ({exc}) and Crossref/ArXiv fallbacks also failed or were rate-limited.")
            filtered = _filter_publication_text(query, fallback, max_results)
            if not filtered:
                if _looks_failed(str(exc)):
                    return _publication_failure(f"Semantic Scholar failed ({exc}) and Crossref/ArXiv fallbacks returned no usable relevant records.")
                return _publication_partial(
                    f"Semantic Scholar failed ({exc}) and Crossref/ArXiv fallbacks yielded no usable relevant records.",
                    "Publication search incomplete; Crossref/ArXiv fallbacks yielded no usable relevant records. Do not treat this as evidence that no relevant publications exist.",
                )
            return _publication_partial(
                f"Semantic Scholar failed ({exc}); ArXiv fallback supplied partial relevant records.",
                f"Semantic Scholar search failed because {exc}, so ArXiv fallback results are returned instead.\n"
                f"{filtered}",
            )
        except Exception:
            return format_error("publications_search", str(exc))
