# Goal 5C phase 1: offline foundation

This phase implements the contracts and read-only import boundary of the
[approved protocol](goal5c-protocol.md), not the complete study. It accepts only
artifacts declaring `data_class: "synthetic"`. There is no participant intake,
provider call, worksheet generator, automatic identity reconciliation or metric
execution. The production code, historical dataset and thresholds are unchanged.

## Running from the repository root

Use the project's existing Python environment; no new dependency is required.
Inputs in these examples are synthetic artifacts conforming to the contracts below.

```powershell
python -m eval.goal5c validate manifest C:\study-synthetic\manifest.json
python -m eval.goal5c import-snapshot --manifest C:\study-synthetic\manifest.json --snapshot C:\study-synthetic\capture.sqlite3 --output C:\study-synthetic\bundle.json
python -m eval.goal5c validate truth C:\study-synthetic\truth.json
python -m eval.goal5c validate measurement C:\study-synthetic\measurement.json
python -m eval.goal5c validate-inputs --measurement C:\study-synthetic\measurement.json --bundle C:\study-synthetic\bundle.json --truth C:\study-synthetic\truth.json
python -m pytest -q tests/test_goal5c_contracts.py tests/test_goal5c_snapshot.py
```

The output directory must already exist outside any Git worktree. Existing files
are never overwritten. The CLI prints counts or a sanitized error and exits 0 or
2; it does not print query text or parser/SQL payloads. Programmatic validators
raise `ContractError` with field-independent diagnostic messages.

The tests construct synthetic databases through the actual session schema and
decision collector, entirely under pytest temporary directories. They provide
executable examples of all contract fields and end-to-end input binding.

## Versioned artifacts

All objects require the exact documented fields; unknown fields/versions are
rejected. Canonical JSON uses sorted object keys, UTF-8, compact separators and
one final LF. List order and exact strings are preserved. Duplicate JSON keys,
nonfinite values and non-JSON coercions are rejected. SHA-256 covers these exact
bytes for JSON artifacts, and exact file bytes for SQLite snapshots.

| Artifact | Schema version | Responsibility |
| --- | --- | --- |
| Acquisition declaration | `goal5c.capture_manifest.v1` | Bind a synthetic registry/executions to a finalized snapshot. |
| Imported bundle | `goal5c.capture_bundle.v1` | Preserve raw events and index examples/representations. |
| Synthetic truth catalog | `goal5c.truth_catalog.v1` | Declare shared document truth and source mappings. |
| Measurement declaration | `goal5c.measurement.v1` | Pin scope and exact referenced inputs, without computing results. |

Each carries `protocol_version: "goal5c.protocol.v1"` and `data_class: "synthetic"`.

### Capture manifest

Top-level fields: `schema_version`, `protocol_version`, `data_class`, `batch_id`,
`capture_commit`, `capture_tree_clean`, `snapshot_sha256`, `environment_sha256`,
`configuration_sha256`, `queries`, `executions`.

`capture_commit` must equal `98bda6c8dbe66f06606329bb929437b00c520467` and
`capture_tree_clean` must be boolean true. The three digest fields are 64-character
lowercase hexadecimal strings. Environment/configuration digests are declarations;
this phase does not load or authenticate their external artifacts.

Query fields: `query_id`, `original_query`, `query_fingerprint`, `intent_family_id`,
`stratum` (`invention`, `scientific`, `operational`). Fingerprints use the existing
capture function. Duplicate original-query fingerprints are rejected.

Execution fields: `execution_id`, `query_id`, `session_id`, `run_id`, `source_type`,
`attempt`, `envelope`, `query_envelope_hash`, `call_query`, `capture_status`,
`declared_event_count`. Sources are lowercase; attempts are positive integers;
counts are nonnegative integers, never booleans. Identifiers are nonempty ASCII
letters/digits/underscore/dot/hyphen, at most 100 characters.

An envelope may be null, or a full JSON object with the actual stored atomic
schema. Unavailable atoms retain hash `""`; an explicit empty list has its distinct
structural hash. Missing envelopes are retained but excluded from first-attempt
provenance eligibility. Session originals must exactly match the registered study
original, and stored attempt queries/run IDs must match execution declarations.

| Capture state | Treatment |
| --- | --- |
| `observed` | Requires non-cache observations and matching declared count. |
| `missing` | No rows; the candidate pool remains unknown. |
| `known_loss` | Keep any remaining rows, exclude from first-attempt provenance eligibility. |
| `cache_only` | Only ineligible patent cache observations; no examples. |
| `empty_output` | Zero captured rows and completed reliable-no-results evidence; pool still unknown. |

**Empty returned evidence does not prove an empty materialized candidate pool.**
All candidates may have been rejected or capture may have failed. `empty_output`
therefore carries `unverified_empty_pool` and never certifies an empty benchmark
pool. Fully verified empty-pool acquisition evidence belongs to a later phase.

### Imported bundle

Import opens SQLite with `mode=ro`, `immutable=1`, `query_only=ON` and untrusted
schema handling; production connection/migration helpers are not called. Active
WAL/SHM/journal companions are rejected. Integrity and file digest are checked,
including a second digest after reading. Connections close explicitly.

Source-attempt completeness requires an existing stored attempt with
`status == "ok"`, `completed == 1` and `error_count == 0`. The production writer
stores the normalized retrieval status in `evidence_results`; `ok_with_hits` and
`reliable_no_results` are derived acknowledgement statuses, not stored success
statuses. Hit count is not a completion requirement. `empty_output` additionally
requires stored `reliable_no_results == 1`, while its candidate pool remains
unverified. Partial/failing attempts, unfinished attempts and attempts with errors
retain their traces but cannot qualify for first-attempt provenance eligibility.
Synthetic fixtures exercise the actual production evidence writer for successful
hits, reliable no-results and incomplete/failing outcomes.

The bundle includes manifest/snapshot digests, registry, execution declarations
and stored attempt outcomes, raw events, unreconciled examples and representations.
Event IDs are `snapshot_sha256:sqlite_row_id`. Every raw column, payload string and
genuine repeated event survives. The pinned source/stage/reason dictionary rejects
unsupported combinations. Web overlap rejection has a derived false `gate_outcome`
while its original NULL `retained` remains unchanged.

Examples preserve capture `example_id` and source-specific identity. Text fallback
hashes are not recomputed from later scoring text: capture may intentionally retain
an earlier identity basis. No cross-source document identity is inferred.

Representations preserve exact title/text with raw-event references.
`fp1_provenance_event_ids` identifies events passing only this phase's execution,
envelope, capture and first-attempt checks. It is **not** a complete benchmark pool,
human label eligibility decision, reconciliation decision or final selection.

### Truth and measurement

Truth catalogs contain `catalog_version`, `truths`, `example_mappings` and
`representation_assessments`, in addition to the common headers. Truth rows have
`truth_id`, `query_id`, `document_entity_id`, `document_version_id`, `rubric_version`
and `label` (`relevant`, `not_relevant`, `uncertain`). The four-part truth identity
is unique and has no source field. Mappings retain `example_id`, `source_type`,
`query_id`, `truth_id`. Representation assessments carry `representation_id`,
`example_id`, `evidence_basis`, `assessment`; evidence sufficiency is not document
truth. These are synthetic finalized declarations, not an assessor/adjudication UI.

Measurements require namespace/title/boundary/attempt scope, source, fully qualified
metric names, acquisition/pool/config/truth versions, `capture_bundle_sha256`, `truth_catalog_sha256`,
retry policy, query cohort and observations. Each observation names its query,
example, attempt, role (`candidate`, `representation`, `idf`), execution,
representation and raw event. `fp1_core` rejects retry inputs in every role.
Query families cannot cross declared discovery/confirmatory/pilot splits.
Structural gates cannot request relevance metrics, and ranking metrics require
the explicit frozen-pool scope.

`validate-inputs` checks bundle/truth digests and joins exact text, source, query,
run, attempt, event and representation references with shared truth. Standalone
`validate` checks only the declaration's structure. Neither command establishes
pool completeness, adjudication quality, final workflow membership, or that a
future evaluator selected the correct split. Those checks precede metric execution
in later phases. Re-import the original snapshot to audit a bundle's provenance.

## Limitations and next boundary

[Phase 2 synthetic preparation](goal5c-synthetic-preparation.md) adds reviewed
candidate inventories and blank blinded worksheets while retaining these limits.

A synthetic declaration is an attestation, not automatic PII detection or proof
of origin. Checksums detect mismatched artifacts; they do not authenticate a
self-declared commit. No real-study input is enabled even if operational privacy
gates have subsequently been satisfied; enabling it needs a reviewed later change.
External environment/configuration metadata and document reconciliation evidence
are not authenticated by these validators.

The next coherent phase can add reviewed pool materialization, richer acquisition
completeness evidence and blinded worksheet contracts using synthetic data. It
must preserve source-independent truth, distinct measurement namespaces and the
privacy/recruitment gates. Actual labels, metrics and prospective threshold
confirmation remain separate reviewed boundaries.
