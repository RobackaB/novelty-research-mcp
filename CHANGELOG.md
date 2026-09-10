# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every release keeps the 7 MCP tool interfaces (names, parameters, response shape)
compatible with the exported Flowise architecture.

## [0.9.7] Documentation translated to English, Slovak localisation corrected

### Changed

- All documentation-only source prose is now in English. Roughly 500 docstrings and comments were translated across the scoring and query core, `research_session.py`, the rendering layer, the three evidence packs, the patent provider, the web and publication providers, the fetch and verify layer, the remaining `tools/` modules, the runtime entry points, `.env.example`, the evaluation harness, the test suite, and `AUDIT.md`. `README.sk.md` remains the intentionally Slovak documentation file; behaviour-carrying, localisation and fixture strings listed below are preserved deliberately.
- Terminology was aligned while translating: "final answer" for the generated output, "report" for the rendered research artefact, and "user answer" only where it names `research_session_user_answer`.

### Fixed

- Three localisation defects in Slovak report output. The per-requirement table header was a hardcoded English literal, the missing-label fallback was hardcoded `"(unspecified)"`, and the not-fully-verified prefix was written without diacritics though the status words directly above it had them — a Slovak report carried five English fragments. The header and the fallback now come from `_section_labels`. English output is unchanged. That code path had no test coverage at all; the added tests assert the absence of English fragments rather than the presence of specific Slovak strings, so they also catch any unlocalised field added there later.

### Verification methodology

- **Stripped-AST equivalence.** Documentation-only Python translation chunks were verified by stripping docstrings from both versions and comparing AST dumps. Comments never reach the AST, so identical stripped trees prove no executable code, constant, regex or vocabulary set changed in those chunks. The test-suite assertion-message changes used the narrower exceptions documented below.
- **String-literal diff.** For the test suite, every non-docstring string constant was enumerated in both versions; each changed literal had to appear in a closed list of 14 approved assertion messages, and fixture, query and expected-output strings had to be byte-identical.
- **Enumerated f-string exception.** Two assertion messages in `test_relevance_quality.py` are f-strings, which the main verifier deliberately refuses to normalise. A separate check proves those are the only `JoinedStr` differences anywhere in `tests/`, that their `entry['id']` interpolation is unchanged, and that no other f-string differs.
- **Byte-identical checks** for the 7 MCP tool descriptions and the 86 `terminal_ui.py` string literals.
- **Numeric integrity** for `AUDIT.md`: every numeric token was compared before and after. No value was added or removed. Historical test counts (126, 145, 159, 166, 189, 217, 229, 243, 258, 266, 270) and all measured precision/recall/F1 figures are preserved exactly, not modernised to the current count.
- **Negative controls.** The two custom test-suite string verifiers were tested against deliberate violations before their passing results were trusted. Two verifier bugs were found and fixed while developing these checks: sorting string constants by `(lineno, value)` misaligned the positional comparison whenever a value changed, and the assumed static-parts shape of an f-string was wrong.

### Preserved deliberately

Slovak that is data or interface, not documentation, is unchanged:

- `STOPWORDS` in `relevance.py` and `QUERY_FILLER_RE` in `publications_search.py` — Slovak stopwords used to tokenise Slovak-language queries.
- `_GENERIC_HEAD_NOUNS` and the query-stripping regexes in `research_session.py` — Slovak vocabulary and meta-question patterns.
- The `_section_labels` table in `user_answer.py` and every `language == "sk"` branch — intentional user-facing localisation.
- The 7 `@mcp.tool()` docstrings in `server.py` — **behaviour-carrying protocol text**. FastMCP transmits them verbatim as MCP tool descriptions to the Flowise LLM, so translating them would change the prompt the model sees. A separate behaviour-change candidate, with routing evidence and regression tests, is deferred.
- The `terminal_ui.py` startup banner — user-facing runtime localisation, asserted by existing tests.
- 13 Slovak literals in `tests/` — fixtures, query-language inputs, expected Slovak output, and the deliberate negative assertion that the old localisation bug is gone.

## [0.9.6] Iteration-order audit and a standing determinism guard

### Added

- `tests/test_determinism.py`, a permanent guard against hash-order dependence returning anywhere. It digests two workloads — the retrieval-side filtering path and the storage-to-rendered-report path — and re-runs each in a fresh interpreter per `PYTHONHASHSEED`. Subprocesses are required: the seed is fixed for the life of a process, so an in-process loop cannot detect this class of bug. Fixtures are deliberately tie-heavy, because a workload with distinct scores never reaches a tie-break and would pass against broken code.

### Audited, no change required

- The codebase was searched for other places where set or dict iteration order could reach observable behaviour: an AST pass over `tools/` and `server*.py` flagged 379 candidate sites, each traced by hand. **No further correctness-affecting dependency was found.** `AUDIT.md` section 18 records every site and its verdict, including the tie-prone ones that are deterministic only because their input is an ordered list.
- The first differential probe written for this audit was invalid: it passed even with the v0.9.5 bug deliberately reintroduced, because injecting pre-built evidence packs bypasses retrieval entirely. Both committed probes are now validated against that reintroduced bug.

### Not done deliberately

- `web_search.py:728` and `evidence_quality.py:107` are tie-prone and deterministic only because their callers pass ordered containers. Adding a final unique tie-break would make them robust rather than merely correct, but that changes current output ordering and is a behavioural change, not an audit finding.

## [0.9.5] Deterministic relevance and production-faithful measurement

### Fixed

- Domain anchor selection was not deterministic. `salient_query_tokens` ranked a **set** of tokens by IDF alone, and `sorted` is stable, so tokens with equal weight kept set-iteration order — which depends on Python's per-process string hash seed. IDF ties are common by construction, so whenever a tie group straddled the `top_n` cut, different anchors survived on different runs and the same query could accept different documents. Forty identical evaluation runs produced three different results, one of which rejected a relevant document. The sort now uses the token as a deterministic secondary key, matching the pattern already used in `patent_search.py`.
- The evaluation harness measured at a hardcoded threshold of 3.5 while the server runs at 3.0 (patent, publication) and 2.8 (web), so every published quality metric described a configuration that never ran. Thresholds are now imported from `tools.relevance` and resolved per source type; a test asserts the two cannot drift apart again.
- The `--no-idf` help text claimed to restore pre-v0.9.0 behaviour. It disables IDF weighting but keeps the stemmer fix, so it produces a third set of numbers matching neither the before nor the after column. Corrected, with the real reproduction procedure documented in `AUDIT.md` 14.4.

### Changed

- `AUDIT.md` 14.4 replaced. The previously published +22 % F1 improvement was the single best cell of a threshold sweep and sat at a threshold no source type uses. At production settings the gain is +4 % F1 and comes with a **17 % recall regression**. The correction, the full sweep and the dataset-size limitation are stated explicitly.
- The regression guard is re-pinned to the production-threshold numbers (0.611 / 0.833 / 0.683). At the old 3.5 the guard passed on every run, which is why the nondeterminism went unnoticed.
- `README.md` no longer implies the measurement is strong evidence; it states the dataset size (3 queries, 20 candidates) up front.

### Added

- `python -m eval.relevance_eval --sweep` prints precision/recall/F1 across thresholds and marks which rows are production settings.
- Tests for deterministic tie-breaking, anchor stability under shuffled query word order, and reproducibility across **subprocesses** — necessary because `PYTHONHASHSEED` is fixed for the life of a process, so a single-process loop cannot detect this class of bug.

### Not done deliberately

- Production thresholds are unchanged. Retuning them on 20 candidates would be fitting to six positive examples.
- `top_n` still cuts mid-tie rather than keeping all equally salient tokens. That is arguably more principled but changes filter semantics, and this dataset cannot measure whether it helps.

## [0.9.4] Patent relevance and report visibility

### Fixed

- The domain anchor now applies to every patent ranking path, not just the low-confidence fallback. Term-rarity weighting cannot catch an off-topic result when all candidates belong to one patent family, because every term then has the same document frequency. An image-comparison filing scoring 3.58 against a log-anomaly query is now rejected.
- A failed or blocked detail fetch no longer erases the search snippet already held, which had made verified-attempt candidates invisible while an unattempted one stayed in the report.
- The same invention filed under several publication numbers is collapsed to one entry, so a single family can no longer fill the patent section.

### Added

- `docs/example-report.md`, a verbatim report from a real run, linked from the README.

## [0.9.3] Dependency compatibility

### Fixed

- The `mcp` dependency was declared as `>=1.0.0`, so a clean install picked up the 2.x line, which is a breaking release: `mcp.server.fastmcp` no longer exists and `streamablehttp_client` was renamed to `streamable_http_client`. Continuous integration caught this on a fresh environment even though local installs worked. The requirement is now `>=1.9.0,<2`, matching the API the project is written against.
- The AlphaXiv Streamable HTTP client is resolved at call time and accepts either function name, so an optional transport can never break the package import.

## [0.9.2] Patent result quality under provider blocking

### Fixed

- WIPO PATENTSCOPE result rows were passed on as evidence snippets including the full international patent classification text. Its generic wording (`recognising patterns`, `computing`, `data`) produced false relevance matches, which put unrelated patents such as image-comparison filings into a log-anomaly report. Classification and form fields are now stripped; if nothing substantive remains, no snippet is produced and relevance is judged from the title alone.
- The low-confidence fallback returned every candidate, including ones scoring 0.0. A minimum relevance floor now applies, so an incomplete result is reported honestly instead of listing clearly unrelated patents.

### Changed

- The keyless Google Patents provider is documented as best-effort: it returns HTTP 503 under the same anti-automation protection that affects patent detail pages, so patent search may fall back to other providers.

## [0.9.0] Measurable relevance and output quality

### Added

- Offline evaluation harness in `eval/` with a gold-standard dataset of three scenarios, one taken verbatim from a real production run. Reports precision, recall, F1, P@k, MAP and MRR; run with `python -m eval.relevance_eval`.
- Corpus-level IDF term weighting (`build_corpus_idf()`) applied as a second pass over the merged candidate set, plus a `salient_query_tokens()` domain anchor. Weights come from the retrieved candidates, so no hardcoded domain vocabulary is introduced.
- IDF reranking wired into publication search, web search and patent ranking, with a guard so stricter filtering can never empty a whole source.
- Quality regression tests that fail if precision drops below the measured level or if IDF stops outperforming uniform term weights.
- Text cleaning helpers in `tools/output_cleaner.py`: `unescape_entities()`, `collapse_repeats()`, `strip_boilerplate()` and `strip_leading_title()`.

### Fixed

- Stemmer never matched singular against plural ("logs" vs. "log", "application" vs. "applications"), so a document about log analysis scored zero credit against a query about logs. Plural stripping now runs first, the guard length dropped to 3, and words ending in "ss" are protected.
- Reports no longer carry undecoded HTML entities, README markdown headings, repository metadata, site navigation or marketing calls to action into finding summaries.

### Changed

- Offline dataset: precision 0.486 → 0.667 (+37 %), F1 0.600 → 0.733 (+22 %), recall unchanged at 0.833. Real-run scenario alone: precision 0.12 → 0.33, F1 0.20 → 0.40.
- Live run on the original query: 5 off-topic results (seismology, ionosphere, GNSS, firefighting robot, social networks) → 0, papers actually about logs 2 → 3, with 19 candidates filtered out.
- A summary is shown only if it contains at least one query term; the finding, its URL and its evidence level stay visible either way.
- Test suite grew from 229 to 258 tests.
- Known measured limit: P@2 in the real-run scenario stays at 0.00, because a purely lexical method cannot separate documents that share nearly the whole query vocabulary but differ in subject.

## [0.8.0] Patent full-text retrieval

### Added

- PDF-first patent fetching. The official patent PDF on `patentimages.storage.googleapis.com` is tried before the HTML page, and that host is not covered by the bot protection that blocks `patents.google.com`.
- Patent metadata (`assignee`, `filing_date`, `grant_date`) and the PDF path are read from the Google Patents XHR response instead of being scraped from HTML, where they came back as `Unknown`.
- Explicit `STATUS: BLOCKED` / `EVIDENCE_LEVEL: FETCH_BLOCKED` state with a `BotBlockedError` class, plus a best-effort Wayback Machine fallback.
- `JINA_API_KEY` support for Jina Reader, sent as an `Authorization: Bearer` header, removing anonymous-tier `403` failures on DOI verification.
- Unpaywall resolution of open-access DOIs to a direct PDF link, so journal publications get full-text analysis and not only arXiv and direct `.pdf` URLs.

### Fixed

- Google Patents answered automated requests with HTTP 503, so claim coverage, description extraction and exact-combination detection never ran for any patent. Concurrent requests to that host are now capped at 2 by a semaphore, and the block is reported honestly instead of as a parse failure.
- `web_evidence_pack` now parses `ERROR: type - message` markers from the search output, so the real provider error reaches the caller instead of a generic "see server logs".
- Query variant selection truncated atoms by position; more specific categories (function, mechanism, constraint) now take precedence over generic ones when trimming to the token limit.
- Writer ACKs duplicated full warning text next to the compact form, and the checklist returned the same retry list under two keys. Warnings are truncated to 240 characters per item and the duplicate key was removed.

### Changed

- Measured on the same query as the production run: patent evidence level 5× snippet-only → 3× claim-verified plus 1× abstract-verified, claim coverage always 0 → up to 1.0, extracted text per patent 0 → 7706 words (US10831585B2), single fetch 15198 ms → 737 ms (roughly 20× faster).
- Text from two-column patent PDFs feeds element coverage only; `CLAIM1` and `ABSTRACT` keep their "not found" sentinels rather than quoting interleaved column text as if it were a verbatim claim.
- `patent_fetch` gained a `pdf_url` parameter (deliberate signature change).
- Test suite grew from 217 to 229 tests.

## [0.5.0] Deeper source analysis

### Added

- `tools/pdf_fetch.py`: PDF sources (datasheets, manuals, papers) are downloaded with a 15 MB limit and text-extracted from the first 25 pages instead of being kept as snippet-only evidence. Verified live on an arXiv PDF at 6123 extracted words.
- `tools/requirement_match.py`: stem-aware requirement matching shared by all three evidence packs and the report; the patent pack previously used naive substring matching.
- `tools/query_expansion.py`: deterministic synonym and acronym expansion (IoT, ML, anomaly/outlier, predictive/condition-based, and similar), so retries search a genuinely different result space and the `synonyms` envelope field is no longer always empty.
- Key-free Google Patents provider using the native `patents.google.com/xhr/query` JSON interface. Live test returned 5 relevant patents (US, EP, CN) with no API keys configured at all.
- arXiv promoted from emergency fallback to a full parallel publication provider with standard block format and a 0.9 quality multiplier.
- Real AlphaXiv MCP integration in `tools/alphaxiv_client.py` against `https://api.alphaxiv.org/mcp/v1` with `ALPHAXIV_API_KEY`, replacing an assumed local `alpha` CLI that never existed. Tested over the real MCP protocol through an in-memory FastMCP server.
- Full patent claims and description sections (`CLAIMS_TEXT` up to 1200 words, `DESCRIPTION_TEXT` up to 1500 words) instead of claim 1 and the abstract only.
- `COVERAGE_TOKENS`: deduplicated tokens of the whole extracted page or PDF (cap 2500), so element coverage sees the entire document rather than a query-focused condensation.
- Full-text PDF analysis for open-access publications with `fulltext_analyzed` and `fulltext_word_count`; a successful full-text read can raise the evidence level to `fetched_excerpt`.
- Cross-source corroboration: the same document (patent number, DOI or URL) confirmed independently by several source types is marked in the report and exposed as `corroborated_documents`.
- Query-focused abstract excerpts for all publication providers, replacing a fixed cut at the first 60 words.
- Deeper web fetch: an `ANALYSIS` field with up to 900 words from the 24 most relevant sentences, used for scoring and coverage while the displayed summary stays short.

### Fixed

- `exact_combination_candidate_found` was never set by any code path, making the `exact_match` verdict and the `exact_combination_found` stop reason dead functionality. Every hit now carries `atom_coverage` and `atom_match_count`, and full coverage by a verified document propagates through SQLite into the checklist and the verdict.
- Stemmer defect where "codes" stemmed to "cod" but "code" to "code", so singular and plural never matched.

### Changed

- Atomic requirements are handed to all three writers, not just the patent one.
- Relevance is upgraded to `direct`/`focused` at 50 % element coverage or higher, and the report shows element coverage for every finding.
- `ALPHA_CLI_PATH` replaced by `ALPHAXIV_API_KEY` in `.env.example` and `docker-compose.yml`; the startup banner now tracks `ALPHAXIV_API_KEY` and `PUBMED_API_KEY`.
- Test suite grew from 126 to 189 tests.

## [0.2.0] Correctness fixes and test suite

### Added

- First test suite: 126 tests across 14 files, all offline with the network stubbed. Install with `pip install -e .[dev]` and run `python -m pytest`.
- `tools/_ttl_cache.py` with a TTL and an entry cap (64 for search, 256 for fetch) plus LRU-style eviction, replacing unbounded in-memory dictionaries.
- Packaging fixes in `pyproject.toml`: `server_http` and `terminal_ui` added to `py-modules` (previously `pip install .` omitted the HTTP entry point), a `mcp-research-server-http` script, a `dev` extra and pytest configuration.
- Healthcheck for the MCP server in `docker-compose.yml` and `Dockerfile`; Flowise now waits for `condition: service_healthy` before starting.
- `.env.example` completed with every variable the code actually reads (`PUBMED_API_KEY`, `FLOWISE_USERNAME`/`PASSWORD`, `MCP_HOST`/`PORT`/`PATH`, `MCP_ALLOWED_HOSTS`/`ORIGINS`, `RESEARCH_SESSION_DB`, UI switches).

### Fixed

- The OpenAlex provider never returned anything: an invalid `authors_count` field in `select` made every call fail with HTTP 400 and the exception was swallowed silently. Verified against the live API before (400) and after (200).
- Requirement coverage fallback compared a count against an index, so the report could claim a source verified a requirement it never touched; it also used label keys that did not exist, leaking English phrases into the Slovak report.
- SQLite connections were never closed, since `with _connect() as conn` only commits or rolls back. Every tool call leaked a handle and kept database files locked on Windows.
- Deprecated arXiv `Search.results()` replaced with `arxiv.Client(...).results(search)` with bounded `page_size` and retry.
- Startup banner advertised four tools the server does not register; it now prints exactly the 7 registered MCP tools, checked by a test against the real registration.
- A non-numeric `relevance_score` from a provider crashed the entire evidence write; added a `_safe_float` helper.
- Playwright was imported at module level, so the `tools` package, the server and the tests could not be imported without it. It is now a lazy import with a fallback timeout class.
- Dead and unreachable code removed in `evidence_quality.py`, `patent_search.py` and `web_evidence_pack.py`; the `non_english` envelope value now maps directly to Slovak output.

## [1.0-thesis] Initial bachelor's thesis submission

Baseline state of the project as submitted, roughly 6700 lines of Python.

- 7 MCP tools covering patents, publications and web search in a single workflow.
- Persistent session state in SQLite with retry budgets, a checklist and evidence-level grading.
- Deterministic server side: query understanding, relevance scoring and report rendering use no LLM inside the MCP server.
- Orchestration through Flowise, with the architecture exported to `flowise_architecture/Flowise_agent.json`.
- No automated tests.
