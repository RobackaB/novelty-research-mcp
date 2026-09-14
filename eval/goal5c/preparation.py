"""Synthetic reviewed candidate inventories and blank blinded worksheets.

Always re-import the snapshot. This module neither certifies a complete pool nor
collects labels, computes metrics, authenticates reviewers or acquires documents.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .contracts import (
    ContractError, PROTOCOL_VERSION, _choice, _header, _index_rows, _list,
    _object, _text, canonical_json_bytes, sha256_json,
)
from .projection import project_text, validate_visible_text
from .snapshot import import_snapshot

PLAN_VERSION = "goal5c.preparation_plan.v1"
RUBRIC_VERSION = "goal5c.relevance.v1"
SELECTION_RULE = "attempt_then_snapshot_row_then_representation.v1"


def _require(condition: bool) -> None:
    if not condition:
        raise ContractError("invalid preparation reference or review")


def _review(row: dict) -> None:
    _text(row["reviewer"])
    _text(row["review_note"])


def _exact_index(rows: Any, key: str, expected: set[str]) -> dict:
    indexed = _index_rows(rows, key)
    _require(set(indexed) == expected)
    return indexed


def _locator(value: Any) -> None:
    if value == "":
        return
    _text(value)
    parsed = urlsplit(value)
    _require(parsed.scheme in {"https", "http"} and bool(parsed.hostname)
             and not parsed.username and not parsed.password and not parsed.fragment)
    _require(not any(char.isspace() or ord(char) < 32 for char in value))
    validate_visible_text(value)
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
        validate_visible_text(decoded)
    _require(re.search(r"%[0-9a-fA-F]{2}", decoded) is None)


def _validated_plan(bundle: dict, plan: dict) -> tuple[dict, dict, dict]:
    canonical_json_bytes(plan)
    _object(plan, {"schema_version", "protocol_version", "data_class", "plan_version",
                   "capture_bundle_sha256", "blind_seed", "rubric_version", "queries",
                   "identity_reviews", "projection_reviews"})
    _header(plan, PLAN_VERSION)
    _text(plan["plan_version"])
    _require(plan["capture_bundle_sha256"] == sha256_json(bundle))
    _require(plan["rubric_version"] == RUBRIC_VERSION)
    seed = plan["blind_seed"]
    _require(type(seed) is str and len(seed) == 64
             and all(char in "0123456789abcdef" for char in seed))
    queries = _exact_index(plan["queries"], "query_id", {row["query_id"] for row in bundle["queries"]})
    for row in queries.values():
        _object(row, {"query_id", "topic_card", "approved_translation", "reviewer", "review_note"})
        _review(row)
        _text(row["topic_card"])
        _require(type(row["approved_translation"]) is str)
        validate_visible_text(row["topic_card"])
        validate_visible_text(row["approved_translation"])
    examples = _index_rows(bundle["examples"], "example_id")
    identities = _exact_index(plan["identity_reviews"], "example_id", set(examples))
    entity_locators: dict[tuple, str] = {}
    for identifier, row in identities.items():
        _object(row, {"example_id", "resolution", "document_entity_id", "document_version_id",
                      "document_locator", "possible_equivalence", "reviewer", "review_note"})
        _review(row)
        _choice(row["resolution"], {"resolved", "provisional", "excluded"})
        _list(row["possible_equivalence"])
        links = row["possible_equivalence"]
        _require(all(type(link) is str and link in examples and link != identifier for link in links))
        _require(len(set(links)) == len(links))
        _require(all(examples[link]["query_id"] == examples[identifier]["query_id"] for link in links))
        if row["resolution"] == "resolved":
            _text(row["document_entity_id"])
            _text(row["document_version_id"])
            _locator(row["document_locator"])
            key = (examples[identifier]["query_id"], row["document_entity_id"], row["document_version_id"])
            # A shared entity must have one reviewed locator; no source decides it.
            previous = entity_locators.setdefault(key, row["document_locator"])
            _require(previous == row["document_locator"] and not links)
        else:
            _require(row["document_entity_id"] == row["document_version_id"] == row["document_locator"] == "")
    representations = _index_rows(bundle["representations"], "representation_id")
    projections = _exact_index(plan["projection_reviews"], "representation_id", set(representations))
    projected = {}
    for identifier, row in projections.items():
        _object(row, {"representation_id", "title_spans", "content_spans", "reviewer", "review_note"})
        _review(row)
        representation = representations[identifier]
        projected[identifier] = {
            "title": project_text(representation["title"], row["title_spans"]),
            "content": project_text(representation["score_text"], row["content_spans"]),
        }
    return queries, identities, projected


def _event_key(event: dict, representation_id: str) -> tuple:
    # SQLite row order is only a reproducible tie-breaker, not provider chronology.
    return event["captured"]["attempt"], event["captured"]["id"], representation_id


def _fp1_events(representation: dict, events: dict) -> list[dict]:
    return [events[identifier] for identifier in representation["fp1_provenance_event_ids"]
            if events[identifier]["captured"]["attempt"] == 1]


def prepare_snapshot(snapshot: Path, manifest: dict, plan: dict) -> tuple[dict, dict]:
    """Return separate worksheet and analyst artifacts from synthetic inputs.

    Reviews are explicit declarations, not authenticated identity or privacy
    approval. Only the worksheet is for annotators; never disclose the plan/map.
    """
    bundle = import_snapshot(snapshot, manifest)
    queries, identities, projected = _validated_plan(bundle, plan)
    events = _index_rows(bundle["raw_events"], "raw_event_id")
    representations = _index_rows(bundle["representations"], "representation_id")
    entities: dict[tuple, list[dict]] = {}
    inventory: dict[tuple, list[tuple]] = {}
    dispositions = []
    for example in bundle["examples"]:
        identifier = example["example_id"]
        identity = identities[identifier]
        resolved = identity["resolution"] == "resolved"
        entity = ((example["query_id"], identity["document_entity_id"], identity["document_version_id"])
                  if resolved else (example["query_id"], "provisional", identifier))
        if resolved:
            entities.setdefault(entity, []).append(example)
        dispositions.append({"example_id": identifier, "identity_review": identity,
                             "raw_event_ids": example["raw_event_ids"],
                             "representation_ids": example["representation_ids"]})
        # Inventory retains unresolved and excluded records too: no silent pool
        # deletion based on identity/label review or lack of safe display text.
        for rep_id in example["representation_ids"]:
            representation = representations[rep_id]
            for event in _fp1_events(representation, events):
                inventory.setdefault((resolved, entity, example["source_type"]), []).append(
                    (_event_key(event, rep_id), example, representation, event))

    inventory_rows = []
    for (_resolved, entity, source), occurrences in sorted(inventory.items()):
        _, example, representation, event = min(occurrences, key=lambda item: item[0])
        inventory_rows.append({
            "query_id": entity[0], "source_type": source,
            "identity_resolution": identities[example["example_id"]]["resolution"],
            "document_entity_id": identities[example["example_id"]]["document_entity_id"],
            "document_version_id": identities[example["example_id"]]["document_version_id"],
            "example_ids": sorted({item[1]["example_id"] for item in occurrences}),
            "representation_id": representation["representation_id"], "raw_event_id": event["raw_event_id"],
            "title": representation["title"], "score_text": representation["score_text"],
            "metric_ready": False,
        })

    items, unblinding, withheld = [], [], []
    for entity, examples in sorted(entities.items()):
        choices = []
        for example in examples:
            for rep_id in example["representation_ids"]:
                if not projected[rep_id]["content"].strip():
                    continue
                for event_id in representations[rep_id]["raw_event_ids"]:
                    choices.append((_event_key(events[event_id], rep_id), rep_id, event_id))
        if not choices:
            withheld.append({"query_id": entity[0], "document_entity_id": entity[1],
                             "document_version_id": entity[2], "reason": "no_reviewed_substantive_text"})
            continue
        _, rep_id, event_id = min(choices)
        truth_key = [*entity, RUBRIC_VERSION]
        blind_id = hmac.new(bytes.fromhex(plan["blind_seed"]), canonical_json_bytes(truth_key), hashlib.sha256).hexdigest()
        identity = identities[examples[0]["example_id"]]
        query = queries[entity[0]]
        # Allowlist only. No source, identity key, seed, hashes, attempts, scores,
        # reasons, ranks or projection metadata in the worksheet.
        items.append({"item_id": blind_id, "topic_card": query["topic_card"],
                      "approved_translation": query["approved_translation"], **projected[rep_id],
                      "document_locator": identity["document_locator"],
                      "response": {"label": None, "evidence_basis": None,
                                   "rationale": "", "supporting_passage": ""}})
        unblinding.append({"item_id": blind_id, "truth_key": truth_key,
                           "example_ids": sorted(example["example_id"] for example in examples),
                           "representation_id": rep_id, "raw_event_id": event_id})
    worksheet = {
        "schema_version": "goal5c.blind_worksheet.v1", "protocol_version": PROTOCOL_VERSION,
        "data_class": "synthetic", "rubric_version": RUBRIC_VERSION,
        "instructions": {
            "relevant": "Substantive evidence directly addresses the registered subject, task or a material aspect.",
            "not_relevant": "Adequate evidence establishes another subject/task or only generic vocabulary overlap.",
            "uncertain": "Insufficient evidence, unresolved identity, access or language prevents judgment.",
            "evidence_basis": ["title_snippet", "abstract", "full_document", "unavailable"],
            "limits": "Relevance does not establish novelty or complete feature coverage. This is a blank synthetic worksheet.",
        },
        "items": sorted(items, key=lambda row: row["item_id"]),
    }
    source_slots = []
    for query in bundle["queries"]:
        for source in ("patent", "publication", "web"):
            executions = [row for row in bundle["executions"]
                          if row["query_id"] == query["query_id"] and row["source_type"] == source]
            source_slots.append({"query_id": query["query_id"], "source_type": source,
                                 "execution_ids": [row["execution_id"] for row in executions],
                                 "state": "observed_executions" if executions else "unexecuted",
                                 "pool_completeness": "unverified", "workflow_membership": "unavailable"})
    analyst = {
        "schema_version": "goal5c.preparation_audit.v1", "protocol_version": PROTOCOL_VERSION,
        "data_class": "synthetic", "capture_bundle_sha256": sha256_json(bundle),
        "preparation_plan_sha256": sha256_json(plan), "worksheet_sha256": sha256_json(worksheet),
        "selection_rule": SELECTION_RULE, "scope": "observed_first_attempt_candidate_inventory",
        "metric_ready": False, "source_slots": source_slots,
        "executions": bundle["executions"], "dispositions": dispositions,
        "projection_reviews": sorted(plan["projection_reviews"], key=lambda row: row["representation_id"]),
        "first_attempt_inventory": inventory_rows, "withheld_entities": withheld,
        "unblinding": sorted(unblinding, key=lambda row: row["item_id"]),
    }
    return worksheet, analyst
