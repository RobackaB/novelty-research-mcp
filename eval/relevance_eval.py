"""Meranie kvality hodnotenia relevancie na gold-standard datasete.

Harness je zámerne offline: pracuje nad uloženými kandidátmi z datasetu,
takže výsledky sú reprodukovateľné a nezávisia od toho, čo práve vrátia
externí poskytovatelia. Slúži na porovnanie skórovacích funkcií pred a po
zmene (bod „merateľné metriky" z obhajoby).

Spustenie:
    python -m eval.relevance_eval
    python -m eval.relevance_eval --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.relevance import build_corpus_idf, evidence_score, is_relevant  # noqa: E402

DATASET_PATH = Path(__file__).with_name("dataset.json")
DEFAULT_THRESHOLD = 3.5


@dataclass
class QueryResult:
    """Výsledok vyhodnotenia jedného dotazu."""

    query_id: str
    provenance: str
    relevant_total: int
    accepted_total: int
    accepted_relevant: int
    precision_at_k: float
    average_precision: float
    reciprocal_rank: float
    ranked: list[tuple[float, int, str]] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """Podiel skutočne relevantných medzi prijatými dokumentmi."""
        return self.accepted_relevant / self.accepted_total if self.accepted_total else 0.0

    @property
    def recall(self) -> float:
        """Podiel zachytených relevantných dokumentov."""
        return self.accepted_relevant / self.relevant_total if self.relevant_total else 0.0

    @property
    def f1(self) -> float:
        """Harmonický priemer presnosti a úplnosti."""
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    """Načíta gold-standard dataset zo súboru."""
    return json.loads(path.read_text(encoding="utf-8"))


def _doc_text(candidate: dict[str, Any]) -> str:
    """Spojí názov a text kandidáta do jedného hodnoteného bloku."""
    return f"{candidate.get('title', '')}\n{candidate.get('text', '')}".strip()


def evaluate_query(
    query_entry: dict[str, Any],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    score_fn: Callable[..., float] | None = None,
    accept_fn: Callable[..., bool] | None = None,
    use_idf: bool = True,
) -> QueryResult:
    """Vyhodnotí jeden dotaz datasetu a vráti metriky kvality."""
    score_fn = score_fn or evidence_score
    accept_fn = accept_fn or is_relevant
    query = query_entry["query"]
    evidence_type = str(query_entry.get("source_type", "publication")).upper()
    candidates = query_entry["candidates"]

    texts = [_doc_text(candidate) for candidate in candidates]
    # Vzácnosť termínov sa počíta nad rovnakou množinou kandidátov, akú by
    # v produkcii vrátili poskytovatelia pre daný dotaz.
    idf = build_corpus_idf(texts) if use_idf else {}

    scored: list[tuple[float, int, str, bool]] = []
    for candidate, text in zip(candidates, texts):
        score = score_fn(query, text, evidence_type, idf=idf) if idf else score_fn(query, text, evidence_type)
        accepted = (
            accept_fn(query, text, evidence_type, threshold=threshold, idf=idf)
            if idf
            else accept_fn(query, text, evidence_type, threshold=threshold)
        )
        scored.append((score, int(candidate["label"]), str(candidate.get("title", "")), accepted))

    # Zoradenie podľa skóre určuje poradie, v akom by ich systém prezentoval.
    scored.sort(key=lambda row: row[0], reverse=True)

    relevant_total = sum(1 for _s, label, _t, _a in scored if label == 1)
    accepted = [row for row in scored if row[3]]
    accepted_relevant = sum(1 for _s, label, _t, _a in accepted if label == 1)

    k = max(1, relevant_total)
    top_k = scored[:k]
    precision_at_k = sum(1 for _s, label, _t, _a in top_k if label == 1) / len(top_k)

    hits = 0
    precision_sum = 0.0
    reciprocal_rank = 0.0
    for index, (_score, label, _title, _acc) in enumerate(scored, start=1):
        if label == 1:
            hits += 1
            precision_sum += hits / index
            if reciprocal_rank == 0.0:
                reciprocal_rank = 1 / index
    average_precision = precision_sum / relevant_total if relevant_total else 0.0

    return QueryResult(
        query_id=str(query_entry["id"]),
        provenance=str(query_entry.get("provenance", "")),
        relevant_total=relevant_total,
        accepted_total=len(accepted),
        accepted_relevant=accepted_relevant,
        precision_at_k=precision_at_k,
        average_precision=average_precision,
        reciprocal_rank=reciprocal_rank,
        ranked=[(score, label, title) for score, label, title, _a in scored],
    )


def evaluate_dataset(
    dataset: dict[str, Any] | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    score_fn: Callable[..., float] | None = None,
    accept_fn: Callable[..., bool] | None = None,
    use_idf: bool = True,
) -> tuple[list[QueryResult], dict[str, float]]:
    """Vyhodnotí celý dataset a vráti výsledky aj súhrnné metriky."""
    data = dataset if dataset is not None else load_dataset()
    results = [
        evaluate_query(
            entry, threshold=threshold, score_fn=score_fn, accept_fn=accept_fn, use_idf=use_idf
        )
        for entry in data["queries"]
    ]
    count = len(results) or 1
    summary = {
        "queries": len(results),
        "precision": sum(r.precision for r in results) / count,
        "recall": sum(r.recall for r in results) / count,
        "f1": sum(r.f1 for r in results) / count,
        "precision_at_k": sum(r.precision_at_k for r in results) / count,
        "map": sum(r.average_precision for r in results) / count,
        "mrr": sum(r.reciprocal_rank for r in results) / count,
    }
    return results, summary


def format_report(results: list[QueryResult], summary: dict[str, float], *, show_ranking: bool = True) -> str:
    """Zostaví čitateľnú textovú správu z výsledkov merania."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("VYHODNOTENIE KVALITY HODNOTENIA RELEVANCIE")
    lines.append("=" * 78)
    for result in results:
        lines.append("")
        lines.append(f"[{result.query_id}]  ({result.provenance})")
        lines.append(
            f"  presnosť={result.precision:.2f}  úplnosť={result.recall:.2f}  "
            f"F1={result.f1:.2f}  P@{max(1, result.relevant_total)}={result.precision_at_k:.2f}  "
            f"AP={result.average_precision:.2f}  RR={result.reciprocal_rank:.2f}"
        )
        lines.append(
            f"  prijatých {result.accepted_total}, z toho relevantných "
            f"{result.accepted_relevant} (relevantných v datasete: {result.relevant_total})"
        )
        if show_ranking:
            lines.append("  poradie podľa skóre:")
            for rank, (score, label, title) in enumerate(result.ranked, start=1):
                mark = "OK " if label == 1 else "   "
                lines.append(f"    {rank:>2}. {mark} {score:5.2f}  {title[:62]}")
    lines.append("")
    lines.append("-" * 78)
    lines.append(
        f"SPOLU  presnosť={summary['precision']:.3f}  úplnosť={summary['recall']:.3f}  "
        f"F1={summary['f1']:.3f}  P@k={summary['precision_at_k']:.3f}  "
        f"MAP={summary['map']:.3f}  MRR={summary['mrr']:.3f}"
    )
    lines.append("-" * 78)
    return "\n".join(lines)


def main() -> None:
    """Spustí vyhodnotenie datasetu a vypíše správu."""
    parser = argparse.ArgumentParser(description="Meranie kvality hodnotenia relevancie.")
    parser.add_argument("--json", action="store_true", help="vypíše výsledky ako JSON")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="prah prijatia")
    parser.add_argument("--no-ranking", action="store_true", help="vynechá podrobné poradie")
    parser.add_argument(
        "--no-idf",
        action="store_true",
        help="vypne váženie vzácnosťou termínov (pôvodné správanie pred v0.9.0)",
    )
    args = parser.parse_args()

    results, summary = evaluate_dataset(threshold=args.threshold, use_idf=not args.no_idf)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "queries": [
                        {
                            "id": r.query_id,
                            "precision": r.precision,
                            "recall": r.recall,
                            "f1": r.f1,
                            "precision_at_k": r.precision_at_k,
                            "average_precision": r.average_precision,
                            "reciprocal_rank": r.reciprocal_rank,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(format_report(results, summary, show_ranking=not args.no_ranking))


if __name__ == "__main__":
    main()
