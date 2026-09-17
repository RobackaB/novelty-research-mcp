# 0.10.0 portfolio milestone

This is a post-thesis engineering milestone, not a production-readiness or
research-effectiveness claim. The submitted prototype remains at `v1.0-thesis`.
The release changes documentation, packaging version and local-demo hygiene;
production retrieval/scoring, MCP interfaces, the historical dataset/evaluator
and Goal 5C implementation remain unchanged from the reviewed 0.9.8 baseline.

## Evidence and stopping point

- The historical benchmark has **3 queries / 20 candidates**. It measures the
  generic scorer on a fixed candidate pool: query-macro precision **0.611**,
  recall **0.833**, F1 **0.683**. It does not replay production retrieval,
  source-specific gates, retries or global retrieval recall.
- Goal 5B adds passive, fail-open decision capture and structural provenance.
- Goal 5C Phases 1–3 provide synthetic snapshot import, reconciliation, blank
  blinded worksheets and original-query roster-freeze rehearsal. Technical
  artifacts do not authenticate preregistration timing or authorize a real study.
- The real pilot, 25–30 genuine original queries, human labelling/adjudication,
  workflow-observed metrics and prospective threshold confirmation are deferred.
  Phase 4 has not started; privacy/recruitment gates still apply.

## Verification

Release checks on Windows, 2026-09-17:

| Check | Result |
|---|---|
| Working checkout, Python 3.14.6, `PYTHONHASHSEED=1` | 954 tests passed. |
| Fresh clone, Python 3.11.15, new venv and editable dev install | Installation and `pip check` passed; after the test-only correction below, 954 tests passed with `PYTHONHASHSEED=97`. |
| Relevance evaluation | Precision 0.611, recall 0.833, F1 0.683; unchanged. |
| MCP stdio and Streamable HTTP on localhost | Both initialized and listed exactly the seven expected tools; no retrieval/model calls. |
| Goal 5C CLI | Top-level help and all five subcommand help paths passed; synthetic round trips and determinism/negative controls passed within the full suite. |
| Focused cache/sorting tests | Six passed; expiry-disabled negative control failed as expected. |
| Documentation/scope checks | Local link targets resolve; historical changelog entries and example report body preserved; protected implementation files unchanged. |
| Compose | `docker compose --env-file .env.example config --quiet` passed. |
| Docker build/start and interactive Flowise import | Blocked by unavailable Docker Desktop Linux engine; not verified. |

The initial fresh clone tested candidate `ad3beaa6cd0715c7466c1a359ce51c877297e681`.
It exposed a pre-existing test assumption: zero-TTL set/get calls can read the same
clock tick on Windows Python 3.11 (953 passed, one failed). The expiry test now
advances a controlled clock and pins the existing strict-greater-than boundary;
the cache implementation is unchanged. The original failure was reproduced with
a same-tick clock, and disabling expiry makes the revised test fail. No test was
skipped or weakened. Candidate `37b6ecc2d1fb8f0436fca638a609f6cf59508be3`
passed all 954 tests in both environments (96.75 seconds on 3.14.6, 104.90 seconds
on 3.11.15). Only this verification record changed afterward.

The fresh environment
resolved MCP SDK 1.30.0, pytest 9.1.1 and pytest-asyncio 1.4.0. No `.env` file,
provider credentials or browser installation were needed. The MCP smoke used
temporary storage and shut down its own server process.

Reproduce the offline path from the repository root:

```bash
python -m venv .venv
# Activate .venv for your shell.
python -m pip install -e ".[dev]"
python -m pip check
python -m pytest -q
python -m eval.relevance_eval
python -m eval.goal5c --help
python -m eval.goal5c import-snapshot --help
python -m eval.goal5c prepare-synthetic --help
python -m eval.goal5c freeze-synthetic --help
```

These checks need internet for dependency installation, but no API credentials,
Docker, browser download or model account. CLI inputs and successful synthetic
round trips are exercised by the Goal 5C tests. `eval` is checkout tooling, not
part of the runtime wheel/image. Dependencies are range-constrained rather than
fully locked; a fresh install verifies one dependency resolution, not all future
resolutions.

### Local demo limits

Compose pins **Flowise 3.1.4** instead of `latest`. Its official release and
Linux image manifest were checked; runtime compatibility remains **unverified**
because the local Docker Desktop Linux engine is unavailable. Compose validation
passes, but no clean image build, container startup or interactive Flowise import
is claimed. Complete these checks on a Docker-enabled host before describing
this version as runtime-tested.

The exported `agentFlow` nodes (`startAgentflow` and `agentAgentflow`) target
Agentflow V2. The versioned UI source supports importing JSON through the canvas
settings. This is source inspection, not an executed browser smoke:
[Flowise 3.1.4 source](https://github.com/FlowiseAI/Flowise/tree/flowise%403.1.4/packages/ui/src/views/agentflowsv2).
After importing, select your own model credential and set the Custom MCP URL to
`http://mcp-research-server:8000/mcp` for Compose networking. The historical export
is preserved. Account setup, model access and a live research run are unverified.

On a suitable host, use `.env.example` as the credential-free starting point,
run `docker compose build --no-cache`, then `docker compose up -d`. Check health,
initialize an MCP client at `/mcp`, list the seven tools, and import the workflow
as described in the README. A model credential is required for a live Flowise run.
Do not equate a TCP health check with a successful MCP handshake.

Both published ports bind to loopback. This is a trusted local demo; MCP has no
public-service user authentication. The build-context allowlist admits only
runtime Python files and build inputs, excluding credentials, databases, caches,
Git history and generated study artifacts. No infrastructure redesign is implied.

## GitHub presentation recommendations

These are drafts only; repository settings, tags and releases are not changed by
this PR. Create a tag/release only after external review, merge and green main CI.

**About:** Python MCP backend for source-grounded research across patents,
publications and web sources, with deterministic scoring, SQLite provenance and
reproducible offline evaluation tooling.

**Topics:** `python`, `mcp`, `information-retrieval`, `sqlite`, `flowise`,
`evaluation`, `research-software`.

**Proposed tag:** `v0.10.0`.

**Release title:** 0.10.0 — Portfolio engineering milestone

### Release notes draft

Post-thesis engineering milestone for a Python MCP research backend:

- Passive candidate-decision capture with query/decomposition provenance and
  fail-open persistence.
- Synthetic Goal 5C Phases 1–3: read-only snapshot import, explicit identity
  reconciliation, blank blinded worksheets and deterministic roster rehearsal.
- Deterministic regression/negative-control coverage and credential-safe provider
  diagnostics in returned and persisted evidence.
- Clearer English/Slovak documentation, offline reviewer setup and scoped Docker
  hygiene. Flowise 3.1.4 is pinned; Docker/Flowise runtime smoke remains outstanding.

The historical fixed-pool benchmark remains at precision 0.611, recall 0.833 and
F1 0.683 on 3 queries / 20 candidates; it does not establish whole-workflow
effectiveness. Real acquisition, human labelling, query-level study measurements
and prospective threshold confirmation remain future work. See the verification
record above for tested environments and limitations.
