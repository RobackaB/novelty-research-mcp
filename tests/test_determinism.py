"""Guard: externally observable output must not depend on iteration order.

Background. v0.9.5 fixed a bug where `salient_query_tokens` ranked a *set* of
tokens by IDF alone. `sorted` is stable, so tied tokens kept set-iteration order,
which depends on Python's per-process string hash seed. Whenever a tie group
straddled the `top_n` cut, different anchors survived on different runs and the
same query could accept different documents.

This module is the standing guard against that class of bug returning anywhere.

Two properties make it work, and both are load-bearing:

1. **Subprocesses.** `PYTHONHASHSEED` is fixed for the lifetime of an interpreter,
   so a loop inside one process cannot detect hash-order dependence at all. Each
   sample must be a fresh interpreter with a different seed.

2. **Tie-heavy fixtures.** A workload whose scores are all distinct never reaches
   a tie-break and would pass against broken code. The corpus below is built so
   the query's discriminative tokens land in IDF tie groups that straddle the
   anchor cut, and so candidate hits share score, evidence level and relevance.

Validated by negative control: with the v0.9.5 fix reverted, `_filtering_digest`
produces a different result for all 12 seeds tried. See AUDIT.md section 18.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SEEDS = ("0", "1", "2", "7", "13", "42", "1337", "65535")

# Every distinctive term appears in exactly one document, so the terms share one
# IDF weight and the discriminative set becomes a single tie group.
CORPUS_QUERY = (
    "detecting anomalies in application logs processing events in real time using "
    "machine learning to recognise unusual patterns creating an incident and "
    "notifying an administrator"
)

CORPUS = [
    "Anomaly detection in application logs using workflow patterns from distributed software systems.",
    "Deep learning approach for detecting anomalies in distributed system logs and event streams.",
    "Real-time classification of seismic events using supervised machine learning methods.",
    "Machine learning models detecting anomalies in ionosphere measurements over time.",
    "Automated incident response and threat intelligence for social media risk management.",
    "Predictive maintenance of industrial motors using vibration analysis and machine learning.",
    "Unusual pattern recognition in network traffic for intrusion detection systems.",
    "An administrator notification framework for automated operational alerting.",
]


def _filtering_digest() -> str:
    """Digest the retrieval-side path: anchor choice, acceptance, merged rerank."""
    from tools.publications_search import _rerank_with_corpus_idf
    from tools.relevance import build_corpus_idf, is_relevant, salient_query_tokens

    idf = build_corpus_idf(CORPUS)
    parts: list[str] = []
    for top_n in (2, 3, 4, 5, 6, 7):
        parts.append(f"anchors[{top_n}]={sorted(salient_query_tokens(CORPUS_QUERY, idf, top_n=top_n))}")
    for doc in CORPUS:
        parts.append(f"{is_relevant(CORPUS_QUERY, doc, 'PUBLICATION', idf=idf)!r}::{doc[:40]}")
    kept, dropped = _rerank_with_corpus_idf([(0.0, doc) for doc in CORPUS], CORPUS_QUERY)
    parts.append(f"dropped={dropped}")
    parts.extend(f"kept {score:.4f}::{block[:40]}" for score, block in kept)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _pipeline_digest() -> str:
    """Digest the storage -> checklist -> rendered report path.

    Hits within a source deliberately share relevance_score, evidence_level and
    relevance, so any ordering decision that falls through to iteration order
    changes the rendered report.
    """
    os.environ["RESEARCH_SESSION_DB"] = os.path.join(tempfile.mkdtemp(), "determinism.sqlite3")
    import tools.research_session as rs

    def hits(prefix: str, score: float, level: str, relevance: str, url_fmt: str) -> list[dict]:
        return [
            {
                "title": f"{prefix} result {i}",
                "url": url_fmt.format(i=i),
                "canonical_id": f"{prefix}-{i}",
                "evidence_level": level,
                "summary": f"Smart door lock with mobile application control, {prefix} {i}.",
                "verified_url": True,
                "relevance": relevance,
                "relevance_score": score,
            }
            for i in range(1, 5)
        ]

    patent_hits = hits("patent", 7.5, "claim_verified", "focused",
                       "https://patents.google.com/patent/US{i}111111B2/en")
    for i, hit in enumerate(patent_hits, start=1):
        hit["patent_number"] = f"US{i}111111B2"

    query = "smart door lock controlled by a mobile application with access codes"
    session_id = json.loads(rs.research_session_start(query))["session_id"]
    rs.research_session_understand_query(session_id, english_query=query)
    for source_type, source_hits in (
        ("patent", patent_hits),
        ("publication", hits("publication", 6.25, "abstract_verified", "adjacent",
                             "https://doi.org/10.1000/pub{i}")),
        ("web", hits("web", 5.0, "fetched_excerpt", "adjacent",
                     "https://example{i}.com/smart-lock")),
    ):
        rs.research_session_save_evidence(
            session_id,
            source_type,
            {
                "source_type": source_type,
                "status": "ok",
                "completed": True,
                "reliable_no_results": False,
                "hits": source_hits,
                "errors": [],
                "warnings": [],
            },
            query=query,
            attempt=1,
        )
    blob = (
        rs.research_session_checklist(session_id)
        + "\n"
        + rs.research_session_user_answer(session_id, original_query=query, debug_mode=True)
    ).replace(session_id, "<SESSION>")  # session id is random per run
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


DIGESTS = {"filtering": _filtering_digest, "pipeline": _pipeline_digest}


def _collect(name: str) -> set[str]:
    """Run one digest in a fresh interpreter per seed and return the distinct results."""
    results: set[str] = set()
    for seed in SEEDS:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), name],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        results.add(completed.stdout.strip())
    return results


def test_filtering_path_is_hash_order_independent():
    """Anchor selection, acceptance and the merged rerank must not vary by seed."""
    results = _collect("filtering")
    assert len(results) == 1, f"filtering path varies with PYTHONHASHSEED: {results}"


def test_rendered_report_is_hash_order_independent():
    """The checklist and the rendered report must not vary by seed."""
    results = _collect("pipeline")
    assert len(results) == 1, f"rendered output varies with PYTHONHASHSEED: {results}"


if __name__ == "__main__":
    print(DIGESTS[sys.argv[1]]())
