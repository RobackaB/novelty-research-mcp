# Patent verification and evidence promotion

This change separates patent discovery from document verification. A candidate
found by Tavily, Exa or WIPO can receive an official PDF address through an exact
publication-number lookup. Both normally ranked and low-confidence returned
candidates are enriched while the HTTP client remains open. Enrichment does not
add candidates, replace discovery provenance, or alter ranking thresholds.

## Fetch order

For each candidate selected by the existing fetch-depth policy:

1. Direct official PDF extraction, when its address is known.
2. Jina Reader for that PDF, when an evidence upgrade remains possible.
3. Direct Google Patents HTML.
4. Normally rendered Google Patents HTML.
5. Jina Reader for the HTML URL.
6. An available Wayback snapshot, independently of the live backend's failure
   mode (including timeouts, errors and weak successful responses).
7. The original provider snippet remains the pack's last-resort evidence.

Number enrichment currently uses Google's structured XHR endpoint. It is not an
independent patent-office database. PDF URLs can therefore remain unavailable
when that endpoint is blocked. Reader and archive access are also best effort.
No CAPTCHA solving, fingerprint evasion, proxy rotation, EPO OPS integration or
USPTO integration is added. An independent structured source would specifically
address the remaining PDF-location dependency and needs separate scope review.

The evidence pack supplies the retained candidate's publication number to an
internal fetch entry point; the public MCP signature is unchanged. Verification
uses that identity to construct the canonical Google document URL, including for
Tavily/Exa results with non-Google discovery URLs. The original discovery URL is
preserved in the hit and is not used as an identity substitute or arbitrary fetch
target. Fetch caching includes the canonical publication identity.

## Evidence contract

Every backend must establish the requested publication from retrieved document
identity. Formatting variations match; publication kind codes are preserved.
An A1 publication cannot verify a B2 publication. Family/deduplication keys are
unchanged. A requested URL, Reader URL envelope, shared title or a citation to
the target in another patent is insufficient verification.
Family/base URL provenance alone is capped at `fetched_excerpt`; strong promotion
requires exact structured identity or exact identity in retrieved content.

The strongest validated result wins:

`claim_verified > abstract_verified > fetched_excerpt`

Only an identifiable claims section with substantive claim-bearing text earns
`claim_verified`. Bare mentions of claims, numbered prose and unavailable-content
notices do not. `abstract_verified` requires an actual abstract; a long document
without an identified abstract is only `fetched_excerpt`. Section extraction is
bounded by subsequent headings, so unrelated later text cannot satisfy a short
or missing section. These are conservative structural heuristics, not legal
claim construction or proof that every invention requirement is supported.

Claims are the maximum supported level and may end the chain early. Otherwise
later backends can upgrade the result. Weaker, failed or equal-strength results
cannot replace stronger evidence; ties retain the first backend deterministically.
The selected result includes diagnostics for every attempted backend.

Actual claim, abstract or excerpt text reaches the evidence pack. Reader's patent
adapter preserves section headings; the existing web/publication Reader cleaning
remains unchanged. Structured evidence lines are not passed through a boilerplate
filter that could delete legitimate words such as "cookie". Coverage tokens keep
their existing normalization and downstream interpretation. The Requirement
Evidence Matrix and verdict algorithms are unchanged.

## Bounds and security

Backend calls remain bounded; the pack's total timeout accounts for the complete
upgrade chain rather than cancelling after the former two-step budget. Candidate
counts, PDF page/byte limits and research-session retry saturation are unchanged.
Clients must allow enough time for search plus this longer verification chain;
external cancellation still cancels the operation.

Provider exceptions are represented by type/status, not raw exception strings.
Patent output is sanitized before evidence persistence, including configured
secrets, bearer credentials and authenticated URLs (also nested in reader or
redirect URLs). Credential-bearing target URLs are not sent to Reader/archive.
PDF diagnostics omit URLs and untrusted exception messages.

Patent document traffic uses an exact host allowlist for the existing Google
Patents, patentimages storage, Reader and archive services (plus Google's static
browser assets). Loopback, private, link-local, unspecified/reserved addresses,
localhost aliases, credentials and nonstandard ports are rejected. HTTP request
hooks validate redirect destinations before transport; patent browser routing
blocks service workers, checks requests and does not follow redirects. The
patent-only URL-reachability path uses the same policy. Discovery URLs outside
the document-host allowlist remain unverified URLs, not silently fetched. Other
source pipelines retain their existing helper defaults. This policy trusts the
listed services and normal DNS/TLS infrastructure; it adds no new provider.

Passive decision capture omits credential-bearing events with a fail-open
diagnostic. It does not silently replace exact scorer input with redacted text
and claim an exact capture. Such a run has incomplete capture and is unsuitable
for evaluation requiring complete provenance. Safe events still persist;
sanitizer/log-handler failure must not affect retrieval. Goal 5C contracts and
implementation are unchanged.

## Verification scope

Offline tests exercise real search/enrichment over `httpx.MockTransport`, actual
Reader HTTP decoding, the complete fetch/pack/session persistence path, each
backend's identity rejection, evidence upgrades and no-downgrade behavior.
Sentinels are checked in returned evidence, diagnostics, logging and SQLite
decision/evidence tables. Defect-restoration controls cover premature client
closure, incorrect evidence labels/promotion, erased kind codes, Reader cleaning,
excerpt loss and unsafe error/capture persistence.

These controlled tests demonstrate behavior, not a measured recovery rate against
live Google blocking. Scanned PDFs without extractable text, unfamiliar document
layouts/languages, unavailable archives, rate limits and client timeouts remain
limitations. No historical evaluation data, relevance weights/thresholds, public
MCP schema or real-study acquisition is changed.
