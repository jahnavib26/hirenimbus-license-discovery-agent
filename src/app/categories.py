"""Conservative mapping from provider types to supplied taxonomy IDs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path


class CategoryMapper:
    """Map only explicitly configured provider types; never infer categories."""

    def __init__(self, mapping: Mapping[str, str | list[str]] | None = None) -> None:
        self._mapping: dict[str, list[str]] = {}
        for raw_key, raw_values in (mapping or {}).items():
            values = [raw_values] if isinstance(raw_values, str) else raw_values
            if not isinstance(raw_key, str) or not isinstance(values, list):
                raise ValueError("Category mappings must contain string keys and values.")
            normalized_values = [value for value in values if isinstance(value, str)]
            self._mapping[_category_key(raw_key)] = normalized_values

    @classmethod
    def from_json(cls, path: str | Path) -> "CategoryMapper":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Category taxonomy mapping must be a JSON object.")

        categories = payload.get("categories")
        if categories is None:
            return cls(payload)
        if not isinstance(categories, list):
            raise ValueError("Category taxonomy 'categories' must be a list.")

        mapping: dict[str, str] = {}
        for category in categories:
            if not isinstance(category, dict):
                raise ValueError("Each taxonomy category must be an object.")
            category_id = category.get("id")
            aliases = category.get("aliases", [])
            if not isinstance(category_id, str) or not isinstance(aliases, list):
                raise ValueError("Each taxonomy category needs an id and alias list.")
            terms = [category_id, category.get("label"), *aliases]
            for term in terms:
                if not isinstance(term, str):
                    continue
                key = _category_key(term)
                existing = mapping.get(key)
                if existing and existing != category_id:
                    raise ValueError(f"Taxonomy alias {term!r} maps to multiple categories.")
                mapping[key] = category_id
        return cls(mapping)

    def map(self, raw_types: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_type in raw_types:
            key = _category_key(raw_type)
            values = self._mapping.get(key)
            if values is None:
                values = [
                    category_id
                    for alias, category_ids in self._mapping.items()
                    if _contains_term(key, alias)
                    for category_id in category_ids
                    if category_id != "other"
                ]
            for value in values:
                if value not in normalized:
                    normalized.append(value)
        return normalized


def _category_key(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.casefold()).strip()


def _contains_term(value: str, term: str) -> bool:
    return bool(term) and re.search(rf"(?:^|\s){re.escape(term)}(?:$|\s)", value) is not None
