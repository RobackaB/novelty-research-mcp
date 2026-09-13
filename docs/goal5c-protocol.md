# Goal 5C: evaluation study protocol

Protocol: `goal5c.protocol.v1`. Design approved; implementation is staged below.
Baseline: `98bda6c8dbe66f06606329bb929437b00c520467`, version 0.9.8.
Goal 5B is closed; [its capture contract](goal5b-decision-capture.md) is authoritative.

## Boundary and architecture

Create an offline, versioned human-labelled candidate pool, decision audit and
measurement workflow. Preserve production thresholds, ranking, providers, public
MCP interfaces and historical `eval/dataset.json`. No private/raw study data belongs
in Git. Phase 1 uses synthetic fixtures, versioned contracts and read-only snapshot
import; it does not conduct acquisition, labelling or statistical analysis.

Pipeline: registered queries and acquisition manifest → immutable capture/session
archive → validated examples, representations and traces → blinded independent
labels and reconciliation → reproducible evaluation views and reports.

## Sampling and acquisition

Target 30 genuine original information needs: ten each concerning inventions or
mechanisms, scientific findings or methods, and products or technical documentation.
Run all three sources for every query. Original queries, not candidates, sources or
retries, are sampling units. These diversity strata do not estimate population usage.

Recruit actual requests; exclude historical evaluation examples, constructed prompts
and duplicate intent families. Register wording, language, provenance, human-written
information-need card and intent family before outcomes. Randomly sample excess
requests within strata using a recorded seed. Freeze roster, ordered reserves and
collection window before retrieval. A 25–29-query cohort is acceptable only when
frozen before outcomes with approximately equal strata. Three separate pilot queries
calibrate workflow/rubric and never enter the study.

Freeze translations before acquisition. Allow only normal checklist-directed retries;
record executed variants, stopping rules and source budgets (currently patent 4,
publication 3, web 3). Do not manually improve queries after observing relevance.
Use a fresh process per original query, retaining normal within-query cache behavior.

Initialize and verify the registered original query before invoking session writers:
writer-created sessions can otherwise store an attempt query as the original. Use a
fresh dedicated database and authenticated corrected-code checkout. At most one
separately identified reacquisition is allowed for documented technical failure before
labels/results are inspected; retain both and select the earliest provenance-valid
acquisition. Weak results, empty retrieval and exhausted retries do not justify replacement.

## Extraction and identity contracts

| Entity | Identity and retained information |
| --- | --- |
| Query | Registry ID, original-query fingerprint, intent family and topic card. |
| Execution | Acquisition/session/source/attempt/run, envelope snapshot and call boundaries. |
| Raw event | Snapshot checksum plus SQLite row ID; all original fields. |
| Raw example | Query fingerprint/source/candidate identity and captured `example_id`. |
| Representation | Exact stage text/title, content checksum and trace references. |
| Document truth | Query/document entity/document version/rubric version; **no source field**. |
| Label | Blind item, evidence inspected, independent assessor and adjudication history. |

Validate captured `example_id` against its components; quarantine conflicts. Retries,
providers and variants add observations, not independent samples. Preserve genuine
repeated events while making repeated snapshot import idempotent. SQLite order and
batch timestamps do not establish universal provider chronology. Occurrence fields
do not identify every provider call; uncertain links remain unknown or explicitly inferred.

Preserve dedupe, merge, truncation and rejection/restoration events. Production
deduplication is not proof of document identity. Classify events through a versioned
source/stage/reason dictionary, not `retained` alone: web overlap rejection can have
NULL retention. Structural policy decisions are not human relevance negatives.
Neither the last flag nor any rejection defines final output. Corroborate membership
from evidence/session outputs; production selects the best-quality attempt, not
necessarily the last attempt or the union of accepted traces.

Preserve explicit document identifiers, conservative document URLs and text-hash
fallbacks with their provenance. Recover missing locators only from authoritative IDs
or verified metadata; citations inside abstracts are not candidate URLs. Automatically
reconcile only compatible authoritative IDs or verified document locators. Similarity
can propose review but cannot merge records. Patent families, versions, preprints,
published articles and pages discussing articles remain distinct absent reviewed evidence.

Verified mirrors of one document version share query-level truth across sources.
Source-specific inputs and outcomes still retain distinct representations/observations.
Representation sufficiency may differ without creating contradictory document truth.
Preserve raw `example_id`; introduce reversible, versioned entity mappings. Text-hash
equality establishes representation equality, not certain document identity. Ambiguous
text-only matches remain provisional with possible-equivalence links. Record every
manual merge, justification and reviewer. Conflicting truth requires adjudication;
truth revisions propagate to all sources. Count resolved documents once within each
declared source pool; assess plausible reconciliation alternatives as sensitivity checks.

## Provenance and eligibility

The acquisition manifest records commit, clean checkout/configuration/dependency
digests, batch/query/session/run/source/attempt identities, pre-attempt envelopes,
translations, checklist actions, ACKs, evidence, diagnostics and artifact checksums.
Exclude credentials. Schema name or timestamp alone cannot prove corrected capture.
Snapshot a quiescent database consistently; offline readers must not invoke production
connection helpers that initialize schemas.

Maintain separate label, benchmark and gate-audit eligibility flags with reason codes.
`dataset_eligible=True` is necessary, not sufficient, for labelling.

| Condition | Required treatment |
| --- | --- |
| Explicit empty decomposition | Valid provenance. |
| Unavailable envelope/hash | Diagnostic preservation; excluded from primary fully specified cohort. |
| Missing original or registry mismatch | Quarantine; never substitute attempt text. |
| Missing capture | Unknown pool, never zero candidates or all rejected. |
| Incomplete attempt/provider failure | Preserve usable evidence; exclude complete-attempt metrics, report separately. |
| Cache/provider markers | Trace-only; no invented fresh pool or document. |
| Malformed/non-document | Exclude from labelling; preserve available diagnostics. |
| Verified completed empty pool | Zero yield; relevance denominators may be undefined. |
| Unexecuted variant/unretrieved document | Unobserved, never an invented negative. |

Known capture warnings preclude completeness claims for affected scopes; absence of
warnings does not prove exhaustive capture. Validate observed lifecycle/output
consistency and publish the full roster with missingness, failures and exclusions.

## Blinded human assessment

One blinded item represents each resolved query/document truth entity. Deterministically
deduplicate evidence without consulting decisions. Start with the earliest substantive
representation and fixed tie-breakers; record exactly what assessors see. Retry evidence
may inform shared truth but cannot change frozen first-attempt scorer inputs. Full-text
access follows an equal, preregistered budget, never preferential access for retained items.

Show random item ID, original topic card, approved translation, title/content packet
and document locator where needed. Hide scores, thresholds, retention, reasons,
provider/scorer/source-stage/retry metadata, ranking, grades, decomposition and verdict.
Use an allowlisted renderer with a reversible projection map: generated score/provider
headers embedded in text must also be hidden. Do not rewrite evidence with an LLM.

| Label | Rubric |
| --- | --- |
| Relevant | Substantive evidence directly addressing the subject/core task or a material aspect registered on the topic card. |
| Not relevant | Adequate evidence establishes another subject/task or only generic vocabulary overlap. |
| Uncertain | Insufficient evidence, unresolved identity, access or language prevents judgment. |

Relevance does not establish novelty, patent anticipation or complete feature coverage.
Record representation sufficiency separately. Require two independent human judgments;
third-assessor adjudication resolves disagreement where possible. Preserve unresolved
uncertainty, original labels, rationale and supporting passage. Record title/snippet,
abstract, full-document or unavailable evidence basis. Report agreement/disagreement and
uncertainty rates without an arbitrary agreement cutoff replacing inspection.

## Measurement contracts and statistics

| Namespace | Mandatory report title and boundary |
| --- | --- |
| `fp1_core` | First-attempt frozen-pool core-scorer benchmark; only first-attempt candidates/representations/IDF. |
| `observed_gate` | Observed production-gate audit; only candidates known to reach the identified gate. |
| `workflow_observed` | Observed production workflow with policy-directed retries; verified stored final evidence. |

Use names such as `fp1_core.publication.query_macro_f1`. Namespace/specification,
acquisition/pool version, attempt rule, query cohort/source, selection boundary,
scoring/IDF configuration, retry policy where applicable, truth version and exclusions
must accompany results. Include scope in filenames, tables and captions. Reject scope
mixing, retry contamination and generic “system F1” labels. Unverifiable workflow pool
or output membership makes the corresponding metric unavailable.

`fp1_core` uses the preregistered analytical query and fixed representation rule,
building IDF over the full selected pool including uncertain items. Generic thresholds
remain patent/publication/web 3.0/3.0/2.8. These differ from production search gates:
patent 2.8/2.2/1.5, publication 3.5 and web 5.0, with anchors, fallback and restoration.
The existing static harness is not workflow replay. Exact replay requires reconstructible
contemporaneous inputs/corpora and verified baseline decisions.

Report per-query/source confusion counts, precision, captured-pool recall, F1, accepted
count, pool size and label coverage. Precision is undefined with no acceptances; recall
is undefined with no positives. F1 is `2TP/(2TP+FP+FN)` when defined. Primary paired F1
uses pools with adjudicated positives; no-positive pools receive separate false-positive
analysis. Average queries equally within source. Optional combined summaries use fixed
one-third source weights on the disclosed all-source eligible cohort. Ranking uses AP/MAP
and fixed P@5 where defined, treating absent positions as empty; historical variable-k
precision must not be called P@5.

Uncertain labels are not negatives. Publish resolved-label coverage and metric-specific
optimistic/pessimistic bounds with consistent truth assignments across paired systems.
Use 10,000 seeded whole-query bootstrap draws within intent strata, carrying all sources,
retries and traces together; report approximate 95% percentile intervals. Pair comparisons
on identical pools/queries/labels/draws. Report per-query differences, leave-one-query-out
results, identity/representation/missingness sensitivity and contributor/domain clustering.
Keep intent families together in splits. Preregister any confirmatory query-level paired
randomization test and correct multiple source claims.

Recall is conditional on the declared captured pool, never global retrieval recall.
Even workflow evidence metrics do not establish final-answer correctness or novelty.

## Discovery versus threshold confirmation

The initial 25–30 queries support baseline measurement/discovery, not production retuning.
Explore a preregistered finite grid with full trade-offs and query-grouped validation.
Freeze one proposed gate/configuration, primary metric, worthwhile improvement, allowed
recall loss and analysis before a new prospective confirmation cohort. Determine its size
from discovery variability and agreed margins. Exclude discovery queries/intent relatives.
Analyse once; inconclusive results authorize no change. Failed confirmation cannot be
reused for selection and validation. Generic scorer sweeps do not validate different gates;
changes affecting acquisition require acquisition-aware confirmation. Threshold deployment
is outside Goal 5C completion.

## Privacy, consent and retention

Before recruitment appoint a data steward, establish applicable processing basis and
institutional review, approve participant materials, storage and annotators. Recruit adults
voluntarily, independently of employment/assessment. Explain purpose, data/access,
external services receiving approved queries, retention, withdrawal and reporting. Obtain
versioned affirmative participation consent; public row-level release is separate/optional.
Do not reuse private historical requests without permission. Exclude sensitive personal
cases, credentials, confidential material and unauthorized third-party information.

Separate contact/consent, private participant-to-random-query linkage, and study artifacts.
Annotators receive no recruitment identities. Public IDs are release-specific random IDs;
query hashes are not anonymization. Review privacy before retrieval. Remove incidental
identifiers only while preserving subject, constraints, negation, quantities and technical
relationships; participant and second reviewer approve equivalence. Freeze that safe text
as the study original before outcomes. Exclude requests requiring substantive alteration.
Never send unreviewed text to external redaction services.

Post-capture redaction creates a derivative; semantic equivalence does not preserve
lexical scores. Do not claim exact replay of altered text with original scores. Public
defaults are protocol/code/synthetic fixtures and disclosure-reviewed aggregates. Encrypt
and restrict raw databases, logs, registries, linkage and worksheets outside Git. Row-level
release requires separate consent, reidentification review and redistribution rights.

Delete submission/redaction working copies within 30 days of acceptance/rejection;
contacts/linkage 12 months after freeze and at most 24 months after enrolment; private
research archives 24 months after freeze. Keep consent evidence separately through the
authorized private retention period. Resolve any institutional variation before enrolment.
Withdrawal propagates through controlled datasets/derivatives; version withdrawals and
backup deletion schedules. Do not promise recovery of third-party downloads. Raw
immutability never overrides authorized deletion.

## Reproducibility, phases and acceptance

Version protocols/registries/manifests, immutable raw snapshots, normalized entities and
reconciliation, blind worksheets/separate unblinding maps, labels/adjudications and derived
views/reports separately. Record parent checksums, algorithms, seeds, ordering and tool
versions. Canonical regeneration must be byte-identical; pinned numerical evaluation must
reproduce under multiple hash seeds. Live provider reruns are new acquisitions.

Phases: (1) synthetic offline contracts/read-only import; (2) reviewed extraction/blinding
and negative controls; (3) gated disjoint pilot and protocol freeze; (4) registered acquisition;
(5) reconciliation/blinded labels; (6) frozen analysis/report; (7) CI/external review.
Prospective threshold confirmation is separate subsequent work.

Goal 5C is done only when:

- Reviewed protocol/deviations and 25–30 registered genuine queries account for all sources/failures.
- Every example/metric traces to authenticated fresh capture and reversible identity decisions.
- Every example has two independent labels plus adjudication or explicit uncertainty.
- Negative controls detect scope/retry leakage, duplicate import, wrong original identity,
  cache-as-document errors, missing capture, restoration mistakes and embedded blinding leaks.
- Replay claims validate exactly; unsupported reconstruction remains unavailable.
- Source scorecards, intervals, uncertainty bounds, robustness and missingness regenerate;
  insufficient source evidence prevents declaring measurement complete.
- Full tests, determinism, historical relevance checks, CI and external review pass without
  protected production/interface/dataset changes.

Operational gates remain mandatory, not unresolved technical design choices. Further
methodological review can improve small-sample power/interval calibration, domain rubrics
and incomplete-pool evaluation if exhaustive judging is unaffordable.

## Methodological references

- [IR evaluation and information needs](https://nlp.stanford.edu/IR-book/html/htmledition/information-retrieval-system-evaluation-1.html)
- [Buckley and Voorhees: incomplete judgments](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=150469)
- [Smucker, Allan and Carterette: IR significance tests](https://ciir-publications.cs.umass.edu/pub/web/getpdf.php?id=744)
- [Cawley and Talbot: selection bias](https://jmlr.org/papers/v11/cawley10a.html)
- [European Commission: processing principles](https://commission.europa.eu/law/law-topic/data-protection/information-business-and-organisations/principles-gdpr_en)
- [EDPB: lawful processing](https://www.edpb.europa.eu/sme/be-compliant/process-personal-data-lawfully_en)
