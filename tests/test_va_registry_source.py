from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.models import BusinessIdentity
from app.va_registry import (
    VA_SCC_COOKIE_URL,
    VA_SCC_SEARCH_URL,
    VASCCRegistrySource,
    _parse_entity_detail,
    _VASCCCandidate,
)


FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
FETCHED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def identity(**overrides: object) -> BusinessIdentity:
    values: dict[str, object] = {
        "business_name": "Blue Ridge Home Services",
        "phone": "+18045550100",
        "address": "100 Main St, Richmond, VA 23219, USA",
        "place_id": "va-place-1",
        "states": ["VA"],
        "normalized_categories": ["plumbing"],
    }
    values.update(overrides)
    return BusinessIdentity(**values)


def source_for(search_results: str, details: dict[str, str]) -> VASCCRegistrySource:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/EntitySearch/Index":
            return httpx.Response(
                200, text=fixture("synthetic_va_scc_search_form.html"), request=request
            )
        if request.method == "POST" and request.url.path == "/EntitySearch/Index":
            assert b"QuickSearch.BusinessName=" in request.content
            return httpx.Response(200, text=search_results, request=request)
        if request.method == "GET" and request.url.path == "/EntitySearch/BusinessInformation":
            entity_id = request.url.params["businessId"]
            return httpx.Response(200, text=details[entity_id], request=request)
        raise AssertionError(f"Unexpected SCC request: {request.method} {request.url}")

    return VASCCRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )


def test_synthetic_va_scc_fixture_establishes_fictitious_name_and_preserves_provenance() -> None:
    source = source_for(
        fixture("synthetic_va_scc_search_results.html"),
        {"SYNTH001": fixture("synthetic_va_scc_entity_detail.html")},
    )

    result = source.enrich(identity())

    assert result.status == "ok"
    assert result.established_entity_id == "SYNTH001"
    assert result.established_entity_name == "Blue Ridge Mechanical LLC"
    assert result.entity_status == "Active"
    assert result.principal_office_address == "100 Main Street, Richmond, VA 23219"
    assert [name.value for name in result.legal_names] == ["Blue Ridge Mechanical LLC"]
    assert [name.value for name in result.dbas] == ["Blue Ridge Home Services"]
    assert [(name.value, name.role) for name in result.principals] == [
        ("Ada Owner", "Manager")
    ]
    assert all(name.entity_id == "SYNTH001" for name in [*result.legal_names, *result.dbas, *result.principals])
    assert all(name.source_url == result.url for name in [*result.legal_names, *result.dbas, *result.principals])
    assert result.fetched_at == FETCHED_AT
    agent_evidence = next(item for item in result.evidence if item.kind == "registered_agent")
    assert agent_evidence.observed["name"] == "Robin Registered Agent"
    assert "Robin Registered Agent" not in [name.value for name in result.principals]


def test_va_scc_exact_legal_name_can_establish_without_address() -> None:
    source = source_for(
        fixture("synthetic_va_scc_search_results.html"),
        {"SYNTH001": fixture("synthetic_va_scc_entity_detail.html")},
    )

    result = source.enrich(
        identity(
            business_name="Blue Ridge Mechanical LLC",
            address=None,
        )
    )

    assert result.status == "ok"
    assert any(item.kind == "exact_legal_name" for item in result.evidence)


def test_va_scc_suffix_equivalent_duplicate_entities_remain_ambiguous() -> None:
    source = source_for(
        fixture("synthetic_va_scc_search_results_multiple.html"),
        {
            "SYNTH001": fixture("synthetic_va_scc_entity_detail.html"),
            "SYNTH002": fixture("synthetic_va_scc_entity_detail_other.html"),
        },
    )

    result = source.enrich(
        identity(business_name="Blue Ridge Mechanical", legal_name=None, dba=None)
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None


def test_va_scc_multiple_unresolved_exact_name_candidates_are_ambiguous() -> None:
    source = source_for(
        fixture("synthetic_va_scc_search_results_multiple.html"),
        {
            "SYNTH001": fixture("synthetic_va_scc_entity_detail.html"),
            "SYNTH002": fixture("synthetic_va_scc_entity_detail_other.html"),
        },
    )

    result = source.enrich(
        identity(business_name="Blue Ridge Mechanical LLC", address=None)
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.legal_names == []
    assert result.principals == []


def test_va_scc_fuzzy_name_or_registered_agent_address_alone_does_not_establish() -> None:
    source = source_for(
        fixture("synthetic_va_scc_search_results.html"),
        {"SYNTH001": fixture("synthetic_va_scc_entity_detail_other.html")},
    )

    result = source.enrich(
        identity(
            business_name="Blue Ridge Mechanic LLC",
            address="100 Main St, Richmond, VA 23219",
        )
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.principals == []


def test_real_sanitized_scc_detail_markup_extracts_office_and_agent_evidence() -> None:
    fallback = _VASCCCandidate(
        entity_id="wrong-id",
        legal_name="wrong-name",
        status=None,
        principal_office_address=None,
        detail_url="https://example.invalid/fallback",
    )
    source_url = (
        "https://cis.scc.virginia.gov/EntitySearch/BusinessInformation"
        "?businessId=11004293"
    )

    candidate = _parse_entity_detail(
        fixture("sanitized_real_va_scc_detail_11004293.html"),
        source_url,
        fallback=fallback,
    )

    assert candidate.entity_id == "11004293"
    assert candidate.legal_name == "Maysteel Porter's, LLC"
    assert candidate.status == "Active"
    assert candidate.principal_office_address == (
        "116 W Jones St, Savannah, GA, 31401 - 4508, USA"
    )
    assert candidate.registered_agent_name == "CORPORATION SERVICE COMPANY"
    assert candidate.registered_office_address == (
        "100 Shockoe Slip Fl 2, Richmond, VA, 23219 - 4100, USA"
    )
    assert candidate.principals == []


def test_va_scc_reports_captcha_and_does_not_submit_search() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text='<script src="https://www.google.com/recaptcha/api.js"></script>',
            request=request,
        )

    source = VASCCRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )
    result = source.enrich(identity())

    assert result.status == "captcha_blocked"
    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", VA_SCC_SEARCH_URL)
    ]


def test_va_scc_accepts_normal_cookie_consent_before_checking_access() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == VA_SCC_COOKIE_URL:
            return httpx.Response(200, json={"status": "success"}, request=request)
        if len(requests) == 1:
            return httpx.Response(200, text="<h1>Cookie Consent</h1>", request=request)
        return httpx.Response(
            200,
            text='<script src="https://www.google.com/recaptcha/api.js"></script>',
            request=request,
        )

    source = VASCCRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )
    result = source.enrich(identity())

    assert result.status == "captcha_blocked"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
