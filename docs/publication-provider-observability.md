# Publication provider failure observability

Clean empty results and provider failures are distinct. On the previous main,
Crossref exceptions could be swallowed by its safe wrapper while another provider
supplied hits, yielding `ok`, `completed=true` and zero errors. PubMed also swallowed
request/XML failures; OpenAlex skipped failed variants; AlphaXiv swallowed transport
and MCP tool errors; arXiv discarded failed markers.

Provider block lists now carry sanitized error strings internally. Existing
aggregation feeds these into PR #34's `publication_provider_error` diagnostics;
there is no new public error schema or persistence table. Healthy variants retain
their hits even if another planned variant fails. Errors follow the existing
provider order, then each provider's query-variant order, regardless of task timing.

- Usable hits plus a failed provider: `partial_failure`, `completed=false`, safe
  structured errors, with usable hits preserved.
- Clean zero results: no provider error; existing no-results confidence rules apply.
- No usable hits with provider failures: incomplete retrieval, never reliable
  negative evidence. Existing partial/failure status selection remains intact;
  rate-limit fallback responses also retain known provider diagnostics.
- Optional AlphaXiv without a key is skipped without a failure. Configured transport
  failures and MCP `isError` results are observable.
- PubMed malformed/error responses are failures, not empty successful searches.
- arXiv status/error markers are authoritative. Error words appearing in a healthy
  paper's text do not manufacture a provider failure. Failed response bodies are
  not echoed into diagnostics.

Requests remain bounded by the existing planned variants and result limits. No
query generation, ranking, scoring, retry scheduling, MCP signature or SQLite
schema changes are included. External provider availability and complete coverage
cannot be established by these offline tests. Diagnostics intentionally omit raw
exception messages and authenticated URLs; some nested errors expose only a safe
exception type rather than detailed provider response content.

Regression tests use real provider wrappers, mocked HTTP/SDK boundaries, real
aggregation and a writer-to-SQLite test. They distinguish clean empty responses,
failures, successful variants, marker failures and optional-provider skips; check
deterministic ordering and sentinel-secret containment; and restore the old
list-only behavior to prove the failure assertions detect swallowed diagnostics.
