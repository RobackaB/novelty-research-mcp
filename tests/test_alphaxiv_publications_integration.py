"""Testy napojenia AlphaXiv MCP klienta do publikačného vyhľadávania."""

from __future__ import annotations

import tools.publications_search as ps


async def test_alpha_search_blocks_uses_dict_items(monkeypatch):
    async def fake_discover(query):
        return [
            {
                "title": "Smart Lock Study",
                "arxivId": "2301.00001",
                "publicationDate": "2023-01-15",
                "organizations": ["MIT"],
                "abstractPreview": "Smart door lock unlocked by a mobile application with access codes.",
            }
        ]

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_search_blocks(
        ["smart door lock mobile application access codes"],
        "smart door lock mobile application access codes",
        5,
        set(),
    )
    assert len(blocks) == 1
    score, block = blocks[0]
    assert score > 0
    assert "Smart Lock Study" in block
    assert "2023" in block
    assert "Organizations listed for this publication result: MIT." in block
    assert "https://arxiv.org/abs/2301.00001" in block
    assert "SOURCE: AlphaXiv" in block


async def test_alpha_search_blocks_uses_real_authors_label(monkeypatch):
    async def fake_discover(query):
        return [{"title": "Paper", "authors": ["Jane Doe"], "abstract": "smart lock mobile application access codes"}]

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_search_blocks(
        ["smart lock mobile application access codes"], "smart lock mobile application access codes", 5, set()
    )
    assert blocks
    _score, block = blocks[0]
    assert "Authors listed for this publication result: Jane Doe." in block


async def test_alpha_search_blocks_falls_back_to_text_parser(monkeypatch):
    async def fake_discover(query):
        return [
            "Title: Smart Lock Text Fallback\n"
            "Abstract: Smart door lock unlocked by a mobile application with access codes.\n"
            "https://arxiv.org/abs/2301.00099"
        ]

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_search_blocks(
        ["smart lock mobile application access codes"], "smart lock mobile application access codes", 5, set()
    )
    assert blocks
    _score, block = blocks[0]
    assert "Smart Lock Text Fallback" in block
    assert "arxiv.org/abs/2301.00099" in block


async def test_alpha_search_blocks_empty_when_no_key_or_no_results(monkeypatch):
    async def fake_discover(query):
        return []

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_search_blocks(["smart lock"], "smart lock", 5, set())
    assert blocks == []


async def test_alpha_search_blocks_deduplicates_across_queries(monkeypatch):
    call_count = {"n": 0}

    async def fake_discover(query):
        call_count["n"] += 1
        return [
            {
                "title": "Dup Paper",
                "arxivId": "2301.00001",
                "abstract": "smart lock mobile application access codes duplicate",
            }
        ]

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_search_blocks(
        ["smart lock mobile application access codes", "variant query mobile application access codes"],
        "smart lock mobile application access codes",
        1,
        set(),
    )
    assert call_count["n"] == 1  # max_results=1 sa dosiahne po prvom dotaze, druhý sa už nespustí
    assert len(blocks) == 1


async def test_alphaxiv_blocks_safe_swallows_exceptions(monkeypatch):
    async def fake_discover(query):
        raise RuntimeError("boom")

    monkeypatch.setattr(ps, "alphaxiv_discover_papers", fake_discover)
    blocks = await ps._alpha_blocks_safe(["smart lock"], "smart lock", 5)
    assert blocks == []
