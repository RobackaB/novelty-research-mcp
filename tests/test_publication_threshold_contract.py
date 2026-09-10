"""Guard: the publication threshold's longstanding divergence stays explicit.

`tools.relevance.THRESHOLDS["PUBLICATION"]` is 3.0, but publication search accepts
at 3.5. That gap predates the post-thesis audit -- both values are present in the
thesis submission commit (eb7b532). Repository history does not establish why, so
the value is preserved rather than reinterpreted: if either number moves, this test
fails and forces a deliberate decision rather than a silent one.
"""

from __future__ import annotations

import ast
import io
import pathlib
import tokenize

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


def _executable_identifiers(source: str) -> set[str]:
    """Every name that appears in executable code, ignoring comments and docstrings.

    Comments never reach the AST, so parsing is what separates "the old name is
    no longer used" from "the characters do not appear in the file". The latter
    would force deleting the explanatory comment that records why the rename
    happened, which is documentation worth keeping.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def test_the_old_name_is_absent_from_executable_code():
    """The historical name must not be a live identifier anywhere in the module."""
    names = _executable_identifiers(MODULE.read_text(encoding="utf-8"))
    assert "MIN_PUBLICATION_RERANK_SCORE" not in names


def test_the_current_name_is_a_live_identifier():
    """Guard against the previous test being satisfied by deleting the constant."""
    names = _executable_identifiers(MODULE.read_text(encoding="utf-8"))
    assert "MIN_PUBLICATION_RELEVANCE_SCORE" in names


def test_every_occurrence_of_the_old_name_is_a_comment():
    """Stronger than the AST check: no occurrence may sit in code or a string.

    Tokenising classifies each occurrence precisely. If the historical name ever
    reappears in a string literal or as code, this fails even though the AST
    identifier check might not catch a string.
    """
    source = MODULE.read_text(encoding="utf-8")
    occurrences = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if "MIN_PUBLICATION_RERANK_SCORE" in token.string:
            occurrences.append((token.start[0], tokenize.tok_name[token.type]))
    non_comment = [(line, kind) for line, kind in occurrences if kind != "COMMENT"]
    assert not non_comment, f"old name outside a comment at {non_comment}"


def test_the_rename_rationale_is_still_documented():
    """The historical name is expected in a comment; deleting it is not the fix.

    This exists so that a future change cannot make the guards above pass by
    removing the explanation of why the constant was renamed.
    """
    source = MODULE.read_text(encoding="utf-8")
    comments = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    assert any("MIN_PUBLICATION_RERANK_SCORE" in comment for comment in comments)
