"""Testy opraveného stemmera, IDF váženia a regresná poistka kvality."""

from __future__ import annotations

from eval.relevance_eval import evaluate_dataset, load_dataset
from tools.relevance import (
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
    """Poistka: kvalita hodnotenia relevancie nesmie klesnúť pod nameranú úroveň.

    Referenčné hodnoty pochádzajú z merania po zavedení IDF váženia
    (v0.9.0). Pôvodná implementácia dosahovala presnosť 0.486 a F1 0.600.
    """
    _results, summary = evaluate_dataset()
    assert summary["precision"] >= 0.66, summary
    assert summary["recall"] >= 0.83, summary
    assert summary["f1"] >= 0.73, summary


def test_idf_improves_precision_over_unweighted_baseline():
    """IDF váženie musí byť merateľne lepšie než pôvodné rovnaké váhy."""
    _r_off, summary_off = evaluate_dataset(use_idf=False)
    _r_on, summary_on = evaluate_dataset(use_idf=True)
    assert summary_on["precision"] > summary_off["precision"]
    assert summary_on["f1"] > summary_off["f1"]
