"""Guard: the publication threshold's longstanding divergence stays explicit.

`tools.relevance.THRESHOLDS["PUBLICATION"]` is 3.0, but publication search accepts
at 3.5. That gap predates the post-thesis audit -- both values are present in the
thesis submission commit (eb7b532). Repository history does not establish why, so
the value is preserved rather than reinterpreted: if either number moves, this test
fails and forces a deliberate decision rather than a silent one.
"""

from __future__ import annotations

import ast
import pathlib

from tools.publications_search import MIN_PUBLICATION_RELEVANCE_SCORE
from tools.relevance import THRESHOLDS

MODULE = pathlib.Path(__file__).resolve().parent.parent / "tools" / "publications_search.py"


def test_publication_threshold_remains_stricter_than_the_generic_one():
    assert MIN_PUBLICATION_RELEVANCE_SCORE == 3.5
    assert THRESHOLDS["PUBLICATION"] == 3.0
    assert MIN_PUBLICATION_RELEVANCE_SCORE > THRESHOLDS["PUBLICATION"]


def test_publication_search_never_falls_back_to_the_generic_threshold():
    """Every is_relevant call in this module must pass the explicit threshold.

    A call that omitted it would silently accept at 3.0, making publication
    filtering inconsistent between providers.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "is_relevant":
            continue
        threshold = next((kw for kw in node.keywords if kw.arg == "threshold"), None)
        if threshold is None or ast.unparse(threshold.value) != "MIN_PUBLICATION_RELEVANCE_SCORE":
            missing.append(node.lineno)
    assert not missing, f"is_relevant without the explicit publication threshold at lines {missing}"


def test_the_old_misleading_name_is_gone():
    """MIN_PUBLICATION_RERANK_SCORE described one of its nine call sites."""
    assert "MIN_PUBLICATION_RERANK_SCORE" not in MODULE.read_text(encoding="utf-8")
