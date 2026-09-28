from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.cache import JsonLastGoodCache
from app.day2_models import BoardSearchResult
from app.models import BusinessIdentity, IdentityLookupResult, LookupError, NormalizedPhone
from app.pipeline import LicensePipeline


FETCHED_AT = datetime(2026, 9, 27, tzinfo=timezone.utc)
BASE_PHONE = "+15125551234"


class FakeResolver:
    def __init__(self, *results: IdentityLookupResult) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        self.calls.append(raw_phone)
        return self.results.pop(0)


class FakeSourceRunner:
    def __init__(self, *results: list[BoardSearchResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def search(self, plan, search_keys) -> list[BoardSearchResult]:
        self.calls += 1
        return self.results.pop(0)


def identity_result(place_id: str = "place-1") -> IdentityLookupResult:
    return IdentityLookupResult(
        found=True,
        confidence="medium",
        input_phone=NormalizedPhone(e164=BASE_PHONE),
        identity=BusinessIdentity(
            business_name=f"Example Plumbing {place_id}",
            phone=BASE_PHONE,
            place_id=place_id,
            states=["TX"],
            normalized_categories=["plumbing"],
        ),
    )


def no_identity_result() -> IdentityLookupResult:
    return IdentityLookupResult(
        found=False,
        confidence="low",
        input_phone=NormalizedPhone(e164=BASE_PHONE),
        notes=["No candidates."],
    )


def failed_identity_result() -> IdentityLookupResult:
    return IdentityLookupResult(
        found=False,
        confidence="low",
        input_phone=NormalizedPhone(e164=BASE_PHONE),
        error=LookupError(kind="lookup_failure", message="provider unavailable"),
    )


def board_result(status: str) -> BoardSearchResult:
    return BoardSearchResult(
        board_id="TSBPE",
        board_name="Texas State Board of Plumbing Examiners",
        jurisdiction="TX",
        strategy="free_licensee_lists",
        search_status=status,
        fetched_at=FETCHED_AT,
    )


def pipeline(tmp_path, resolver, runner=None) -> LicensePipeline:
    return LicensePipeline(
        identity_resolver=resolver,
        source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"),
    )


def test_equivalent_formats_share_cache_and_skip_providers(tmp_path) -> None:
    resolver = FakeResolver(identity_result())
    runner = FakeSourceRunner([board_result("not_found")])
    service = pipeline(tmp_path, resolver, runner)

    first = service.find_licenses("(512) 555-1234")
    second = service.find_licenses("+1 512 555 1234")

    assert first.cache.status == "miss_saved"
    assert second.cache.status == "hit"
    assert second.input_phone.e164 == BASE_PHONE
    assert resolver.calls == ["(512) 555-1234"]
    assert runner.calls == 1
    assert list(json.loads((tmp_path / "results.json").read_text())) == [BASE_PHONE]


def test_extension_uses_same_base_cache_key(tmp_path) -> None:
    resolver = FakeResolver(identity_result())
    runner = FakeSourceRunner([board_result("ok")])
    service = pipeline(tmp_path, resolver, runner)

    service.find_licenses("512-555-1234")
    cached = service.find_licenses("512-555-1234 ext. 42")

    assert cached.cache.status == "hit"
    assert cached.input_phone.extension == "42"
    assert resolver.calls == ["512-555-1234"]


def test_invalid_input_does_not_create_cache_entry(tmp_path) -> None:
    resolver = FakeResolver(
        IdentityLookupResult(
            found=False,
            confidence="low",
            error=LookupError(kind="invalid_input", message="invalid phone"),
        )
    )
    service = pipeline(tmp_path, resolver)

    result = service.find_licenses("123")

    assert result.pipeline_status == "failed"
    assert result.identity_result.error.kind == "invalid_input"
    assert result.cache.status == "miss_not_saved"
    assert not (tmp_path / "results.json").exists()


def test_refresh_complete_replaces_last_good_result(tmp_path) -> None:
    resolver = FakeResolver(identity_result("old"), identity_result("new"))
    runner = FakeSourceRunner(
        [board_result("not_found")], [board_result("not_found")]
    )
    service = pipeline(tmp_path, resolver, runner)

    service.find_licenses("5125551234")
    refreshed = service.find_licenses("5125551234", refresh=True)
    cached = service.find_licenses("5125551234")

    assert refreshed.pipeline_status == "complete"
    assert refreshed.cache.status == "refresh_saved"
    assert cached.cache.status == "hit"
    assert cached.identity_result.identity.place_id == "new"
    assert len(resolver.calls) == 2


@pytest.mark.parametrize(
    ("status", "expected_cache_status"),
    [
        ("unreachable", "refresh_partial_preserved"),
        ("captcha_blocked", "refresh_partial_preserved"),
    ],
)
def test_partial_refresh_is_returned_and_last_good_is_preserved(
    tmp_path, status, expected_cache_status
) -> None:
    resolver = FakeResolver(identity_result("old"), identity_result("attempt"))
    runner = FakeSourceRunner(
        [board_result("not_found")], [board_result(status)]
    )
    service = pipeline(tmp_path, resolver, runner)

    service.find_licenses("5125551234")
    attempt = service.find_licenses("5125551234", refresh=True)
    cached = service.find_licenses("5125551234")

    assert attempt.pipeline_status == "partial"
    assert attempt.cache.status == expected_cache_status
    assert attempt.cache.last_good_preserved is True
    assert attempt.identity_result.identity.place_id == "attempt"
    assert attempt.license_result.board_results[0].search_status == status
    assert cached.identity_result.identity.place_id == "old"


def test_failed_refresh_is_returned_and_last_good_is_preserved(tmp_path) -> None:
    resolver = FakeResolver(identity_result("old"), failed_identity_result())
    runner = FakeSourceRunner([board_result("not_found")])
    service = pipeline(tmp_path, resolver, runner)

    service.find_licenses("5125551234")
    attempt = service.find_licenses("5125551234", refresh=True)
    cached = service.find_licenses("5125551234")

    assert attempt.pipeline_status == "failed"
    assert attempt.cache.status == "refresh_failed_preserved"
    assert attempt.cache.last_good_preserved is True
    assert attempt.identity_result.error.kind == "lookup_failure"
    assert cached.identity_result.identity.place_id == "old"
    assert runner.calls == 1


@pytest.mark.parametrize("status", ["unreachable", "captcha_blocked"])
def test_partial_without_previous_cache_is_not_saved(tmp_path, status) -> None:
    service = pipeline(
        tmp_path,
        FakeResolver(identity_result()),
        FakeSourceRunner([board_result(status)]),
    )

    result = service.find_licenses("5125551234")

    assert result.pipeline_status == "partial"
    assert result.cache.status == "miss_not_saved"
    assert not (tmp_path / "results.json").exists()


def test_failed_without_previous_cache_is_not_saved(tmp_path) -> None:
    service = pipeline(tmp_path, FakeResolver(failed_identity_result()))

    result = service.find_licenses("5125551234")

    assert result.pipeline_status == "failed"
    assert result.cache.status == "miss_not_saved"
    assert not (tmp_path / "results.json").exists()


@pytest.mark.parametrize("status", ["ok", "not_found", "skipped"])
def test_completed_board_statuses_produce_complete_result(tmp_path, status) -> None:
    service = pipeline(
        tmp_path,
        FakeResolver(identity_result()),
        FakeSourceRunner([board_result(status)]),
    )

    result = service.find_licenses("5125551234")

    assert result.pipeline_status == "complete"
    assert result.license_result.board_results[0].search_status == status
    assert result.cache.status == "miss_saved"


def test_honest_day1_not_found_is_complete_and_cached(tmp_path) -> None:
    resolver = FakeResolver(no_identity_result())
    runner = FakeSourceRunner()
    service = pipeline(tmp_path, resolver, runner)

    first = service.find_licenses("5125551234")
    second = service.find_licenses("(512) 555-1234")

    assert first.pipeline_status == "complete"
    assert first.identity_result.found is False
    assert first.license_result is None
    assert first.cache.status == "miss_saved"
    assert second.cache.status == "hit"
    assert runner.calls == 0
