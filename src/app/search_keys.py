"""Generate board-search keys solely from established Day 1 names."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from app.day2_models import SearchKey, SearchKeySource
from app.models import BusinessIdentity


_SOURCE_FIELDS: tuple[SearchKeySource, ...] = (
    "legal_name",
    "dba",
    "business_name",
    "owner_principal",
)

_SUFFIX_SEQUENCES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("limited", "liability", "company"), "llc"),
    (("l", "l", "c"), "llc"),
    (("i", "n", "c"), "inc"),
    (("c", "o", "r", "p"), "corp"),
    (("c", "o"), "co"),
)

_SUFFIX_VARIANTS = {
    "llc": "llc",
    "inc": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corporation": "corp",
    "co": "co",
    "company": "co",
}


def normalize_search_name(value: str) -> str:
    """Normalize a name without performing fuzzy matching."""

    case_normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = re.findall(r"[^\W_]+", case_normalized, flags=re.UNICODE)

    for suffix_tokens, canonical in _SUFFIX_SEQUENCES:
        if tuple(tokens[-len(suffix_tokens) :]) == suffix_tokens:
            tokens[-len(suffix_tokens) :] = [canonical]
            break
    if tokens:
        tokens[-1] = _SUFFIX_VARIANTS.get(tokens[-1], tokens[-1])

    return " ".join(tokens)


def name_relationship(
    left: str,
    right: str,
) -> Literal["exact_normalized", "partial", "fuzzy", "none"]:
    """Classify a conservative normalized relationship between two names."""

    left_normalized = normalize_search_name(left)
    right_normalized = normalize_search_name(right)
    if not left_normalized or not right_normalized:
        return "none"
    if left_normalized == right_normalized:
        return "exact_normalized"

    shorter = min(len(left_normalized), len(right_normalized))
    if shorter >= 4 and (
        left_normalized in right_normalized or right_normalized in left_normalized
    ):
        return "partial"
    if shorter >= 5 and SequenceMatcher(
        None, left_normalized, right_normalized
    ).ratio() >= 0.88:
        return "fuzzy"
    return "none"


def generate_search_keys(identity: BusinessIdentity) -> list[SearchKey]:
    """Return established Day 1 names in preferred search order."""

    keys: list[SearchKey] = []
    for source_field in _SOURCE_FIELDS:
        original_value = getattr(identity, source_field)
        if original_value is None or not original_value.strip():
            continue
        keys.append(
            SearchKey(
                original_value=original_value,
                normalized_value=normalize_search_name(original_value),
                source_field=source_field,
            )
        )
    return keys
