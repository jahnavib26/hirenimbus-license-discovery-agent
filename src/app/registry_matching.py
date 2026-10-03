"""Narrow exact comparisons used while establishing official registry entities."""

from __future__ import annotations

import re
import unicodedata

from app.search_keys import name_relationship, normalize_search_name


_TERMINAL_LEGAL_SUFFIXES = {"llc", "inc", "corp"}
_ADDRESS_TOKENS = {
    "street": "st",
    "road": "rd",
    "avenue": "ave",
    "place": "pl",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "suite": "unit",
    "ste": "unit",
    "apt": "unit",
    "apartment": "unit",
    "unit": "unit",
    "#": "unit",
}


def registry_legal_name_equal(left: str, right: str) -> bool:
    """Match legal names exactly, allowing one trailing entity suffix to differ."""

    if name_relationship(left, right) == "exact_normalized":
        return True

    left_tokens = _normalized_registry_legal_tokens(left)
    right_tokens = _normalized_registry_legal_tokens(right)
    left_has_suffix = bool(left_tokens and left_tokens[-1] in _TERMINAL_LEGAL_SUFFIXES)
    right_has_suffix = bool(right_tokens and right_tokens[-1] in _TERMINAL_LEGAL_SUFFIXES)
    if not left_has_suffix and not right_has_suffix:
        return False
    if left_has_suffix:
        left_tokens = left_tokens[:-1]
    if right_has_suffix:
        right_tokens = right_tokens[:-1]
    return bool(left_tokens) and left_tokens == right_tokens


def _normalized_registry_legal_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Treat periods between initials as punctuation, so "A.B.C." and "ABC"
    # remain the same written name without joining ordinary separate words.
    normalized = re.sub(
        r"(?<!\w)((?:[^\W_]\.)+[^\W_])(?=\W|$)",
        lambda match: match.group(1).replace(".", ""),
        normalized,
        flags=re.UNICODE,
    )
    return normalize_search_name(normalized).split()


def registry_addresses_equal(left: str | None, right: str | None) -> bool:
    """Compare registry and Places addresses with exact conservative postal rules."""

    if not left or not right:
        return False
    left_tokens = _normalized_address_tokens(left)
    right_tokens = _normalized_address_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True

    left_base, left_unit = _split_address_unit(left_tokens)
    right_base, right_unit = _split_address_unit(right_tokens)
    if left_base != right_base:
        return False
    if left_unit is None or right_unit is None:
        # An omitted unit is compatible only when the other address has a value.
        return (left_unit is None and _has_unit_marker(right_tokens, right_unit)) or (
            right_unit is None and _has_unit_marker(left_tokens, left_unit)
        )
    return left_unit == right_unit


def _normalized_address_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    raw_tokens = re.findall(r"#|[^\W_]+", normalized, flags=re.UNICODE)
    tokens = [_ADDRESS_TOKENS.get(token, token) for token in raw_tokens]
    while len(tokens) >= 3 and tokens[-3:] == ["united", "states", "america"]:
        tokens = tokens[:-3]
    if len(tokens) >= 2 and tokens[-2:] == ["united", "states"]:
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in {"us", "usa"}:
        tokens = tokens[:-1]
    # "Apartment #5" and "Suite #5" contain two consecutive unit markers.
    compact: list[str] = []
    for token in tokens:
        if token == "unit" and compact and compact[-1] == "unit":
            continue
        compact.append(token)
    return compact


def _split_address_unit(tokens: list[str]) -> tuple[list[str], str | None]:
    try:
        marker = tokens.index("unit")
    except ValueError:
        return tokens, None

    value_index = marker + 1
    while value_index < len(tokens) and tokens[value_index] == "unit":
        value_index += 1
    if value_index == len(tokens):
        return tokens, None
    unit = tokens[value_index]
    base = tokens[:marker] + tokens[value_index + 1 :]
    return base, unit


def _has_unit_marker(tokens: list[str], _unit: str | None) -> bool:
    """Distinguish an omitted unit from a malformed marker without a value."""

    return "unit" in tokens and _split_address_unit(tokens)[1] is not None
