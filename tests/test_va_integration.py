from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import board_adapters
from app.cache import JsonLastGoodCache
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity, IdentityLookupResult, NormalizedPhone
from app.pipeline import LicensePipeline
from app.va_registry import VASCCRegistrySource


FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
FETCHED_AT = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


class Resolver:
    def __init__(self, place_id: str = "va-place-1") -> None:
        self.place_id = place_id
        self.calls = 0

    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        self.calls += 1
        return IdentityLookupResult(
            found=True,
            confidence="high",
            input_phone=NormalizedPhone(e164="+18045551234"),
            identity=BusinessIdentity(
                business_name="Blue Ridge Home Services",
                phone="+18045551234",
                address="100 Main St, Richmond, VA 23219",
                place_id=self.place_id,
                states=["VA"],
                normalized_categories=["plumbing"],
            ),
        )


class RecordingRunner:
    def __init__(self, delegate: LicenseSourceRunner) -> None:
        self.delegate = delegate
        self.search_keys = None

    def search(self, plan, search_keys):
        self.search_keys = search_keys
        return self.delegate.search(plan, search_keys)


def test_synthetic_va_registry_key_reaches_dpor_retrieval_and_final_matching(
    tmp_path, monkeypatch
) -> None:
    search_form = (FIXTURES / "synthetic_va_scc_search_form.html").read_text(encoding="utf-8")
    search_results = (FIXTURES / "synthetic_va_scc_search_results.html").read_text(
        encoding="utf-8"
    )
    entity_detail = (FIXTURES / "synthetic_va_scc_entity_detail.html").read_text(
        encoding="utf-8"
    )
    dpor_payload = (FIXTURES / "synthetic_va_dpor.tsv").read_text(encoding="utf-8")
    dpor_url = "https://www.dpor.virginia.gov/fixture-2705b.txt"
    monkeypatch.setattr(board_adapters, "DPOR_LISTS", (("2705 B", dpor_url),))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cis.scc.virginia.gov":
            if request.method == "POST":
                return httpx.Response(200, text=search_results, request=request)
            if request.url.path == "/EntitySearch/BusinessInformation":
                return httpx.Response(200, text=entity_detail, request=request)
            return httpx.Response(200, text=search_form, request=request)
        if str(request.url) == dpor_url:
            return httpx.Response(200, text=dpor_payload, request=request)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry_source = VASCCRegistrySource(client, clock=lambda: FETCHED_AT)
    runner = RecordingRunner(
        LicenseSourceRunner(
            client=client,
            clock=lambda: FETCHED_AT,
            min_request_interval_seconds=0,
        )
    )
    pipeline = LicensePipeline(
        identity_resolver=Resolver(),
        source_runner=runner,
        registry_sources=[registry_source],
        cache=JsonLastGoodCache(tmp_path / "results.json"),
    )

    result = pipeline.find_licenses("804-555-1234")

    registry_legal_key = next(
        key
        for key in result.search_keys
        if key.origin == "registry" and key.source_field == "legal_name"
    )
    assert registry_legal_key.original_value == "Blue Ridge Mechanical LLC"
    assert registry_legal_key.entity_id == "SYNTH001"
    assert runner.search_keys == result.search_keys
    assert result.registry_results[0].fetched_at == FETCHED_AT
    assert result.license_result.board_results[0].search_status == "ok"
    assert result.license_result.board_results[0].match_decisions[0].accepted is True
    accepted = result.license_result.accepted_licenses[0]
    assert accepted.matched_identity_value == "Blue Ridge Mechanical LLC"
    assert accepted.evidence_url == dpor_url
    assert accepted.fetched_at == FETCHED_AT


def test_va_captcha_fallback_runs_dpor_with_base_keys_and_preserves_last_good(
    tmp_path, monkeypatch
) -> None:
    dpor_payload = (FIXTURES / "synthetic_va_dpor.tsv").read_text(encoding="utf-8")
    dpor_url = "https://www.dpor.virginia.gov/synthetic-captcha-fallback.txt"
    monkeypatch.setattr(board_adapters, "DPOR_LISTS", (("2705 B", dpor_url),))
    dpor_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cis.scc.virginia.gov":
            return httpx.Response(
                200,
                text='<script src="https://www.google.com/recaptcha/api.js"></script>',
                request=request,
            )
        if str(request.url) == dpor_url:
            dpor_requests.append(request)
            return httpx.Response(200, text=dpor_payload, request=request)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def registry_source() -> VASCCRegistrySource:
        return VASCCRegistrySource(client, clock=lambda: FETCHED_AT)

    def runner() -> RecordingRunner:
        return RecordingRunner(
            LicenseSourceRunner(
                client=client,
                clock=lambda: FETCHED_AT,
                min_request_interval_seconds=0,
            )
        )

    empty_cache_path = tmp_path / "empty-results.json"
    empty_runner = runner()
    first = LicensePipeline(
        identity_resolver=Resolver("attempt-without-cache"),
        source_runner=empty_runner,
        registry_sources=[registry_source()],
        cache=JsonLastGoodCache(empty_cache_path),
    ).find_licenses("804-555-1234")

    assert first.identity_result.identity.place_id == "attempt-without-cache"
    assert first.registry_results[0].status == "captcha_blocked"
    assert [key.original_value for key in first.search_keys] == [
        "Blue Ridge Home Services"
    ]
    assert all(key.origin == "base_identity" for key in first.search_keys)
    assert empty_runner.search_keys == first.search_keys
    assert first.license_result.board_results[0].search_status == "not_found"
    assert first.pipeline_status == "partial"
    assert first.cache.status == "miss_not_saved"
    assert not empty_cache_path.exists()

    cache_path = tmp_path / "last-good.json"
    cache = JsonLastGoodCache(cache_path)
    seed = LicensePipeline(
        identity_resolver=Resolver("complete-last-good"),
        source_runner=runner(),
        registry_sources=[],
        cache=cache,
    ).find_licenses("804-555-1234")
    assert seed.pipeline_status == "complete"
    before_refresh = cache_path.read_bytes()

    refresh_runner = runner()
    refreshed = LicensePipeline(
        identity_resolver=Resolver("partial-refresh-attempt"),
        source_runner=refresh_runner,
        registry_sources=[registry_source()],
        cache=cache,
    ).find_licenses("804-555-1234", refresh=True)

    assert refreshed.identity_result.identity.place_id == "partial-refresh-attempt"
    assert refreshed.registry_results[0].status == "captcha_blocked"
    assert [key.origin for key in refreshed.search_keys] == ["base_identity"]
    assert refresh_runner.search_keys == refreshed.search_keys
    assert refreshed.license_result.board_results[0].search_status == "not_found"
    assert refreshed.pipeline_status == "partial"
    assert refreshed.cache.status == "refresh_partial_preserved"
    assert refreshed.cache.last_good_preserved is True
    assert cache_path.read_bytes() == before_refresh

    cache_reader_resolver = Resolver("must-not-resolve")
    cached = LicensePipeline(
        identity_resolver=cache_reader_resolver,
        source_runner=runner(),
        registry_sources=[registry_source()],
        cache=cache,
    ).find_licenses("804-555-1234")
    assert cached.identity_result.identity.place_id == "complete-last-good"
    assert cached.cache.status == "hit"
    assert cache_reader_resolver.calls == 0
    assert len(dpor_requests) == 3
