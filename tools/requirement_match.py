"""Zdieľané porovnávanie atomických požiadaviek dotazu s textom dôkazu.

Modul poskytuje jednotnú, stemming-aware logiku pre výpočet pokrytia
požiadaviek naprieč patentovým, publikačným aj webovým evidence packom
a finálnym reportom, aby všetky časti systému merali pokrytie rovnako.
"""

from __future__ import annotations

import re
from typing import Any

_STEM_NORMALIZATION = {
    "verification": "verify",
    "verified": "verify",
    "verifies": "verify",
    "verifying": "verify",
    "authentication": "authenticate",
    "authenticated": "authenticate",
    "authenticates": "authenticate",
    "authenticating": "authenticate",
    "authorization": "authorize",
    "authorized": "authorize",
    "authorizes": "authorize",
    "authorizing": "authorize",
    "locking": "lock",
    "locked": "lock",
    "locks": "lock",
    "unlocking": "unlock",
    "unlocked": "unlock",
    "unlocks": "unlock",
    "logging": "log",
    "logged": "log",
    "logs": "log",
    "scheduled": "schedule",
    "scheduling": "schedule",
}

# Poradie je dôležité: špecifické plurálové prípony pred všeobecným "s",
# aby "codes" -> "code" a zároveň "batches" -> "batch".
_STEM_SUFFIXES = (
    ("ies", "y"),
    ("sses", "ss"),
    ("xes", "x"),
    ("ches", "ch"),
    ("shes", "sh"),
    ("zes", "z"),
    ("s", ""),
)


def stem_requirement_token(token: str) -> str:
    """Upraví token požiadavky do tvaru vhodného na porovnávanie."""
    token = token.lower().strip()
    if token in _STEM_NORMALIZATION:
        return _STEM_NORMALIZATION[token]
    if len(token) <= 3:
        return token
    for suffix, replacement in _STEM_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + replacement
    return token


def blob_tokens(text: str) -> set[str]:
    """Vytvorí množinu tokenov a ich normalizovaných tvarov z textu."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    out: set[str] = set()
    for token in tokens:
        out.add(token)
        out.add(stem_requirement_token(token))
    return out


def part_matches(part: str, tokens: set[str]) -> bool:
    """Overí, či sa jedna časť požiadavky zhoduje s tokenmi v texte."""
    part = part.lower().strip()
    if not part:
        return False
    return part in tokens or stem_requirement_token(part) in tokens


def term_matches_blob(term: str, tokens: set[str], compact_blob: str) -> bool:
    """Overí, či sa výraz požiadavky zhoduje s textovým blokom nálezu."""
    parts = re.findall(r"[a-z0-9]+", str(term or "").lower())
    if not parts:
        return False
    joined = "".join(parts)
    if len(parts) > 1:
        return joined in compact_blob or all(part_matches(part, tokens) for part in parts)
    return part_matches(parts[0], tokens)


def requirement_match_strength(terms: list[str], text: str) -> str:
    """Určí, či text pokrýva požiadavku úplne, čiastočne alebo vôbec."""
    cleaned_terms = [str(term).lower().strip() for term in terms if str(term).strip()]
    if not cleaned_terms:
        return "none"
    tokens = blob_tokens(text)
    compact_blob = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    matched = sum(1 for term in cleaned_terms if term_matches_blob(term, tokens, compact_blob))
    if matched == len(cleaned_terms):
        return "full"
    if matched:
        return "partial"
    return "none"


def _atom_term_sets(atomic_requirements: list[dict[str, Any]] | None) -> list[list[str]]:
    """Pripraví zoznamy termínov z atomických požiadaviek."""
    if not atomic_requirements:
        return []
    term_sets: list[list[str]] = []
    for atom in atomic_requirements:
        if not isinstance(atom, dict):
            continue
        terms = [str(t or "").lower().strip() for t in (atom.get("terms") or [])]
        terms = [t for t in terms if t]
        if terms:
            term_sets.append(terms)
    return term_sets


def atom_coverage(text: str, atomic_requirements: list[dict[str, Any]] | None) -> tuple[float, int]:
    """Vypočíta podiel atomických požiadaviek plne pokrytých textom.

    Vracia dvojicu (pokrytie 0.0-1.0 zaokrúhlené na 4 miesta, počet plne
    pokrytých požiadaviek). Text bez obsahu alebo prázdne požiadavky
    vracajú (0.0, 0).
    """
    term_sets = _atom_term_sets(atomic_requirements)
    if not term_sets or not (text or "").strip():
        return 0.0, 0
    tokens = blob_tokens(text)
    compact_blob = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    matched = 0
    for terms in term_sets:
        if all(term_matches_blob(term, tokens, compact_blob) for term in terms):
            matched += 1
    total = len(term_sets)
    return round(matched / total, 4), matched


def covers_all_atoms(text: str, atomic_requirements: list[dict[str, Any]] | None) -> bool:
    """Zistí, či text plne pokrýva všetky atomické požiadavky dotazu."""
    term_sets = _atom_term_sets(atomic_requirements)
    if not term_sets:
        return False
    coverage, matched = atom_coverage(text, atomic_requirements)
    return matched == len(term_sets) and coverage >= 1.0


def unique_coverage_tokens(text: str, cap: int = 2500) -> str:
    """Zhustí celý text dokumentu na unikátne tokeny pre výpočet pokrytia.

    Pokrytie prvkov overuje prítomnosť termínov, takže deduplikované tokeny
    zachovávajú výsledok zhody a pritom umožňujú preniesť obsah celej stránky
    alebo dokumentu v kompaktnej podobe.
    """
    tokens = re.findall(r"[a-z0-9][a-z0-9-]{1,}", (text or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= max(1, cap):
            break
    return " ".join(out)
