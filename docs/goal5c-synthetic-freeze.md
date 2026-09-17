# Goal 5C implementation Phase 3: synthetic preregistration rehearsal

This increment prepares the selection and declaration contracts needed before the
[protocol's](goal5c-protocol.md) gated pilot. It does **not** conduct that pilot,
calibrate the rubric empirically, or complete the real-study protocol freeze.
The Phase-1 importer and Phase-2 preparation remain unchanged.

The only new operation samples declared synthetic original queries. It makes no
provider calls, reads no capture databases, processes no participant information,
collects no human labels and computes no relevance metrics. All real execution
remains disabled. The current result is suitable for an offline rehearsal, not an
authorization to recruit or acquire data.

## Original-query registry

`goal5c.query_registry.v1` requires exactly `schema_version`, `protocol_version`,
`data_class`, `registry_version` and `queries`. Protocol is `goal5c.protocol.v1`;
data class must be `synthetic`. Versions and query/family IDs use 1–100 ASCII
letters, digits, underscores, dots or hyphens.

Each query requires exactly:

| Field | Contract |
| --- | --- |
| `query_id` | Unique registry identity for an original information need. |
| `original_query` | Nonempty exact original wording, preserved without rewriting. |
| `query_fingerprint` | Existing Goal 5B fingerprint: NFKC, collapsed whitespace and lower-case identity semantics. |
| `intent_family_id` | Unique declared family across the entire registry. |
| `stratum` | `invention`, `scientific` or `operational`. |
| `language` | Nonempty declared language. |
| `topic_card` | Nonempty preregistered information-need card. |
| `approved_translation` | Exact frozen translation, or an empty string. |
| `provenance` | Object containing `kind: "synthetic_fixture"` and nonempty `fixture_reference`. |
| `registered_at` | Valid UTC `YYYY-MM-DDTHH:MM:SSZ` declaration. |

Duplicate IDs, fingerprints or intent families are rejected globally, including
pilots and reserves. One retry, source occurrence or candidate must not be
registered as another original. Unknown fields, including outcome scores,
candidate counts, retry ordinals and participant/contact records, are rejected.
Synthetic fixtures intentionally do not satisfy the real study's genuine-need
requirement. Equality checks cannot discover undisclosed semantic duplication,
prove that a row is an original need or automatically detect private content.

## Freeze plan

`goal5c.freeze_plan.v1` has exactly these fields:

- `schema_version`, `protocol_version`, `data_class`, `freeze_version`;
- `registry_sha256`: canonical checksum of the exact registry;
- `sampling_seed`: 64 lowercase hexadecimal characters;
- `pilot_query_ids`: exactly three distinct existing query IDs;
- `study_quotas`: integer counts for all three strata, totaling 25–30, with the
  largest and smallest counts differing by at most one;
- `frozen_at`, `pilot_window`, `study_window`;
- `outcomes_observed`: exactly boolean `false`.

Window objects contain only `start` and `end`, in the same UTC format. Every
query must be registered by `frozen_at`; the strict order is:

```text
frozen_at < pilot start < pilot end < study start < study end
```

These are planned rehearsal dates. No wall-clock or signed registration service
authenticates them, and a false declaration cannot be detected from dates alone.
The declared roster freeze precedes outcomes; it is distinct from the later
empirical protocol/rubric freeze informed by the actual disjoint pilot.

The protocol specifies three disjoint pilots but no mandatory pilot-stratum
allocation, so pilot IDs are explicitly preregistered without an invented balance
requirement. Each study stratum must have enough nonpilot originals for its quota;
insufficient registries fail rather than silently reducing the target.

## Deterministic selection and reserves

`freeze_registry(registry, plan)` first validates both complete contracts. It
excludes pilot IDs, then orders originals independently within each stratum by
HMAC-SHA-256 of `[sampling_rule, stratum, query_fingerprint]`, using the recorded
seed. Query ID is the deterministic collision tie-breaker. The first quota-sized
prefix forms discovery; the remainder forms ordered reserves. Strata are emitted
in the fixed order invention, scientific, operational; reserve priority is within
stratum. All registry rows occur exactly once across pilots, discovery and reserves.

Selection ignores registry row ordering, source counts, candidates and outcomes.
Original queries remain the sampling unit; no candidate-level independence,
bootstrap, metric denominator or recall estimate is inferred. This first cohort
supports discovery only and never authorizes production threshold selection.

The result preserves wording, topic cards, translations and provenance for every
row. Registry and plan digests cover exact canonical JSON, including array order:
reordering input rows does not alter selection, but legitimately changes parent
digests. Regeneration from identical inputs produces byte-identical artifacts
under different `PYTHONHASHSEED` values.

Reserves are frozen bookkeeping, **not automatic replacement authority**. Do not
replace queries because retrieval is weak, empty or retry-exhausted. Same-query
reacquisition for documented technical failure is distinct, limited by the
protocol to at most one and requires later reviewed acquisition handling. This
phase performs neither promotions nor reacquisition.

## Output and operational gates

The output schema is `goal5c.synthetic_freeze.v1`, with scope
`synthetic_preregistration_rehearsal`. It carries parent checksums, the sampling
algorithm/seed, declared schedule/quotas, `pilot_queries`, `discovery_queries` and
`ordered_reserves`. `planned_execution` records all three sources per original,
planned maximum attempts patent/publication/web 4/3/3, normal checklist-directed
retry policy and a fresh process per original. These are declarations, not proof
that any source ran; the rehearsal records zero executed calls.

The following remain false: `real_execution_authorized`,
`actual_pilot_completed`, `empirical_protocol_freeze_completed` and
`confirmatory_threshold_selection_authorized`. Every operational gate is
`not_verified`. There is no input field or CLI switch to approve these gates.

Before real work, the protocol still requires a named steward, institutional and
processing-basis review, participant materials/consent, secure storage/access,
annotator approval, separation of registry identities, retention/withdrawal and
semantic-preserving redaction review. Passing synthetic validation establishes
none of these. A future reviewed implementation and operational evidence are
required; this output cannot unlock real execution even if external approvals
are later obtained.

## CLI and verification boundary

Run from the repository root in the development environment. Like the other
evaluation tools, this CLI is not packaged in the server wheel or runtime image.

```powershell
python -m eval.goal5c freeze-synthetic --registry C:\study-synthetic\registry.json --plan C:\study-synthetic\freeze-plan.json --output C:\study-synthetic\roster.json
```

The output parent must exist outside Git worktrees. The existing exclusive-create
writer refuses overwrites and flushes canonical JSON. Retain exact input files and
output checksum for replay; checksums establish consistency, not authenticated
pre-outcome timing or origin. A failed write may leave an incomplete file; validate
it and regenerate into a new path. CLI success counts are registry counts only.
No generated registry/roster or private/raw study data belongs in Git.

Later phases still need actual gated pilot calibration, approved protocol freeze,
authenticated fresh acquisition, complete-pool/scorer eligibility, two independent
human labels and adjudication, and frozen query-level statistical analysis. No
global retrieval recall can be established from the captured candidate pool.
