"""Small durable cache for last-good license-discovery results."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonLastGoodCache:
    """Store one JSON result per normalized base phone number."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self, normalized_phone: str) -> dict[str, Any] | None:
        entries = self._read_entries()
        value = entries.get(normalized_phone)
        return value if isinstance(value, dict) else None

    def put(self, normalized_phone: str, result: dict[str, Any]) -> None:
        """Atomically save a complete result without endangering the old file."""

        entries = self._read_entries()
        entries[normalized_phone] = result
        serialized = json.dumps(entries, indent=2, sort_keys=True) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def _read_entries(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("The license cache must contain a JSON object.")
        return parsed
