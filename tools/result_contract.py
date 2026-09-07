"""Shared markers and status information for search results."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

RetrievalStatus = Literal["ok", "partial_failure", "failed"]

GENERIC_MECHANISM_TOKENS = {
    "activate",
    "activated",
    "activation",
    "automatic",
    "automated",
    "control",
    "controlled",
    "trigger",
    "triggered",
    "self",
    "apparatus",
    "device",
    "mechanism",
    "method",
    "process",
    "system",
    "external",
    "internal",
    "portable",
    "electric",
    "electrical",
    "energy",
    "power",
    "safe",
    "safety",
}

EVIDENCE_LEVEL_MULTIPLIER = {
    "claim_verified": 1.2,
    "abstract_verified": 1.08,
    "verified_metadata": 1.0,
    "verified_page": 1.0,
    "fetched_excerpt": 1.0,
    "search_snippet_only": 0.82,
    "citation_only": 0.65,
    "fetch_timeout": 0.5,
    "fetch_failed": 0.45,
    "provider_error": 0.35,
    "unverified": 0.7,
}

STRONG_EVIDENCE_LEVELS = {"claim_verified", "abstract_verified"}
MEDIUM_EVIDENCE_LEVELS = {"verified_metadata", "verified_page", "fetched_excerpt"}
WEAK_EVIDENCE_LEVELS = {"search_snippet_only", "citation_only"}
FAILED_EVIDENCE_LEVELS = {"fetch_timeout", "fetch_failed", "provider_error"}

def evidence_level_multiplier(evidence_level: str | None) -> float:
    """Return the weight of an evidence level for local scoring."""
    return EVIDENCE_LEVEL_MULTIPLIER.get(str(evidence_level or "").strip().lower(), 0.7)

@dataclass
class NormalizedResult:
    status: RetrievalStatus
    completed: bool
    reliable_no_results: bool
    query: str = ""
    hits: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

def _bool(value: bool) -> str:
    """Convert a boolean into the text form used in output."""
    return "TRUE" if value else "FALSE"

def status_markers(result: NormalizedResult) -> str:
    """Build the standard status information for a tool's output."""
    lines = [
        f"STATUS: {result.status.upper()}",
        f"COMPLETED: {_bool(result.completed)}",
        f"RELIABLE_NO_RESULTS: {_bool(result.reliable_no_results)}",
        f"ERROR_COUNT: {len(result.errors)}",
    ]
    if result.query:
        lines.append(f"QUERY: {result.query}")
    for error in result.errors:
        lines.append(f"ERROR: {error.get('type', 'error')} - {error.get('message', 'unknown')}")
    for note in result.notes:
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)

def prepend_markers(result: NormalizedResult, body: str) -> str:
    """Prepend status information to the textual body of a result."""
    return f"{status_markers(result)}\n\n{body.strip()}".strip()

def parse_status_marker(text: str) -> str | None:
    """Read the tool status out of the status markers."""
    found = re.search(r"^STATUS:\s*([A-Z_]+)\s*$", text or "", flags=re.I | re.M)
    return found.group(1).lower() if found else None

def parse_completed_marker(text: str) -> bool | None:
    """Read the completion flag out of the status markers."""
    found = re.search(r"^COMPLETED:\s*(TRUE|FALSE)\s*$", text or "", flags=re.I | re.M)
    return found.group(1).upper() == "TRUE" if found else None

def parse_reliable_no_results_marker(text: str) -> bool | None:
    """Read the reliable-no-results flag out of the status markers."""
    found = re.search(r"^RELIABLE_NO_RESULTS:\s*(TRUE|FALSE)\s*$", text or "", flags=re.I | re.M)
    return found.group(1).upper() == "TRUE" if found else None


def parse_error_count(text: str) -> int | None:
    """Read the error count out of the status markers."""
    found = re.search(r"^ERROR_COUNT:\s*(\d+)\s*$", text or "", flags=re.I | re.M)
    return int(found.group(1)) if found else None

def has_hits(text: str) -> bool:
    """Determine whether a result body contains at least one source link."""
    if not text:
        return False
    body = re.sub(r"^(STATUS|COMPLETED|RELIABLE_NO_RESULTS|ERROR_COUNT|QUERY|ERROR|NOTE):.*$", "", text, flags=re.I | re.M)
    return bool(re.search(r"https?://|Patent page URL|Semantic Scholar URL|Paper landing page|Source page URL", body))