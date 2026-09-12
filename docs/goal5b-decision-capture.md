# Goal 5B: passive candidate-decision capture

Baseline: `cbfd5babe2ba7f041c963384bdaa06a874cc6f39` (0.9.8).
Reviewed v5 patch: `98be4f4bfecf20281d2a659c0c923e1b80d865f6`.

## Contract and boundary

Goal 5B records candidate decisions for later evaluation. It does not change
retrieval, thresholds, ranking, provider selection, final answers, the seven MCP
interfaces, or `eval/dataset.json`. Goal 5C owns extraction, blinded labelling,
dataset expansion, and query-level statistical analysis.

The capture boundary is materialized search candidates and their existing
selection gates. It includes rejected documents that would otherwise disappear.
Malformed non-document provider records, documents the remote provider never
returns, unexecuted query variants after existing early exits, and downstream
fetch/verification verdicts are not reconstructed as search decisions. Existing
session/evidence records retain retrieval completion and failure information.

| Source | Observed decisions |
| --- | --- |
| Patent | Cache hit/miss; provider and merged deduplication; provider cap; domain anchor; each evaluated primary, relaxed and floor tier; final display truncation. |
| Publication | All six providers' existing local relevance gates, including structured/text AlphaXiv; fallback text filtering; provider and merged deduplication; corpus-IDF rerank; fillback; final display truncation. |
| Web | All three providers' URL policies; first-winner URL collisions; overlap/score gates; corpus-IDF rerank; restoration when that pass rejects everything; final display truncation. |

Preserved local gates differ from generic `relevance.THRESHOLDS`: patent search
uses 2.8, relaxed 2.2, floor 1.5; publication filtering uses 3.5; web search uses
5.0. Generic PATENT/PUBLICATION/WEB values remain 3.0/3.0/2.8. Capture reports
the gate actually evaluated; none of these constants was retuned.

## Architecture and semantics

The session writer creates an in-memory `DecisionCollector` using the stored
original query, canonical session ID, run, source and attempt. Optional internal
forwarding carries it through the evidence pack into search. Task-local contexts
reach provider helpers without changing their call signatures; they are reset
after each invocation. Provenance sidecars are read only by capture code, never
by production selection.

- Query fingerprints use exactly the session identity normalization: NFKC,
  collapsed whitespace, then `lower()` (not `casefold()`). Fingerprints retain
  32 hex characters; the existing production query hash retains 24. Missing
  session rows or blank stored original queries disable capture with a sanitized
  warning. Collector setup accepts no attempt-query fallback.
- The envelope hash separately detects structural atomic-requirement drift. It
  hashes version-tagged canonical JSON containing the actual stored `category`,
  `label`, `terms` and `too_broad_for_element_retry` fields. Dictionary key order
  and extra metadata are ignored; field values and list order are preserved.
  Atom order influences variant construction and truncation; term prefixes are
  used in web query variants, so sorting these lists would hide meaningful drift.
  An unavailable envelope/atomic list has hash `""`; an explicit empty list has a
  non-empty structural hash. Malformed atoms fail capture setup with a warning.
  These hashes describe the atomic decomposition, not every envelope field.
- `decision_query` is the scorer/gate input; `query_variant` is the argument of
  the provider function that produced the occurrence. It is not necessarily the
  literal HTTP wire query: patent providers can add site restrictions/wrappers.
- `score_text` is the exact text at that stage, without diagnostic truncation.
  Numeric scores already computed by production are retained. Rejected fallback
  blocks are rescored only inside a guarded observer when the score gate ran;
  short-circuited overlap/anchor failures need no invented score or threshold.
- `retained` is a stage-local relevance/gate outcome, not the final report
  verdict. Structural URL, dedupe and truncation events leave it NULL. A later
  fillback can restore an earlier rejected candidate; both events remain.
- Publication DOI/arXiv/document URL identity survives formatting and reranking.
  Without an ID/URL, the original candidate's title/text fallback basis is carried
  through the sidecar while each event retains its own scored text. Citation URLs
  in abstracts cannot substitute for the document's identity. Text-only records
  with genuinely different provider text may still need reconciliation in 5C.
- `example_id` joins one query/source/document across stages and retries;
  provider, variant, session and attempt remain separate provenance. It is not
  a unique event identifier; SQLite `id` distinguishes observations.
- Provider status headers and cache observations are not dataset-eligible.
  A warm patent cache returns the same bytes, calls no providers and emits only
  a cache marker, not fabricated scoring events for cached winners.

## Failure isolation and storage

The writer persists captured events in `evaluation_candidate_decisions` after
retrieval, including observations collected before an exception. Evaluation DDL,
its index and the insert batch share a separate rollback boundary. Mandatory
production schema setup never depends on the evaluation schema: an incomplete
evaluation table must not break session operations.

Collector construction, record/field extraction, context handling and persistence
are fail-open. Failures emit warnings with the exception class, without sensitive
request text; even a failing logging handler cannot alter retrieval. Payloads are
detached JSON primitive snapshots. Persistence failure must leave ACKs, evidence,
retry plans and final reports unchanged.

No retention policy, exporter or new public MCP tool is introduced. Untruncated
text increases storage use; operators should account for database growth. A
broken evaluation schema is reported and left intact, not destructively repaired.

## Verification and negative controls

The v5 suite passed despite missing provider hooks, web rerank/fallback traces,
patent tier coverage and mandatory-schema coupling. Added tests exercise:

- Real HTTP/XML/AlphaXiv/arXiv response parsing below publication provider gates,
  with accepted/rejected input, request-schedule parity and concurrent isolation.
- Patent real-score rejection plus controlled tier boundaries; web URL policies,
  rerank rejection/restoration/truncation and diagnostic computation failure.
- Real writer → pack → search → SQLite paths for all sources and two attempts:
  complete ACKs, production-table rows, duplicate handling, retry checklists and
  report bytes match with capture enabled, disabled, raising, setup failing,
  evaluation INSERT failing, and evaluation DDL failing.
- Real hook-removal controls for six publication providers, all writer-to-pack
  and pack-to-search edges, patent threshold observation and web merged capture.
  Restoring mandatory evaluation DDL coupling breaks production; removing the
  text-identity sidecar breaks cross-stage identity. These controls require the
  corresponding positive oracle to fail.

The three pinned pack fixtures were independently regenerated using baseline
source loaded from Git in memory and matched byte for byte. Their SHA-256 values:

| Pack | SHA-256 |
| --- | --- |
| Patent | `cc48518e6237bf4d24a993681a13a868760a4a11be7ca11ea6ce8cfb3a3d3036` |
| Publication | `7c0f36ea0524222862669b33f8a3c501024a6200e085f219a431bc55c1cbb9a9` |
| Web | `b76cddf072e2731fb44ee2f03ae31b17c1086218fe798bbdcd1beefd9a898528` |

Run `python -m pytest -q`, `python -m eval.relevance_eval`, and
`git diff --check origin/main`; repeat tests with multiple `PYTHONHASHSEED`
values. Evaluation must retain macro-averaged per-query precision 0.611,
recall 0.833 and F1 0.683. These are not candidate-level binomial proportions
or evidence of improved retrieval quality.

Local verification does not replace GitHub CI on Python 3.11–3.13 or final PR
review. Push, PR creation and merge remain separate approval steps.

### Initial local completion verification (before PR #25)

On Python 3.14.6, the final code passes **455 tests** with `PYTHONHASHSEED=1`
(57.99 s) and `PYTHONHASHSEED=97` (58.77 s). The suite includes 16 new explicit
hook-removal/defect-restoration controls. The full relevance command exits 0
with the unchanged baseline metrics above; `git diff --check origin/main` is
clean. `server.py`, `tools/relevance.py` and `eval/dataset.json` are unchanged.
Python 3.11–3.13 GitHub CI remains pending; no remote work was performed.

### PR provenance review follow-up

PR #25 review identified label-only decomposition hashing, an unsafe original
query fallback to retry text, and divergent Unicode case normalization. All three
are corrected by the contracts above. Existing local capture rows are not
rewritten: earlier label-only hashes and casefold-based query/text identities
must not be mixed with fresh capture for Goal 5C; recapture those observations.
The `atomic_requirements.v1` hash-input tag separates the structural algorithm
from the earlier label-only input without changing the SQLite or MCP schema.

`test_capture_query_identity.py` covers all four structural fields, meaningful
list ordering, irrelevant mapping ordering, unavailable versus empty envelopes
through SQLite persistence, missing original queries and Unicode parity. Five
additional defect-restoration controls exercise label-only hashing, conflated
unavailable/empty envelopes, casefold normalization, and original-query fallback
with both a missing row and a blank stored query. Integrated session tests also
verify unchanged ACKs, production rows, retries and reports when either missing
provenance condition disables capture across all sources and two attempts.

Follow-up local verification on Python 3.14.6: **491 tests passed** with
`PYTHONHASHSEED=1` (62.95 s) and `PYTHONHASHSEED=97` (66.40 s), including all five
new negative controls and the earlier controls. Full relevance evaluation remains
precision 0.611, recall 0.833, F1 0.683, P@k 0.667, MAP 0.759, MRR 0.778.
`git diff --check` is clean. Fresh Python 3.11–3.13 GitHub CI and final review are
required before merge.

### Files changed after v5

```text
docs/goal5b-decision-capture.md
tests/test_capture_coverage.py
tests/test_capture_publication_lifecycle.py
tests/test_capture_query_identity.py
tests/test_capture_search_lifecycle.py
tests/test_capture_session_equivalence.py
tests/test_capture_storage_guards.py
tests/test_decision_capture.py
tools/decision_capture.py
tools/patent_search.py
tools/publications_search.py
tools/research_session.py
tools/web_search.py
```

The integrated patch also retains these v5 files without further edits:

```text
tests/fixtures/pack_baseline_cbfd5ba.json
tests/test_capture_foundation.py
tests/test_capture_pack_equivalence.py
tests/test_capture_provenance.py
tests/test_capture_regression_guards.py
tests/test_decision_capture_controls.py
tools/patent_evidence_pack.py
tools/publication_evidence_pack.py
tools/web_evidence_pack.py
```
