"""Building the final answer from merged evidence."""

from __future__ import annotations

import ast
import json
from typing import Any

from .merge_evidence_pack import _find_balanced_json, _safe_parse

SUMMARY_DISPLAY_LIMIT = 280
WARNING_DISPLAY_LIMIT = 240


def _as_dict(value: Any) -> dict[str, Any]:
    """Return the value as a dict, or an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return the value as a list, or an empty list."""
    return value if isinstance(value, list) else []


def _decode_string_payload(text: str) -> Any:
    """Parse a text input that may contain nested structured data."""
    current: Any = text
    for _ in range(3):
        if not isinstance(current, str):
            return current
        trimmed = current.strip()
        if not trimmed:
            return ""
        parsed = _safe_parse(trimmed)
        if isinstance(parsed, dict) and "raw" in parsed:
            try:
                return ast.literal_eval(trimmed)
            except (ValueError, SyntaxError):
                return parsed
        if isinstance(parsed, str):
            current = parsed
            continue
        return parsed
    return current


def _unwrap_payload(value: Any) -> Any:
    """Unwrap the common Flowise or MCP wrappers and return their inner content."""
    if isinstance(value, str):
        decoded = _decode_string_payload(value)
        if isinstance(decoded, dict) and decoded.get("raw") == value:
            return value
        if decoded is not value:
            return _unwrap_payload(decoded)
        return value

    if isinstance(value, list):
        if len(value) == 1:
            return _unwrap_payload(value[0])
        text_parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        if text_parts:
            return "\n".join(text_parts)
        return value

    if not isinstance(value, dict):
        return value

    if value.get("source_type") == "merged_evidence_pack":
        return value

    if value.get("type") == "text" and isinstance(value.get("text"), str):
        return value["text"]

    content = value.get("content")
    if isinstance(content, list):
        return _unwrap_payload(content)

    for key in ("merged_pack", "MERGED_RESEARCH_PACK_JSON", "text", "result", "output"):
        nested = value.get(key)
        if nested is not None and nested is not value:
            return _unwrap_payload(nested)

    return value


def _parse_pack(value: Any) -> dict[str, Any]:
    """Parse the merged evidence pack out of wrappers or text input."""
    unwrapped = _unwrap_payload(value)
    parsed = _safe_parse(unwrapped)
    if isinstance(parsed, dict):
        if parsed.get("source_type") == "merged_evidence_pack":
            return parsed
        nested = _unwrap_payload(parsed)
        if nested is not parsed and nested != unwrapped:
            return _parse_pack(nested)
    if isinstance(unwrapped, str):
        label_index = unwrapped.lower().find("merged_research_pack_json:")
        json_text = _find_balanced_json(unwrapped, label_index if label_index >= 0 else 0)
        parsed_json = _safe_parse(json_text) if json_text else None
        if isinstance(parsed_json, dict):
            if parsed_json.get("source_type") == "merged_evidence_pack":
                return parsed_json
            nested = _unwrap_payload(parsed_json)
            if nested is not parsed_json and nested != unwrapped:
                return _parse_pack(nested)
    return {
        "source_type": "merged_evidence_pack",
        "query": "",
        "overall_status": "failed",
        "patents": _missing_source("patent"),
        "publications": _missing_source("publication"),
        "web": _missing_source("web"),
        "flags": {},
        "warnings": ["Merged pack was missing or malformed"],
        "errors": [{"type": "missing_or_malformed_merged_pack", "message": "Merged pack was missing or malformed"}],
    }


def normalize_final_response(value: Any) -> str:
    """Convert the final answer into plain text output."""
    unwrapped = _unwrap_payload(value)
    if isinstance(unwrapped, str):
        return unwrapped
    if unwrapped is None:
        return ""
    return json.dumps(unwrapped, ensure_ascii=False)


def _missing_source(source_type: str) -> dict[str, Any]:
    """Build an empty source block for a missing input."""
    return {
        "source_type": source_type,
        "status": "failed",
        "completed": False,
        "reliable_no_results": False,
        "hits": [],
        "errors": [{"type": "missing_input", "message": "No input received"}],
        "warnings": ["No input received"],
    }


def _one_line(text: Any, limit: int = 360) -> str:
    """Truncate a value to a single line of bounded length."""
    collapsed = " ".join(_clean_text_field(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _clean_text_field(text: Any) -> str:
    """Strip leftover serialised JSON content from a text field."""
    value = str(text or "")
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
        index = value.find(marker)
        if index > 0:
            value = value[:index]
            break
    return value.strip(" ,{}[]'\"\\")


def _coerce_hit_list(value: Any) -> list[Any]:
    """Convert a hits input into a list of hits where its shape allows."""
    if isinstance(value, list):
        return value
    parsed = _unwrap_payload(value)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, str):
        decoded = _decode_string_payload(parsed)
        if isinstance(decoded, list):
            return decoded
    return []


def _source_section(source: dict[str, Any], label: str, hits: list[dict[str, Any]]) -> list[str]:
    """Build the text section for one evidence source."""
    lines = [
        f"Status: `{source.get('status', 'failed')}`, completed: `{source.get('completed') is True}`, reliable_no_results: `{source.get('reliable_no_results') is True}`."
    ]
    if not hits:
        if source.get("status") == "ok" and source.get("completed") is True and source.get("reliable_no_results") is True:
            lines.append(f"No relevant {label.lower()} evidence was returned by the {label.lower()} search.")
        else:
            lines.append(f"No usable {label.lower()} hits are available from this pack; treat this as incomplete retrieval, not evidence of absence.")
        return lines

    for index, hit in enumerate(hits, start=1):
        hit_dict = _as_dict(hit)
        title = _one_line(hit_dict.get("title") or f"{label} hit {index}", 140)
        evidence = hit_dict.get("evidence_level", "unknown")
        verified = hit_dict.get("verified_url")
        relevance = hit_dict.get("relevance")
        patent_number = hit_dict.get("patent_number")
        summary = _one_line(hit_dict.get("summary"), SUMMARY_DISPLAY_LIMIT)
        descriptor = f"{index}. {title}"
        if patent_number:
            descriptor += f" ({patent_number})"
        descriptor += f" - evidence_level: `{evidence}`, verified_url: `{verified}`"
        if relevance:
            descriptor += f", relevance: `{relevance}`"
        lines.append(descriptor + ".")
        if summary:
            lines.append(f"   Summary: {summary}")
    return lines


def _valid_hits(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Select and clean the usable hits from a source block."""
    hits: list[dict[str, Any]] = []
    for hit in _coerce_hit_list(source.get("hits")):
        hit_dict = _as_dict(hit)
        if not hit_dict:
            continue
        title = _one_line(hit_dict.get("title"), 140)
        url = _one_line(hit_dict.get("url"), 220)
        summary = _one_line(hit_dict.get("summary"), 160)
        evidence = hit_dict.get("evidence_level")
        verified = hit_dict.get("verified_url")
        if not any([title, url, summary, evidence, verified is not None]):
            continue
        cleaned = dict(hit_dict)
        for key in ("title", "url", "patent_number", "evidence_level", "summary", "relevance", "doi"):
            if key in cleaned:
                cleaned[key] = _clean_text_field(cleaned[key])
        hits.append(cleaned)
    return hits


def _focused_publication_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter the publications down to those with focused relevance."""
    return [hit for hit in hits if str(hit.get("relevance") or "").lower() == "focused"]


def _warnings_errors(pack: dict[str, Any]) -> list[str]:
    """Summarise the warnings and errors from the merged pack."""
    warnings = [_one_line(item, WARNING_DISPLAY_LIMIT) for item in _as_list(pack.get("warnings")) if item]
    errors = [_one_line(item, WARNING_DISPLAY_LIMIT) for item in _as_list(pack.get("errors")) if item]
    lines: list[str] = []
    if warnings:
        lines.append("Warnings: " + "; ".join(warnings[:4]))
    if errors:
        lines.append("Errors: " + "; ".join(errors[:4]))
    return lines


def _direct_answer(pack: dict[str, Any], patent_hits: list[dict[str, Any]], publication_hits: list[dict[str, Any]], web_hits: list[dict[str, Any]]) -> str:
    """Build a brief conservative answer from the available hits."""
    status = pack.get("overall_status", "failed")
    if status == "failed":
        return (
            "The workflow did not retrieve usable merged evidence, so this pack cannot support a novelty or "
            "patentability conclusion."
        )
    focused_publications = _focused_publication_hits(publication_hits)
    if patent_hits or web_hits or focused_publications:
        return (
            "Based on the returned evidence, related prior art exists, but the exact full combination should be "
            "treated cautiously unless one returned source explicitly contains every required element."
        )
    if publication_hits:
        return (
            "The returned publication hits appear weak or adjacent to the product-specific concept, so they should not "
            "be treated as strong prior art without manual review."
        )
    return "No returned source type contains hits in this merged pack; this should not be broadened beyond the completed source searches."


def final_answer_pack(merged_pack: Any, original_query: str = "", draft_answer: str = "") -> str:
    """Build a conservative final answer from the merged evidence wrapper."""
    pack = _parse_pack(merged_pack)
    query = _one_line(original_query or pack.get("query"))
    patents = _as_dict(pack.get("patents")) or _missing_source("patent")
    publications = _as_dict(pack.get("publications")) or _missing_source("publication")
    web = _as_dict(pack.get("web")) or _missing_source("web")
    patent_hits = _valid_hits(patents)
    publication_hits = _valid_hits(publications)
    web_hits = _valid_hits(web)

    sections: list[tuple[str, list[str]]] = [
        (
            "## 1. Direct Answer",
            [
                _direct_answer(pack, patent_hits, publication_hits, web_hits),
                f"Original query: {query}" if query else "Original query: not present in merged pack.",
                f"Overall retrieval status: `{pack.get('overall_status', 'failed')}`.",
            ],
        ),
        ("## 2. Strongest Patent Evidence", _source_section(patents, "Patent", patent_hits)),
        ("## 3. Strongest Publication Evidence", _source_section(publications, "Publication", publication_hits)),
        ("## 4. Strongest Web Evidence", _source_section(web, "Web", web_hits)),
        (
            "## 5. Important Gaps Or Incomplete Retrieval",
            [
                "Do not treat `partial_failure` or `failed` source status as evidence that no prior art exists.",
                "Snippet-level evidence is weak and should not be read as full claim or abstract verification.",
                "A single-source anticipation conclusion requires one returned source summary to establish every required element of the claimed invention together in the same record.",
                "Publication hits marked `adjacent` or `generic` should not be treated as strong product-specific prior art.",
                *_warnings_errors(pack),
            ],
        ),
        (
            "## 6. Patentability Risk Assessment",
            [
                "This is a provisional prior-art risk read, not a legal opinion.",
                (
                    "Risk is elevated by returned patent/web hits, but remains uncertain because evidence levels, source statuses, and exact claim limitations must be reviewed."
                    if patent_hits or web_hits
                    else "Risk cannot be assessed from hits because this pack does not contain usable prior-art hits."
                ),
                (
                    "The publication search may be described as returning no relevant publications only because it is `ok`, completed, reliable_no_results=true, and has no hits."
                    if publications.get("status") == "ok" and publications.get("completed") is True and publications.get("reliable_no_results") is True and not _as_list(publications.get("hits"))
                    else "The publication result should be treated according to its status and not as broad absence of publication evidence."
                ),
            ],
        ),
        (
            "## 7. Sources",
            [
                f"Patent hits: {len(patent_hits)}.",
                f"Publication hits: {len(publication_hits)}.",
                f"Web hits: {len(web_hits)}.",
                "Source details are limited to the titles, URLs, summaries, evidence levels, and verification flags in the merged pack.",
            ],
        ),
    ]

    output: list[str] = []
    for title, lines in sections:
        output.append(title)
        output.extend(line for line in lines if line)
        output.append("")
    return "\n".join(output).strip()
