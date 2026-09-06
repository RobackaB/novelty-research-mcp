"""Testy opraveného stemmera, IDF váženia a regresná poistka kvality."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from eval.relevance_eval import SWEEP_THRESHOLDS, evaluate_dataset, load_dataset
from tools.relevance import (
    THRESHOLDS,
    _stem,
    build_corpus_idf,
    evidence_score,
    is_relevant,
    salient_query_tokens,
    tokens,
)


# --- Oprava stemmera ---------------------------------------------------------

def test_singular_and_plural_now_share_a_stem():
    """Pôvodne sa 'log'/'logs' ani 'application'/'applications' nikdy nezhodovali."""
    assert _stem("logs") == _stem("log")
    assert _stem("applications") == _stem("application")
    assert _stem("events") == _stem("event")
    assert _stem("incidents") == _stem("incident")
    assert _stem("anomalies") == _stem("anomaly")


def test_double_s_words_are_not_over_stemmed():
    """Slová končiace na 'ss' nesmú prísť o koncovku ('process' != 'proces')."""
    assert _stem("process") == "process"
    assert _stem("processes") == _stem("process")
    assert _stem("class") == "class"


def test_short_tokens_unchanged():
    assert _stem("log") == "log"
    assert _stem("api") == "api"


def test_log_document_now_overlaps_log_query():
    assert tokens("log records") & tokens("system logs")


# --- IDF nad množinou kandidátov ---------------------------------------------

def test_build_corpus_idf_needs_minimum_corpus():
    assert build_corpus_idf([]) == {}
    assert build_corpus_idf(["only one document about logs"]) == {}


def test_rare_terms_weigh_more_than_common_terms():
    docs = [
        "machine learning detection of anomalies in application logs",
        "machine learning detection of anomalies in seismic signals",
        "machine learning detection of anomalies in engine vibration",
        "machine learning detection of anomalies in network traffic",
    ]
    idf = build_corpus_idf(docs)
    # "machine"/"learning" sú vo všetkých dokumentoch, "logs" iba v jednom.
    assert idf[_stem("logs")] > idf[_stem("machine")]
    assert idf[_stem("logs")] > idf[_stem("learning")]


def test_idf_weighting_penalises_generic_only_match():
    docs = [
        "anomaly detection in application logs of software systems",
        "anomaly detection in seismic events using machine learning",
        "anomaly detection in excavator sensor data using machine learning",
        "anomaly detection in ionosphere measurements using machine learning",
    ]
    idf = build_corpus_idf(docs)
    query = "detecting anomalies in application logs"
    on_topic = docs[0]
    off_topic = docs[1]
    assert evidence_score(query, on_topic, "PUBLICATION", idf=idf) > evidence_score(
        query, off_topic, "PUBLICATION", idf=idf
    )


def test_evidence_score_without_idf_keeps_previous_behaviour():
    query = "smart door lock mobile application"
    text = "A smart door lock controlled by a mobile application"
    assert evidence_score(query, text, "PUBLICATION") > 0
    assert evidence_score(query, text, "PUBLICATION", idf=None) == evidence_score(
        query, text, "PUBLICATION"
    )


# --- Doménová kotva ----------------------------------------------------------

def test_salient_tokens_are_the_rarest_query_terms():
    docs = [
        "machine learning anomaly detection in application logs",
        "machine learning anomaly detection in seismology",
        "machine learning anomaly detection in robotics",
        "machine learning anomaly detection in medicine",
    ]
    idf = build_corpus_idf(docs)
    salient = salient_query_tokens("machine learning anomaly detection in application logs", idf, top_n=2)
    assert _stem("logs") in salient
    assert _stem("machine") not in salient


def test_salient_tokens_empty_without_idf():
    assert salient_query_tokens("any query text here", {}) == set()


# --- Determinizmus výberu kotiev ---------------------------------------------

def test_tied_tokens_break_deterministically_by_token():
    """Anchors must not depend on set iteration order.

    Every candidate token here appears in exactly one document, so all of them
    share one IDF weight and the whole candidate set is a single tie group.
    With the cut at top_n=2 the outcome is decided purely by the tie-break, so
    a weight-only sort would return whatever the process hash seed produced.
    """
    docs = [
        "alpha detection system",
        "bravo detection system",
        "charlie detection system",
        "delta detection system",
    ]
    idf = build_corpus_idf(docs)
    query = "alpha bravo charlie delta"
    selected = salient_query_tokens(query, idf, top_n=2)
    assert selected == {"alpha", "bravo"}, selected


def test_salient_tokens_stable_across_repeated_calls_with_shuffled_input():
    """The same query must yield the same anchors however its tokens are ordered."""
    docs = [
        "anomaly detection in application logs of distributed software",
        "anomaly detection in seismic events using machine learning",
        "anomaly detection in engine vibration using machine learning",
        "anomaly detection in network traffic using machine learning",
    ]
    idf = build_corpus_idf(docs)
    words = "detecting anomalies in application logs and incident notification".split()
    baseline = salient_query_tokens(" ".join(words), idf)
    for seed in range(20):
        shuffled = words[:]
        random.Random(seed).shuffle(shuffled)
        assert salient_query_tokens(" ".join(shuffled), idf) == baseline


def test_evaluation_is_reproducible_in_a_fresh_interpreter():
    """Guard against hash-order nondeterminism returning anywhere in the pipeline.

    Subprocesses are used deliberately: PYTHONHASHSEED is fixed for the lifetime
    of a process, so a single-process loop cannot detect this class of bug.
    """
    script = (
        "from eval.relevance_eval import evaluate_dataset;"
        "_r, s = evaluate_dataset();"
        "print(f\"{s['precision']:.6f} {s['recall']:.6f} {s['f1']:.6f}\")"
    )
    outputs = set()
    for _ in range(5):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(completed.stdout.strip())
    assert len(outputs) == 1, f"evaluation is not reproducible across processes: {outputs}"


def test_document_without_any_salient_term_is_rejected():
    docs = [
        "anomaly detection in application logs of distributed software",
        "anomaly detection using machine learning for weather forecasting",
        "anomaly detection using machine learning for crop yields",
        "anomaly detection using machine learning for traffic flow",
    ]
    idf = build_corpus_idf(docs)
    query = "anomaly detection in application logs of distributed software"
    assert is_relevant(query, docs[0], "PUBLICATION", threshold=1.0, idf=idf) is True
    assert is_relevant(query, docs[1], "PUBLICATION", threshold=1.0, idf=idf) is False


# --- Regresná poistka kvality ------------------------------------------------

def test_eval_dataset_loads_and_is_labelled():
    data = load_dataset()
    assert data["queries"], "dataset nesmie byť prázdny"
    for entry in data["queries"]:
        labels = {int(c["label"]) for c in entry["candidates"]}
        assert labels <= {0, 1}
        assert 1 in labels, f"{entry['id']} musí obsahovať aspoň jeden relevantný dokument"
        assert 0 in labels, f"{entry['id']} musí obsahovať aspoň jeden nerelevantný dokument"


def test_relevance_quality_does_not_regress():
    """Guard: quality must not fall below what was measured at production settings.

    Measured at the thresholds the server applies (3.0 for publications), after
    anchor selection was made deterministic:

        precision 0.611   recall 0.833   F1 0.683

    The v1.0-thesis scorer at the same thresholds reaches 0.519 / 1.000 / 0.655.
    The previously pinned 0.66 / 0.83 / 0.73 came from threshold 3.5, which no
    source type uses. Floors are kept slightly below the measured values so an
    improvement does not fail the build.
    """
    _results, summary = evaluate_dataset()
    assert summary["precision"] >= 0.60, summary
    assert summary["recall"] >= 0.83, summary
    assert summary["f1"] >= 0.68, summary


def test_eval_uses_production_thresholds_by_default():
    """The harness must measure the configuration the server runs, not its own.

    This is the drift guard. The dataset is all `publication`, so the default run
    must equal an explicit run at THRESHOLDS["PUBLICATION"] and must differ from
    the 3.5 the harness used to hardcode.
    """
    _r_default, default = evaluate_dataset()
    _r_prod, production = evaluate_dataset(threshold=THRESHOLDS["PUBLICATION"])
    assert default == production, (default, production)

    _r_old, old_hardcoded = evaluate_dataset(threshold=3.5)
    assert default != old_hardcoded, "3.5 is not a production threshold; they must differ"

    for result in _r_default:
        assert result.threshold == THRESHOLDS["PUBLICATION"]


def test_raising_the_threshold_trades_recall_for_precision():
    """The sweep exists because this trade-off is the whole story of the component."""
    _r_low, low = evaluate_dataset(threshold=2.5)
    _r_high, high = evaluate_dataset(threshold=4.5)
    assert low["recall"] > high["recall"], (low, high)


def test_sweep_covers_every_production_threshold():
    """A sweep that skipped a production setting would hide the number that matters."""
    for value in THRESHOLDS.values():
        assert value in SWEEP_THRESHOLDS, f"{value} is used in production but not swept"


def test_idf_improves_precision_over_unweighted_baseline():
    """IDF váženie musí byť merateľne lepšie než pôvodné rovnaké váhy."""
    _r_off, summary_off = evaluate_dataset(use_idf=False)
    _r_on, summary_on = evaluate_dataset(use_idf=True)
    assert summary_on["precision"] > summary_off["precision"]
    assert summary_on["f1"] > summary_off["f1"]
