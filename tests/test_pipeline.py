from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.cache import JsonLastGoodCache
from app.day2_models import BoardSearchResult
from app.models import (
    BusinessIdentity,
    Evidence,
    IdentityCandidateHypothesis,
    IdentityLookupResult,
    LookupError,
    NormalizedPhone,
)
from app.pipeline import LicensePipeline
from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.website import WebsiteFetchError, WebsitePage


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
        self.search_keys = None

    def search(self, plan, search_keys) -> list[BoardSearchResult]:
        self.calls += 1
        self.search_keys = search_keys
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
    assert list(json.loads((tmp_path / "results.json").read_text())) == [f"phase2:{BASE_PHONE}"]


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


def test_first_party_legal_name_seeds_registry_corroboration_and_provenanced_board_key(tmp_path) -> None:
    result = identity_result("structured-place")
    result.identity.states = ["VA"]
    result.identity.legal_name = "Example Mechanical LLC"

    class ExactLegalRegistry:
        jurisdiction = "VA"

        def enrich(self, identity):
            assert identity.legal_name == "Example Mechanical LLC"
            return RegistryEnrichmentResult(
                jurisdiction="VA", registry="VA SCC", status="ok",
                url="https://cis.scc.virginia.gov/entity/123", fetched_at=FETCHED_AT,
                established_entity_id="123", established_entity_name="Example Mechanical LLC",
                legal_names=[RegistryName(
                    value="Example Mechanical LLC", kind="legal_name", source_name="VA SCC",
                    source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123",
                )],
                dbas=[RegistryName(
                    value="Example Mechanical", kind="dba", role="fictitious name",
                    source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/123",
                    entity_id="123",
                )],
                principals=[RegistryName(
                    value="Ada Example", kind="principal", role="manager",
                    source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/123",
                    entity_id="123",
                )],
                evidence=[RegistryEvidence(
                    kind="exact_legal_name", source_name="VA SCC",
                    source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123",
                    observed={"legal_name": "Example Mechanical LLC"},
                )],
            )

    runner = FakeSourceRunner([board_result("not_found")])
    service = LicensePipeline(
        identity_resolver=FakeResolver(result), source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "registry-identity.json"),
        registry_sources=[ExactLegalRegistry()],
    )
    output = service.find_licenses("512-555-1234")

    assert output.registry_results[0].established_entity_id == "123"
    legal, dba, principal = [key for key in output.search_keys if key.origin == "registry"]
    assert (legal.original_value, legal.source_field, legal.entity_id, legal.source_url) == (
        "Example Mechanical LLC", "legal_name", "123", "https://cis.scc.virginia.gov/entity/123"
    )
    assert (dba.original_value, dba.source_field, dba.registry_role) == (
        "Example Mechanical", "dba", "fictitious name"
    )
    assert (principal.original_value, principal.source_field, principal.registry_role) == (
        "Ada Example", "owner_principal", "manager"
    )
    assert any(key.original_value == "Example Mechanical LLC" for key in runner.search_keys)


def test_license_number_discovery_uses_shared_bounded_homepage_links(tmp_path) -> None:
    resolved = identity_result("linked-license-place")
    assert resolved.identity is not None
    resolved.identity.website = "https://example.test/"
    resolved.identity.states = ["CA"]
    resolved.identity.normalized_categories = ["plumbing"]
    license_page = "https://example.test/licenses/"

    class Websites:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, url: str) -> WebsitePage:
            self.calls.append(url)
            if url == "https://example.test/":
                return WebsitePage(
                    requested_url=url,
                    final_url=url,
                    visible_text="Example Plumbing",
                    phones=(),
                    internal_identity_links=(
                        license_page,
                        "https://outside.test/license/",
                    ),
                )
            if url == license_page:
                return WebsitePage(
                    requested_url=url,
                    final_url=url,
                    visible_text="CSLB License #123456",
                    phones=(),
                )
            raise WebsiteFetchError("not available in this injected site")

    class EvidenceRunner:
        def __init__(self) -> None:
            self.evidence = []

        def search(self, plan, search_keys, license_number_evidence=None):
            self.evidence = list(license_number_evidence or [])
            return [BoardSearchResult(
                board_id="CSLB",
                board_name="California Contractors State License Board",
                jurisdiction="CA",
                strategy="cslb_license_master",
                search_status="not_found",
                fetched_at=FETCHED_AT,
            )]

    websites = Websites()
    runner = EvidenceRunner()
    output = LicensePipeline(
        identity_resolver=FakeResolver(resolved),
        source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "bounded-license-pages.json"),
        registry_sources=[],
        website_client=websites,
    ).find_licenses("512-555-1234")

    assert license_page in websites.calls
    assert "https://outside.test/license/" not in websites.calls
    assert [(item.likely_board_id, item.normalized_number) for item in runner.evidence] == [
        ("CSLB", "123456")
    ]
    assert output.license_number_evidence == runner.evidence


def test_registry_hard_evidence_promotes_unique_first_party_candidate_before_board_search(tmp_path) -> None:
    candidate_identity = BusinessIdentity(
        business_name="Example Mechanical", phone=BASE_PHONE, place_id="hypothesis-1",
        states=["VA"], normalized_categories=["hvac"],
    )
    website_evidence = Evidence(
        source="official_business_website", source_id="hypothesis-1",
        url="https://example.test/contact/",
        observed={"canonical_domain": "example.test", "extracted_fields": ["name", "telephone"]},
    )
    unresolved = IdentityLookupResult(
        found=False, confidence="low", input_phone=NormalizedPhone(e164=BASE_PHONE),
        candidate_hypotheses=[IdentityCandidateHypothesis(
            identity=candidate_identity, evidence=[website_evidence],
        )],
    )

    class ExactRegistry:
        jurisdiction = "VA"
        source_name = "VA SCC"

        def enrich(self, identity):
            assert identity.place_id == "hypothesis-1"
            return RegistryEnrichmentResult(
                jurisdiction="VA", registry="VA SCC", status="ok", url="https://cis.scc.virginia.gov/entity/123",
                established_entity_id="123", established_entity_name="Example Mechanical LLC",
                legal_names=[RegistryName(value="Example Mechanical LLC", kind="legal_name", source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123")],
                dbas=[RegistryName(value="Example Mechanical", kind="dba", source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123")],
                principals=[RegistryName(value="Ada Example", kind="principal", role="manager", source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123")],
                evidence=[RegistryEvidence(
                    kind="exact_fictitious_name_relationship", source_name="VA SCC",
                    source_url="https://cis.scc.virginia.gov/entity/123", entity_id="123",
                    observed={"fictitious_name": "Example Mechanical"},
                )],
            )

    runner = FakeSourceRunner([board_result("not_found")])
    output = LicensePipeline(
        identity_resolver=FakeResolver(unresolved), source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "hypothesis-hard.json"),
        registry_sources=[ExactRegistry()],
    ).find_licenses("512-555-1234")

    assert output.identity_result.found is True
    assert output.identity_result.identity.place_id == "hypothesis-1"
    assert output.registry_results[0].candidate_place_id == "hypothesis-1"
    assert {key.original_value for key in output.search_keys if key.origin == "registry"} == {
        "Example Mechanical LLC", "Example Mechanical", "Ada Example"
    }
    assert runner.calls == 1


def test_registry_name_similarity_without_hard_evidence_does_not_promote_candidate(tmp_path) -> None:
    candidate_identity = BusinessIdentity(
        business_name="Example Mechanical", phone=BASE_PHONE, place_id="hypothesis-weak",
        states=["VA"], normalized_categories=["hvac"],
    )
    unresolved = IdentityLookupResult(
        found=False, confidence="low", input_phone=NormalizedPhone(e164=BASE_PHONE),
        candidate_hypotheses=[IdentityCandidateHypothesis(identity=candidate_identity)],
    )

    class WeakRegistry:
        jurisdiction = "VA"
        source_name = "VA SCC"

        def enrich(self, identity):
            return RegistryEnrichmentResult(
                jurisdiction="VA", registry="VA SCC", status="ok", url="https://cis.scc.virginia.gov/entity/456",
                established_entity_id="456", established_entity_name="Example Mechanical Services LLC",
                legal_names=[RegistryName(value="Example Mechanical Services LLC", kind="legal_name", source_name="VA SCC", source_url="https://cis.scc.virginia.gov/entity/456", entity_id="456")],
                evidence=[RegistryEvidence(
                    kind="scc_entity_candidate", source_name="VA SCC",
                    source_url="https://cis.scc.virginia.gov/entity/456", entity_id="456",
                    observed={"entity_name": "Example Mechanical Services LLC"},
                )],
            )

    output = LicensePipeline(
        identity_resolver=FakeResolver(unresolved), source_runner=FakeSourceRunner([board_result("not_found")]),
        cache=JsonLastGoodCache(tmp_path / "hypothesis-weak.json"),
        registry_sources=[WeakRegistry()],
    ).find_licenses("512-555-1234")

    assert output.identity_result.found is False
    assert output.license_result is None
    assert output.search_keys == []


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


@pytest.mark.parametrize("status", ["ok", "not_found"])
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


def test_skipped_source_path_is_partial_and_preserves_issue_kind(tmp_path) -> None:
    service = pipeline(
        tmp_path, FakeResolver(identity_result()), FakeSourceRunner([board_result("skipped")])
    )
    result = service.find_licenses("5125551234")
    assert result.pipeline_status == "partial"
    assert result.cache.status == "miss_not_saved"


def test_registry_enrichment_threads_expanded_keys_and_exposes_provenance(tmp_path) -> None:
    class FakeRegistry:
        jurisdiction = "VA"

        def enrich(self, identity):
            return RegistryEnrichmentResult(
                jurisdiction="VA", registry="VA SCC", status="ok",
                url="https://registry.example/entity/42", established_entity_id="42",
                established_entity_name=identity.business_name,
                legal_names=[RegistryName(value="Registry Legal", kind="legal_name", source_name="VA SCC")],
                evidence=[RegistryEvidence(kind="exact_name", source_name="VA SCC")],
            )

    identity = identity_result()
    identity.identity.states = ["VA"]
    board = board_result("not_found")
    runner = FakeSourceRunner([board])
    service = LicensePipeline(
        identity_resolver=FakeResolver(identity), source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"), registry_sources=[FakeRegistry()],
    )
    result = service.find_licenses("5125551234")
    assert result.registry_results[0].established_entity_id == "42"
    assert [key.original_value for key in result.search_keys] == ["Example Plumbing place-1", "Registry Legal"]
    assert result.search_keys[1].entity_id == "42"
    assert runner.search_keys == result.search_keys


def test_tx_does_not_invoke_configured_registry_source(tmp_path) -> None:
    class FakeRegistry:
        jurisdiction = "TX"
        calls = 0
        def enrich(self, identity):
            self.calls += 1
            raise AssertionError("TX must not use registry enrichment")

    registry = FakeRegistry()
    service = LicensePipeline(
        identity_resolver=FakeResolver(identity_result()),
        source_runner=FakeSourceRunner([board_result("not_found")]),
        cache=JsonLastGoodCache(tmp_path / "results.json"), registry_sources=[registry],
    )
    service.find_licenses("5125551234")
    assert registry.calls == 0


def test_registry_failure_falls_back_to_base_keys_and_keeps_board_search_running(tmp_path) -> None:
    class BrokenRegistry:
        jurisdiction = "VA"
        def enrich(self, identity):
            raise RuntimeError("registry unavailable")

    identity = identity_result()
    identity.identity.states = ["VA"]
    runner = FakeSourceRunner([board_result("not_found")])
    service = LicensePipeline(
        identity_resolver=FakeResolver(identity), source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"), registry_sources=[BrokenRegistry()],
    )
    result = service.find_licenses("5125551234")
    assert result.registry_results[0].status == "unreachable"
    assert [key.origin for key in result.search_keys] == ["base_identity"]
    assert runner.calls == 1
    assert result.pipeline_status == "partial"
    assert result.cache.status == "miss_not_saved"


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


def phone_search_hypothesis(
    place_id: str, business_name: str = "Example Plumbing"
) -> IdentityCandidateHypothesis:
    return IdentityCandidateHypothesis(
        identity=BusinessIdentity(
            business_name=business_name,
            legal_name=f"{business_name} LLC",
            phone=BASE_PHONE,
            address="1 Main Street, Baltimore, MD 21201",
            place_id=place_id,
            states=["MD"],
            normalized_categories=["plumbing"],
        ),
        evidence=[Evidence(
            source="google_places_phone_search",
            source_id=place_id,
            observed={
                "candidate_discovery_path": "Google Places phone text search",
                "places_phone_verification": "mismatch",
                "registry_search_only": True,
            },
        )],
        notes=["Untrusted acquisition hypothesis."],
    )


def hypothesis_lookup(*candidates: tuple[str, str]) -> IdentityLookupResult:
    return IdentityLookupResult(
        found=False,
        confidence="low",
        input_phone=NormalizedPhone(e164=BASE_PHONE),
        candidate_hypotheses=[
            phone_search_hypothesis(place_id, business_name)
            for place_id, business_name in candidates
        ],
    )


def exact_registry_result() -> RegistryEnrichmentResult:
    url = "https://egov.maryland.gov/BusinessExpress/EntitySearch"
    return RegistryEnrichmentResult(
        jurisdiction="MD",
        registry="Maryland SDAT",
        status="ok",
        url=url,
        established_entity_id="MD-ENTITY-7",
        established_entity_name="Example Plumbing LLC",
        evidence=[RegistryEvidence(
            kind="exact_legal_name",
            source_name="Maryland SDAT",
            source_url=url,
            entity_id="MD-ENTITY-7",
            observed={"Business Name": "Example Plumbing LLC"},
        )],
    )


def test_unverified_places_phone_candidate_reaches_registry_but_is_not_promoted(tmp_path) -> None:
    class NotFoundRegistry:
        jurisdiction = "MD"
        calls: list[str] = []

        def enrich(self, identity):
            self.calls.append(identity.place_id)
            return RegistryEnrichmentResult(
                jurisdiction="MD", registry="Maryland SDAT", status="not_found"
            )

    registry = NotFoundRegistry()
    runner = FakeSourceRunner()
    service = LicensePipeline(
        identity_resolver=FakeResolver(
            hypothesis_lookup(("place-mismatch", "Example Plumbing"))
        ),
        source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"),
        registry_sources=[registry],
    )

    result = service.find_licenses(BASE_PHONE)

    assert registry.calls == ["place-mismatch"]
    assert result.identity_result.found is False
    assert result.identity_result.identity is None
    assert result.license_result is None
    assert runner.calls == 0


def test_multiple_unrelated_places_candidates_remain_ambiguous(tmp_path) -> None:
    class OneExactRegistry:
        jurisdiction = "MD"

        def enrich(self, identity):
            if identity.place_id == "place-one":
                return exact_registry_result()
            return RegistryEnrichmentResult(
                jurisdiction="MD", registry="Maryland SDAT", status="not_found"
            )

    runner = FakeSourceRunner()
    service = LicensePipeline(
        identity_resolver=FakeResolver(hypothesis_lookup(
            ("place-one", "Example Plumbing"),
            ("place-two", "Unrelated Services"),
        )),
        source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"),
        registry_sources=[OneExactRegistry()],
    )

    result = service.find_licenses(BASE_PHONE)

    assert result.identity_result.found is False
    assert result.identity_result.identity is None
    assert len(result.registry_results) == 2
    assert runner.calls == 0


def test_places_candidates_can_be_promoted_when_registry_proves_same_entity(tmp_path) -> None:
    class SameEntityRegistry:
        jurisdiction = "MD"

        def enrich(self, identity):
            return exact_registry_result()

    runner = FakeSourceRunner([board_result("not_found")])
    service = LicensePipeline(
        identity_resolver=FakeResolver(hypothesis_lookup(
            ("place-one", "Example Plumbing"),
            ("place-two", "Example Plumbing"),
        )),
        source_runner=runner,
        cache=JsonLastGoodCache(tmp_path / "results.json"),
        registry_sources=[SameEntityRegistry()],
    )

    result = service.find_licenses(BASE_PHONE)

    assert result.identity_result.found is True
    assert result.identity_result.identity.place_id == "place-one"
    assert any("same registry entity" in note for note in result.identity_result.notes)
    assert runner.calls == 1
