import json

import pytest

from app import cache as cache_module
from app.cache import JsonLastGoodCache


def test_cache_keeps_distinct_entries_by_normalized_key(tmp_path) -> None:
    cache = JsonLastGoodCache(tmp_path / "results.json")
    cache.put("+15125551234", {"marker": "first"})
    cache.put("+17375551200", {"marker": "second"})

    assert cache.get("+15125551234") == {"marker": "first"}
    assert cache.get("+17375551200") == {"marker": "second"}
    assert set(json.loads(cache.path.read_text(encoding="utf-8"))) == {
        "+15125551234",
        "+17375551200",
    }


def test_atomic_write_failure_does_not_destroy_prior_result(
    tmp_path, monkeypatch
) -> None:
    cache = JsonLastGoodCache(tmp_path / "results.json")
    cache.put("+15125551234", {"marker": "last-good"})
    original_bytes = cache.path.read_bytes()

    def fail_replace(source, destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(cache_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        cache.put("+15125551234", {"marker": "new"})

    assert cache.path.read_bytes() == original_bytes
    assert cache.get("+15125551234") == {"marker": "last-good"}
    assert list(tmp_path.glob("*.tmp")) == []
