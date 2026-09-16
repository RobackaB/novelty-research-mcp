# Provider failure diagnostics

Provider error messages are diagnostic data, not retrieved evidence. Google CSE
puts its API key in a request URL; HTTPX includes that URL in an HTTP status
exception's string representation. Previously, web search copied the exception
into error markers, and the web writer persisted it in both `evidence_json` and
`normalized_json`. The immediate writer ACK did not contain the key.

## Rendering contract

`tools._provider_errors.provider_error_message` returns only the exception class
name and, for `httpx.HTTPStatusError`, the numeric HTTP status. It never renders
the exception message, request URL/headers or response body. This also avoids
copying tokens from timeout messages, arbitrary provider messages or nested
exception groups. Provider identity remains in the surrounding diagnostic.

The formatter is used at web search, patent search and publication search error
boundaries and in the AlphaXiv client failure log. Fixed application-authored
configuration/unavailable messages remain descriptive. Candidate text, document
URLs, scoring, ranking, provider execution and retry policies are unchanged.

Publication fallback classification still examines the original exception where
the existing code requires it. The redacted diagnostic is never substituted into
that decision. Error counts, completion flags and failure/no-results distinctions
remain intact; emitted error text intentionally changes.

## Adjacent-path review and limits

- Google CSE: confirmed query-parameter credential exposure; covered through the
  real provider, evidence pack and session writer with mocked HTTP responses.
- Tavily and Exa: keys travel in headers/bodies, so an ordinary HTTP status URL
  does not itself expose them. Shared web/patent exception rendering is protected
  against echoed credentials and credential-bearing redirect URLs nonetheless.
- Semantic Scholar and AlphaXiv: header authentication; publication diagnostic
  boundaries and the AlphaXiv failure log use the same formatter.
- PubMed: query-parameter authentication, but its request exceptions are caught
  and return empty records. Regression tests cover both search and fetch errors;
  this change does not alter that existing failure policy.
- Crossref, OpenAlex and arXiv: no configured provider secret in the inspected
  request paths. Parallel publication errors and outer fallback diagnostics are
  protected. Standalone document-fetch/arXiv diagnostics were not refactored.

This is not general query/document privacy filtering. Query text and source URLs
remain part of normal evidence and some existing logs. Independently enabled
third-party HTTP debug logging is outside this formatter's boundary. Existing
databases or previously exported errors are not rewritten or scrubbed.

## Verification

`tests/test_provider_error_redaction.py` exercises HTTP 403/429/503 and timeout
failures, persisted evidence, mixed provider outcomes, application logs and
publication fallback branches. It compares sanitized output against the original
rendering with only the diagnostic replaced, and checks identical writer ACKs,
duplicate handling and retry plans. A defect-restoration negative control replaces
the web formatter with `str` and requires both returned/persisted leak checks to
fail. All fixtures use synthetic credentials and stubbed I/O.
