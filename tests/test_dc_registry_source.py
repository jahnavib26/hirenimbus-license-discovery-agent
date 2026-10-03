from datetime import datetime, timezone

import httpx

from app.dc_registry import (
    DC_BENEFICIAL_OWNER_QUERY_URL,
    DC_CORPORATE_QUERY_URL,
    DC_PAGE_SIZE,
    DC_TRADE_NAME_QUERY_URL,
    DCDLCPRegistrySource,
)
from app.models import BusinessIdentity
from app.search_keys import generate_expanded_search_keys


FETCHED_AT = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
FILE_NUMBER = "C-SYNTH-001"


def identity(**overrides: object) -> BusinessIdentity:
    values: dict[str, object] = {
        "business_name": "Capitol Comfort",
        "phone": "+12025550100",
        "address": "100 Main St NW, Washington, DC 20001, USA",
        "place_id": "dc-place-1",
        "states": ["DC"],
        "normalized_categories": ["hvac"],
    }
    values.update(overrides)
    return BusinessIdentity(**values)


def corporate(file_number: str = FILE_NUMBER) -> dict[str, object]:
    return {
        "FILE_NUMBER": file_number,
        "ENTITY_STATUS": "Active - In Good Standing",
        "BUSINESS_NAME": "Capitol Mechanical LLC",
        "BUSNIESS_ADDRESS_LINE1": "100 Main Street NW",
        "BUSNIESS_ADDRESS_LINE2": None,
        "BUSNIESS_ADDRESS_LINE3": None,
        "BUSNIESS_ADDRESS_LINE4": None,
        "BUSINESS_CITY": "Washington",
        "BUSINESS_STATE": "DC",
        "ZIPCODE": "20001",
        "BUSINESS_COUNTRY": "USA",
        "RA_NAME": "Registered Agent Services Inc.",
        "RA_ADDRESS1": "200 Agent Street NW",
        "RA_ADDRESS2": None,
        "RA_ADDRESS3": None,
        "RA_ADDRESS4": None,
        "RA_CITY": "Washington",
        "RA_STATE": "DC",
        "RA_ZIPCODE": "20002",
    }


def trade(file_number: str = FILE_NUMBER) -> dict[str, object]:
    return {
        "TRADE_NAME": "Capitol Comfort",
        "EFFECTIVE_DATE": 1700000000000,
        "TRADENAME_STATUS": "Active Trade Name",
        "FILE_NUMBER": "TN-SYNTH-001",
        "INITIAL_FILENUMBER": file_number,
    }


def owner(file_number: str = FILE_NUMBER) -> dict[str, object]:
    return {
        "NAME": "Ada Owner",
        "ADDRESS": "100 Main Street NW, Washington, DC 20001, USA",
        "BUSINESSNAME": "Capitol Mechanical LLC",
        "INITIALFILENUMBER": file_number,
        "STATUS": "Active - In Good Standing",
    }


def arcgis_response(request: httpx.Request, rows: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "features": [{"attributes": row} for row in rows],
            "exceededTransferLimit": False,
        },
        request=request,
    )


def test_dc_corporate_and_trade_name_join_establishes_official_alias() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        where = request.url.params["where"]
        if request.url.path.endswith("/0/query"):
            rows = [] if "BUSINESS_NAME" in where else [corporate()]
            return arcgis_response(request, rows)
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [trade()])
        if request.url.path.endswith("/2/query"):
            return arcgis_response(request, [])
        raise AssertionError(f"Unexpected request {request.url}")

    source = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )

    result = source.enrich(identity())

    assert result.status == "ok"
    assert result.established_entity_id == FILE_NUMBER
    assert result.established_entity_name == "Capitol Mechanical LLC"
    assert result.principal_office_address == (
        "100 Main Street NW, Washington, DC, 20001, USA"
    )
    assert [name.value for name in result.dbas] == ["Capitol Comfort"]
    assert result.dbas[0].role == "current trade name"
    assert result.dbas[0].raw_field == "TRADE_NAME linked by INITIAL_FILENUMBER"
    assert result.dbas[0].source_url and "FeatureServer/1/query" in result.dbas[0].source_url
    assert any(item.kind == "exact_trade_name_relationship" for item in result.evidence)
    agent = next(item for item in result.evidence if item.kind == "registered_agent")
    assert agent.observed["RA_NAME"] == "Registered Agent Services Inc."
    assert result.principals == []
    assert result.fetched_at == FETCHED_AT
    assert any("FILE_NUMBER" in request.url.params["where"] for request in requests)
    assert all(request.url.params["outFields"] != "*" for request in requests)
    corporate_fields = set(next(
        request.url.params["outFields"].split(",")
        for request in requests if request.url.path == httpx.URL(DC_CORPORATE_QUERY_URL).path
    ))
    assert {"FILE_NUMBER", "BUSINESS_NAME", "ENTITY_STATUS", "RA_NAME"} <= corporate_fields
    trade_fields = set(next(
        request.url.params["outFields"].split(",")
        for request in requests if request.url.path == httpx.URL(DC_TRADE_NAME_QUERY_URL).path
    ))
    assert {"TRADE_NAME", "INITIAL_FILENUMBER", "TRADENAME_STATUS"} <= trade_fields
    owner_fields = set(next(
        request.url.params["outFields"].split(",")
        for request in requests if request.url.path == httpx.URL(DC_BENEFICIAL_OWNER_QUERY_URL).path
    ))
    assert owner_fields == {"NAME", "INITIALFILENUMBER", "STATUS"}
    assert all(
        request.url.params["resultRecordCount"] == str(DC_PAGE_SIZE)
        for request in requests
    )


def test_historical_trade_name_alone_cannot_establish_corporate_entity() -> None:
    historical_trade = {**trade(), "TRADENAME_STATUS": "Expired"}

    def handler(request: httpx.Request) -> httpx.Response:
        where = request.url.params["where"]
        if request.url.path.endswith("/0/query"):
            return arcgis_response(
                request, [] if "BUSINESS_NAME" in where else [corporate()]
            )
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [historical_trade])
        if request.url.path.endswith("/2/query"):
            return arcgis_response(request, [])
        raise AssertionError(f"Unexpected request {request.url}")

    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity(address=None))

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.dbas == []
    assert any(
        item.kind == "historical_trade_name_relationship_evidence_only"
        for item in result.evidence
    )


def test_independently_established_entity_contributes_historical_dba_key() -> None:
    historical_trade = {**trade(), "TRADENAME_STATUS": "Inactive - Cancelled"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/0/query"):
            return arcgis_response(request, [corporate()])
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [historical_trade])
        if request.url.path.endswith("/2/query"):
            return arcgis_response(request, [])
        raise AssertionError(f"Unexpected request {request.url}")

    base = identity(business_name="Capitol Mechanical LLC", address=None)
    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])
    historical_key = next(
        key for key in expanded if key.original_value == "Capitol Comfort"
    )

    assert result.status == "ok"
    assert result.dbas[0].role == "historical trade name"
    assert "Inactive - Cancelled" in " ".join(result.dbas[0].notes)
    assert historical_key.source_field == "dba"
    assert historical_key.origin == "registry"
    assert historical_key.registry_role == "historical trade name"


def test_transferred_trade_name_is_evidence_only_and_never_a_key() -> None:
    transferred_trade = {**trade(), "TRADENAME_STATUS": "Transferred"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/0/query"):
            return arcgis_response(request, [corporate()])
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [transferred_trade])
        if request.url.path.endswith("/2/query"):
            return arcgis_response(request, [])
        raise AssertionError(f"Unexpected request {request.url}")

    base = identity(business_name="Capitol Mechanical LLC", address=None)
    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])

    assert result.status == "ok"
    assert result.dbas == []
    assert all(key.original_value != "Capitol Comfort" for key in expanded)
    transferred_evidence = next(
        item
        for item in result.evidence
        if item.kind == "linked_trade_name"
        and item.observed["TRADENAME_STATUS"] == "Transferred"
    )
    assert "evidence only" in " ".join(transferred_evidence.notes)


def test_dc_beneficial_owner_join_requires_established_file_number() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        where = request.url.params["where"]
        if request.url.path.endswith("/0/query"):
            return arcgis_response(request, [corporate()])
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [])
        if request.url.path.endswith("/2/query"):
            assert FILE_NUMBER in where
            return arcgis_response(request, [owner()])
        raise AssertionError(f"Unexpected request {request.url}")

    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity(business_name="Capitol Mechanical LLC", address=None))

    assert result.status == "ok"
    assert [(name.value, name.role) for name in result.principals] == [
        ("Ada Owner", "beneficial owner")
    ]
    assert result.principals[0].entity_id == FILE_NUMBER
    assert result.principals[0].source_url and "FeatureServer/2/query" in result.principals[0].source_url
    evidence = next(
        item for item in result.evidence if item.kind == "linked_beneficial_owner"
    )
    assert evidence.observed["INITIALFILENUMBER"] == FILE_NUMBER


def test_non_active_corporation_keeps_exactly_linked_last_reported_owner() -> None:
    inactive_corporate = {**corporate(), "ENTITY_STATUS": "Revoked"}
    inactive_owner = {**owner(), "STATUS": "Revoked"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/0/query"):
            return arcgis_response(request, [inactive_corporate])
        if request.url.path.endswith("/1/query"):
            return arcgis_response(request, [])
        if request.url.path.endswith("/2/query"):
            return arcgis_response(request, [inactive_owner])
        raise AssertionError(f"Unexpected request {request.url}")

    base = identity(business_name="Capitol Mechanical LLC", address=None)
    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])
    owner_key = next(key for key in expanded if key.original_value == "Ada Owner")

    assert result.status == "ok"
    assert result.entity_status == "Revoked"
    assert result.principals[0].role == "beneficial owner"
    assert result.principals[0].notes[0] == (
        "last reported; relationship currentness unavailable"
    )
    assert "Revoked" in " ".join(result.principals[0].notes)
    assert owner_key.source_field == "owner_principal"
    assert owner_key.registry_role == "beneficial owner"


def test_dc_multiple_unresolved_exact_legal_names_are_ambiguous() -> None:
    second = {**corporate("C-SYNTH-002"), "BUSNIESS_ADDRESS_LINE1": None}

    def handler(request: httpx.Request) -> httpx.Response:
        where = request.url.params["where"]
        if request.url.path.endswith("/0/query") and "BUSINESS_NAME" in where:
            return arcgis_response(request, [corporate(), second])
        if request.url.path.endswith(("/1/query", "/2/query")):
            return arcgis_response(request, [])
        raise AssertionError(f"Unexpected request {request.url}")

    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity(business_name="Capitol Mechanical LLC", address=None))

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.legal_names == []
    assert result.dbas == []
    assert result.principals == []
    expanded = generate_expanded_search_keys(
        identity(business_name="Capitol Mechanical LLC", address=None), [result]
    )
    assert all(key.origin == "base_identity" for key in expanded)


def test_dc_query_paginates_using_actual_page_length_and_selected_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = request.url.params["resultOffset"]
        row = {"FILE_NUMBER": "ENTITY-1" if offset == "0" else "ENTITY-2"}
        return httpx.Response(
            200,
            json={
                "features": [{"attributes": row}],
                "exceededTransferLimit": offset == "0",
            },
            request=request,
        )

    source = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )
    records = source._query(
        DC_CORPORATE_QUERY_URL, "1=1", required_fields=("FILE_NUMBER",)
    )

    assert [record.fields["FILE_NUMBER"] for record in records] == ["ENTITY-1", "ENTITY-2"]
    assert [request.url.params["resultOffset"] for request in requests] == ["0", "1"]
    assert all(request.url.params["resultRecordCount"] == str(DC_PAGE_SIZE) for request in requests)
    assert all(request.url.params["outFields"] == "FILE_NUMBER" for request in requests)


def test_dc_query_retries_one_read_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("synthetic read timeout", request=request)
        return arcgis_response(request, [{"FILE_NUMBER": "ENTITY-1"}])

    source = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    )
    records = source._query(
        DC_CORPORATE_QUERY_URL, "1=1", required_fields=("FILE_NUMBER",)
    )

    assert calls == 2
    assert records[0].fields["FILE_NUMBER"] == "ENTITY-1"


def test_dc_query_does_not_retry_permanent_http_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="maintenance", request=request)

    result = DCDLCPRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity())

    assert result.status == "unreachable"
    assert calls == 1
