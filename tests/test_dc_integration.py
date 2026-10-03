from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from app.cache import JsonLastGoodCache
from app.dc_registry import DCDLCPRegistrySource
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity, IdentityLookupResult, NormalizedPhone
from app.pipeline import LicensePipeline


FETCHED_AT = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
FILE_NUMBER = "C-SYNTH-DC-1"


class Resolver:
    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        return IdentityLookupResult(
            found=True,
            confidence="high",
            input_phone=NormalizedPhone(e164="+12025551234"),
            identity=BusinessIdentity(
                business_name="Capitol Mechanical LLC",
                phone="+12025551234",
                place_id="dc-place-1",
                states=["DC"],
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


def _arcgis(request: httpx.Request, rows: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "features": [{"attributes": row} for row in rows],
            "exceededTransferLimit": False,
        },
        request=request,
    )


def _pipeline(
    tmp_path, holder_name: str | list[str]
) -> tuple[LicensePipeline, RecordingRunner, list[str]]:
    holder_names = [holder_name] if isinstance(holder_name, str) else holder_name
    corporate = {
        "FILE_NUMBER": FILE_NUMBER,
        "ENTITY_STATUS": "Active - In Good Standing",
        "BUSINESS_NAME": "Capitol Mechanical LLC",
        "BUSNIESS_ADDRESS_LINE1": None,
        "BUSNIESS_ADDRESS_LINE2": None,
        "BUSNIESS_ADDRESS_LINE3": None,
        "BUSNIESS_ADDRESS_LINE4": None,
        "BUSINESS_CITY": None,
        "BUSINESS_STATE": None,
        "ZIPCODE": None,
        "BUSINESS_COUNTRY": None,
        "RA_NAME": "Registered Agent Services Inc.",
        "RA_ADDRESS1": "200 Agent Street NW",
        "RA_ADDRESS2": None,
        "RA_ADDRESS3": None,
        "RA_ADDRESS4": None,
        "RA_CITY": "Washington",
        "RA_STATE": "DC",
        "RA_ZIPCODE": "20002",
    }
    trade = {
        "TRADE_NAME": "Capitol Comfort",
        "TRADENAME_STATUS": "Active Trade Name",
        "FILE_NUMBER": "TN-SYNTH-DC-1",
        "INITIAL_FILENUMBER": FILE_NUMBER,
    }
    owner = {
        "NAME": "Ada Owner",
        "BUSINESSNAME": "Capitol Mechanical LLC",
        "INITIALFILENUMBER": FILE_NUMBER,
        "STATUS": "Active - In Good Standing",
    }
    board_searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "maps2.dcgis.dc.gov":
            where = request.url.params["where"]
            if request.url.path.endswith("/0/query"):
                return _arcgis(request, [corporate])
            if request.url.path.endswith("/1/query"):
                return _arcgis(
                    request,
                    [] if "UPPER(TRADE_NAME)" in where else [trade],
                )
            if request.url.path.endswith("/2/query"):
                assert FILE_NUMBER in where
                return _arcgis(request, [owner])
        if request.url.path.endswith("/GetLicenseSearchDetailsByFilter"):
            form = parse_qs(request.content.decode())
            searched_name = form["licenseeName"][0]
            board_searches.append(searched_name)
            rows = []
            if searched_name == "Ada Owner":
                rows.extend(
                    {
                        "licenseeName": returned_holder,
                        "licenseType": "Plumber Master",
                        "licenseStatus": "Active",
                        "licenseNumber": f"PM-SYNTH-DC-{index}",
                        "initialIssueDate": "01/01/2024",
                        "licenseExpirationDate": "03/31/2028",
                        "discipline": "Industrial Trades",
                    }
                    for index, returned_holder in enumerate(holder_names, start=1)
                )
            return httpx.Response(
                200,
                json={
                    "licenseSearchDetailsList": rows,
                    "pageIndex": 1,
                    "pageSize": 100,
                    "recordCount": len(rows),
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = DCDLCPRegistrySource(client, clock=lambda: FETCHED_AT)
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
        registry_sources=[registry],
        cache=JsonLastGoodCache(tmp_path / "dc-results.json"),
    )
    return pipeline, runner, board_searches


def test_dc_registry_principal_reaches_opla_and_unique_exact_holder_is_accepted(
    tmp_path,
) -> None:
    pipeline, runner, board_searches = _pipeline(tmp_path, "Ada Owner")

    result = pipeline.find_licenses("202-555-1234")

    assert result.pipeline_status == "complete"
    assert result.registry_results[0].established_entity_id == FILE_NUMBER
    assert [name.value for name in result.registry_results[0].dbas] == [
        "Capitol Comfort"
    ]
    assert [(name.value, name.role) for name in result.registry_results[0].principals] == [
        ("Ada Owner", "beneficial owner")
    ]
    assert runner.search_keys == result.search_keys
    assert [key.original_value for key in result.search_keys] == [
        "Capitol Mechanical LLC",
        "Capitol Mechanical LLC",
        "Capitol Comfort",
        "Ada Owner",
    ]
    assert board_searches == [
        "Capitol Mechanical LLC",
        "Capitol Comfort",
        "Ada Owner",
    ]
    board = result.license_result.board_results[0]
    assert board.board_id == "DC_INDUSTRIAL_TRADES"
    assert board.search_status == "ok"
    assert board.match_decisions[0].accepted is True
    assert board.match_decisions[0].match_confidence == "medium"
    assert board.match_decisions[0].matched_identity_field == "owner_principal"
    assert result.license_result.accepted_licenses[0].license_number == (
        "PM-SYNTH-DC-1"
    )


def test_dc_fuzzy_or_partial_principal_holder_remains_rejected(tmp_path) -> None:
    pipeline, _, _ = _pipeline(tmp_path, "Ada Owner Services")

    result = pipeline.find_licenses("202-555-1234")

    decision = result.license_result.board_results[0].match_decisions[0]
    assert decision.accepted is False
    assert decision.name_relationship == "partial"
    assert result.license_result.accepted_licenses == []


def test_dc_duplicate_exact_person_holders_remain_unresolved(tmp_path) -> None:
    pipeline, _, _ = _pipeline(tmp_path, ["Ada Owner", "ADA OWNER"])

    result = pipeline.find_licenses("202-555-1234")

    decisions = result.license_result.board_results[0].match_decisions
    assert len(decisions) == 2
    assert all(decision.accepted is False for decision in decisions)
    assert result.license_result.accepted_licenses == []
