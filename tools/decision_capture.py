"""Passive capture of candidate decisions for offline evaluation.

The evaluation dataset needs the candidates a scorer *rejected*, and those are
discarded inside the search layer as soon as the decision is made. The search
layer, however, has no session context: `publications_search(query, max_results,
english_query)` knows nothing about the session, run or attempt it serves.

This module resolves that split. A `DecisionCollector` is created by
`research_session`, which owns the provenance, and passed down as an optional
keyword argument. Low-level code records decision-specific fields only; the
collector stamps the immutable context. Nothing here writes to a database or a
network, and nothing here is ever read by scoring or ranking code.

Safety contract, relied on by the tests:

* append-only, copied primitive data, never a reference to a live candidate
* `record()` cannot raise into the caller -- `safe_record` swallows everything
* `score_text` is preserved byte for byte, never truncated
* a collector that raises on every call must leave retrieval output unchanged
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION: Final = "candidate_decision.v1"

# Closed enums. A value outside these sets is recorded as-is but flagged by the
# schema test, so a typo cannot silently create a new category.
DECISION_STAGES: Final[frozenset[str]] = frozenset(
    {
        "candidate_created",
        "url_gate",
        "content_gate",
        "merge_collision",
        "provider_filter",
        "dedupe",
        "merged_rerank",
        "fill_back",
        "domain_anchor",
        "threshold_filter",
        "truncation",
        "cache_lookup",
    }
)

DECISION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "below_threshold",
        "no_discriminative_term",
        "low_value_url",
        "url_policy_rejected",
        "error_page",
        "duplicate_of",
        "discarded_duplicate_url",
        "below_overlap_gate",
        "beyond_limit",
        "restored_to_meet_minimum",
        "restored_after_empty_rerank",
        "relaxed_threshold_applied",
        "floor_applied",
        "cache_hit",
        "cache_miss",
        "no_candidates",
    }
)

# Stages where a keep/reject decision genuinely happened. Everywhere else
# `retained` stays None, because true/false would misdescribe the operation.
_RELEVANCE_STAGES: Final[frozenset[str]] = frozenset(
    {"provider_filter", "merged_rerank", "domain_anchor", "threshold_filter", "fill_back"}
)

_WHITESPACE_RE = re.compile(r"\s+")

# Unit and record separators. Neither can occur in a query or a title, so the
# composed hash inputs cannot collide through a delimiter appearing in the data.
_UNIT_SEP: Final = "\x1f"
_RECORD_SEP: Final = "\x1e"


def normalize_query_for_hash(query: str) -> str:
    """Match research_session normalization without importing the writer layer."""
    normalized = unicodedata.normalize("NFKC", str(query or ""))
    normalized = _WHITESPACE_RE.sub(" ", normalized.strip())
    return normalized.lower()


def query_fingerprint(original_query: str) -> str:
    """Identify an evaluation query by its original text alone.

    Deliberately excludes the atomic requirements: those are re-derived per
    session, so folding them in would let a change in decomposition turn one
    semantic query into two independent samples and inflate the query count.
    session_id and run_id are provenance and are excluded for the same reason.
    """
    normalized = normalize_query_for_hash(original_query)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def query_envelope_hash(atomic_requirements: Any) -> str:
    """Hash the stored atomic schema, independently of primary query identity.

    None means unavailable; [] is an explicitly empty decomposition. Preserve
    list order: query variants use atom order and term prefixes. Dictionary key
    order and non-schema metadata are irrelevant. Reject malformed atoms rather
    than silently presenting partial provenance as a valid decomposition.
    """
    if atomic_requirements is None:
        return ""
    if not isinstance(atomic_requirements, list):
        raise ValueError("invalid atomic requirement list")
    atoms: list[dict[str, Any]] = []
    for atom in atomic_requirements:
        if (
            not isinstance(atom, dict)
            or not isinstance(atom.get("category"), str)
            or not isinstance(atom.get("label"), str)
            or not isinstance(atom.get("terms"), list)
            or not all(isinstance(term, str) for term in atom["terms"])
            or not isinstance(atom.get("too_broad_for_element_retry"), bool)
        ):
            raise ValueError("invalid atomic requirement schema")
        atoms.append({key: atom[key] for key in (
            "category", "label", "terms", "too_broad_for_element_retry",
        )})
    canonical = json.dumps(
        {"schema": "atomic_requirements.v1", "atoms": atoms},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def candidate_identity(
    canonical_id: str = "", url: str = "", title: str = "", score_text: str = ""
) -> tuple[str, str]:
    """Return (identity, identity_kind) using the agreed fallback order."""
    clean_id = str(canonical_id or "").strip()
    if clean_id:
        return clean_id, "canonical_id"
    clean_url = _normalize_url(url)
    if clean_url:
        return clean_url, "url"
    basis = f"{normalize_query_for_hash(title)}{_UNIT_SEP}{normalize_query_for_hash(score_text)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32], "text_hash"


# Only these are removed. Dropping the whole query string would merge
# ?id=1 and ?id=2 into one identity, collapsing two distinct documents.
_TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
        "_ga", "igshid", "spm", "yclid", "dclid", "wbraid", "gbraid",
    }
)
# Deliberately NOT treated as tracking: ref, referrer, source. They are generic
# names that carry real meaning on many sites (?source=manual vs ?source=api,
# ?ref=1 vs ?ref=2), and collapsing them would merge distinct documents.


def _normalize_url(url: str) -> str:
    """Normalise a URL for identity, preserving meaningful query parameters.

    Scheme, a leading www., the fragment and any trailing slash are dropped.
    Query parameters are kept except for an explicit tracking list, and repeated
    meaningful parameters are preserved in order, because ?tag=a&tag=b is not
    the same document as ?tag=a.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    # Scheme and host are case-insensitive per RFC 3986; the path and query are
    # not. Lowercasing everything merged /Article with /article and ?id=ABC with
    # ?id=abc, collapsing genuinely distinct documents into one identity.
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    text = text.split("#", 1)[0]
    path, _, query = text.partition("?")
    host, slash, rest = path.partition("/")
    path = host.lower() + slash + rest
    path = path.rstrip("/")
    if not query:
        return path
    kept = [
        pair
        for pair in query.split("&")
        # Only the parameter NAME is matched case-insensitively; the value keeps
        # its case, so ?id=ABC and ?id=abc remain distinct.
        if pair and pair.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    ]
    return f"{path}?{'&'.join(kept)}" if kept else path


def example_id(fingerprint: str, source_type: str, identity: str) -> str:
    """Identify one labellable example: a candidate under a specific query.

    Query-scoped, so the same document judged against a different query is a
    different example. Stage, provider, attempt and retry are excluded, so one
    candidate seen many times in one query collapses to a single human label
    with many decision traces attached.
    """
    basis = _RECORD_SEP.join([fingerprint, str(source_type or "").strip().lower(), identity])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CollectorContext:
    """Immutable provenance owned by research_session, stamped onto every event."""

    session_id: str = ""
    run_id: str = ""
    source_type: str = ""
    attempt: int = 0
    query_fingerprint: str = ""
    query_envelope_hash: str = ""


@dataclass
class DecisionCollector:
    """Append-only in-memory sink for candidate decisions.

    Holds no connection and performs no I/O. Production ranking and filtering
    never read `events`; the only consumer is research_session's persistence
    step, which runs after the evidence pack has returned.
    """

    context: CollectorContext = field(default_factory=CollectorContext)
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        decision_stage: str,
        decision_reason: str,
        score_text: str = "",
        title: str = "",
        identity_title: str | None = None,
        identity_score_text: str | None = None,
        canonical_id: str = "",
        url: str = "",
        decision_query: str = "",
        query_variant: str = "",
        provider: str = "",
        score_at_decision: float | None = None,
        threshold_at_decision: float | None = None,
        retained: bool | None = None,
        dataset_eligible: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one decision. Copies every value; never touches the candidate."""
        if decision_stage not in DECISION_STAGES:
            raise ValueError(f"unknown decision_stage: {decision_stage!r}")
        if decision_reason not in DECISION_REASONS:
            raise ValueError(f"unknown decision_reason: {decision_reason!r}")
        identity, identity_kind = candidate_identity(
            canonical_id=canonical_id, url=url,
            title=title if identity_title is None else identity_title,
            score_text=score_text if identity_score_text is None else identity_score_text,
        )
        # `retained` is meaningful only where a keep/reject decision happened.
        # Structural events (dedupe, merge collision, truncation) leave it NULL
        # rather than claiming a relevance verdict that was never made.
        effective_retained = retained if decision_stage in _RELEVANCE_STAGES else None
        self.events.append(
            {
                "schema_version": SCHEMA_VERSION,
                "query_fingerprint": self.context.query_fingerprint,
                "query_envelope_hash": self.context.query_envelope_hash,
                "session_id": self.context.session_id,
                "run_id": self.context.run_id,
                "source_type": self.context.source_type,
                "attempt": int(self.context.attempt),
                "decision_stage": str(decision_stage),
                "decision_reason": str(decision_reason),
                "candidate_identity": identity,
                "candidate_identity_kind": identity_kind,
                "example_id": example_id(
                    self.context.query_fingerprint, self.context.source_type, identity
                ),
                "title": str(title or ""),
                # Stored verbatim. The dataset builder needs the exact text the
                # scorer saw, so this must never be trimmed or summarised.
                "score_text": str(score_text or ""),
                # The query actually executed at this decision, kept separate
                # from the fingerprint so retries and variants of one original
                # query stay a single evaluation sample.
                "decision_query": str(decision_query or ""),
                "query_variant": str(query_variant or ""),
                "provider": str(provider or ""),
                "score_at_decision": None if score_at_decision is None else float(score_at_decision),
                "threshold_at_decision": (
                    None if threshold_at_decision is None else float(threshold_at_decision)
                ),
                "retained": effective_retained,
                # False for anything that is not a candidate document: provider
                # marker headers, cache-lookup observations. The trace is kept,
                # but the dataset builder must never offer these for labelling.
                "dataset_eligible": bool(dataset_eligible),
                # A recursive JSON snapshot prevents later nested mutations
                # from rewriting captured provenance or retaining live objects.
                "payload": (
                    json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
                    if isinstance(payload, dict) else None
                ),
            }
        )


def safe_record(collector: Any, **fields: Any) -> None:
    """Record a decision without letting capture affect retrieval.

    Every hook in the search and evidence-pack layers calls this rather than
    `collector.record` directly, so a collector that is None, malformed or
    raising can never change what the pipeline returns.
    """
    if collector is None:
        return
    try:
        collector.record(**fields)
    except Exception as exc:  # noqa: BLE001 - capture must never surface to the caller
        _warn_capture_failure(LOGGER, "decision capture failed; retrieval is unaffected", exc)


def _warn_capture_failure(logger: logging.Logger, message: str, error: Exception) -> None:
    """Warn without exposing request data or letting a broken handler escape."""
    try:
        logger.warning("%s (%s)", message, type(error).__name__)
    except Exception:  # noqa: BLE001 - diagnostics must remain fail-open
        pass


@contextmanager
def capture_scope(variable: ContextVar[Any], value: Any) -> Iterator[None]:
    """Scope optional instrumentation without changing the body's exceptions."""
    token = None
    try:
        token = variable.set(value)
    except Exception as exc:  # noqa: BLE001 - capture setup is diagnostic only
        _warn_capture_failure(LOGGER, "decision capture context setup failed", exc)
    try:
        yield
    finally:
        if token is not None:
            try:
                variable.reset(token)
            except Exception as exc:  # noqa: BLE001 - preserve the retrieval outcome
                _warn_capture_failure(LOGGER, "decision capture context reset failed", exc)
