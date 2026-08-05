"""Pomocné vyhľadávanie odborných článkov v databáze arXiv."""

from __future__ import annotations

import asyncio
import re

import arxiv

from .output_cleaner import clean_output, format_error, trim_words
from .result_contract import NormalizedResult, prepend_markers


def _run_search(query: str, max_results: int) -> list[arxiv.Result]:
    """Vykoná samotné arXiv vyhľadanie a vráti surové záznamy článkov."""
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    return list(search.results())


async def arxiv_search(query: str, max_results: int = 5) -> str:
    """Vyhľadá články v arXiv a vráti stručné zhrnutia výsledkov."""
    try:
        results = await asyncio.to_thread(_run_search, query.strip(), max(1, min(max_results, 20)))
        if not results:
            return prepend_markers(
                NormalizedResult(status="ok", completed=True, reliable_no_results=True, query=query),
                "No ArXiv papers matched the requested query.",
            )
        blocks = []
        for paper in results:
            abstract = trim_words(re.sub(r"\s+", " ", paper.summary), 80)
            authors = ", ".join(author.name for author in paper.authors)
            blocks.append(
                "\n".join(
                    [
                        f"Paper title from ArXiv: **{paper.title.strip()}**.",
                        f"Authors listed on the paper record: {authors}.",
                        f"Publication year reported by ArXiv: {paper.published.year}.",
                        f"Paper landing page on ArXiv: {paper.entry_id}.",
                        f"Direct PDF download link for the paper: {paper.pdf_url}.",
                        f"Abstract excerpt from the paper record: {abstract}",
                    ]
                )
            )
        return prepend_markers(
            NormalizedResult(status="ok", completed=True, reliable_no_results=False, query=query, hits=blocks),
            clean_output("\n\n".join(blocks)),
        )
    except Exception as exc:
        return prepend_markers(
            NormalizedResult(
                status="failed",
                completed=False,
                reliable_no_results=False,
                query=query,
                errors=[{"type": "arxiv_search_failed", "message": str(exc)}],
                notes=["Do not treat this as evidence that no relevant ArXiv publications exist."],
            ),
            format_error("arxiv_search", str(exc)) + "\nSTATUS: FAILED\nEVIDENCE: Do not treat this as negative evidence.",
        )
