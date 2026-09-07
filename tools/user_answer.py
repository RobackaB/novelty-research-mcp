"""Building the user-facing output from a stored research session."""

from __future__ import annotations

import json
import re
from typing import Any

from .evidence_quality import CONFIDENCES, VERDICTS, decide_verdict_and_confidence, grade_source
from .final_answer_pack import _parse_pack, _valid_hits
from .output_cleaner import (
    collapse_repeats,
    strip_boilerplate,
    strip_leading_title,
    unescape_entities,
)
from .relevance import tokens as _relevance_tokens
from .requirement_match import (
    blob_tokens as _blob_tokens,
    part_matches as _part_matches,
    requirement_match_strength as _requirement_match_strength,
    stem_requirement_token as _stem_requirement_token,
    term_matches_blob as _term_matches_blob,
)

SUMMARY_CHAR_LIMIT = 360      
TITLE_CHAR_LIMIT = 220
URL_CHAR_LIMIT = 320
TOTAL_WORD_LIMIT = 2500
NON_ASCII_DROP_THRESHOLD = 0.40  

_SUMMARY_PREFIX_RE = re.compile(
    r"^\s*(?:claim evidence|search evidence|search snippet|abstract excerpt|snippet|abstract|summary)\s*[:\-]\s*",
    re.IGNORECASE,
)

_CONTENT_CUTOFF_RE = re.compile(
    r"\b(?:claim\s+evidence|search\s+evidence|search\s+snippet|abstract\s+excerpt)\s*[:\-]",
    re.IGNORECASE,
)

_FETCH_ARTIFACT_RE = re.compile(
    r"\b(?:fetched\s+(?:abstract|excerpt|title|content|page)|url\s+source|markdown\s+content|published\s+time|warning:\s*target\s+url)\s*[:\-]",
    re.IGNORECASE,
)

_TABLE_PIPE_RE = re.compile(r"\|.*\|.*\|")
_TABLE_REPLACEMENT_EN = "[Citation table - open the source URL for the abstract.]"
_TABLE_REPLACEMENT_SK = "[Citačná tabuľka - pre abstrakt otvorte URL zdroja.]"
_ADJACENT_RELEVANCE = {"adjacent", "generic", "loose"}


def _as_dict(value: Any) -> dict[str, Any]:
    """Return the input dict, or an empty dict for any other type."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return the input list, or an empty list for any other type."""
    return value if isinstance(value, list) else []


def _word_count(text: str) -> int:
    """Count the words in a text."""
    return len(re.findall(r"\S+", text or ""))


def _trim_to_words(text: str, limit: int = TOTAL_WORD_LIMIT) -> str:
    """Truncate a text to a maximum word count without altering its line structure."""
    if _word_count(text) <= limit:
        return text.rstrip()
    out_lines: list[str] = []
    used = 0
    for line in text.split("\n"):
        line_words = _word_count(line)
        if used + line_words <= limit:
            out_lines.append(line)
            used += line_words
            continue
        remaining = max(0, limit - used)
        if remaining > 0:
            tokens = re.findall(r"\S+", line)
            partial = " ".join(tokens[:remaining]).rstrip(" .,;")
            if partial:
                out_lines.append(partial + "...")
        break
    return "\n".join(out_lines).rstrip()


def _detect_language(query: str, query_envelope: dict[str, Any] | None = None) -> str:
    """Determine the output language from the query wrapper or the characters in the question."""
    envelope_language = str((query_envelope or {}).get("language") or "").lower()
    if envelope_language in {"sk", "non_english"}:
        return "sk"
    if envelope_language in {"en", "mixed"}:
        return "en"
    if re.search(r"[^\x00-\x7f]", query or ""):
        return "sk"
    return "en"


def _source_counts(pack: dict[str, Any]) -> dict[str, int]:
    """Count the hits for the patent, publication and web sources."""
    return {
        "patent": len(_as_list(_as_dict(pack.get("patents")).get("hits"))),
        "publication": len(_as_list(_as_dict(pack.get("publications")).get("hits"))),
        "web": len(_as_list(_as_dict(pack.get("web")).get("hits"))),
    }


def _source_grades(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Assess the quality of each source in the merged wrapper."""
    return {
        "patent": grade_source(_as_dict(pack.get("patents")), "patent"),
        "publication": grade_source(_as_dict(pack.get("publications")), "publication"),
        "web": grade_source(_as_dict(pack.get("web")), "web"),
    }


def _sanitize_text(text: Any, limit: int) -> str:
    """Clean a text field and truncate it to a given character count."""
    raw = str(text or "").replace("\r", " ")
    raw = _SUMMARY_PREFIX_RE.sub("", raw)
    cutoff = _CONTENT_CUTOFF_RE.search(raw)
    if cutoff:
        raw = raw[: cutoff.start()]
    artifact = _FETCH_ARTIFACT_RE.search(raw)
    if artifact:
        raw = raw[: artifact.start()]
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) <= limit:
        return raw
    truncated = raw[:limit]
    sentence_end = max(
        truncated.rfind(". "),
        truncated.rfind("? "),
        truncated.rfind("! "),
    )
    if sentence_end >= int(limit * 0.6):
        return truncated[: sentence_end + 1].rstrip()
    word_end = truncated.rfind(" ")
    if word_end >= int(limit * 0.6):
        return truncated[:word_end].rstrip() + "..."
    return truncated.rstrip() + "..."


def _is_mostly_non_ascii(text: str, threshold: float = NON_ASCII_DROP_THRESHOLD) -> bool:
    """Determine whether a text contains too many non-ASCII characters."""
    if not text:
        return False
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return non_ascii / max(1, len(text)) >= threshold


def _sanitize_display_text(text: Any, limit: int) -> str:
    """Clean display text of HTML entities and duplicated phrases."""
    return _sanitize_text(collapse_repeats(unescape_entities(text)), limit)


def _sanitize_summary(text: Any, limit: int, language: str, title: Any = "") -> str:
    """Clean a hit's summary before inserting it into the user-facing answer."""
    raw_initial = str(text or "")
    if _TABLE_PIPE_RE.search(raw_initial):
        return ""
    if language != "sk" and _is_mostly_non_ascii(raw_initial):
        return ""
    cleaned = strip_boilerplate(collapse_repeats(unescape_entities(raw_initial)))
    if title:
        cleaned = strip_leading_title(cleaned, collapse_repeats(unescape_entities(title)))
    return _sanitize_text(cleaned, limit)


def _conclusion_phrase(verdict: str, language: str) -> str:
    """Return a short verbal summary of the verdict in the selected language."""
    if language == "sk":
        if verdict in {"exact_match", "close_prior_art"}:
            return "Vrátené agregované dôkazy naznačujú relevantný alebo blízky prior art."
        if verdict == "adjacent_only":
            return "Vrátené dôkazy sú skôr susedné alebo nepriamo relevantné, nie priama anticipácia."
        if verdict == "no_reliable_prior_art":
            return "Spoľahlivo dokončené zdroje nevrátili relevantný prior art v rozsahu vykonaného vyhľadávania."
        return "Vyhľadávanie je čiastočné a nestačí na negatívny záver o absencii prior art."
    if verdict in {"exact_match", "close_prior_art"}:
        return "The aggregated evidence indicates relevant or close prior art."
    if verdict == "adjacent_only":
        return "The returned evidence is adjacent rather than a strong direct match."
    if verdict == "no_reliable_prior_art":
        return "The reliably completed sources did not return relevant prior art within this search scope."
    return "Retrieval is partial, so the result cannot support a negative prior-art conclusion."


def _uncertainty_text(verdict: str, retrieval: str, language: str) -> str:
    """Return the uncertainty caveat text for the verdict and retrieval state."""
    if language == "sk":
        if verdict == "no_reliable_prior_art":
            return "Toto neznamená, že prior art neexistuje mimo spoľahlivo dokončeného vyhľadávania."
        if retrieval == "partial":
            return "Časť vyhľadávania bola neistá alebo slabá; timeouty, 403, failed fetch a snippet-only dôkazy nie sú brané ako silný dôkaz."
        return "Záver je konzervatívny a mal by sa overiť manuálnou patentovou analýzou."
    if verdict == "no_reliable_prior_art":
        return "This does not prove absence of prior art outside the reliably completed searches."
    if retrieval == "partial":
        return "Some retrieval was partial or weak, so timeouts, 403s, failed fetches, and snippet-only hits are not treated as strong evidence."
    return "This is a conservative research summary, not a legal patentability opinion."


def _section_labels(language: str) -> dict[str, str]:
    """Return the section labels of the user answer in the selected language."""
    if language == "sk":
        return {
            "summary": "## Zhrnutie",
            "state_of_art": "## Stav techniky",
            "patents": "### Patenty",
            "publications": "### Vedecké zdroje",
            "web": "### Webové zdroje",
            "novelty": "## Hodnotenie novosti",
            "novelty_score": "Stupeň novosti",
            "flaws": "## Nedostatky existujúcich riešení",
            "innovation": "## Priestor pre inováciu",
            "sources": "## Zdroje",
            "no_relevant": "Žiadne relevantné záznamy v tomto zdroji.",
            "incomplete": "Vyhľadávanie tohto zdroja nie je spoľahlivo dokončené; absenciu nemožno tvrdiť.",
            "no_focused_publication": "Žiadne fokusované publikačné dôkazy - vrátené záznamy boli iba čiastočne súvisiace.",
            "no_focused_web": "Žiadne fokusované webové dôkazy - vrátené záznamy boli iba čiastočne súvisiace.",
            "evidence": "Úroveň dôkazu",
            "verified_url_yes": "overený URL",
            "verified_url_no": "neoverený URL",
            "verdict_label": "Verdikt",
            "confidence_label": "Dôvera",
            "retrieval_label": "Úplnosť vyhľadávania",
            "quality_label": "Kvalita zdrojov",
            "query_label": "Pôvodná otázka",
            "patent_number_label": "Patent",
            "relevance_label": "relevancia",
            "adjacent_marker": "len susedné",
            "template_disclaimer": "_(deterministická šablóna - nie je odvodená z konkrétnych dôkazov)_",
            "retrieval_complete": "úplné",
            "retrieval_partial": "čiastočné",
            "retrieval_degraded": "nestabilné",
            "retrieval_mixed_partial": "zmiešané/čiastočné",
            "retrieval_reliable_no_results": "spoľahlivé bez výsledkov",
            "retrieval_failed": "zlyhané",
            "direct_label": "Najsilnejšie nálezy",
            "weak_leads_label": "Slabšie súvisiace nálezy",
            "uncertainty_section": "## Limity a neistota",
            "uncertainty_partial": "Vyhľadávanie bolo čiastočné - niektoré zdroje vrátili chyby alebo iba slabé výsledky.",
            "uncertainty_complete": "Toto je deterministická rešerš a nie právne stanovisko k patentovateľnosti.",
            "coverage_label": "Pokrytie kritických požiadaviek",
            "element_column": "Prvok",
            "status_column": "Stav",
            "strongest_source_column": "Najsilnejší zdroj",
            "unspecified_label": "(neuvedený)",
            "no_critical_reqs": "Pre tento dotaz neboli automaticky extrahované konkrétne kritické požiadavky.",
            "novelty_cannot_assess": "Nedá sa spoľahlivo posúdiť pri čiastočnom vyhľadávaní.",
            "flaws_no_data": "Žiadne dostupné údaje v dátach neumožňujú spoľahlivo identifikovať konkrétne nedostatky existujúcich riešení.",
            "flaws_partial": "Pri čiastočnom vyhľadávaní nie je možné identifikovať všetky nedostatky existujúcich riešení.",
            "flaws_with_evidence": "Existujúce riešenia podľa vrátených dôkazov riešia hlavnú funkciu, no detailné limity je potrebné overiť priamo zo zdrojových dokumentov.",
            "innov_no_data": "Vzhľadom na absenciu údajov existuje priestor pre inováciu, ale konkrétny smer treba odvodiť po manuálnej analýze.",
            "innov_close": "Smerujte inováciu do špecifických zlepšení, ktoré nie sú pokryté existujúcim prior art.",
            "innov_adjacent": "Existuje priestor pre inováciu kombináciou prvkov, ktoré sa nevyskytujú spolu v existujúcom prior art.",
            "innov_no_prior_art": "V rozsahu vykonaného vyhľadávania je priestor pre inováciu otvorený; pred patentovaním však odporúčame manuálnu rešerš.",
            "no_sources": "Žiadne URL adresy neboli získané v tomto prieskume.",
            "coverage_requirements_word": "požiadaviek plne overených",
            "coverage_meta_label": "pokrytie prvkov",
            "corroboration_label": "Nezávislé potvrdenie",
            "corroborated_marker": "potvrdené viacerými zdrojmi",
        }
    return {
        "summary": "## Summary",
        "state_of_art": "## State of the art",
        "patents": "### Patents",
        "publications": "### Publications",
        "web": "### Web sources",
        "novelty": "## Novelty assessment",
        "novelty_score": "Novelty score",
        "flaws": "## Flaws of existing solutions",
        "innovation": "## Space for innovation",
        "sources": "## Sources",
        "no_relevant": "No relevant records from this source.",
        "incomplete": "This source's search is not reliably complete; absence cannot be claimed.",
        "no_focused_publication": "No focused publication evidence - returned records were only adjacent to the query.",
        "no_focused_web": "No focused web evidence - returned records were only adjacent to the query.",
        "evidence": "Evidence level",
        "verified_url_yes": "verified URL",
        "verified_url_no": "unverified URL",
        "verdict_label": "Verdict",
        "confidence_label": "Confidence",
        "retrieval_label": "Retrieval",
        "quality_label": "Source quality",
        "query_label": "Original query",
        "patent_number_label": "Patent",
        "relevance_label": "relevance",
        "adjacent_marker": "adjacent only",
        "template_disclaimer": "_(deterministic template - not derived from specific evidence)_",
        "retrieval_complete": "complete",
        "retrieval_partial": "partial",
        "retrieval_degraded": "degraded",
        "retrieval_mixed_partial": "mixed/partial",
        "retrieval_reliable_no_results": "reliable no-results",
        "retrieval_failed": "failed",
        "direct_label": "Strongest matches",
        "weak_leads_label": "Weaker leads",
        "uncertainty_section": "## Limitations and uncertainty",
        "uncertainty_partial": "Retrieval was partial - some sources returned errors or only weak results, so absence of strong matches is not the same as absence of prior art.",
        "uncertainty_complete": "This is a deterministic research summary, not a patentability legal opinion.",
        "coverage_label": "Critical requirement coverage",
        "element_column": "Element",
        "status_column": "Status",
        "strongest_source_column": "Strongest source",
        "unspecified_label": "(unspecified)",
        "no_critical_reqs": "No specific critical requirements were automatically extracted from this query.",
        "novelty_cannot_assess": "Cannot be reliably assessed under partial retrieval.",
        "flaws_no_data": "No retrieved data allows reliable identification of specific flaws in existing solutions.",
        "flaws_partial": "Under partial retrieval, not all flaws of existing solutions can be identified.",
        "flaws_with_evidence": "The retrieved evidence shows existing solutions cover the main function, but detailed limits must be verified directly from the source documents.",
        "innov_no_data": "With no usable retrieval data, space for innovation exists but the direction must be determined through manual analysis.",
        "innov_close": "Direct innovation toward specific improvements not covered by the existing prior art.",
        "innov_adjacent": "Space for innovation exists in combinations of elements that do not co-occur in the existing prior art.",
        "innov_no_prior_art": "Within this search scope, the space for innovation appears open; manual prior-art search is still recommended before filing.",
        "no_sources": "No URLs were retrieved in this search.",
        "coverage_requirements_word": "requirements fully verified",
        "coverage_meta_label": "element coverage",
        "corroboration_label": "Cross-source corroboration",
        "corroborated_marker": "corroborated across sources",
    }


def _novelty_score(verdict: str, confidence: str, retrieval: str) -> tuple[int | None, str]:
    """Convert a verdict into an approximate numeric novelty score."""
    if verdict == "partial_retrieval" or retrieval == "partial":
        return None, "partial"
    base = {
        "exact_match": 5,
        "close_prior_art": 20,
        "adjacent_only": 55,
        "no_reliable_prior_art": 85,
    }.get(verdict)
    if base is None:
        return None, "unknown"
    delta = {"high": -5, "medium": 0, "low": +10}.get(confidence, 0)
    score = max(0, min(100, base + delta))
    return score, "ok"


def _summary_carries_evidence(summary: str, query_tokens: set[str] | None) -> bool:
    """Check whether a summary carries any content related to the query at all.

    Once navigational clutter is stripped, what remains of a page can read
    fluently while having nothing to do with the query. Such a summary is worse
    than none in the report, so the hit is then shown with verifiable fields only.
    """
    if not query_tokens:
        return True
    return bool(_relevance_tokens(summary) & query_tokens)


def _render_hit_block(
    index: int,
    hit: dict[str, Any],
    labels: dict[str, str],
    language: str,
    query_tokens: set[str] | None = None,
) -> list[str]:
    """Render a single hit as a short block in the final answer."""
    title = _sanitize_display_text(hit.get("title"), TITLE_CHAR_LIMIT) or f"Hit {index}"
    url = _sanitize_text(hit.get("url"), URL_CHAR_LIMIT)
    patent_number = _sanitize_text(hit.get("patent_number"), 60)
    summary = _sanitize_summary(hit.get("summary"), SUMMARY_CHAR_LIMIT, language, title=title)
    if summary and not _summary_carries_evidence(summary, query_tokens):
        summary = ""
    evidence = _sanitize_text(hit.get("evidence_level"), 60)
    verified = hit.get("verified_url")
    relevance_raw = _sanitize_text(hit.get("relevance"), 60).lower()
    is_adjacent = relevance_raw in _ADJACENT_RELEVANCE

    head_parts = [f"{index}."]
    title_marker = f" {labels['adjacent_marker']}" if is_adjacent else ""
    if url:
        head_parts.append(f"[{title}]({url}){title_marker}")
    else:
        head_parts.append(f"{title}{title_marker}")
    if patent_number:
        head_parts.append(f"({patent_number})")
    block = [" ".join(head_parts)]
    if summary:
        block.append(f"   {summary}")
    meta_bits: list[str] = []
    if evidence:
        meta_bits.append(f"{labels['evidence']}: `{evidence}`")
    if verified is True:
        meta_bits.append(f"`{labels['verified_url_yes']}`")
    elif verified is False:
        meta_bits.append(f"`{labels['verified_url_no']}`")
    if relevance_raw:
        meta_bits.append(f"{labels['relevance_label']}: `{relevance_raw}`")
    coverage_raw = hit.get("claim_coverage")
    if not isinstance(coverage_raw, (int, float)) or coverage_raw <= 0:
        coverage_raw = hit.get("atom_coverage")
    if isinstance(coverage_raw, (int, float)) and coverage_raw > 0:
        meta_bits.append(f"{labels['coverage_meta_label']}: {round(float(coverage_raw) * 100)}%")
    if hit.get("cross_source_corroborated") is True:
        meta_bits.append(f"`{labels['corroborated_marker']}`")
    if meta_bits:
        block.append("   _" + " · ".join(meta_bits) + "_")
    return block


def _hit_is_direct(hit: dict[str, Any]) -> bool:
    """Determine whether a hit constitutes direct or strong evidence."""
    relevance = str(hit.get("relevance") or "").strip().lower()
    if relevance in {"adjacent", "generic", "loose"}:
        return False
    evidence = str(hit.get("evidence_level") or "").strip().lower()
    strong_levels = {"claim_verified", "abstract_verified", "verified_metadata", "verified_page", "fetched_excerpt"}
    return evidence in strong_levels


_FAILED_HIT_EVIDENCE_LEVELS = frozenset({
    "fetch_failed", "fetch_timeout", "provider_error", "unverified",
})


def _is_failed_hit(hit: dict[str, Any]) -> bool:
    """Determine whether a hit came from a failed or unverified retrieval."""
    level = str(hit.get("evidence_level") or "").strip().lower()
    return level in _FAILED_HIT_EVIDENCE_LEVELS


def _is_report_visible_hit(source_type: str, hit: dict[str, Any]) -> bool:
    """Decide whether a hit should appear in the user-facing answer."""
    if _is_failed_hit(hit):
        return False
    relevance = str(hit.get("relevance") or "").strip().lower()
    if source_type in {"publication", "web"} and relevance == "generic":
        return False
    return True


def _select_rendered_hits(
    source: dict[str, Any], hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Select the hits suitable for display in the final answer."""
    source_type = str(source.get("source_type") or "").strip().lower()
    return [hit for hit in hits if _is_report_visible_hit(source_type, hit)]


def _render_source_subsection(
    header: str,
    source: dict[str, Any],
    hits: list[dict[str, Any]],
    labels: dict[str, str],
    language: str,
    query_tokens: set[str] | None = None,
) -> list[str]:
    """Render the section for one source type, with its direct and weaker hits."""
    lines = [header]
    completed = source.get("completed") is True
    reliable_no = source.get("reliable_no_results") is True
    source_type = str(source.get("source_type") or "").strip().lower()
    body_hits = [hit for hit in hits if _is_report_visible_hit(source_type, hit)]
    if not body_hits:
        if completed and reliable_no:
            lines.append(labels["no_relevant"])
        else:
            lines.append(labels["incomplete"])
        return lines
    hits = body_hits

    direct = [hit for hit in hits if _hit_is_direct(hit)]
    weak_leads = [hit for hit in hits if not _hit_is_direct(hit)]

    if direct:
        if weak_leads:
            lines.append(f"**{labels['direct_label']}**")
        for index, hit in enumerate(direct, start=1):
            lines.extend(_render_hit_block(index, hit, labels, language, query_tokens))
            lines.append("")
    if weak_leads:
        if direct:
            lines.append(f"**{labels['weak_leads_label']}**")
        for index, hit in enumerate(weak_leads, start=len(direct) + 1):
            lines.extend(_render_hit_block(index, hit, labels, language, query_tokens))
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _humanize_retrieval(retrieval: str, labels: dict[str, str]) -> str:
    """Convert the internal retrieval state into user-facing text."""
    mapping = {
        "complete": labels["retrieval_complete"],
        "partial": labels["retrieval_partial"],
        "degraded": labels["retrieval_degraded"],
        "mixed_partial": labels["retrieval_mixed_partial"],
        "reliable_no_results": labels["retrieval_reliable_no_results"],
        "failed": labels["retrieval_failed"],
        "complete_or_reliable_no_results": labels["retrieval_complete"],
    }
    return mapping.get(retrieval, retrieval or labels["retrieval_partial"])


def _render_summary_section(
    verdict: str,
    confidence: str,
    retrieval: str,
    source_grades: dict[str, dict[str, Any]],
    language: str,
    labels: dict[str, str],
    exact_combination_found: bool = False,
    corroborated_documents: list[str] | None = None,
) -> list[str]:
    """Build the opening summary of the research result."""
    grade_text = ", ".join(
        f"{source}: {data.get('quality_grade', 'missing')}" for source, data in source_grades.items()
    )
    retrieval_display = _humanize_retrieval(retrieval, labels)
    out = [
        labels["summary"],
        f"**{labels['verdict_label']}:** `{verdict}` · **{labels['confidence_label']}:** `{confidence}` · **{labels['retrieval_label']}:** `{retrieval_display}`",
        _conclusion_phrase(verdict, language),
        f"**{labels['quality_label']}:** {grade_text}.",
    ]
    if corroborated_documents:
        shown = "; ".join(corroborated_documents[:4])
        out.append(f"**{labels['corroboration_label']}:** {shown}.")
    if not exact_combination_found and verdict != "no_reliable_prior_art":
        if language == "sk":
            out.append(
                "> **Žiadny zdroj nepotvrdil presnú kombináciu všetkých prvkov dotazu.** "
                "Závery sa opierajú o čiastočné prekrytia rôznych zdrojov, nie o jediný anticipačný dokument."
            )
        else:
            out.append(
                "> **No single source verified the exact combination of all query elements.** "
                "Conclusions rest on partial overlaps across separate sources, not a single anticipating document."
            )
    return out


def _render_novelty_section(
    verdict: str,
    confidence: str,
    retrieval: str,
    language: str,
    labels: dict[str, str],
    exact_combination_found: bool = False,
) -> list[str]:
    """Build the section assessing novelty and confidence."""
    if verdict == "exact_match" and exact_combination_found:
        score, _status = _novelty_score(verdict, confidence, retrieval)
        if score is not None:
            score_line = f"**{labels['novelty_score']}:** {score}%"
            explanation = _conclusion_phrase(verdict, language)
            return [labels["novelty"], score_line, explanation]
    qualitative = _qualitative_novelty_label(verdict, confidence, retrieval, language)
    score_line = f"**{labels['novelty_score']}:** {qualitative}"
    explanation = _conclusion_phrase(verdict, language)
    if not exact_combination_found and verdict != "no_reliable_prior_art":
        if language == "sk":
            explanation = explanation + " Žiadny zdroj nepotvrdil presnú kombináciu všetkých prvkov dotazu."
        else:
            explanation = explanation + " No single source verified the exact combination of all query elements."
    return [labels["novelty"], score_line, explanation]


def _qualitative_novelty_label(
    verdict: str,
    confidence: str,
    retrieval: str,
    language: str,
) -> str:
    """Return a verbal description of the approximate novelty score."""
    if retrieval in {"partial", "degraded", "mixed_partial", "failed"}:
        return "low confidence - partial retrieval" if language != "sk" else "nízka istota - neúplné vyhľadávanie"
    mapping_en = {
        "exact_match": "high prior-art risk (close anticipation)",
        "close_prior_art": "moderate-to-high prior-art risk (close, not exact)",
        "adjacent_only": "moderate prior-art risk (adjacent only)",
        "no_reliable_prior_art": "low prior-art risk in searched sources",
        "partial_retrieval": "low confidence - partial retrieval",
    }
    mapping_sk = {
        "exact_match": "vysoké riziko prior art (blízka anticipácia)",
        "close_prior_art": "stredné až vysoké riziko prior art (blízke, nie presné)",
        "adjacent_only": "stredné riziko prior art (len susedné)",
        "no_reliable_prior_art": "nízke riziko prior art v prehľadaných zdrojoch",
        "partial_retrieval": "nízka istota - neúplné vyhľadávanie",
    }
    table = mapping_sk if language == "sk" else mapping_en
    return table.get(verdict, "indeterminate" if language != "sk" else "neurčené")


def _render_flaws_section(
    verdict: str,
    retrieval: str,
    has_any_hits: bool,
    labels: dict[str, str],
) -> list[str]:
    """Build the section on the shortcomings of existing solutions."""
    if retrieval == "partial":
        body = labels["flaws_partial"]
    elif not has_any_hits:
        body = labels["flaws_no_data"]
    else:
        body = labels["flaws_with_evidence"]
    return [labels["flaws"], labels["template_disclaimer"], f"- {body}"]


def _render_innovation_section(
    verdict: str,
    has_any_hits: bool,
    labels: dict[str, str],
) -> list[str]:
    """Build the section on the room available for innovation."""
    if not has_any_hits:
        body = labels["innov_no_data"]
    elif verdict in {"exact_match", "close_prior_art"}:
        body = labels["innov_close"]
    elif verdict == "adjacent_only":
        body = labels["innov_adjacent"]
    elif verdict == "no_reliable_prior_art":
        body = labels["innov_no_prior_art"]
    else:
        body = labels["innov_no_data"]
    return [labels["innovation"], labels["template_disclaimer"], f"- {body}"]


def _render_sources_section(
    all_hits: list[dict[str, Any]],
    labels: dict[str, str],
) -> list[str]:
    """Build the list of unique source URLs."""
    seen: set[str] = set()
    lines: list[str] = [labels["sources"]]
    counter = 0
    for hit in all_hits:
        url = _sanitize_text(hit.get("url"), URL_CHAR_LIMIT)
        if not url or url in seen:
            continue
        seen.add(url)
        counter += 1
        lines.append(f"{counter}. {url}")
    if counter == 0:
        lines.append(labels["no_sources"])
    return lines


def _render_uncertainty_section(
    retrieval: str,
    critical_requirements: list[str],
    requirement_coverage: dict[str, int],
    labels: dict[str, str],
    per_requirement_status: list[dict[str, Any]] | None = None,
    language: str = "en",
) -> list[str]:
    """Build the section covering limits, uncertainty and requirement coverage."""
    lines = [labels["uncertainty_section"]]
    if retrieval in {"partial", "degraded", "mixed_partial", "failed"}:
        lines.append(f"- {labels['uncertainty_partial']}")
    else:
        lines.append(f"- {labels['uncertainty_complete']}")

    if per_requirement_status:
        lines.append("")
        if language == "sk":
            header = "**Pokrytie po jednotlivých prvkoch:**"
            status_words = {
                "verified": "overené",
                "partially_indicated": "čiastočne naznačené",
                "not_verified": "neoverené",
            }
            no_source_word = "žiadny zdroj"
        else:
            header = "**Element-by-element coverage:**"
            status_words = {
                "verified": "verified",
                "partially_indicated": "partially indicated",
                "not_verified": "not verified",
            }
            no_source_word = "no source"
        lines.append(header)
        lines.append("")
        lines.append(
            f"| {labels['element_column']} | {labels['status_column']} "
            f"| {labels['strongest_source_column']} | {labels['evidence']} |"
        )
        lines.append("|---|---|---|---|")
        for row in per_requirement_status:
            label = _sanitize_text(row.get("label") or "", 80) or labels["unspecified_label"]
            category = str(row.get("category") or "").replace("_", " ")
            full_label = f"{label} ({category})" if category else label
            status = status_words.get(row.get("status", ""), row.get("status", ""))
            source = row.get("strongest_source") or no_source_word
            level = row.get("evidence_level") or "-"
            lines.append(f"| {full_label} | {status} | {source} | `{level}` |")
        not_full = [
            (
                _sanitize_text(row.get("label") or "", 80) or labels["unspecified_label"],
                status_words.get(row.get("status", ""), row.get("status", "")),
            )
            for row in per_requirement_status
            if row.get("status") != "verified"
        ]
        if not_full:
            lines.append("")
            prefix = "Nie úplne overené prvky:" if language == "sk" else "Not fully verified elements:"
            details = "; ".join(f"{label} ({status})" for label, status in not_full)
            lines.append(f"{prefix} {details}.")
        return lines

    if critical_requirements:
        lines.append(f"**{labels['coverage_label']}:**")
        total = len(critical_requirements)
        coverage_word = labels.get("coverage_requirements_word", "requirements fully verified")
        for source in ("patent", "publication", "web"):
            covered = max(0, min(int(requirement_coverage.get(source, 0) or 0), total))
            lines.append(f"- {source}: {covered}/{total} {coverage_word}")
    else:
        lines.append(f"- {labels['no_critical_reqs']}")
    return lines


def _render_user_answer(
    query: str,
    pack: dict[str, Any],
    verdict: str,
    confidence: str,
    retrieval: str,
    source_grades: dict[str, dict[str, Any]],
    language: str,
    critical_requirements: list[str] | None = None,
    requirement_coverage: dict[str, int] | None = None,
    atomic_requirements: list[dict[str, Any]] | None = None,
    retrieval_status_notes: list[str] | None = None,
    corroborated_documents: list[str] | None = None,
) -> str:
    """Build the complete user-facing answer from the merged evidence wrapper."""
    labels = _section_labels(language)
    patents = _as_dict(pack.get("patents"))
    publications = _as_dict(pack.get("publications"))
    web = _as_dict(pack.get("web"))
    patent_hits = _valid_hits(patents)
    publication_hits = _valid_hits(publications)
    web_hits = _valid_hits(web)
    has_any_hits = bool(patent_hits or publication_hits or web_hits)

    query_text = _sanitize_text(query, 320) or ("(neuvedená)" if language == "sk" else "(not provided)")

    sections: list[list[str]] = []

    sections.append([f"# {query_text}"])

    exact_combination_found = any(
        bool(grade.get("exact_combination_candidate_found"))
        for grade in (source_grades or {}).values()
    )
    sections.append(
        _render_summary_section(
            verdict, confidence, retrieval, source_grades, language, labels,
            exact_combination_found=exact_combination_found,
            corroborated_documents=corroborated_documents,
        )
    )

    if retrieval_status_notes:
        heading = "## Retrieval status notes" if language != "sk" else "## Poznámky k získavaniu dát"
        notes_section = [heading]
        for note in retrieval_status_notes:
            text = str(note or "").strip()
            if text:
                notes_section.append(f"- {text}")
        if len(notes_section) > 1:
            sections.append(notes_section)

    # The query tokens are used to discard summaries that, once the clutter is
    # stripped, no longer relate to the query at all -- typically leftovers of a
    # web page's navigation.
    query_tokens = _relevance_tokens(query)
    state_of_art = [labels["state_of_art"]]
    state_of_art.extend(
        _render_source_subsection(labels["patents"], patents, patent_hits, labels, language, query_tokens)
    )
    state_of_art.append("")
    state_of_art.extend(
        _render_source_subsection(
            labels["publications"], publications, publication_hits, labels, language, query_tokens
        )
    )
    state_of_art.append("")
    state_of_art.extend(
        _render_source_subsection(labels["web"], web, web_hits, labels, language, query_tokens)
    )
    sections.append(state_of_art)

    sections.append(
        _render_novelty_section(
            verdict, confidence, retrieval, language, labels, exact_combination_found
        )
    )
    per_req_status = (
        _per_requirement_status(pack, atomic_requirements) if atomic_requirements else None
    )
    sections.append(
        _render_uncertainty_section(
            retrieval,
            critical_requirements or [],
            requirement_coverage or {},
            labels,
            per_requirement_status=per_req_status,
            language=language,
        )
    )
    rendered_patent_hits = _select_rendered_hits(patents, patent_hits)
    rendered_pub_hits = _select_rendered_hits(publications, publication_hits)
    rendered_web_hits = _select_rendered_hits(web, web_hits)
    sections.append(
        _render_sources_section(
            rendered_patent_hits + rendered_pub_hits + rendered_web_hits, labels
        )
    )

    rendered = "\n\n".join(
        "\n".join(line for line in section if line is not None) for section in sections
    )
    return _trim_to_words(rendered, TOTAL_WORD_LIMIT)

_VERIFIED_EVIDENCE_LEVELS = frozenset({
    "claim_verified",
    "abstract_verified",
    "verified_metadata",
    "verified_page",
    "fetched_excerpt",
})

_FOCUSED_RELEVANCE_LEVELS = frozenset({"focused", "direct", "exact"})

def _is_verified_hit(hit: dict[str, Any]) -> bool:
    """Determine whether a hit has a usable evidence level and is not merely adjacent."""
    level = str(hit.get("evidence_level") or "").strip().lower()
    if level not in _VERIFIED_EVIDENCE_LEVELS:
        return False
    relevance = str(hit.get("relevance") or "").strip().lower()
    if relevance and relevance in _ADJACENT_RELEVANCE:
        return False
    return True

def _hit_text_blob(hit: dict[str, Any]) -> str:
    """Join a hit's text fields into a single string for matching."""
    parts = [
        str(hit.get("title") or ""),
        str(hit.get("summary") or ""),
        str(hit.get("snippet") or ""),
    ]
    return " ".join(parts).lower()

def _requirement_keyword_sets(
    critical_requirements: list[str],
    atomic_requirements: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    """Prepare the keywords for each critical requirement."""
    if atomic_requirements:
        sets: list[list[str]] = []
        for atom in atomic_requirements:
            terms = atom.get("terms") if isinstance(atom, dict) else None
            if isinstance(terms, list) and terms:
                cleaned = [str(t).lower().strip() for t in terms if str(t).strip()]
                if cleaned:
                    sets.append(cleaned[:4])
        if sets:
            return sets
    sets = []
    for req in critical_requirements:
        req_lower = str(req or "").lower().strip()
        if not req_lower:
            continue
        tokens = [t for t in re.findall(r"[\w-]{4,}", req_lower) if t.isascii()]
        if tokens:
            sets.append(tokens[:3])
    return sets

def _requirement_coverage(
    pack: dict[str, Any],
    critical_requirements: list[str],
    atomic_requirements: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Count how many critical requirements each source type covers."""
    if not critical_requirements and not atomic_requirements:
        return {"patent": 0, "publication": 0, "web": 0}

    if atomic_requirements:
        requirements = [
            [str(term) for term in atom.get("terms")]
            for atom in atomic_requirements
            if isinstance(atom, dict) and isinstance(atom.get("terms"), list) and atom.get("terms")
        ]
    else:
        requirements = _requirement_keyword_sets(critical_requirements, None)

    if not requirements:
        return {"patent": 0, "publication": 0, "web": 0}

    coverage: dict[str, int] = {}
    for source_name, source_key in (("patent", "patents"), ("publication", "publications"), ("web", "web")):
        source = _as_dict(pack.get(source_key))
        hits = _valid_hits(source)
        covered = 0
        for terms in requirements:
            if any(
                _is_verified_hit(_as_dict(hit))
                and _requirement_match_strength(terms, _hit_text_blob(_as_dict(hit))) == "full"
                for hit in hits
            ):
                covered += 1
        coverage[source_name] = covered
    return coverage

def _single_hit_covers_all_atoms(
    pack: dict[str, Any],
    atomic_requirements: list[dict[str, Any]],
) -> bool:
    """Determine whether a single verified hit covers every atomic requirement."""
    if not atomic_requirements:
        return False
    atom_term_sets = [
        [str(t).lower().strip() for t in (atom.get("terms") or []) if str(t).strip()]
        for atom in atomic_requirements
    ]
    atom_term_sets = [terms for terms in atom_term_sets if terms]
    if not atom_term_sets:
        return False
    for source_key in ("patents", "publications", "web"):
        source = _as_dict(pack.get(source_key))
        for hit in _valid_hits(source):
            hit_dict = _as_dict(hit)
            if not _is_verified_hit(hit_dict):
                continue
            blob = _hit_text_blob(hit_dict)
            if all(
                _requirement_match_strength(terms, blob) == "full"
                for terms in atom_term_sets
            ):
                return True
    return False

def _per_requirement_status(
    pack: dict[str, Any],
    atomic_requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the coverage assessment for each atomic requirement."""
    if not atomic_requirements:
        return []
    rows: list[dict[str, Any]] = []
    source_levels = (
        ("patent", "patents"),
        ("publication", "publications"),
        ("web", "web"),
    )
    for atom in atomic_requirements:
        terms = [str(t).lower() for t in (atom.get("terms") or []) if str(t).strip()]
        if not terms:
            continue
        verified_source: tuple[str, str] | None = None  
        partial_source: tuple[str, str] | None = None
        for source_name, source_key in source_levels:
            source = _as_dict(pack.get(source_key))
            hits = _valid_hits(source)
            for hit in hits:
                hit_dict = _as_dict(hit)
                blob = _hit_text_blob(hit_dict)
                match_strength = _requirement_match_strength(terms, blob)
                if match_strength == "none":
                    continue
                evidence_level = str(hit_dict.get("evidence_level") or "").lower()
                if match_strength == "full" and _is_verified_hit(hit_dict):
                    if verified_source is None:
                        verified_source = (source_name, evidence_level)
                    break
                if partial_source is None:
                    hit_relevance = str(hit_dict.get("relevance") or "").strip().lower()
                    if hit_relevance in {"adjacent", "loose", "generic"}:
                        displayed_level = "search_snippet_only"
                    else:
                        displayed_level = evidence_level or "search_snippet_only"
                    if hit_relevance in {"adjacent", "loose", "generic"}:
                        labelled_source = f"{source_name} ({hit_relevance})"
                    else:
                        labelled_source = source_name
                    partial_source = (labelled_source, displayed_level)
            if verified_source:
                break
        if verified_source:
            status = "verified"
            strongest_source, evidence_level = verified_source
        elif partial_source:
            status = "partially_indicated"
            strongest_source, evidence_level = partial_source
        else:
            status = "not_verified"
            strongest_source, evidence_level = "", ""
        rows.append(
            {
                "category": atom.get("category") or "",
                "label": atom.get("label") or "",
                "status": status,
                "strongest_source": strongest_source,
                "evidence_level": evidence_level,
            }
        )
    return rows

_PATENT_KIND_SUFFIX_RE = re.compile(r"([A-Z]{2}\d{4,})[A-Z]\d?$")
_GOOGLE_PATENT_URL_ID_RE = re.compile(r"patents\.google\.com/patent/([a-z0-9]+)", re.IGNORECASE)


def _hit_doc_ids(hit: dict[str, Any]) -> set[str]:
    """Build canonical document identifiers for matching across sources."""
    ids: set[str] = set()
    patent_number = str(hit.get("patent_number") or "").upper().strip()
    if patent_number and patent_number != "UNKNOWN":
        ids.add("pn:" + _PATENT_KIND_SUFFIX_RE.sub(r"\1", patent_number))
    doi = str(hit.get("doi") or "").lower().strip().rstrip(".,;")
    if doi:
        ids.add("doi:" + doi)
    url = str(hit.get("url") or "").lower().strip().rstrip("/")
    if url:
        url_match = _GOOGLE_PATENT_URL_ID_RE.search(url)
        if url_match:
            ids.add("pn:" + _PATENT_KIND_SUFFIX_RE.sub(r"\1", url_match.group(1).upper()))
        else:
            ids.add("url:" + url)
    return ids


def _mark_cross_source_corroboration(pack: dict[str, Any]) -> list[str]:
    """Find documents confirmed by more than one source type and mark those hits.

    Returns a list of readable descriptions of the corroborated documents, and
    sets the `cross_source_corroborated` flag on the hits in the pack itself.
    """
    sources = (("patent", "patents"), ("publication", "publications"), ("web", "web"))
    id_to_sources: dict[str, set[str]] = {}
    id_display: dict[str, str] = {}
    per_source_hits: dict[str, list[Any]] = {}
    for source_name, source_key in sources:
        hits = _as_list(_as_dict(pack.get(source_key)).get("hits"))
        per_source_hits[source_name] = hits
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            for doc_id in _hit_doc_ids(hit):
                id_to_sources.setdefault(doc_id, set()).add(source_name)
                if doc_id not in id_display:
                    display = str(
                        hit.get("patent_number") or hit.get("doi") or hit.get("title") or hit.get("url") or ""
                    ).strip()
                    id_display[doc_id] = display[:120]
    corroborated_ids = {doc_id for doc_id, names in id_to_sources.items() if len(names) >= 2}
    if not corroborated_ids:
        return []
    for hits in per_source_hits.values():
        for hit in hits:
            if isinstance(hit, dict) and _hit_doc_ids(hit) & corroborated_ids:
                hit["cross_source_corroborated"] = True
    labels: list[str] = []
    seen_display: set[str] = set()
    for doc_id in sorted(corroborated_ids):
        display = id_display.get(doc_id) or doc_id
        if display.lower() in seen_display:
            continue
        seen_display.add(display.lower())
        source_names = ", ".join(sorted(id_to_sources[doc_id]))
        labels.append(f"{display} ({source_names})")
    return labels


def _pseudo_atoms_from_requirements(
    critical_requirements: list[str],
) -> list[dict[str, Any]]:
    """Build display atoms from textual critical requirements when no atomic decomposition exists."""
    pseudo: list[dict[str, Any]] = []
    for requirement in critical_requirements or []:
        keyword_sets = _requirement_keyword_sets([str(requirement)], None)
        if not keyword_sets:
            continue
        pseudo.append(
            {
                "category": "",
                "label": str(requirement),
                "terms": keyword_sets[0],
            }
        )
    return pseudo


def build_user_answer_payload(
    merged_pack: Any,
    original_query: str = "",
    query_envelope: dict[str, Any] | None = None,
    debug_report: str = "",
    debug_mode: bool = False,
    retrieval_status_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build the structured payload containing the user-facing answer."""
    pack = _parse_pack(merged_pack)
    query = str(original_query or pack.get("query") or "")
    corroborated_documents = _mark_cross_source_corroboration(pack)
    source_grades = _source_grades(pack)
    envelope = _as_dict(query_envelope)
    critical_requirements = [
        str(item) for item in envelope.get("critical_requirements") or [] if str(item).strip()
    ]
    atomic_requirements_raw = envelope.get("critical_requirements_atomic") or []
    atomic_requirements: list[dict[str, Any]] = [
        item for item in atomic_requirements_raw if isinstance(item, dict)
    ]
    requirement_coverage = _requirement_coverage(
        pack, critical_requirements, atomic_requirements=atomic_requirements
    )
    single_hit_full_coverage = _single_hit_covers_all_atoms(pack, atomic_requirements)
    verdict, confidence, retrieval = decide_verdict_and_confidence(
        source_grades,
        critical_requirements=critical_requirements or None,
        requirement_coverage=requirement_coverage,
        single_hit_full_coverage=single_hit_full_coverage,
        original_query=query,
    )
    if verdict not in VERDICTS:
        verdict = "partial_retrieval"
    if confidence not in CONFIDENCES:
        confidence = "low"
    exact_combination_found = any(
        bool(grade.get("exact_combination_candidate_found"))
        for grade in (source_grades or {}).values()
    )
    if (
        retrieval in {"partial", "degraded", "mixed_partial", "failed"}
        and not exact_combination_found
        and confidence == "high"
    ):
        confidence = "medium"
    display_atomic_requirements = atomic_requirements or _pseudo_atoms_from_requirements(
        critical_requirements
    )
    per_requirement_status = (
        _per_requirement_status(pack, display_atomic_requirements)
        if display_atomic_requirements
        else []
    )
    language = _detect_language(query, query_envelope)
    source_counts = _source_counts(pack)
    user_answer = _render_user_answer(
        query,
        pack,
        verdict,
        confidence,
        retrieval,
        source_grades,
        language,
        critical_requirements=critical_requirements,
        requirement_coverage=requirement_coverage,
        atomic_requirements=display_atomic_requirements,
        retrieval_status_notes=retrieval_status_notes or [],
        corroborated_documents=corroborated_documents,
    )
    novelty, _ = _novelty_score(verdict, confidence, retrieval)
    has_exact_candidate = any(
        data.get("exact_combination_candidate_found") is True
        for data in source_grades.values()
    )

    payload: dict[str, Any] = {
        "source_type": "research_session_user_answer",
        "status": "ok",
        "user_answer": user_answer,
        "verdict": verdict,
        "confidence": confidence,
        "retrieval_completeness": retrieval,
        "language": language,
        "word_count": _word_count(user_answer),
        "novelty_score_percent": novelty,
        "source_counts": source_counts,
        "retrieval_status_notes": list(retrieval_status_notes or []),
        "source_quality_grades": {
            source: str(data.get("quality_grade") or "missing")
            for source, data in source_grades.items()
        },
        "critical_requirement_count": len(critical_requirements),
        "requirement_coverage_by_source": requirement_coverage,
        "critical_requirement_statuses": per_requirement_status,
        "single_source_contains_all_critical_elements": single_hit_full_coverage or has_exact_candidate,
        "corroborated_documents": corroborated_documents,
    }
    if debug_mode:
        payload["debug_report"] = debug_report
    return json.loads(json.dumps(payload, ensure_ascii=False))