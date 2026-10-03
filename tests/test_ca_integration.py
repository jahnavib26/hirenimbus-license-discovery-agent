from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.ca_registry import CASOSRegistrySource
from app.cache import JsonLastGoodCache
from app.day2_models import BoardSearchResult
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity, IdentityLookupResult, NormalizedPhone
from app.pipeline import LicensePipeline


REGISTRY_FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
LICENSE_FIXTURES = Path(__file__).parent / "fixtures" / "license_sources"
FETCHED_AT = datetime(2026, 10, 1, 19, 30, tzinfo=timezone.utc)


class Resolver:
    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        return IdentityLookupResult(
            found=True,
            confidence="high",
            input_phone=NormalizedPhone(e164="+19165551234"),
            identity=BusinessIdentity(
                business_name="Example Builders",
                phone="+19165551234",
                address="1 Main St, Sacramento, CA 95814",
                place_id="ca-place-1",
                states=["CA"],
                normalized_categories=["renovation"],
            ),
        )


class RecordingRunner:
    def __init__(self, delegate: LicenseSourceRunner) -> None:
        self.delegate = delegate
        self.search_keys = None

    def search(self, plan, search_keys):
        self.search_keys = search_keys
        return self.delegate.search(plan, search_keys)


class BaseKeyRunner:
    def __init__(self) -> None:
        self.search_keys = None

    def search(self, plan, search_keys):
        self.search_keys = search_keys
        return [
            BoardSearchResult(
                board_id="CSLB",
                board_name="California Contractors State License Board",
                jurisdiction="CA",
                strategy="cslb_license_master",
                search_status="not_found",
                fetched_at=FETCHED_AT,
            )
        ]


def test_ca_registry_legal_name_reaches_cslb_and_final_matching(tmp_path) -> None:
    manual_evidence = (
        REGISTRY_FIXTURES / "synthetic_manual_ca_sos_evidence.json"
    ).read_text(encoding="utf-8")
    cslb_csv = (LICENSE_FIXTURES / "cslb.csv").read_text(encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            marker = str(calls)
            return httpx.Response(
                200,
                text=(
                    f'<input type="hidden" name="__VIEWSTATE" value="view-{marker}">'
                    f'<input type="hidden" name="__EVENTVALIDATION" value="event-{marker}">'
                ),
                request=request,
            )
        return httpx.Response(
            200,
            text=cslb_csv,
            headers={"content-type": "text/csv"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = RecordingRunner(
        LicenseSourceRunner(
            client=client,
            clock=lambda: FETCHED_AT,
            min_request_interval_seconds=0,
        )
    )
    result = LicensePipeline(
        identity_resolver=Resolver(),
        source_runner=runner,
        registry_sources=[
            CASOSRegistrySource(
                manual_evidence=manual_evidence, clock=lambda: FETCHED_AT
            )
        ],
        cache=JsonLastGoodCache(tmp_path / "ca-results.json"),
    ).find_licenses("916-555-1234")

    assert runner.search_keys == result.search_keys
    assert any(
        key.original_value == "Example Builders LLC" and key.origin == "registry"
        for key in result.search_keys
    )
    assert result.license_result.board_results[0].search_status == "ok"
    assert result.license_result.accepted_licenses[0].license_number == "123456"
    assert result.license_result.accepted_licenses[0].matched_identity_value == (
        "Example Builders LLC"
    )
    assert result.pipeline_status == "complete"


def test_ca_waf_registry_failure_still_runs_cslb_path_with_places_only_keys(
    tmp_path,
) -> None:
    def waf(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Access Denied", request=request)

    runner = BaseKeyRunner()
    result = LicensePipeline(
        identity_resolver=Resolver(),
        source_runner=runner,
        registry_sources=[
            CASOSRegistrySource(
                httpx.Client(transport=httpx.MockTransport(waf)),
                clock=lambda: FETCHED_AT,
            )
        ],
        cache=JsonLastGoodCache(tmp_path / "ca-fallback.json"),
    ).find_licenses("916-555-1234")

    assert result.registry_results[0].status == "unreachable"
    assert runner.search_keys == result.search_keys
    assert [key.original_value for key in result.search_keys] == ["Example Builders"]
    assert all(key.origin == "base_identity" for key in result.search_keys)
    assert result.pipeline_status == "partial"
