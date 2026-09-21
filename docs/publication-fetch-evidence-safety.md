# Publication fetch evidence contract

Publication discovery and detail verification are separate. A discovered paper
remains available when its publisher page cannot supply a verified abstract.

- `abstract_verified`: substantive text extracted from explicit abstract markup
  (`citation_abstract`, an abstract section/div, or an element with ID `abstract`).
  Placeholder, access-wall and navigation text do not qualify. The conservative
  content guard requires at least 12 alphabetic words and eight distinct words.
- `verified_metadata`: a retrieved title or DOI without an identifiable abstract.
- `fetched_excerpt`: useful retrieved description or Reader text without proof
  of abstract structure, including short abstract text that does not pass the
  stricter verification guard. Excerpts require six distinct alphabetic words
  and the same placeholder/access-wall guard. The shared Reader cleaner removes headings, so even a
  Reader response that mentions “Abstract” is conservatively kept at this level.
- `fetch_failed`: no usable fetched evidence. Existing discovery evidence remains
  available under the pack's existing fallback rules.

Generic description metadata is never sufficient for abstract verification.
The pack preserves weaker levels and fetched content, and does not label empty
or `Unknown` abstract fields as fetched abstracts. Short genuine abstracts and
unsupported publisher markup may be conservatively under-classified; no semantic
or publication-identity proof is claimed from these syntactic guards alone.

Publication fetch, pack and writer exception boundaries use the existing
`provider_error_message` helper rather than exception text. Existing text
redaction utilities (currently housed in `patent_safety`) are reused without
changing patent code, including for authenticated URLs and configured secrets.
Offline regressions check returned fetch text, pack results, writer acknowledgements,
SQLite text structures and captured logs with synthetic credential sentinels.

This change does not resolve the separate checklist/reporting issue where strong
surviving evidence can conceal incomplete retrieval. Retry logic, thresholds,
the requirement matrix and public MCP interfaces remain outside this change.
