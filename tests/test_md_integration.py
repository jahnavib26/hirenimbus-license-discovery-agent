from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from app.cache import JsonLastGoodCache
from app.board_adapters import MD_ELECTRICIANS_NAME_URL
from app.day2_models import BoardSearchResult
from app.license_sources import LicenseSourceRunner
from app.md_registry import MDSDATRegistrySource
from app.models import BusinessIdentity, IdentityLookupResult, NormalizedPhone
from app.pipeline import LicensePipeline


REGISTRY_FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
LICENSE_FIXTURES = Path(__file__).parent / "fixtures" / "license_sources"
FETCHED_AT = datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)


class Resolver:
    def __init__(self, categories=None) -> None:
        self.categories = categories if categories is not None else ["renovation"]

    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        return IdentityLookupResult(
            found=True,
            confidence="high",
            input_phone=NormalizedPhone(e164="+14105551234"),
            identity=BusinessIdentity(
                business_name="Harbor Home Services",
                phone="+14105551234",
                address="100 Harbor St, Baltimore, MD 21201",
                place_id="md-place-1",
                states=["MD"],
                normalized_categories=self.categories,
            ),
        )


class RecordingRunner:
    def __init__(self, delegate: LicenseSourceRunner) -> None:
        self.delegate = delegate
        self.plan = None
        self.search_keys = None

    def search(self, plan, search_keys):
        self.plan = plan
        self.search_keys = search_keys
        return self.delegate.search(plan, search_keys)


class BaseKeyRunner:
    def __init__(self) -> None:
        self.search_keys = None
        self.calls = 0

    def search(self, plan, search_keys):
        self.calls += 1
        self.search_keys = search_keys
        return [
            BoardSearchResult(
                board_id="MHIC",
                board_name="Maryland Home Improvement Commission",
                jurisdiction="MD",
                strategy="mhic_public_query",
                search_status="not_found",
                fetched_at=FETCHED_AT,
            )
        ]


def test_md_manual_registry_expands_keys_before_existing_mhic_captcha_attempt(
    tmp_path,
) -> None:
    manual_evidence = (
        REGISTRY_FIXTURES / "synthetic_manual_md_sdat_evidence.json"
    ).read_text(encoding="utf-8")
    captcha = (LICENSE_FIXTURES / "captcha.html").read_text(encoding="utf-8")
    board_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        board_requests.append(request)
        return httpx.Response(200, text=captcha, request=request)

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
            MDSDATRegistrySource(
                manual_evidence=manual_evidence, clock=lambda: FETCHED_AT
            )
        ],
        cache=JsonLastGoodCache(tmp_path / "md-results.json"),
    ).find_licenses("410-555-1234")

    assert result.registry_results[0].status == "ok"
    assert runner.search_keys == result.search_keys
    assert [key.original_value for key in result.search_keys] == [
        "Harbor Home Services",
        "Harbor Renovations LLC",
        "Harbor Home Services",
        "Morgan Manager",
    ]
    assert result.license_result.board_results[0].search_status == "captcha_blocked"
    assert result.license_result.board_results[0].search_keys == result.search_keys
    assert result.pipeline_status == "partial"
    assert len(board_requests) == 1


def test_md_electrician_manual_registry_keys_are_audited_at_captcha_gated_board(
    tmp_path,
) -> None:
    manual_evidence = (
        REGISTRY_FIXTURES / "synthetic_manual_md_sdat_evidence.json"
    ).read_text(encoding="utf-8")
    captcha = (LICENSE_FIXTURES / "captcha.html").read_text(encoding="utf-8")
    board_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        board_requests.append(request)
        return httpx.Response(200, text=captcha, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = RecordingRunner(
        LicenseSourceRunner(
            client=client,
            clock=lambda: FETCHED_AT,
            min_request_interval_seconds=0,
        )
    )
    result = LicensePipeline(
        identity_resolver=Resolver(categories=["electrical"]),
        source_runner=runner,
        registry_sources=[
            MDSDATRegistrySource(
                manual_evidence=manual_evidence, clock=lambda: FETCHED_AT
            )
        ],
        cache=JsonLastGoodCache(tmp_path / "md-electrician-results.json"),
    ).find_licenses("410-555-1234")

    assert result.registry_results[0].status == "ok"
    assert runner.plan.selections[0].board_id == "MD_ELECTRICIANS"
    assert runner.plan.selections[0].source_url == MD_ELECTRICIANS_NAME_URL
    assert runner.search_keys == result.search_keys
    assert [key.original_value for key in result.search_keys] == [
        "Harbor Home Services",
        "Harbor Renovations LLC",
        "Harbor Home Services",
        "Morgan Manager",
    ]
    board_result = result.license_result.board_results[0]
    assert board_result.board_id == "MD_ELECTRICIANS"
    assert board_result.search_status == "captcha_blocked"
    assert board_result.search_keys == result.search_keys
    assert result.pipeline_status == "partial"
    assert [(request.method, str(request.url)) for request in board_requests] == [
        ("GET", MD_ELECTRICIANS_NAME_URL)
    ]

    example = json.loads(
        (Path(__file__).parents[1] / "examples/deterministic_registry_flow_md.json")
        .read_text(encoding="utf-8")
    )
    assert example["source_test"] == (
        "tests/test_md_integration.py::"
        "test_md_electrician_manual_registry_keys_are_audited_at_captcha_gated_board"
    )
    assert example["board_selection"]["board_id"] == runner.plan.selections[0].board_id
    assert example["board_search_result"]["search_status"] == board_result.search_status
    assert example["board_search_result"]["search_keys"] == [
        key.model_dump(mode="json") for key in result.search_keys
    ]


def test_md_access_limited_registry_still_runs_board_with_places_only_keys(
    tmp_path,
) -> None:
    runner = BaseKeyRunner()
    result = LicensePipeline(
        identity_resolver=Resolver(),
        source_runner=runner,
        registry_sources=[MDSDATRegistrySource(clock=lambda: FETCHED_AT)],
        cache=JsonLastGoodCache(tmp_path / "md-fallback.json"),
    ).find_licenses("410-555-1234")

    assert result.registry_results[0].status == "skipped"
    assert runner.calls == 1
    assert runner.search_keys == result.search_keys
    assert [key.original_value for key in result.search_keys] == [
        "Harbor Home Services"
    ]
    assert all(key.origin == "base_identity" for key in result.search_keys)
    assert result.pipeline_status == "partial"
