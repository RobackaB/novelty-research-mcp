"""Spájanie patentových, publikačných a webových dôkazov do jedného wrapperu."""

from __future__ import annotations

import ast
import json
import re
import warnings
from typing import Any


SOURCE_TYPES = {"patent", "publication", "web"}
STATUSES = {"ok", "partial_failure", "failed"}
TEXT_TAIL_MARKERS = (
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
)


def _as_array(value: Any) -> list[Any]:
    """Vráti hodnotu ako zoznam alebo prázdny zoznam."""
    return value if isinstance(value, list) else []


def _safe_parse(value: Any) -> Any:
    """Bezpečne načíta JSON text alebo vráti surový obsah."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return {"raw": str(value)}

    trimmed = value.strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        repaired = _repair_json_payload_text(trimmed)
        if repaired != trimmed:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        return {"raw": trimmed}


def _repair_json_payload_text(text: str) -> str:
    """Opraví bežné chyby v JSON texte bez dopĺňania nových údajov."""
    repaired = text.strip()
    repaired = re.sub(r'",\\n(\s*")', r'",\n\1', repaired)
    repaired = re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", repaired)
    repaired = re.sub(
        r'("(?:verified_url|relevance)"\s*:\s*(?:"[^"]*"|true|false|null))\s*\]\s*,\s*("(?:errors|warnings)"\s*:)',
        r"\1}],\2",
        repaired,
    )
    return repaired


def _decode_string_payload(text: str) -> Any:
    """Načíta textový vstup, ktorý môže obsahovať vnorené štruktúrované dáta."""
    current: Any = text
    for _ in range(3):
        if not isinstance(current, str):
            return current
        trimmed = current.strip()
        if not trimmed:
            return ""
        parsed = _safe_parse(trimmed)
        if isinstance(parsed, dict) and parsed.get("raw") == trimmed:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    return ast.literal_eval(trimmed)
            except (ValueError, SyntaxError):
                return parsed
        if isinstance(parsed, str):
            current = parsed
            continue
        return parsed
    return current


def _unwrap_payload(value: Any, depth: int = 0) -> Any:
    """Rozbalí bežné Flowise alebo MCP wrappery a vráti ich vnútorný obsah."""
    if depth > 8:
        return value

    if isinstance(value, str):
        decoded = _decode_string_payload(value)
        if isinstance(decoded, dict) and decoded.get("raw") == value:
            return value
        if decoded is not value:
            return _unwrap_payload(decoded, depth + 1)
        return value

    if isinstance(value, list):
        if len(value) == 1:
            return _unwrap_payload(value[0], depth + 1)
        text_parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        if text_parts:
            return _unwrap_payload("\n".join(text_parts), depth + 1)
        for item in value:
            if isinstance(item, dict) and item.get("source_type") in SOURCE_TYPES:
                return item
        return value

    if not isinstance(value, dict):
        return value

    if value.get("source_type") in SOURCE_TYPES:
        return value

    if value.get("type") == "text" and isinstance(value.get("text"), str):
        return _unwrap_payload(value["text"], depth + 1)

    content = value.get("content")
    if content is not None:
        return _unwrap_payload(content, depth + 1)

    for key in ("PATENT_EVIDENCE_JSON", "PUBLICATION_EVIDENCE_JSON", "WEB_EVIDENCE_JSON", "text", "result", "output"):
        nested = value.get(key)
        if nested is not None and nested is not value:
            return _unwrap_payload(nested, depth + 1)

    return value


def _text_of(value: Any) -> str:
    """Prevedie vstupnú hodnotu na text vhodný na ďalšie spracovanie."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("raw"), str):
        return value["raw"]
    return json.dumps(value, ensure_ascii=False)


def _find_balanced_json(text: str, start_index: int) -> str | None:
    """Nájde v texte celý JSON objekt začínajúci na zadanom indexe."""
    first_brace = text.find("{", max(0, start_index))
    if first_brace < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(first_brace, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[first_brace : index + 1]

    return None


def _parse_json_after_label(bundle: str, labels: list[str]) -> Any:
    """Nájde a načíta JSON objekt, ktorý nasleduje za niektorým zo štítkov."""
    lower = bundle.lower()
    for label in labels:
        index = lower.find(label.lower())
        if index < 0:
            continue
        json_text = _find_balanced_json(bundle, index + len(label))
        parsed = _safe_parse(json_text) if json_text else None
        if parsed and not (isinstance(parsed, dict) and "raw" in parsed):
            return parsed
    return None


def _parse_all_json_objects(bundle: str) -> list[Any]:
    """Načíta všetky samostatné JSON objekty nájdené v texte."""
    objects: list[Any] = []
    cursor = 0
    while cursor < len(bundle):
        json_text = _find_balanced_json(bundle, cursor)
        if not json_text:
            break
        parsed = _safe_parse(json_text)
        if parsed and not (isinstance(parsed, dict) and "raw" in parsed):
            objects.append(parsed)
        cursor = bundle.find(json_text, cursor) + len(json_text)
    return objects


def _find_by_source_type(objects: list[Any], source_type: str) -> Any:
    """Nájde medzi kandidátmi objekt so zadaným typom zdroja."""
    for item in objects:
        unwrapped = _unwrap_payload(item)
        if isinstance(unwrapped, dict) and unwrapped.get("source_type") == source_type:
            return unwrapped
    return None


def _raw_mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    """Vyberie hodnotu zo slovníka bez ohľadu na veľkosť písmen v kľúči."""
    lower_map = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value is not None:
            return value
    return None


def _mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    """Vyberie hodnotu zo slovníka a rozbalí prípadný wrapper."""
    value = _raw_mapping_value(mapping, *keys)
    return _unwrap_payload(value) if value is not None else None


def _extract_embedded_source(value: Any, source_type: str) -> Any:
    """Pokúsi sa nájsť vnorený zdroj podľa typu v textovom alebo štruktúrovanom vstupe."""
    text = _text_of(value)
    if not text:
        return None

    patterns = (
        f'"source_type": "{source_type}"',
        f'"source_type":"{source_type}"',
        f'\\"source_type\\": \\"{source_type}\\"',
        f'\\"source_type\\":\\"{source_type}\\"',
        f"'source_type': '{source_type}'",
    )
    for pattern in patterns:
        cursor = 0
        while True:
            index = text.find(pattern, cursor)
            if index < 0:
                break
            start = text.rfind("{", 0, index)
            if start >= 0:
                json_text = _find_balanced_json(text, start)
                parsed = _safe_parse(json_text) if json_text else None
                unwrapped = _unwrap_payload(parsed)
                if _is_source_type(unwrapped, source_type):
                    return unwrapped
            cursor = index + len(pattern)
    return None


def _mapping_text(mapping: dict[str, Any], *keys: str) -> str:
    """Vyberie hodnotu zo slovníka a prevedie ju na text."""
    value = _mapping_value(mapping, *keys)
    return _text_of(value).strip()


def _missing_source(source_type: str) -> dict[str, Any]:
    """Vytvorí normalizovaný blok pre chýbajúci zdroj."""
    return {
        "source_type": source_type,
        "status": "failed",
        "completed": False,
        "reliable_no_results": False,
        "hits": [],
        "errors": [{"type": "missing_input", "message": "No input received"}],
        "warnings": ["No input received"],
    }


def _clean_text_field(value: Any) -> str:
    """Očistí textové pole od zvyškov vloženého JSON obsahu."""
    text = str(value or "")
    for marker in TEXT_TAIL_MARKERS:
        index = text.find(marker)
        if index > 0:
            text = text[:index]
            break
    return " ".join(text.strip(" ,{}[]'\"\\").split())


def _clean_url_field(value: Any) -> str:
    """Očistí URL pole a odstráni z neho medzery."""
    return re.sub(r"\s+", "", _clean_text_field(value))


def _repair_hit(hit: Any) -> dict[str, Any] | None:
    """Znormalizuje jeden nález zo zdrojových dát."""
    if not isinstance(hit, dict):
        return None

    repaired = dict(hit)
    summary = str(repaired.get("summary") or "")
    if repaired.get("verified_url") is None and re_search_verified_true(summary):
        repaired["verified_url"] = True
    for key in ("title", "url", "patent_number", "doi", "evidence_level", "summary", "relevance"):
        if key in repaired:
            repaired[key] = _clean_url_field(repaired[key]) if key == "url" else _clean_text_field(repaired[key])
    return repaired


def re_search_verified_true(text: str) -> bool:
    """Zistí, či zvyšný text obsahuje príznak verified_url=true."""
    compact = (text or "").replace("\\n", "\n").replace('\\"', '"').replace(" ", "").lower()
    return '"verified_url":true' in compact or "'verified_url':true" in compact


def _repair_hits(hits: Any) -> list[dict[str, Any]]:
    """Znormalizuje a odfiltruje zoznam nálezov zo zdrojových dát."""
    repaired: list[dict[str, Any]] = []
    for hit in _as_array(hits):
        repaired_hit = _repair_hit(hit)
        if repaired_hit:
            repaired.append(repaired_hit)
    return repaired


def _normalize_source(value: Any, source_type: str) -> dict[str, Any]:
    """Prevedie dáta jedného zdroja do spoločnej štruktúry."""
    if not isinstance(value, dict):
        return _missing_source(source_type)

    normalized = dict(value)
    normalized["source_type"] = source_type
    normalized["status"] = normalized.get("status") if normalized.get("status") in STATUSES else "failed"
    normalized["completed"] = normalized.get("completed") is True
    normalized["reliable_no_results"] = normalized.get("reliable_no_results") is True
    normalized["hits"] = _repair_hits(normalized.get("hits"))
    normalized["errors"] = _as_array(normalized.get("errors"))
    normalized["warnings"] = _as_array(normalized.get("warnings"))
    return normalized


def _is_source_type(value: Any, source_type: str) -> bool:
    """Zistí, či dáta patria požadovanému typu zdroja."""
    return isinstance(value, dict) and value.get("source_type") == source_type


def _overall_status(parts: list[dict[str, Any]]) -> str:
    """Určí výsledný stav zlúčeného wrapperu podľa dostupnosti zdrojov."""
    statuses = [part["status"] for part in parts]
    if all(status == "failed" for status in statuses):
        return "failed"
    if all(status == "ok" for status in statuses):
        return "complete"
    return "partial"


def _can_claim_no(part: dict[str, Any]) -> bool:
    """Zistí, či zdroj spoľahlivo tvrdí, že nenašiel relevantné výsledky."""
    return (
        part["status"] == "ok"
        and part["completed"] is True
        and part["reliable_no_results"] is True
        and len(part["hits"]) == 0
    )


def _query_after_label(bundle: str) -> str:
    """Vytiahne pôvodný dotaz zo staršieho textového wrapper formátu."""
    lower = bundle.lower()
    label = "original_query:"
    index = lower.find(label)
    if index < 0:
        return ""
    rest = bundle[index + len(label) :]
    next_labels = [
        "\npatent_evidence_json:",
        "\npublication_evidence_json:",
        "\nweb_evidence_json:",
        "\nmerged_research_pack_json:",
    ]
    end = len(rest)
    rest_lower = rest.lower()
    for next_label in next_labels:
        next_index = rest_lower.find(next_label)
        if next_index >= 0:
            end = min(end, next_index)
    return " ".join(rest[:end].strip().split())


def _source_arg(value: Any, source_type: str) -> Any:
    """Pripraví vstup jedného zdroja z priameho argumentu alebo vnoreného wrapperu."""
    parsed = _safe_parse(value) if isinstance(value, str) else value
    unwrapped = _unwrap_payload(parsed)
    if _is_source_type(unwrapped, source_type):
        return unwrapped
    return _extract_embedded_source(value, source_type) or _extract_embedded_source(unwrapped, source_type) or unwrapped


def _looks_like_legacy_bundle(value: Any) -> bool:
    """Zistí, či vstup vyzerá ako starší spoločný textový wrapper."""
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value}
        return bool({"patent_evidence_json", "publication_evidence_json", "web_evidence_json", "original_query"} & keys)
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return (
        "patent_evidence_json:" in lowered
        or "publication_evidence_json:" in lowered
        or "web_evidence_json:" in lowered
        or "original_query:" in lowered
    )


def merge_evidence_pack(
    patent_evidence_json: Any = None,
    publication_evidence_json: Any = None,
    web_evidence_json: Any = None,
    original_query: str = "",
    evidence_bundle: Any = None,
) -> str:
    """Spojí patentové, publikačné a webové dôkazy do jednej JSON štruktúry."""
    shape_warnings: list[str] = []
    if (
        evidence_bundle is None
        and publication_evidence_json is None
        and web_evidence_json is None
        and not original_query
        and _looks_like_legacy_bundle(patent_evidence_json)
    ):
        evidence_bundle = patent_evidence_json
        patent_evidence_json = None

    using_separate_args = any(
        value is not None for value in (patent_evidence_json, publication_evidence_json, web_evidence_json)
    )

    if using_separate_args:
        patents_input = _source_arg(patent_evidence_json, "patent") if patent_evidence_json is not None else None
        publications_input = (
            _source_arg(publication_evidence_json, "publication") if publication_evidence_json is not None else None
        )
        web_input = _source_arg(web_evidence_json, "web") if web_evidence_json is not None else None
        query = str(original_query or "").strip()

        if patent_evidence_json is not None and not _is_source_type(patents_input, "patent"):
            shape_warnings.append("patent_evidence_json did not contain source_type=patent; used fallback extraction.")
        if publication_evidence_json is not None and not _is_source_type(publications_input, "publication"):
            shape_warnings.append(
                "publication_evidence_json did not contain source_type=publication; used fallback extraction."
            )
        if web_evidence_json is not None and not _is_source_type(web_input, "web"):
            shape_warnings.append("web_evidence_json did not contain source_type=web; used fallback extraction.")
    else:
        raw_bundle = _safe_parse(evidence_bundle)
        mapping = raw_bundle if isinstance(raw_bundle, dict) else {}
        bundle = _text_of(raw_bundle)
        parsed_objects = _parse_all_json_objects(bundle)

        patents_candidate = _unwrap_payload(
            (
            _mapping_value(mapping, "PATENT_EVIDENCE_JSON", "patents", "patent")
            or _parse_json_after_label(bundle, ["PATENT_EVIDENCE_JSON:", "PATENTS:", "PATENT:"])
            or _find_by_source_type(parsed_objects, "patent")
            )
        )
        if patents_candidate is not None and not _is_source_type(patents_candidate, "patent"):
            shape_warnings.append("PATENT_EVIDENCE_JSON did not contain source_type=patent; used fallback extraction.")
        patents_input = (
            patents_candidate
            if (_is_source_type(patents_candidate, "patent") or patents_candidate is None)
            else _find_by_source_type(parsed_objects, "patent")
            or _extract_embedded_source(_raw_mapping_value(mapping, "PATENT_EVIDENCE_JSON", "patents", "patent"), "patent")
            or _extract_embedded_source(bundle, "patent")
        )

        publications_candidate = _parse_json_after_label(
            bundle, ["PUBLICATION_EVIDENCE_JSON:", "PUBLICATIONS:", "PUBLICATION:"]
        )
        publications_candidate = _unwrap_payload(
            (
            _mapping_value(mapping, "PUBLICATION_EVIDENCE_JSON", "publications", "publication")
            or publications_candidate
            or _find_by_source_type(parsed_objects, "publication")
            )
        )
        publications_input = (
            publications_candidate
            if (_is_source_type(publications_candidate, "publication") or publications_candidate is None)
            else _find_by_source_type(parsed_objects, "publication")
            or _extract_embedded_source(
                _raw_mapping_value(mapping, "PUBLICATION_EVIDENCE_JSON", "publications", "publication"), "publication"
            )
            or _extract_embedded_source(bundle, "publication")
        )
        if publications_candidate is not None and not _is_source_type(publications_candidate, "publication"):
            shape_warnings.append("PUBLICATION_EVIDENCE_JSON did not contain source_type=publication; used fallback extraction.")

        web_candidate = _unwrap_payload(
            (
            _mapping_value(mapping, "WEB_EVIDENCE_JSON", "web", "WEB_EVIDENCE")
            or _parse_json_after_label(bundle, ["WEB_EVIDENCE_JSON:", "WEB:", "WEB_EVIDENCE:"])
            or _find_by_source_type(parsed_objects, "web")
            )
        )
        if web_candidate is not None and not _is_source_type(web_candidate, "web"):
            shape_warnings.append("WEB_EVIDENCE_JSON did not contain source_type=web; used fallback extraction.")
        web_input = (
            web_candidate
            if (_is_source_type(web_candidate, "web") or web_candidate is None)
            else _find_by_source_type(parsed_objects, "web")
            or _extract_embedded_source(_raw_mapping_value(mapping, "WEB_EVIDENCE_JSON", "web", "WEB_EVIDENCE"), "web")
            or _extract_embedded_source(bundle, "web")
        )
        query = _mapping_text(mapping, "ORIGINAL_QUERY", "query") or _query_after_label(bundle)

    patents = _normalize_source(patents_input, "patent")
    publications = _normalize_source(publications_input, "publication")
    web = _normalize_source(web_input, "web")

    merged = {
        "source_type": "merged_evidence_pack",
        "query": query,
        "overall_status": _overall_status([patents, publications, web]),
        "patents": patents,
        "publications": publications,
        "web": web,
        "flags": {
            "may_claim_no_patents": _can_claim_no(patents),
            "may_claim_no_publications": _can_claim_no(publications),
            "may_claim_no_web": _can_claim_no(web),
            "has_patent_evidence": len(patents["hits"]) > 0,
            "has_publication_evidence": len(publications["hits"]) > 0,
            "has_web_evidence": len(web["hits"]) > 0,
        },
        "warnings": _as_array(patents.get("warnings")) + _as_array(publications.get("warnings")) + _as_array(web.get("warnings")) + shape_warnings,
        "errors": _as_array(patents.get("errors")) + _as_array(publications.get("errors")) + _as_array(web.get("errors")),
    }
    return json.dumps(merged, ensure_ascii=False, indent=2)
