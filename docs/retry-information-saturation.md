# Retry information saturation

Document duplication is only a preliminary saturation signal. From attempt two,
the checklist may mark a source `retry_saturated` only when all of these hold:

- The existing duplicate/inserted ratio is at least 0.8 with at least one inserted hit.
- Every latest hit has an identical stored representation for the same canonical
  identity in an earlier attempt. A new document, changed text, evidence level,
  verification or other hit metadata prevents saturation.
- The latest attempt is completed, `ok`, error-free and graded strong, medium or
  reliable-no-results. An older best attempt cannot conceal a weak/latest failure.
- The query envelope supplies the source variant list and atomic requirements;
  no planned query remains unused and no actionable atomic requirement is uncovered.

Query usage comes from persisted attempt queries with the existing query-hash
normalization, not an assumption that attempt numbers correspond to variants.
An available unused variant replaces a generic repeat query. Element-guided
follow-ups retain their existing construction and coverage test, but are considered
per source even when another source is retrying. Uncovered requirements are checked
before saturation can suppress a source.

For sources already retried, observed evidence changes, unused variants or an
incomplete/weak latest result preserve a retry within the existing source budget.
First-attempt quality readiness and best-attempt selection are unchanged. Budget
exhaustion is a separate stopping condition, not proof of saturation.

This is conservative exhaustion of **known stored paths**, not proof that no better
external evidence exists. Exact hit comparison may spend another attempt on harmless
metadata changes; this is preferred to suppressing an unrecognized improvement.
Canonical identity and atomic coverage retain their existing limitations. No new
provider, query decomposition, scoring, verdict or Requirement Evidence Matrix logic
is introduced.

`tests/test_retry_information_saturation.py` exercises real SQLite writes and checklist
generation across all three sources. Eight defect-restoration controls replace the
saturation predicate with the old ratio-only implementation and require the same
regression assertions to fail, rather than merely reporting successful mutation execution.
