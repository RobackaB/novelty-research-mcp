# Goal 5C Phase 2: synthetic reviewed preparation

This phase implements the extraction/blinding preparation boundary in the
[approved protocol](goal5c-protocol.md), using the
[Phase-1 snapshot foundation](goal5c-offline-foundation.md). It produces a reviewed
observed candidate inventory and **blank synthetic worksheets**. It does not
acquire documents, collect labels, establish a complete pool, materialize scorer
inputs, select production workflow outputs or compute metrics.

All inputs remain `data_class: "synthetic"` and
`protocol_version: "goal5c.protocol.v1"`. Production scoring, ranking, thresholds,
providers, MCP interfaces, historical dataset and evaluator are unchanged.
Operational privacy/recruitment gates remain mandatory and unopened by this CLI.

## API and workflow

```python
from pathlib import Path
from eval.goal5c.preparation import prepare_snapshot

worksheet, analyst = prepare_snapshot(Path("synthetic.sqlite"), manifest, plan)
```

The function **re-imports the original snapshot** through the Phase-1 read-only
importer, including its checksum, storage/provenance and execution checks. It does
not accept an edited capture bundle as a substitute. The review plan must bind to
the canonical SHA-256 of the re-imported bundle. Imports retain the Phase-1 fixed
corrected capture baseline and synthetic-only validation.

Every imported query needs a topic-card review, every example an identity review,
and every representation a projection review, including records ultimately
withheld or excluded. Missing, duplicate and unknown references fail validation.
Prepare a new versioned plan when reviews change; retain the original synthetic
snapshot, manifest and plan to regenerate both output artifacts.

## Review-plan contract

The exact top-level fields for `goal5c.preparation_plan.v1` are:

| Field | Meaning |
| --- | --- |
| `schema_version` | `goal5c.preparation_plan.v1` |
| `protocol_version` | `goal5c.protocol.v1` |
| `data_class` | `synthetic` |
| `plan_version` | Non-empty version identifier for this review plan. |
| `capture_bundle_sha256` | Canonical SHA-256 of the imported Phase-1 bundle. |
| `blind_seed` | Exactly 64 lowercase hexadecimal characters; analyst-only seed. |
| `rubric_version` | `goal5c.relevance.v1` |
| `queries` | Exactly one query-review row per imported query. |
| `identity_reviews` | Exactly one identity-review row per imported example. |
| `projection_reviews` | Exactly one projection-review row per representation. |

Each `queries` row has `query_id`, `topic_card`, `approved_translation`, `reviewer`
and `review_note`. The topic card and review declarations must be non-empty;
`approved_translation` may be empty. Visible fields pass the same conservative
metadata checks as evidence. A reviewer declaration records responsibility; the
code does not authenticate that person or certify their judgment.

Each `identity_reviews` row has `example_id`, `resolution`, `document_entity_id`,
`document_version_id`, `document_locator`, `possible_equivalence`, `reviewer` and
`review_note`. Resolution is `resolved`, `provisional` or `excluded`.

- Resolved rows require non-empty document entity/version identifiers, no
  possible-equivalence links, and a reviewed locator or an empty locator.
- Provisional/excluded rows require empty entity/version/locator fields. Their
  possible-equivalence lists may name distinct imported examples for the same
  query, without duplicates or self-links. These links never merge documents.
- All resolved examples sharing a query/entity/version must specify the same
  locator. A locator must be an HTTP(S) URL without credentials, fragment or
  whitespace. Encoded residual metadata is checked conservatively too.

Matching IDs, URLs, text hashes or provider deduplication do not silently reconcile
documents. Explicit reviews determine the mapping. The validator checks consistency,
not whether the claimed identity is true. Keep distinct versions and unresolved
text-only records separate until reviewed evidence supports reconciliation.

Each `projection_reviews` row has `representation_id`, `title_spans`,
`content_spans`, `reviewer` and `review_note`. Both span fields are lists of
`[start, end]` integer pairs; they select the captured representation's `title`
and `score_text`, respectively. An empty list deliberately omits that field.

## Exact evidence projection and blinding

`eval.goal5c.projection.project_text(text, spans)` uses half-open **Unicode
character indexes**, not UTF-8 byte offsets. Spans must be ordered, nonoverlapping
and within the original string. Each selects complete LF-delimited lines: a start
is zero or immediately after LF; an end is EOF, before the line terminator or
immediately after LF. CRLF is supported without splitting the pair.

Selected substrings retain their exact contents, case and spacing. Touching spans
and gaps containing only spaces, tabs or LF/CRLF line endings coalesce, preserving
the exact source whitespace. Equivalent segmentations therefore render identically:
`"A\nB\n"` with `[[0, 2], [2, 4]]` or `[[0, 1], [2, 4]]` renders exactly like
`[[0, 4]]`, without an omission marker. CRLF pairs remain intact, and blank-line
gaps retain their original spacing. Only gaps containing other characters are
replaced with `\n[…]\n`. Leading/trailing unselected text is not restored; the
canonical behavior concerns gaps between selections. Original review ranges remain
in the analyst audit, so review-plan checksums may differ even when visible
evidence is identical.
There is no LLM rewriting, summarization or automatic semantic redaction.

`validate_visible_text(text)` checks topic cards, translations, projected titles,
content and locators for known system metadata. It recognizes actual production
wrappers, including `SOURCE: OpenAlex`, publication provider title/year headers,
`WEB_SEARCH_PROVIDER`, local rerank scores, normalized result markers and decision,
source-stage, retry, rank, grade and threshold fields. Unicode compatibility
normalization is used only for detection; hidden controls and format characters
fail closed. Neither operation rewrites the visible evidence.

Whole-line selection prevents exposing a metadata value by cutting off its label.
Consequently a title embedded in a provider-labelled line cannot be recovered by
selecting only the title substring. Use another reviewed plain representation or
withhold the item. Conservative checks may reject legitimate scientific wording;
do not bypass them by paraphrasing evidence.

These are lexical defenses, **not universal semantic blinding or privacy detection**.
A reviewer must inspect every selected field and locator for remaining bias and
meaning preservation. Attestations do not satisfy real-study execution gates.

## Identity, inventories and evidence selection

One worksheet item corresponds to each resolved
`(query_id, document_entity_id, document_version_id, rubric_version)` key. Source
type is absent: publication and web observations of the same reviewed document
version share one potential truth item. Source-specific examples, representations
and traces remain separate in the analyst artifact. No truth labels are generated.

The analyst's `first_attempt_inventory` is explicitly scoped as
`observed_first_attempt_candidate_inventory`. It uses only representation events
admitted by Phase-1 `fp1_provenance_event_ids`, with attempt one checked again.
Resolved entities are grouped within each source; unresolved/excluded examples
remain individually represented. Each selected row preserves exact original title
and scorer text, event/representation references, contributing examples and
`metric_ready: false`. Lack of a safe worksheet projection does not erase a record
from this inventory.

Worksheet selection is separate and may use evidence from retries. The declared
rule `attempt_then_snapshot_row_then_representation.v1` selects the first reviewed
representation with non-whitespace projected content, ordered by attempt number,
captured SQLite row ID and representation ID. “Substantive” requires reviewer
judgment; the machine check establishes only non-empty content. SQLite row order is
a reproducible tie-breaker, **not provider chronology**. Scores, retention and
decision reasons do not select worksheet evidence. Entities without qualifying
projected content are withheld with `no_reviewed_substantive_text`.

Retry evidence used in a worksheet cannot alter the separate first-attempt raw
inventory. Neither artifact is a frozen complete pool or a ready scorer corpus.
Empty returned evidence still cannot certify an empty candidate pool. Every
query/source slot records `pool_completeness: "unverified"` and
`workflow_membership: "unavailable"`; unexecuted slots remain explicit.

## Outputs and reproducibility

The worksheet schema is `goal5c.blind_worksheet.v1`. Its top-level fields are
`schema_version`, `protocol_version`, `data_class`, `rubric_version`, `instructions`
and `items`. Every item contains only:

```text
item_id, topic_card, approved_translation, title, content,
document_locator, response
```

`response` starts as `{"label": null, "evidence_basis": null, "rationale": "",
"supporting_passage": ""}`. Instructions describe relevant/not-relevant/uncertain
and evidence basis, without collecting or validating judgments in this phase.

Item IDs are HMAC-SHA-256 of the canonical truth key using `blind_seed`; items are
sorted by that opaque ID. Changing the seed changes the IDs and display order.
Keep the seed-bearing plan with analyst provenance, never with the worksheet.
The worksheet contains no seed, original registry/entity keys, source labels,
decision metadata, projection ranges or input checksums. HMAC identifiers do not
turn arbitrary worksheet content into anonymous data.

The analyst schema is `goal5c.preparation_audit.v1`. It records capture/plan/worksheet
checksums, selection rule and scope, source slots, executions, every example's
identity disposition and references, projection reviews, first-attempt inventory,
withheld entities and reversible unblinding mappings. Preserve it together with
the original plan; the audit's plan checksum alone cannot recover the seed or all
review inputs. Canonical regeneration from unchanged inputs must be byte-identical.

## CLI and delivery boundary

From the repository root, using synthetic inputs outside Git:

```powershell
python -m eval.goal5c prepare-synthetic --manifest C:\study-synthetic\manifest.json --snapshot C:\study-synthetic\capture.sqlite --plan C:\study-synthetic\plan.json --output-dir C:\study-synthetic\prepared-v1
```

The output parent must exist, the output directory must be new, and the resolved
location must be outside Git worktrees. Existing directories/files are never
overwritten. Outputs are canonical JSON, written exclusively and flushed:

1. `analyst-only.json` — restricted analyst audit and unblinding map.
2. `worksheet.json` — the only artifact intended for annotator delivery.
3. `COMPLETE.json` — written last, containing `worksheet_sha256` and
   `analyst_sha256` over the canonical artifacts.

Treat a directory without a valid completion marker and matching artifact hashes
as incomplete. The writes are not a transactional directory rename; an interrupted
run may leave an invalid partial directory. Regenerate into a different new
directory. The completion marker is a consistency check, not a signature or proof
of origin. Do not distribute the directory wholesale: **deliver only
`worksheet.json`**, never the plan, snapshot, manifest or analyst audit.

## Remaining phases and limits

[Implementation Phase 3](goal5c-synthetic-freeze.md) adds a synthetic
preregistration/roster-freeze rehearsal; it does not conduct the gated pilot.

Later work must establish complete-pool/scorer-input eligibility and any defensible
workflow reconstruction; add independent assessor submissions and adjudication;
and implement frozen analysis, uncertainty and robustness reports. Real pilot,
recruitment, acquisition and private-data handling require the protocol's named
steward, institutional/consent, access, retention and semantic-preserving redaction
gates. None is authorized or executed by successful synthetic preparation.
