"""Helpers for bounding the number of search variants."""

from __future__ import annotations

import re

PATENT_SEARCH_DOMAINS = [
    "patents.google.com",
    "patents.justia.com",
    "worldwide.espacenet.com",
    "patentscope.wipo.int",
    "lens.org",
]
SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9_ -]{1,48}:")


def _dedupe_candidates(candidates: list[str], source: str, max_variants: int) -> list[str]:
    """Remove duplicate query variants and keep only the permitted number."""
    seen: set[str] = set()
    variants = []
    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", candidate).strip(" .:-")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append(cleaned)
        if len(variants) >= max(1, max_variants):
            break
    return variants or [source.strip()]


def section_query_variants(query: str, label: str, max_variants: int = 2, legacy_marker: str | None = None) -> list[str]:
    """Read the query variants from a plan section or from the older text format."""
    source = query or ""
    lines = source.splitlines()
    candidates: list[str] = []
    in_section = False
    normalized_label = label.upper().rstrip(":") + ":"
    for line in lines:
        stripped = line.strip()
        if SECTION_HEADER_RE.match(stripped):
            if in_section:
                break
            in_section = stripped.upper().startswith(normalized_label)
            stripped = stripped.split(":", 1)[1].strip() if in_section else ""
        if in_section and stripped:
            candidates.append(re.sub(r"^[-*]\s*", "", stripped))
    if not candidates and legacy_marker:
        legacy_line = next((line for line in lines if legacy_marker.lower() in line.lower()), source)
        legacy_line = re.sub(rf"^.*?{re.escape(legacy_marker)}[^:]*:\s*", "", legacy_line, flags=re.I)
        candidates = re.split(r"\s+\|\s+|\n|;", legacy_line)
    return _dedupe_candidates(candidates, source, max_variants)


def compact_query_variants(query: str, max_variants: int = 2) -> list[str]:
    """Return a shortened list of patent query variants."""
    return section_query_variants(query, "PATENT_QUERIES", max_variants, "patent search queries")


def patent_search_plan(query: str, max_variants: int = 2, max_domains: int = 5, max_requests: int = 10) -> list[tuple[str, str]]:
    """Prepare the combinations of patent queries and allowed domains."""
    plan: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for variant in compact_query_variants(query, max_variants=max_variants):
        for domain in PATENT_SEARCH_DOMAINS[:max(1, max_domains)]:
            item = (variant, domain)
            if item in seen:
                continue
            seen.add(item)
            plan.append(item)
            if len(plan) >= max(1, max_requests):
                return plan
    return plan
