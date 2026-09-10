# In-depth audit and improvements — Flowise MCP Research Server (v0.9.4)

This document summarises the results of an in-depth audit of the whole codebase
(~6,700 lines of Python), the fixes implemented (v0.2.0), the qualitative extension of
deep source analysis (v0.3.0, section 8), the search and output improvements (v0.4.0,
section 9), whole-document processing for requirement coverage (v0.5.0, section 10), the
real AlphaXiv MCP integration (v0.6.0, section 11), the fixes that came out of analysing
an actual production run in Flowise (v0.7.0, section 12), working around the Google
Patents block through the official patent PDFs (v0.8.0, section 13), the measurable
improvement in relevance scoring (v0.9.0, section 14), text quality in the final report
(v0.9.1, section 15), patent result quality when the provider is blocked (v0.9.2,
section 16) and patent hit quality verified by a full run (v0.9.4, section 17).
Every change is verified: **270 automated tests pass**, the server started after the
changes and the MCP `initialize` handshake returned a valid response.

## 1. Fixed defects (correctness)

### 1.1 The OpenAlex provider was entirely non-functional — `tools/publications_search.py`
`_openalex_search` sent the OpenAlex API a `select` parameter containing a field that does
not exist, `authors_count`. The API answers **HTTP 400** to every such call, and the
exception was silently swallowed in `_openalex_blocks_safe`, so one of the five publication
providers never returned a single result. Verified by a live API call both before the fix
(400) and after it (200).
**Fix:** the invalid field was removed from `select`.

### 1.2 Faulty requirement-coverage logic in the report — `tools/user_answer.py`
In the fallback branch of `_render_uncertainty_section`, the **number** of requirements a
source covered was compared against the requirement's **index** (`count >= index`). The
result: the report could claim "verified disclosure by: patent" even when the source had
verified an entirely different requirement. It also used labels
(`verified_disclosure_by`, `not_verified`) that did not exist in the label dictionary, so
the Slovak report contained English phrases.
**Fix:** the fallback branch now shows an honest per-source summary ("patent: 2/5
requirements fully verified"), and whenever critical requirements exist, "pseudo-atoms" are
generated for the element-by-element detail, so the *Element-by-element coverage* table
appears even without an atomic decomposition. Pseudo-atoms are used **for display only** —
the verdict and confidence logic is unchanged.

### 1.3 SQLite connection leak — `tools/research_session.py`
`with _connect() as conn:` relied on the `sqlite3.Connection` context manager, which
commits or rolls back the transaction but **never closes the connection**. Every tool call
therefore left an open handle on the database (and on the WAL/SHM files) until garbage
collection — a problem both for a long-running server and on Windows, where the files stay
locked.
**Fix:** `_connect()` is now a `@contextmanager` that always closes the connection after
use. The roughly 20 existing call sites work unchanged. A new test,
`test_db_file_not_locked_after_operations`, verifies that the database file can be renamed
on Windows after the operations complete, meaning no handle is held.

### 1.4 Deprecated arXiv API — `tools/arxiv_search.py`
The deprecated `Search.results()` was in use, which newer versions of the `arxiv` library
are removing.
**Fix:** moved to `arxiv.Client(...).results(search)` with a bounded `page_size` and retry.

### 1.5 The startup banner listed tools that do not exist — `terminal_ui.py`
The banner printed four old names (`patent_evidence_pack`, `merge_evidence_pack`, ...)
that the server does not register at all.
**Fix:** the banner prints exactly the 7 registered MCP tools; a test checks this against
the actual registration through `server.mcp.list_tools()`.

### 1.6 Unbounded in-memory cache growth — `tools/patent_search.py`, `tools/patent_fetch.py`
Both `_PATENT_SEARCH_CACHE` and `_PATENT_FETCH_CACHE` were plain dictionaries with no
limit, so the memory of a long-running server grew without bound.
**Fix:** a new module `tools/_ttl_cache.py` (`TTLCache`) with both a TTL and a cap on the
number of records (64 for search, 256 for fetch), evicting the oldest entries LRU-style.

### 1.7 Write crash on malformed provider data — `tools/research_session.py`
`_insert_raw_items` called `float(hit.get("relevance_score") ...)` unguarded, so a
non-numeric value from a provider brought down the entire evidence write.
**Fix:** a new `_safe_float` helper, with the same safe conversion applied in
`_row_quality_key`.

### 1.8 Playwright as a hard dependency — `tools/chromium_scraper.py`
Importing Playwright at module level meant that without Playwright installed the whole
`tools` package could not be imported, and therefore neither the server nor the tests could
run.
**Fix:** a lazy import inside the function plus a substitute `PlaywrightTimeoutError` class.
The web and patent fetch fallbacks (Jina, Wayback, Crossref, static HTTP) already handle
this state correctly, so the server is usable without the Chromium layer.

## 2. Minor fixes and cleanup

- `tools/evidence_quality.py`: the duplicated `weak` branch was merged and a dead variable removed.
- `tools/patent_search.py`: the dead function `_matches_required_concept` was removed (it always returned True).
- `tools/web_evidence_pack.py`: the condition skipping unsupported URLs was unreachable,
  because the title always carries the placeholder "Untitled result" — the placeholder no longer counts as a real title.
- `tools/user_answer.py` `_detect_language`: the envelope value `non_english` (which
  `research_session._detect_query_language` genuinely produces) now maps directly to Slovak output;
  previously it relied on a regex over the query alone.
- `tools/research_session.py`: the `trace_events` filter `source_type != ''` (the column is NOT NULL),
  duplicate quality scoring removed from `_row_quality_key`, imports moved above `LOGGER`.
- `tools/web_search.py`: cosmetic (an f-string with no placeholder).

## 3. Configuration and deployment

- **`pyproject.toml`**: version 0.2.0; `server_http` and `terminal_ui` added to `py-modules`
  (previously `pip install .` did not include the HTTP entry point); a new
  `mcp-research-server-http` script; `[project.optional-dependencies] dev` (pytest,
  pytest-asyncio); `[tool.pytest.ini_options]`.
- **`.env.example`**: every variable the code actually reads but that was missing has been added:
  `PUBMED_API_KEY`, `ALPHA_CLI_PATH`, `FLOWISE_USERNAME/PASSWORD`, `MCP_HOST/PORT/PATH`,
  `MCP_ALLOWED_HOSTS/ORIGINS`, `RESEARCH_SESSION_DB`, `NO_COLOR`/`MCP_PLAIN_UI`/`MCP_NO_UI`.
- **`docker-compose.yml`**: `PUBMED_API_KEY` passthrough added; a healthcheck for the MCP
  server (a TCP check on port 8000); Flowise now waits on `condition: service_healthy`, so it
  does not start before the MCP server is ready.
- **`Dockerfile`**: a `HEALTHCHECK` instruction for running standalone outside compose.

## 4. New test suite (previously: no tests)

`tests/` — **126 tests**, none of which touch the network (network access is stubbed via monkeypatch):

| File | Covers |
|---|---|
| `test_query_normalize.py` | cleaning queries from Flowise (dict/JSON/list inputs) |
| `test_relevance.py` | tokenisation, scoring, relevance thresholds, subject anchors |
| `test_result_contract.py` | status markers and their round-trip parsing |
| `test_evidence_quality.py` | source grading, verdicts, confidence, degradations |
| `test_hit_sort_and_cache.py` | hit ordering, TTLCache (expiry, eviction) |
| `test_patent_filters_and_bounds.py` | patent number extraction, result validation, query plan |
| `test_merge_evidence_pack.py` | merging sources, the legacy bundle, URL repairs, flags |
| `test_final_answer_pack.py` | the debug report, its sections, conservative behaviour on error |
| `test_user_answer.py` | the answer payload, language (SK/EN), the coverage table, pseudo-atoms |
| `test_research_session.py` | the whole SQLite workflow: start → understand → save → checklist → answer, duplicates, budget, a closed session, file locking, async writers with stubs |
| `test_evidence_packs_stubbed.py` | the patent/publication/web evidence pack pipeline with the network stubbed |
| `test_web_and_publication_helpers.py` | tracking parameters, low-value URLs, MDPI→DOI, PubMed XML, the OpenAlex abstract |
| `test_server_and_ui.py` | registration of exactly 7 MCP tools, `_safe_writer_ack`, the banner |
| `test_output_cleaner.py` | HTML cleaning, claim coverage, normalising patent identity |

To run: `pip install -e .[dev]` (or `pip install pytest pytest-asyncio`) and `python -m pytest`.

## 5. Functional verification

1. `python -m compileall` — no errors.
2. `python -m pytest tests` — **126 passed**.
3. `python server_http.py` — the server started, and `POST /mcp` with a JSON-RPC `initialize`
   returned **HTTP 200** and a valid `serverInfo` ("Research Server").

## 6. What was deliberately left unchanged

- The interfaces of all 7 MCP tools (names, parameters, the shape of the JSON responses) —
  fully compatible with the exported Flowise architecture `flowise_architecture/Flowise_agent.json`.
- The verdict and confidence logic (`decide_verdict_and_confidence`) — its behaviour is now
  pinned by tests, not changed.
- The deterministic character of the system (no LLM calls on the server side).

## 8. Deep source analysis (v0.3.0)

An extension addressing the "deepen source analysis" and "improve relevance scoring"
points from the defence presentation: the system now processes substantially more content
from each source and measures query requirement coverage uniformly across the whole flow.

### 8.1 PDF document processing — `tools/pdf_fetch.py` (new module)
PDF sources (datasheets, manuals, scholarly articles) were previously discarded as
"snippet-only" evidence — the document's content was never read. The new module downloads
the PDF (15 MB limit, streamed), extracts text from the first 25 pages via `pypdf`, and the
web evidence pack processes it as full `fetched_excerpt` evidence. If extraction fails, the
original snippet-only behaviour remains. Verified live on a real arXiv PDF (6,123 words
extracted, requirement coverage computed correctly).

### 8.2 Deeper web fetch — `tools/web_search.py`
A fetched page previously yielded at most ~450 words (8 sentences). The fetch output now
additionally carries an `ANALYSIS` field with up to 900 words drawn from the 24 most
relevant sentences. The displayed summary stays short; the extended text is used for
scoring and coverage computation.

### 8.3 Full patent claims — `tools/patent_fetch.py`
Only claim 1 and the abstract were previously extracted from a patent page. The whole
claims section is now extracted as well (`CLAIMS_TEXT`, up to 1,200 words), and requirement
coverage is computed over claim 1 plus all claims plus the abstract. A combination of
elements spread across dependent claims — the typical case — is therefore now caught.

### 8.4 Uniform coverage measurement — `tools/requirement_match.py` (new module)
Stem-aware requirement matching (previously only in the final report) is factored into a
shared module used by all three evidence packs and by the report. The patent pack
previously used a naive `term in text` check with no inflection handling. A stemmer defect
was fixed at the same time: `codes` stemmed to `cod` while `code` stemmed to `code`, so
singular and plural never matched; a new suffix ordering resolves this.

### 8.5 Atom coverage for every source, and reviving exact_match
The audit showed that **nothing anywhere set** `exact_combination_candidate_found` — the
`exact_match` verdict branch and the `exact_combination_found` stop_reason were dead
functionality. Now:
- all three writers receive the atomic requirements from the query envelope (previously only the patent writer),
- every hit carries `atom_coverage`/`atom_match_count` (the share of query elements fully
  covered by the document's content),
- on full coverage by a verified document, `exact_combination_candidate_found=True` is set,
  which travels through SQLite into the checklist (`stop_reason=exact_combination_found`)
  and the verdict (`exact_match`) — confirmed by an integration test,
- relevance is upgraded to `direct`/`focused` at high coverage (≥ 50 %),
  and the report shows "element coverage: X %" for each hit.

### 8.6 v0.3.0 verification
- `python -m pytest` — **145 passed** (19 new tests: requirement_match, pdf_fetch with a
  hand-built valid PDF, the PDF pipeline in the web pack, an exact candidate from full
  claims, publication relevance upgrade, passing atoms to the writers, and end-to-end
  propagation of the exact flag through to the verdict).
- A live test of PDF extraction on a real arXiv document.
- The server started and MCP `initialize` returned HTTP 200.

## 9. Search and output improvements (v0.4.0)

### 9.1 Google Patents as a key-free patent provider — `tools/patent_search.py`
Patent search previously rested on Tavily/Exa API keys and fragile WIPO scraping — without
keys the system found essentially no patents. The added provider
`_google_patents_xhr_search` uses the native JSON endpoint `patents.google.com/xhr/query`
(no API key) and is counted among the primary providers, so its completion with no hits is
a reliable negative signal. Verified live: a smart lock query returned 5 relevant patents
(US, EP, CN) with no keys configured at all. `include_domains` for Tavily/Exa was extended
with patents.justia.com and worldwide.espacenet.com at the same time.

### 9.2 Synonym and acronym query expansion — `tools/query_expansion.py` (new module)
The `synonyms` field of the query envelope was previously always empty, and retry attempts
often repeated near-identical query variants. A new deterministic lexicon (highly reliable
technical equivalents: IoT ↔ internet of things, ML ↔ machine learning, anomaly ↔ outlier
detection, predictive ↔ condition-based maintenance, smart ↔ intelligent…) generates
variants that preserve meaning. The envelope now populates `synonyms`, and the variant
lists for patent/publication/web include the expansions — so retry attempts genuinely
search a different result space.

### 9.3 arXiv as a full parallel provider — `tools/publications_search.py`
arXiv was previously used only as an emergency fallback after Semantic Scholar failed. It
now runs in parallel with the other five providers, its blocks use the standard format (so
title/DOI deduplication works across providers), and it carries a quality multiplier of 0.9.

### 9.4 Query-focused abstract excerpts — `tools/publications_search.py`
Abstracts were previously truncated to the first 60 words, so a relevant sentence at the
end of a long abstract never reached the evidence. `_relevant_abstract_excerpt` now selects
the sentences with the greatest overlap with the query (preserving their order) for every
provider (Semantic Scholar, Crossref, PubMed, OpenAlex, AlphaXiv).

### 9.5 Cross-source document corroboration — `tools/user_answer.py`
When the same document (a patent number without its kind code, a DOI or a URL) is
confirmed independently by more than one source type, the system now detects it: the report
contains an "Independent confirmation: …" line, the affected hits carry a "confirmed by
multiple sources" marker, and the payload gains a `corroborated_documents` field.
Independent confirmation from different search paths is a strong signal that a hit is
correct.

### 9.6 v0.4.0 verification
- `python -m pytest` — **159 passed** (14 new tests: query expansion and envelope
  integration, parsing the Google Patents XHR response, the arXiv adapter and dedup keys,
  query-focused excerpts, corroboration detection including patent family matching).
- Live test: `patent_search` returned 5 relevant patents with no API keys.
- A live test of the XHR response shape before the parser was implemented.
- The server started and MCP `initialize` returned HTTP 200.

## 10. Requirement coverage from whole documents (v0.5.0)

Up to v0.4.0, query requirement coverage was computed from condensations — a selection of
sentences relevant to the query. That had a weakness: a sentence containing a missing
element that did not make the selection was never counted towards coverage. Coverage now
sees the whole document's content.

### 10.1 Web: whole-page tokens — `tools/web_search.py`, `tools/web_evidence_pack.py`
The fetch tool's output carries a new `COVERAGE_TOKENS` field — the deduplicated tokens of
the **whole** extracted page text (capped at 2,500 unique tokens). Because coverage
matching only tests for the presence of terms, a deduplicated token set preserves the
matching result while carrying the whole page's content compactly. The displayed summary
and relevance scoring are unchanged, since more text would dilute the signal there. The
same applies to PDF documents in the web pack.

### 10.2 Patents: the description section — `tools/patent_fetch.py`, `tools/patent_evidence_pack.py`
Alongside the claims and the abstract, the description of the invention is now extracted
from the patent page (`DESCRIPTION_TEXT`, up to 1,500 words; selectors
`section[itemprop='description']` and alternatives). The description tends to be technically
richer than the legal text of the claims — requirement coverage is computed over the claims
plus the abstract plus the description.

### 10.3 Publications: full PDF texts — `tools/publication_evidence_pack.py`
For candidates with a freely available full text (arXiv `abs` → `pdf`, direct `.pdf`
links), the whole article is downloaded through the existing PDF pipeline (at most 3
documents per attempt, and only when atomic requirements exist). The full text is used for
requirement coverage; the hit carries `fulltext_analyzed` and `fulltext_word_count`. If the
publication page fetch failed, successfully reading the full text raises the evidence level
to `fetched_excerpt` — so a publication can support
`exact_combination_candidate_found` even without a verified abstract.

### 10.4 v0.5.0 verification
- `python -m pytest` — **166 passed** (7 new tests: COVERAGE_TOKENS contain sentences
  outside the condensation, coverage from whole-page tokens, extraction and use of the
  patent description, deriving the PDF URL, coverage from a publication's full text with an
  evidence level upgrade, and the economical behaviour when no atomic requirements exist).
- The server started and MCP `initialize` returned HTTP 200.

## 11. A real AlphaXiv MCP integration (v0.6.0)

While verifying the `.env` variables it emerged that `ALPHA_CLI_PATH` (an optional local
`alpha` CLI tool) had been a non-working assumption from the start — AlphaXiv does not in
fact expose any installable CLI, but **its own remote MCP server** at
`https://api.alphaxiv.org/mcp/v1`, authorised through `Authorization: Bearer <key>` (the
key is created under Settings > API Keys on alphaxiv.org). Verified by directly downloading
the MCP server's official documentation (`/docs/mcp`), which describes the `discover_papers`
tool (input: `keywords`, `question`, `difficulty`; output: 5-15 articles with title,
publication date, organizations, abstract preview, arXiv ID).

### 11.1 New module `tools/alphaxiv_client.py`
Since the project already depends on the `mcp` package (for its own server side), the same
package also provides the client side (`mcp.client.streamable_http.streamablehttp_client`
plus `mcp.ClientSession`). The new module connects, calls `discover_papers` and handles the
result (`CallToolResult`) robustly: `structuredContent` first (keys `result`, `papers`,
`results`, `data`, `items`), then the text blocks (one JSON object per block — exactly how
FastMCP serialises returned lists, verified by a direct test against the library before
implementation), and finally the joined text as a fallback for the text parser.
Without `ALPHAXIV_API_KEY` it attempts no network connection at all; on failure (a bad key,
a timeout, a tool error) it quietly returns an empty list — the provider is always merely
supplementary. Verified live: an invalid key fails cleanly in 0.83 s without hanging.

### 11.2 Wiring into `tools/publications_search.py`
The original `_alpha_executable`/`_run_alpha_command` (a subprocess call to a local
binary) is replaced by a call to `alphaxiv_client.discover_papers`. `_format_alpha_item` is
extended with AlphaXiv's documented fields (`abstractPreview`, `publicationDate`,
`organizations`) — when an item carries only `organizations` (institutions) and no `authors`
(people), the report labels it correctly as "Organizations listed…" rather than misleadingly
as authors.

### 11.3 Testing over the real MCP protocol without a network
The `mcp` package provides `mcp.shared.memory.create_connected_server_and_client_session`,
a helper that connects a client to a server over an in-memory transport without HTTP. The
tests therefore stand up a real temporary FastMCP server exposing a `discover_papers` tool,
and our client calls it over the genuine MCP protocol (JSON-RPC framing, the initialize
handshake, result serialisation). This is not an ordinary mock but verification against the
library's real implementation, merely without the network layer.

### 11.4 Configuration
`.env.example` and `docker-compose.yml`: `ALPHA_CLI_PATH` replaced by `ALPHAXIV_API_KEY`.
The startup banner (`terminal_ui.py`) now also tracks `ALPHAXIV_API_KEY` and `PUBMED_API_KEY`.

### 11.5 v0.6.0 verification
- `python -m pytest` — **189 passed** (23 new tests: parsing `CallToolResult` for every
  response shape, a full protocol run over an in-memory FastMCP server including
  multi-item results and a tool error, wiring into `_alpha_search_blocks` through both the
  dict and the text branch, deduplication, and the quiet fallback on an exception).
- A live test against the real `mcp` package confirmed the exact shape of `CallToolResult`
  for tools returning a list (a separate JSON block per item plus `structuredContent` under
  the `result` key) — the parsing logic rests on this finding.
- A live resilience test: an invalid `ALPHAXIV_API_KEY` fails in 0.83 s without hanging.
- The server started and MCP `initialize` returned HTTP 200.

## 12. Fixes from analysing a real production run (v0.7.0)

A user ran a real query through Flowise (an English question about anomaly detection in
logs, 227 s, ~249k tokens) and sent the whole transcript including the raw MCP responses.
Analysing that run — including hypotheses verified live against Google Patents, Justia and
the Wayback Machine — uncovered six concrete problems.

### 12.1 Google Patents blocks automated requests (the most significant finding)
A live test (`httpx.get` against `patents.google.com/patent/...`) returned **HTTP 503**
with a body reading *"...your computer or network may be sending automated queries..."* —
the identical block the user's container was experiencing. The consequence: across all 4
patent evidence attempts, nearly every `patent_fetch` ended in a masked error
(`STATUS: FAILED`, `CLAIM1: No first claim was extracted`), even though the page had in
fact never been reachable. The patent number could still be extracted from the URL block,
so the error looked like an ordinary parsing failure.
As a result, `claim_coverage`/`DESCRIPTION_TEXT`/`exact_combination_candidate_found` from
v0.3.0–v0.5.0 **never ran at all** for patents — confirmed in the output too: no patent hit
showed "element coverage".

**Fix (`tools/patent_fetch.py`):**
- `_is_bot_block_page()` recognises the block page by its characteristic text.
- A new `BotBlockedError` class and an explicit `STATUS: BLOCKED` / `EVIDENCE_LEVEL: FETCH_BLOCKED`
  instead of the misleading "no claim/abstract" — `patent_evidence_pack.py` now writes a
  comprehensible sentence about the block into the warning, not truncated raw text.
- An `asyncio.Semaphore` capping concurrent requests to `patents.google.com` at 2 at a time
  across the whole run (previously up to 6 fetches against the same domain could start at
  once — very likely what triggers the block).
- A best-effort fallback to a Wayback Machine snapshot when a block is detected.
  **Honest admission:** the live test showed that Wayback coverage for a particular patent
  page is not reliable (in my test it returned 0 bytes) — it is a bonus when it works, not a
  guaranteed fix. Justia was tried and rejected: it is protected by a Cloudflare challenge
  (`"Just a moment..."`) and cannot be used without a full browser.
- Live verification of the fix: a real `patent_fetch` call against a genuinely blocked page
  now returns a clean `STATUS: BLOCKED` instead of the confusing `STATUS: FAILED`.

### 12.2 Jina Reader without an API key (403 on several publications)
`tools/jina_reader.py` supported no key at all; the free anonymous rate limit showed up in
the output as repeated `403 Forbidden` responses while verifying DOI links.
**Fix:** a `JINA_API_KEY` environment variable, sent as `Authorization: Bearer`.

### 12.3 web_evidence_pack lost the real cause of an error
Unlike `publication_evidence_pack.py`, which has `_errors_from_markers`,
`web_evidence_pack.py` never parsed the `ERROR: type - message` lines out of the
`web_search` output — on a provider failure the supervisor received only the generic
sentence "see server logs". **Fix:** the same parser was added; the `errors` field now
carries the real type and message, and the warning quotes the specific cause.

### 12.4 Query variant selection dropped an important element by position, not by meaning
In the real run the fourth and final patent attempt lost "processes events in real time" —
`_query_variants_from_atoms` in `tools/research_session.py` mechanically trimmed
`labels[len//2:]` regardless of importance. **Fix:** the remaining atoms are ordered so that
more specific categories (`function`, `mechanism_or_principle`, `constraint`) take
precedence over the general `object_or_form_factor` if trimming to the token limit is
needed — the core of the query is now kept intact instead of arbitrarily discarding half of
it by position.

### 12.5 Full-text PDF was not attempted for journal DOI links (only arXiv)
Most publication hits in the real run were `doi.org/10.3390/...` and similar journal
links, and the v0.5.0 PDF pipeline only activated for arXiv or direct `.pdf` URLs.
**Fix:** a new `_unpaywall_pdf_url()` function in `publication_evidence_pack.py` calls the
free Unpaywall API (no key, only a contact e-mail — the placeholder `*@example.com` is
rejected, hence a private `.local` domain) and finds a direct PDF link for open-access DOIs.
Verified live on three real DOIs from the production run — all three resolved to a working
PDF link (for example `mdpi.com/.../pdf?version=...`).

### 12.6 ACK responses duplicated content and inflated the token count
Alongside `compact_warnings`, the writer ACK also carried the full unedited `warnings`
field with the same, longer text including fragments of failed scraping (for example
`"...CLAIM1: No fir"`) — in mild conflict with the prompt instruction to "never inspect
fetched pages", and needlessly inflating the context (the run reached 249k tokens).
At the same time, `research_session_checklist` sent the same list of retry actions twice
under two keys (`retry_actions` and `recommended_next_actions`).
**Fix:** a new `_truncated_warnings()` function shortens each item's text to 240 characters
(the number of items is unchanged, so `warning_count` still holds); the duplicate
`recommended_next_actions` key was removed, and the internal `research_session_plan_next`
function now reads from `retry_actions`.

### 12.7 v0.7.0 verification
- `python -m pytest` — **217 passed** (21 new tests including a full simulation of the
  Google block and Wayback recovery, the semaphore concurrency cap, the Jina Bearer header,
  parsing errors from the web_search markers, Unpaywall resolution against both a mocked and
  a live-verified API shape, importance-driven query variant selection, warning truncation
  and the unification of retry_actions).
- Three independent live verifications straight against the internet **before** any code was
  written (the Google Patents block, the Justia Cloudflare block, Wayback availability) and
  one **after** the fix (a real `patent_fetch` call against a blocked page now returns a
  clean `STATUS: BLOCKED` instead of the misleading `STATUS: FAILED`).
- Live verification of Unpaywall against exactly the DOIs that appeared in the real run.
- The server started and MCP `initialize` returned HTTP 200.

## 13. Working around the block through the official patent PDFs (v0.8.0)

Version 0.7.0 made the Google Patents block **visible and honest**, but did not solve it —
the whole deep patent analysis (claims, description, `claim_coverage`,
`exact_combination_candidate_found`) still never ran, because the patent's content could
not be obtained. Version 0.8.0 addresses that.

### 13.1 Finding: the XHR response contains the path to the official PDF
Examining the raw response from `patents.google.com/xhr/query` — the endpoint already used
for searching — showed that every result carries a `pdf` field with a path on a **separate
Google storage bucket**:

```
"pdf": "30/12/f3/1b616ac6c5a32a/US10831585.pdf"
```

Live verification confirmed that `patentimages.storage.googleapis.com` is **not behind the
anti-automation protection** that blocks `patents.google.com`. The XHR endpoint also returns
metadata that was previously scraped from the HTML in vain (`assignee`, `filing_date`,
`grant_date`) — in the real run all of these were `Unknown`.

### 13.2 Implementation: a PDF-first strategy
- `tools/patent_search.py`: `PatentCandidate` extended with `pdf_url`, `assignee`,
  `filing_date`, `grant_date`; the XHR parser captures them and `_json_response` passes them on.
- `tools/patent_fetch.py`: `patent_fetch(url, timeout_ms, pdf_url="")` tries the **official
  PDF first** (the full text of both the claims and the description) and uses the HTML page
  only as a fallback. A new `_pdf_fields()` function builds the output from the PDF text.
- `tools/patent_evidence_pack.py`: passes `pdf_url` through and counts `COVERAGE_TOKENS`
  towards the `claim_coverage` computation.

### 13.3 An honest limitation: two-column typesetting
Patent PDFs are typeset in two columns, and extraction interleaves the lines of both
(verified on a real document — claim 1 came out mixed with the text of the adjacent column).
I also tested `pypdf`'s layout mode; it does preserve positions, but line widths are so
inconsistent that detecting the column boundary would be fragile.

The chosen solution therefore **does not pretend** that it can quote a claim verbatim:
- the full text goes into `COVERAGE_TOKENS` and serves to **verify requirement coverage**,
  which is about term occurrence — column interleaving is entirely harmless there,
- the `CLAIM1` and `ABSTRACT` fields deliberately carry the standard "not found" sentinels,
  so the existing filters exclude them from both the displayed summary and the coverage
  computation (otherwise meta sentences about the PDF would cause false matches on words
  such as "claims" or "layout"),
- a clean snippet and the XHR metadata are still used for readable display.

### 13.4 Measured effect (live, the same kind of query as in the production run)
| Metric | before (v0.7.0) | after (v0.8.0) |
|---|---|---|
| Patent evidence level | 5× `search_snippet_only` | 3× `claim_verified`, 1× `abstract_verified` |
| `claim_coverage` | always 0 (never ran) | 0.25 / **1.0** / 0.5 |
| `exact_combination_candidate_found` | never | **yes** (US10762444B2) |
| Text extracted per patent | 0 words | 7,706 words (US10831585B2) |
| Time for one fetch | 15,198 ms (blocked) | **737 ms** |

A ~20× speedup on the operation that dominated total time in the production run (227 s) is
a side effect, but a significant one.

### 13.5 v0.8.0 verification
- `python -m pytest` — **229 passed** (12 new tests: claims section detection, coverage
  token emission, preferring the PDF over the HTML, three fallback scenarios (an empty PDF,
  an exception, text that is too short), capturing the `pdf` path and metadata from the XHR
  response, passing `pdf_url` through the evidence pack, and a dedicated regression test
  verifying that meta sentences about the PDF never contaminate coverage or the summary).
- Live verifications: availability of the storage bucket, the full `patent_fetch` chain on
  a patent that had failed in the production run, and a complete `patent_evidence_pack` run
  with atomic requirements.
- Three existing test stubs had to be updated to the new `patent_fetch` signature — a
  deliberate API change, not a regression.

## 14. Measurable improvement in relevance scoring (v0.9.0)

Analysis of the real run showed that of ten publications returned, only two were on topic.
Rather than guessing, I first built the **measurement apparatus**, then fixed the causes and
measured the changes — which also addresses the "extend testing: more scenarios, repeated
runs and measurable metrics" point from the defence.

### 14.1 The evaluation harness — `eval/`
- `eval/dataset.json` — a gold-standard dataset with three scenarios. The candidates of the
  `anomaly_logs_real_run` scenario are **taken verbatim from a real run** of the system in
  Flowise, so the measurement is not on synthetic data. The other two scenarios correspond
  to the demonstration examples from the defence and deliberately contain documents that
  share generic vocabulary but belong elsewhere.
- `eval/relevance_eval.py` — computes precision, recall, F1, P@k, MAP and MRR. It runs
  offline over the stored candidates, so results reproduce exactly and do not depend on
  whatever the external providers happen to return.

To run: `python -m eval.relevance_eval` (the `--no-idf` switch disables IDF weighting for
a direct comparison; see section 14.4 for what it does and does not revert).

### 14.2 Measured cause: a faulty stemmer
```
logs         -> logs        log          -> log            never match
application  -> applicate   applications -> application    never match
```
The guard condition `len(token) <= 4` left "logs" untouched, and the derivational rule
`ation → ate` was applied before plural removal. A document about "log analysis" therefore
earned **zero credit** against a query mentioning "logs" — the defect affected every query.
Fix: plurals are stripped first, the guard length was lowered to 3, and an exception was
added for words ending in "ss" (so that "process" does not end up as "proces").

### 14.3 Measured cause: every term carried the same weight
A query about anomalies in logs shares generic vocabulary (`machine`, `learning`, `real`,
`time`, `detection`, `anomaly`) with seismology, the ionosphere and a firefighting robot
alike. With equal weights that was enough to clear the threshold — the robotic firefighting
system even scored **higher (3.95)** than both genuinely relevant papers about logs (3.53
and 3.01).

The solution: `build_corpus_idf()` weights terms by their rarity **within the candidate set
just retrieved**. This is deliberately not a fixed word list — the system stays free of
built-in domain vocabularies, in line with the original design, and adapts to any topic.
Terms present in nearly every candidate lose weight; rare domain terms (`logs`, `incident`,
`administrator`) gain it. A domain anchor, `salient_query_tokens()`, was added as well.

Because each provider filters on its own, and at that moment it is not yet known which
terms discriminate, the weighting runs as a **second pass over the merged set** of
candidates (`_rerank_with_corpus_idf`) — with a safeguard so that stricter filtering can
never empty a source entirely. The same mechanism is wired into web search and into patent
ranking.

### 14.4 Measured result — the offline dataset

> **Correction (v0.9.5).** The table originally published here reported precision
> 0.486 → 0.667 and F1 0.600 → 0.733, a +22 % F1 gain. Those numbers were measured
> at threshold **3.5**, which the harness hardcoded but which **no source type
> actually uses** — the server runs at 3.0 (patent, publication) and 2.8 (web).
> The figures below are measured at the production thresholds. The gain is real
> but much smaller than published, and it costs recall.

Measured at the production threshold for the dataset's source type (publication,
3.0), after anchor selection was made deterministic (section 14.8):

| Metric | v1.0-thesis | current | change |
|---|---|---|---|
| Precision | 0.519 | **0.611** | +18 % |
| Recall | 1.000 | **0.833** | **−17 %** |
| F1 | 0.655 | **0.683** | +4 % |

**The recall regression is the important line.** The v1.0-thesis scorer accepted
every relevant document in the dataset; the current one drops one. IDF weighting
buys precision by discarding candidates, and on this dataset one of the discarded
candidates is relevant. Whether that trade is worth it cannot be decided here —
see the limitation below.

#### Threshold sweep

A single metric at a single threshold hides that trade-off entirely. Reproduce
with `python -m eval.relevance_eval --sweep`:

| prah | v1.0 P | v1.0 R | v1.0 F1 | teraz P | teraz R | teraz F1 | |
|---|---|---|---|---|---|---|---|
| 2.50 | 0.374 | 1.000 | 0.534 | 0.651 | 1.000 | 0.748 | |
| 2.80 | 0.463 | 1.000 | 0.610 | 0.611 | 0.833 | 0.683 | ← produkcia (web) |
| 3.00 | 0.519 | 1.000 | 0.655 | 0.611 | 0.833 | 0.683 | ← produkcia (patent, publication) |
| 3.20 | 0.486 | 0.833 | 0.600 | 0.611 | 0.833 | 0.683 | |
| 3.50 | 0.486 | 0.833 | 0.600 | 0.667 | 0.833 | 0.733 | ← originally published |
| 4.00 | 0.556 | 0.667 | 0.600 | 0.556 | 0.667 | 0.600 | |
| 4.50 | 0.556 | 0.667 | 0.600 | 0.556 | 0.500 | 0.489 | |

The published +22 % was the single best cell in this table. At 4.00 the two
scorers are identical; at 4.50 the current one is **worse**.

#### Reproducing the original scorer (v1.0-thesis)

`--no-idf` disables IDF weighting but **keeps the stemmer fix**, so it is not the
original scorer — it produces a third set of numbers (0.431 / 0.556 at 3.5)
matching neither column above. The help text used to claim otherwise; it has been
corrected. To measure the true v1.0-thesis behaviour, inject the historical scorer
through the `score_fn` / `accept_fn` parameters the harness already exposes:

```bash
git show v1.0-thesis:tools/relevance.py > /tmp/relevance_v1.py
sed -i 's/^from \.result_contract/from tools.result_contract/' /tmp/relevance_v1.py
```

```python
import sys; sys.path.insert(0, "/tmp")
import relevance_v1 as old
from eval.relevance_eval import evaluate_dataset

_results, summary = evaluate_dataset(
    score_fn=old.evidence_score, accept_fn=old.is_relevant, use_idf=False
)
```

#### Why these numbers are weak evidence

The dataset is **3 queries, 20 candidates, 6 relevant documents**, and only one
query (`anomaly_logs_real_run`) comes from a real run — the other two were
constructed by the author, which means the negatives were chosen by the same
person who wrote the scorer. A difference of 0.028 in F1 across 3 queries is not
a measurable improvement; it is one document changing side. **No claim of the
form "+X % better" is supportable at this dataset size**, and the numbers above
should be read as a smoke test that the component is not broken, not as evidence
that it is good. Expanding the dataset is the prerequisite for any further tuning
work, and production thresholds are deliberately left unchanged until then.

The real-run scenario on its own: precision 0.12 → **0.33**, F1 0.20 → **0.40**
(candidates accepted 8 → 3, of which relevant is still 1 of 2).

### 14.5 Measured result — a live run on the original query
| | original run | after the change |
|---|---|---|
| Seismology, ionosphere, GNSS, firefighting robot, social networks | 5 results | **0** |
| Papers directly about logs | 2 | **3** |
| Candidates filtered out | – | **19** |

A new hit that had previously not reached the results: *"Detecting Anomalies in Logs by
Combining NLP features with Embedding or TF-IDF"*. The remaining results shifted from
entirely foreign domains to an adjacent security one (IDS, threat hunting).

### 14.6 An honestly acknowledged remaining limitation
The **P@2 metric in the real-run scenario remains 0.00**. The reason is fundamental: a
document about anomaly detection in *social networks* contains almost the entire vocabulary
of the query (`anomalies`, `patterns`, `machine learning`, `real-time`, `automated incident
response`) and differs from a query about *application logs* in its subject, not in its
words. **No purely lexical method can separate these reliably** — semantic understanding
(vector representations) is required. The harness is ready for that: it only needs an
additional scoring function, measured the same way.

### 14.7 v0.9.0 verification
- `python -m pytest` — **243 passed** (14 new tests: singular/plural matching, protection
  for words ending in "ss", the computation and effect of IDF, the domain anchor, a dataset
  integrity check, and two **quality regression tests** that fail if precision drops below
  the measured level or if IDF stops outperforming the original equal weights).
- A live run of publication search on the original query (table 14.5).

### 14.8 Nondeterminism in domain anchor selection (v0.9.5)

Aligning the harness with the production thresholds immediately exposed a latent
bug that the wrong threshold had been hiding: **the relevance filter was not
deterministic**. Forty identical runs of `python -m eval.relevance_eval` in
separate processes produced three different results:

```
29 runs   precision 0.611   recall 0.833   F1 0.683
11 runs   precision 0.556   recall 0.833   F1 0.639
 (rarer)  precision 0.500   recall 0.667   F1 0.550   <- a relevant document rejected
```

Cause — `tools/relevance.py`, `salient_query_tokens`:

```python
candidates = discriminative_tokens(query) or tokens(query)   # a set
ranked = sorted(candidates, key=lambda token: idf.get(token, _DEFAULT_IDF_WEIGHT), reverse=True)
return set(ranked[: max(1, top_n)])
```

`sorted` is stable, so tokens with equal IDF keep their input order — the
iteration order of a **set**, which depends on Python's per-process string hash
randomisation. IDF ties are common by construction: any two tokens appearing in
the same number of candidate documents get exactly the same weight. The first
dataset query alone produces five distinct tie groups among 17 candidate tokens.
When a tie group straddles the `top_n = 6` cut, which anchors survive changes
between runs, and `is_relevant` rejects any document sharing none of them.

This was a **production** defect, not only an evaluation one: the same query
submitted to the running server could return different documents after a restart.

**Oprava:** a deterministic secondary sort key.

```python
ranked = sorted(candidates, key=lambda token: (-idf.get(token, _DEFAULT_IDF_WEIGHT), token))
```

The token itself carries no semantic meaning as a tie-break; it only has to be
stable. `tools/patent_search.py` already used the same pattern
(`key=lambda item: (-item.score, item.patent_number)`).

Deliberately **not** done: widening `top_n` to keep every token tied at the cut
boundary. That is arguably more principled — splitting two equally salient tokens
is unjustifiable — but it changes filter semantics, and with 20 candidates there
is no way to measure whether it helps. Deferred until the dataset is larger.

Why the bug survived this long: at threshold 3.5 the acceptance decisions happen
to land identically regardless of which anchors are chosen, so the old regression
guard passed on every run. Only at the production thresholds do the outcomes
diverge. **The wrong measurement configuration was masking the defect.**

### 14.9 Verification v0.9.5

- `python -m pytest` — **276 passed**, six consecutive runs, no flakes. Before the
  fix the re-pinned guard failed 2 runs in 6.
- 25 independent processes of `python -m eval.relevance_eval` now return an
  identical result; `--sweep` output is byte-identical across 5 processes
  (verified by `md5sum`).
- Anchor selection is identical under `PYTHONHASHSEED` values 0, 1, 7, 42, 12345.
- New tests: deterministic tie-break on an all-tied candidate set; anchor
  stability under 20 shuffles of the query word order; and a reproducibility test
  that runs the evaluation in **subprocesses**, since `PYTHONHASHSEED` is fixed
  for the life of a process and a single-process loop cannot detect this bug.
- The harness now imports `THRESHOLDS` from `tools.relevance` instead of keeping
  its own constant, and a drift test asserts the two cannot disagree again.

## 15. Text cleanliness in the final report (v0.9.1)

Re-reading the report from the real run showed that clutter with no evidential value was
reaching the output and spoiling the impression the result made. Checking the code confirmed
that **none of these cases was being cleaned**.

| Defect in the report | Example from the real run |
|---|---|
| Undecoded HTML entities | `Log Intelligence &amp; SIEM Platform` |
| Markdown header from the README reader | `# Repository: PhilipLykov/LogPulseAI` |
| Repository metadata | `- Stars: 0 - Forks: 0 - Watchers:` |
| Navigation and marketing calls to action | `is available now. Click here to check it out.` |
| Doubled phrase from the page title | `Rootly \| Rootly Anomaly Scoring Engine` |
| Repeated word | `Product Product` |
| Title repeated at the start of the summary | the hit listed twice in a row |

### 15.1 The solution
Four reusable functions were added to `tools/output_cleaner.py`:
`unescape_entities()` (which also decodes multiply-encoded `&amp;amp;`),
`collapse_repeats()`, `strip_boilerplate()` and `strip_leading_title()`.
They are wired into the rendering layer `tools/user_answer.py`, so they apply regardless of
which source the text came from, and they **do not modify stored evidence** — only its
presentation.

### 15.2 Summaries with no evidential value
Once the clutter is stripped, what remains of a page can be readable yet unrelated to the
query (in the real run, for instance, *"Top People Making the World More Reliable"*). Such a
summary is worse than none in the report, so it is displayed only if it contains at least
one term from the query. The hit itself, including its URL and evidence level, remains
displayed.

### 15.3 v0.9.1 verification
`python -m pytest` — **258 passed** (15 new tests, two of which work directly with verbatim
strings from the real report, and one of which verifies that a hit with a contentless
summary is displayed while the summary itself is not).

## 16. Patent result quality when the provider is blocked (v0.9.2)

The first complete run of the system outside Flowise (calling the MCP tools directly, with
no API keys at all) produced three patents in the output titled *"Method, system and
computer program for comparing images"* — image comparison, as the answer to a query about
anomaly detection in logs.

### 16.1 Cause: a double failure
The key-free Google Patents provider from v0.4.0 returned **HTTP 503 for every query** in
this run — the same anti-automation protection that section 12 addresses. The provider is
therefore not broken, but neither is it reliable: sometimes it gets through, sometimes it is
blocked. Search consequently fell back to scraping WIPO PATENTSCOPE, which exposed two
separate defects.

### 16.2 Classification clutter treated as evidence text
The **whole result table row** was being sent as the "snippet", including the full
international patent classification listing:

```
Int.Class G06K 9/00 G PHYSICS 06 COMPUTING; CALCULATING OR COUNTING K GRAPHICAL
DATA READING; ... 9 Methods or arrangements for recognising patterns
```

The table row contains no abstract at all, so this text was not evidence but metadata. It
also contains generic technical words (*recognising patterns*, *computing*, *data*) which
caused **false matches against almost any technical query** — precisely how image comparison
attached itself to a query about logs.

**Fix:** `_wipo_snippet()` strips the classification, the form fields, the row's sequence
number and the date. If fewer than five substantive words survive the cleanup, no snippet is
produced at all and relevance is judged on the patent title alone.

### 16.3 Fallback mode returned anything at all
When no candidate cleared the relevance threshold, `_rank(allow_low_confidence=True)`
returned **every** candidate, including those scoring 0.0 — in the real run, for example,
*"PHOTOELECTRIC CONVERSION DEVICE"*.

**Fix:** fallback mode now has a lower bound too, `MIN_RELEVANCE_FLOOR`. Better to return
nothing and mark the state honestly as incomplete than to have the report carry obviously
unrelated patents.

### 16.4 v0.9.2 verification
- `python -m pytest` — **266 passed** (8 new tests built on a verbatim row from the real
  run: stripping the classification, preserving substantive content, the score dropping
  against an unrelated query, and the lower bound in fallback mode including the case where
  returning nothing is correct).
- Verified against real data: the cleaned snippet's score against the logs query fell.

## 17. Patent hit quality verified by a full run (v0.9.4)

The first complete run of the system outside Flowise exposed three further defects, which
only manifest once the main patent provider is blocked.

### 17.1 Patents had no domain anchor
The publication branch requires a hit to share a subject term with the query; the patent
branch had no such condition. A patent about **image comparison** therefore reached a score
of **3.58** against a query about anomalies in logs and cleared even the main threshold of
2.8, because nothing but generic technical vocabulary (*method*, *system*, *device*)
connected them.

The term-rarity weighting from v0.9.0 did not catch this, for a structural reason: all the
candidates came from **one patent family**, so every term had the same document frequency
and IDF had nothing to distinguish.

**Fix:** `_shares_discriminative_term()` is applied to every candidate in every scoring
branch. After the change, genuinely related patents took the leading positions (static code
analysis, anomaly detection in network operations).

### 17.2 A failed verification erased evidence already obtained
When `patent_fetch` failed or was blocked, the evidence level was overwritten to
`fetch_failed` and rendering hid such a hit. The outcome was inverted: patents the system
**had tried to verify disappeared from the report**, while a candidate outside the
verification limit remained in it.

**Fix:** if a candidate carries a usable search snippet, it falls back to
`search_snippet_only` after a failed verification instead of the failed level. The
publication branch already behaved this way.

### 17.3 One application filled the whole section
Deduplication compared only the publication number, so the same invention filed under four
numbers (national, international, continuations) filled four of the six slots.
**Fix:** deduplication by normalised title as well.

### 17.4 v0.9.4 verification
- `python -m pytest` — **270 passed** (4 new tests: the anchor in the main branch,
  preserving genuinely related patents, merging a family, and keeping distinct short
  titles).
- The whole workflow was re-run after each fix; the resulting report is stored as
  `docs/example-report.md`.

## 18. Audit of iteration-order dependencies (v0.9.6)

After the v0.9.5 nondeterminism fix, the whole codebase was audited for other
places where the iteration order of an unordered container could reach
externally observable behaviour. **Result: no further correctness-affecting
dependency was found.** This section records what was checked and how, so the
negative result is verifiable rather than asserted.

### 18.1 Method

*Static.* An AST pass over `tools/*.py` and `server*.py` — not grep, which misses
comprehensions and chained calls — flagged 379 candidate sites across `sorted`,
`min`/`max`, `next`, `join`, and slicing. Each was then traced by hand to
determine whether an unordered value can actually reach it.

*Dynamic.* Two workloads were digested and re-run in a fresh interpreter per
`PYTHONHASHSEED`, because the seed is fixed for the life of a process and a
single-process loop cannot detect this class of bug at all.

**The first dynamic probe was wrong, and that matters.** A probe driving the full
workflow through `research_session_save_evidence` produced identical output
across 12 seeds — but so did it with the v0.9.5 bug deliberately reintroduced.
Injecting pre-built evidence packs bypasses retrieval and filtering entirely, so
the probe never reached the code the bug lived in. A second probe targeting the
filtering path directly was then validated the same way, and with the bug
reintroduced it produced **12 different digests for 12 seeds**.

Both probes are now permanent tests (`tests/test_determinism.py`). Every
differential check of this kind must be validated against a known bug before its
passing result means anything.

### 18.2 What was found

| Site | Verdict |
|---|---|
| `relevance.py` `salient_query_tokens` | Fixed in v0.9.5. |
| `patent_search.py:482` `sorted(ranked, key=(-score, patent_number))` | Safe — explicit unique tie-break. |
| `patent_search.py` `_dedupe` | Safe — sets used for membership tests only; output order follows the input list. |
| `_hit_sort.py` `sort_hits_by_relevance` | Tie-prone `(rank, score)` key, but input is a list and the sort is stable. Deterministic. |
| `evidence_quality.py:107` `scored_hits[0]` | Tie-prone — decides which hit is "top" and reaches the output. Input is a list, so deterministic. |
| `publications_search.py:757` `rejected.sort(key=score)` | Tie-prone — decides which rejected candidates are restored. Input is a list. Deterministic. |
| `web_search.py:728` `sorted(merged_results.values(), key=(-score, _rank_domain(url)))` | `_rank_domain` returns `(int, domain)`, so ties survive only for equal score *and* equal domain, falling back to dict insertion order. Deterministic, but the narrowest margin in the codebase. |
| `query_expansion.py:63` `sorted(_MAPPING, key=-len)` | Heavy ties, but `_MAPPING` is built from a tuple and a dict literal with `tuple(sorted(...))` values, so insertion order is deterministic. |
| `research_session.py:905` `sorted(other_atoms, key=binary)` | Binary key, near-total ties, list input, stable sort. Deterministic. |
| 9 keyless `sorted()` calls over sets | Safe by construction — alphabetical or numeric order. |
| SQL view `deduped_best_evidence_view` | Safe — `ORDER BY verified_url DESC, relevance_score DESC, id ASC` ends in a unique column. |

No occurrence of `list(set(...))` or `tuple(set(...))` exists anywhere in the
codebase, and no `next(iter(...))` over an unordered container.

The recurring reason nothing else broke is that this codebase already sorts sets
*without* a key in the places it converts them to output, which yields
alphabetical order. `salient_query_tokens` was the outlier precisely because it
needed a ranking key, and that is where the secondary key was forgotten.

### 18.3 Deliberately left unchanged

`web_search.py:728` and `evidence_quality.py:107` are deterministic today but rely
on the caller passing an ordered container. Adding a final unique tie-break
(`url`, `canonical_id`) would make them robust rather than merely correct — but
it would change the current output ordering, which is a behavioural change and
was therefore not made as part of an audit. Recorded here as a candidate.

**Post-audit note (v0.9.8).** Both candidates were subsequently hardened with
explicit stable tie-breaks: `url` for merged web results, and an identity tuple
for `top_hit` selection. Section 18.2 above remains the historical v0.9.6 audit
record and is deliberately not rewritten; see CHANGELOG 0.9.8 for those changes.

### 18.4 Verification

- `python -m pytest` — **278 passed**, unchanged across `PYTHONHASHSEED` values
  0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144.
- Both digests identical across 20 seeds.
- Negative control: reverting the v0.9.5 fix makes
  `test_filtering_path_is_hash_order_independent` fail with 8 distinct digests.
- The guard costs about 9 s (16 subprocess spawns); the suite runs in ~13 s.

## 19. Ideas for further improvement (unscheduled)

- Reuse a single Playwright browser instance instead of starting a new one for every fetch.
- A persistent (SQLite) cache for patent_fetch across container restarts.
- Extending `_MDPI_ISSN_TO_CODE`, or generic DOI resolution through Crossref, for more publishers.
- Per-provider latency measurement and metrics export (a Prometheus endpoint, for instance) for the evaluation chapter.
- Text extraction from DOCX/PPTX attachments, as is already done for PDF.
- The Espacenet OPS API (free with registration) as another structured patent source.
- **Semantic reranking with vector representations** — the only way to separate documents
  with near-identical vocabulary but a different subject (the limitation in section 14.6).
  Measurable with the existing harness.
- If the Google Patents block recurs even after lowering concurrency, consider Playwright
  with a persistent browser context (cookies/fingerprint) instead of a fresh anonymous
  session for every fetch — blocks tend to be sensitive to fingerprint "freshness", not just
  to request volume.
- Use the other AlphaXiv MCP tools (`get_paper_content`, `answer_pdf_queries`) for deep
  analysis of specific AlphaXiv/arXiv articles beyond `discover_papers`.
