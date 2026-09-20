# Per-attempt query execution

The session original query is immutable provenance and the decision-capture
fingerprint input. It is distinct from both the initial translation and a
backend-generated retry query.

- **Resolved source attempt 1:** keep the original query in attempt storage;
  when provided, the initial English translation drives retrieval and scoring.
  A missing-source checklist action with `attempt_no=1` has this same contract,
  even if other sources have already retried.
- **Resolved source attempt greater than 1:** the writer's query is authoritative.
  The initial translation cannot override it, including when an older client
  continues sending that translation. Server allocation of `attempt_no=0` uses
  the resolved attempt to make this decision.
- Source-specific deterministic transformations remain intact. Publication
  compaction and provider query variants derive from the selected attempt query;
  provider strings need not be byte-identical to stored query text.

Flowise passes the original query and translation for first attempts, then exact
`retry_actions[].query` and an empty `english_query` for retries. It never invents
or translates a retry. ACK translation flags are interpreted per attempt:
`received` reports receipt, whereas `used_for_search` and `used_for_scoring`
describe whether translation was selected as the execution basis. A stale
translation can therefore be received but unused. Duplicate/rejected calls and
writer short circuits do not claim execution. These flags are not proof that a
provider succeeded or that substantive evidence was obtained. Only the checklist
authorizes subsequent retries.

## Reproduction and validation

On main `394d1bda63774b5016b7951344d32e40ddb43607`, all three real writer/pack
paths stored `battery-free motor temperature sensor` for attempt 2 while the
search boundary received the stale translation `motor vibration sensor`.
The writer forwarded the translation unconditionally, and each evidence pack
gave it precedence. The Flowise prompt also contradicted its own retry plan by
requiring the original query and translation on every call.

Tests exercise real writers, evidence packs, searches, scoring and SQLite capture,
with only external provider/fetch I/O replaced. They cover initial English and
multilingual execution, retries, element follow-ups, missing sources, automatic
attempt allocation, duplicate ACKs, hashes, immutable envelopes and original-query
fingerprints. Defect restoration must fail the same execution assertions for
stale-translation and original-query substitution. A separate prompt contract
control rejects restoration of the old all-writers hard rule.

## Independent merged-hardening review

An initial selected suite of 240 patent identity/security/fallback, verification
depth, retry-saturation and publication-diagnostic regressions passed. Inspection
confirmed exact publication promotion gates, guarded patent redirects, unchanged
six-fetch bounds, exclusion of volatile attempt logs from saturation comparison,
and consistent retry readiness flags.

Separate findings remain out of this change:

- **High:** the real Crossref safe wrapper can swallow an underlying exception;
  with usable results from another provider, the real aggregator returned `ok`,
  `completed=true`, zero errors. Crossref, PubMed, OpenAlex and AlphaXiv wrappers
  catch exceptions into empty lists; arXiv also skips failed marker outputs.
  Semantic Scholar's explicit error/rate-limit flags and exceptions reaching the
  outer aggregator remain observable. A separate wrapper contract change is
  warranted; the partial-diagnostic fix cannot report failures it never receives.
- **Medium:** patent verification selection accepts a whitespace-only URL as
  fetchable and can spend a slot before a valid candidate. The safety layer still
  rejects unsafe fetching; this is a budget-utilization limitation.
- **Medium:** a synthetic web search result with `partial_failure`,
  `completed=false` and a structured provider error emerges from the real web
  evidence pack with `completed=true`. The partial status and error survive,
  but completeness is inconsistent. This requires its own contract review/fix.

No live provider or Flowise runtime verification is claimed. Existing imported
Flowise workflows require reimporting/updating their supervisor instructions.
