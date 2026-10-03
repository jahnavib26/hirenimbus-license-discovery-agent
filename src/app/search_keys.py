"""Generate board-search keys solely from established Day 1 names."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from app.day2_models import SearchKey, SearchKeySource
from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult
from app.registry_sources import registry_result_is_established


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
    """Return established base identity names in preferred search order."""

    return _keys_from_identity_fields(identity)


def generate_expanded_search_keys(
    identity: BusinessIdentity,
    registry_results: list[RegistryEnrichmentResult] | None = None,
) -> list[SearchKey]:
    """Combine base and established registry names, business names before people."""

    base = _keys_from_identity_fields(identity)
    base_business = [key for key in base if key.source_field != "owner_principal"]
    base_people = [key for key in base if key.source_field == "owner_principal"]
    registry_business: list[SearchKey] = []
    registry_people: list[SearchKey] = []
    for result in registry_results or []:
        if not registry_result_is_established(result):
            continue
        for name in [*result.legal_names, *result.dbas]:
            source_field: SearchKeySource = "legal_name" if name.kind == "legal_name" else "dba"
            registry_business.append(_make_key(
                name.value,
                source_field,
                origin="registry",
                registry_name=name.source_name or result.registry,
                entity_id=name.entity_id or result.established_entity_id,
                source_url=name.source_url or result.url,
                registry_role=name.role,
            ))
        for name in result.principals:
            registry_people.append(_make_key(
                name.value,
                "owner_principal",
                origin="registry",
                registry_name=name.source_name or result.registry,
                entity_id=name.entity_id or result.established_entity_id,
                source_url=name.source_url or result.url,
                registry_role=name.role,
            ))
    return _unique_keys([*base_business, *registry_business, *base_people, *registry_people])


def _keys_from_identity_fields(identity: BusinessIdentity) -> list[SearchKey]:

    keys: list[SearchKey] = []
    for source_field in _SOURCE_FIELDS:
        original_value = getattr(identity, source_field)
        if original_value is None or not original_value.strip():
            continue
        keys.append(_make_key(original_value, source_field))
    return keys


def _make_key(
    value: str,
    source_field: SearchKeySource,
    *,
    origin: str = "base_identity",
    registry_name: str | None = None,
    entity_id: str | None = None,
    source_url: str | None = None,
    registry_role: str | None = None,
) -> SearchKey:
    return SearchKey(
        original_value=value,
        normalized_value=normalize_search_name(value),
        source_field=source_field,
        origin=origin,
        registry_name=registry_name,
        entity_id=entity_id,
        source_url=source_url,
        registry_role=registry_role,
    )


def _unique_keys(keys: list[SearchKey]) -> list[SearchKey]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[SearchKey] = []
    for key in keys:
        marker = (key.normalized_value, key.source_field, key.entity_id)
        if marker not in seen:
            seen.add(marker)
            result.append(key)
    return result
