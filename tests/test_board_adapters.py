from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import io
from pathlib import Path
from urllib.parse import parse_qs
import zipfile

import httpx

from app import board_adapters
from app.board_adapters import MHIC_BUSINESS_NAME_URL
from app.day2_models import BoardSelection, BoardSelectionResult, SearchKey
from app.board_selection import select_boards
from app.license_matching import match_candidate
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity
from app.search_keys import normalize_search_name


FIXTURES = Path(__file__).parent / "fixtures" / "license_sources"
FETCHED_AT = datetime(2026, 9, 27, 15, 30, tzinfo=timezone.utc)


def search_key(value: str) -> SearchKey:
    from app.search_keys import normalize_search_name

    return SearchKey(
        original_value=value,
        normalized_value=normalize_search_name(value),
        source_field="business_name",
    )


def selection(
    *,
    board_id: str,
    board_name: str,
    strategy: str,
    source_url: str,
    categories: list[str],
) -> BoardSelection:
    return BoardSelection(
        jurisdiction=(
            "TX" if board_id in {"TDLR", "TSBPE"}
            else "MD" if board_id in {"MHIC", "MD_ELECTRICIANS"}
            else board_id[:2]
        ),
        board_id=board_id,
        board_name=board_name,
        strategy=strategy,
        source_url=source_url,
        applicable_categories=categories,
    )


def run(
    selected: BoardSelection,
    handler: httpx.MockTransport,
    keys: list[SearchKey],
):
    client = httpx.Client(transport=handler)
    return LicenseSourceRunner(
        client=client,
        clock=lambda: FETCHED_AT,
        min_request_interval_seconds=0,
    ).search(
        BoardSelectionResult(selections=[selected]), keys
    )[0]


def cslb_xlsx_row(values: list[str]) -> bytes:
    headers = [
        "LicenseNumber", "BusinessName", "Address", "City", "State", "Zip",
        "PhoneNumber", "IssueDate", "ExpirationDate", "Classification", "Status",
    ]
    strings = headers + values
    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
    rows = []
    for row_number, offset in ((1, 0), (2, len(headers))):
        cells = "".join(
            f'<c r="{chr(65 + index)}{row_number}" t="s"><v>{offset + index}</v></c>'
            for index in range(len(headers))
        )
        rows.append(f'<row r="{row_number}">{cells}</row>')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        output.writestr(
            "xl/worksheets/sheet.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows)}</sheetData></worksheet>",
        )
    return archive.getvalue()


def test_successful_tdlr_retrieval_preserves_raw_candidates_and_provenance() -> None:
    payload = (FIXTURES / "tdlr.json").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data.texas.gov"
        assert request.url.params["$q"] == "Example Electric LLC"
        return httpx.Response(200, text=payload, request=request)

    result = run(
        selection(
            board_id="TDLR",
            board_name="Texas Department of Licensing and Regulation",
            strategy="tdlr_all_licenses_open_data",
            source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
            categories=["electrical", "hvac"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Electric LLC")],
    )

    assert result.search_status == "ok"
    assert [candidate.raw_license_status for candidate in result.candidates] == [None, None]
    assert [candidate.expiration_date for candidate in result.candidates] == [
        "03/31/2027",
        "08/15/2026",
    ]
    assert all(candidate.holder_name_role == "business" for candidate in result.candidates)
    assert all(candidate.fetched_at == FETCHED_AT for candidate in result.candidates)
    assert all("data.texas.gov/resource/7358-krk7.json" in candidate.evidence_url for candidate in result.candidates)
    assert result.candidates[0].source_reference == "TDLR All Licenses Socrata dataset 7358-krk7"


def test_dc_industrial_trades_adapter_uses_public_opla_search_and_filters_trade() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetLicenseSearchDetailsByFilter")
        form = parse_qs(request.content.decode())
        requests.append(form)
        return httpx.Response(
            200,
            json={
                "licenseSearchDetailsList": [
                    {
                        "licenseeName": "Ada Owner",
                        "licenseType": "Refrig/Air Cond Master Mechanic",
                        "licenseStatus": "Active",
                        "licenseNumber": "RM-SYNTH-1",
                        "initialIssueDate": "01/01/2024",
                        "licenseExpirationDate": "09/30/2028",
                        "discipline": "Industrial Trades",
                    },
                    {
                        "licenseeName": "Ada Owner",
                        "licenseType": "Steam Engineer, Class 1",
                        "licenseStatus": "Active",
                        "licenseNumber": "SE-SYNTH-1",
                    },
                ],
                "pageIndex": 1,
                "pageSize": 100,
                "recordCount": 2,
            },
            request=request,
        )

    result = run(
        selection(
            board_id="DC_INDUSTRIAL_TRADES",
            board_name="District of Columbia Board of Industrial Trades",
            strategy="dc_opla_industrial_trades",
            source_url=(
                "https://govservices.dcra.dc.gov/oplaportal/"
                "Home/GetLicenseSearchDetails"
            ),
            categories=["hvac"],
        ),
        httpx.MockTransport(handler),
        [search_key("Ada Owner")],
    )

    assert result.search_status == "ok"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.holder_name == "Ada Owner"
    assert candidate.holder_name_role == "person"
    assert candidate.raw_license_type == "Refrig/Air Cond Master Mechanic"
    assert candidate.raw_classification == "hvac"
    assert candidate.state == "DC"
    assert candidate.source_fields["discipline"] == "Industrial Trades"
    assert candidate.fetched_at == FETCHED_AT
    assert requests[0]["discipline"] == ["Industrial Trades"]
    assert requests[0]["licenseeName"] == ["Ada Owner"]


def test_dc_plumbing_search_acquires_exact_registry_named_electrical_and_hvac_rows() -> None:
    business = BusinessIdentity(
        business_name="Example Plumbing",
        phone="+12025550123",
        place_id="dc-place-1",
        states=["DC"],
        normalized_categories=["plumbing"],
    )
    plan = select_boards(business)
    selected = plan.selections[0]
    expanded_key = SearchKey(
        original_value="Example Plumbing Corporation",
        normalized_value=normalize_search_name("Example Plumbing Corporation"),
        source_field="legal_name",
        origin="registry",
        registry_name="DC Corporate Registration",
        entity_id="ENTITY-1",
        source_url="https://registry.example/entities/ENTITY-1",
    )
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        requests.append(form)
        return httpx.Response(
            200,
            json={
                "licenseSearchDetailsList": [
                    {
                        "licenseeName": "Example Plumbing Corporation",
                        "licenseType": "Electrical Contractor",
                        "licenseStatus": "Active",
                        "licenseNumber": "EL-SYNTH-1",
                    },
                    {
                        "licenseeName": "Example Plumbing Corporation",
                        "licenseType": "Refrigeration/Air Conditioning Contractor",
                        "licenseStatus": "Active",
                        "licenseNumber": "HVAC-SYNTH-1",
                    },
                    {
                        "licenseeName": "Unrelated Services LLC",
                        "licenseType": "Electrical Contractor",
                        "licenseStatus": "Active",
                        "licenseNumber": "EL-SYNTH-2",
                    },
                ],
                "pageIndex": 1,
                "pageSize": 100,
                "recordCount": 3,
            },
            request=request,
        )

    result = run(selected, httpx.MockTransport(handler), [expanded_key])

    assert selected.applicable_categories == ["plumbing", "electrical", "hvac"]
    assert requests[0]["licenseeName"] == ["Example Plumbing Corporation"]
    assert {item.raw_classification for item in result.candidates} == {
        "electrical",
        "hvac",
    }
    assert all(item.holder_name != "Unrelated Services LLC" for item in result.candidates)

    decisions = [match_candidate(business, item, [expanded_key]) for item in result.candidates]
    assert all(item.accepted for item in decisions)
    assert all(item.matched_identity_field == "legal_name" for item in decisions)
    unrelated = result.candidates[0].model_copy(
        update={"holder_name": "Unrelated Services LLC"}
    )
    assert not match_candidate(business, unrelated, [expanded_key]).accepted


def test_reachable_zero_record_source_is_not_found_not_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    result = run(
        selection(
            board_id="TDLR",
            board_name="TDLR",
            strategy="tdlr_all_licenses_open_data",
            source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
            categories=["electrical"],
        ),
        httpx.MockTransport(handler),
        [search_key("No Such Business")],
    )

    assert result.search_status == "not_found"
    assert result.candidates == []
    assert "not an unlicensed conclusion" in result.notes[0]


def test_network_failure_is_unreachable_not_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    result = run(
        selection(
            board_id="TDLR",
            board_name="TDLR",
            strategy="tdlr_all_licenses_open_data",
            source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
            categories=["electrical"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Electric")],
    )

    assert result.search_status == "unreachable"
    assert result.candidates == []


def test_success_response_with_wrong_tdlr_schema_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"message": "maintenance"}], request=request)

    result = run(
        selection(
            board_id="TDLR",
            board_name="TDLR",
            strategy="tdlr_all_licenses_open_data",
            source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
            categories=["electrical"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Electric")],
    )

    assert result.search_status == "unreachable"
    assert result.candidates == []
    assert "expected fields" in result.notes[0]


def test_html_success_response_is_unreachable_not_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>Maintenance page</body></html>",
            request=request,
        )

    result = run(
        selection(
            board_id="TSBPE",
            board_name="TSBPE",
            strategy="free_licensee_lists",
            source_url="https://tsbpe.texas.gov/free-licensee-list/",
            categories=["plumbing"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Plumbing")],
    )

    assert result.search_status == "unreachable"
    assert result.candidates == []
    assert "expected fields" in result.notes[0]


def test_mhic_captcha_is_reported_and_never_bypassed() -> None:
    captcha = (FIXTURES / "captcha.html").read_text(encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=captcha, request=request)

    result = run(
        selection(
            board_id="MHIC",
            board_name="Maryland Home Improvement Commission",
            strategy="mhic_public_query",
            source_url="https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_search/OP_search.cgi?calling_app=HIC::HIC_qselect",
            categories=["renovation"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Builders")],
    )

    assert result.search_status == "captcha_blocked"
    assert result.search_keys == [search_key("Example Builders")]
    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", MHIC_BUSINESS_NAME_URL)
    ]


def test_tsbpe_keeps_current_expired_and_suspended_rows() -> None:
    payload = (FIXTURES / "tsbpe.csv").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=payload,
            headers={"content-type": "text/csv"},
            request=request,
        )

    keys = [
        search_key("Example Plumbing LLC"),
        search_key("Example Plumbing Incorporated"),
        search_key("Example Plumbing Corporation"),
    ]
    result = run(
        selection(
            board_id="TSBPE",
            board_name="Texas State Board of Plumbing Examiners",
            strategy="free_licensee_lists",
            source_url="https://tsbpe.texas.gov/free-licensee-list/",
            categories=["plumbing"],
        ),
        httpx.MockTransport(handler),
        keys,
    )

    assert result.search_status == "ok"
    assert [candidate.raw_license_status for candidate in result.candidates] == [
        "Current",
        "Expired",
        "Suspended",
    ]
    assert result.candidates[0].holder_name_role == "person"
    assert result.candidates[0].issued_date == "03/06/1987"
    assert result.candidates[0].expiration_date == "11/30/2026"
    assert all(candidate.evidence_url == "https://tsbpe.texas.gov/download-csv/RMP/" for candidate in result.candidates)


def test_dpor_tab_delimited_candidate_extraction(monkeypatch) -> None:
    payload = (FIXTURES / "dpor.tsv").read_text(encoding="utf-8")
    url = "https://www.dpor.virginia.gov/official-2705b.txt"
    monkeypatch.setattr(board_adapters, "DPOR_LISTS", (("2705 B", url),))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, request=request)

    result = run(
        selection(
            board_id="DPOR",
            board_name="Virginia DPOR",
            strategy="dpor_regulant_lists",
            source_url="https://www.dpor.virginia.gov/RegulantLists",
            categories=["plumbing"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Services LLC")],
    )

    assert result.search_status == "ok"
    assert result.candidates[0].license_number == "2705000100"
    assert result.candidates[0].raw_classification == "PLB ELE HVA"
    assert result.candidates[0].holder_name_role == "business"
    assert result.candidates[0].issued_date == "01/15/2018"
    assert result.candidates[0].expiration_date == "12/31/2027"
    assert result.candidates[0].evidence_url == url


def test_cslb_official_portal_flow_extracts_master_csv_candidate() -> None:
    payload = (FIXTURES / "cslb.csv").read_text(encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            marker = str(calls)
            html = (
                f'<input type="hidden" name="__VIEWSTATE" value="view-{marker}">'
                f'<input type="hidden" name="__EVENTVALIDATION" value="event-{marker}">'
            )
            return httpx.Response(200, text=html, request=request)
        return httpx.Response(
            200,
            text=payload,
            headers={"content-type": "text/csv"},
            request=request,
        )

    result = run(
        selection(
            board_id="CSLB",
            board_name="California Contractors State License Board",
            strategy="cslb_license_master",
            source_url="https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList",
            categories=["renovation"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Builders LLC")],
    )

    assert calls == 3
    assert result.search_status == "ok"
    assert result.candidates[0].license_number == "123456"
    assert result.candidates[0].raw_license_status == "Inactive"
    assert result.candidates[0].issued_date == "01/15/2010"
    assert result.candidates[0].expiration_date == "06/30/2027"
    assert result.candidates[0].source_reference == "CSLB License Master CSV"


def test_cslb_direct_bulk_fallback_retries_timeout_parses_current_schema_and_caches() -> None:
    portal_url = "https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList"
    direct_calls = 0
    portal_calls = 0
    payload = "\n".join([
        "LicenseNo,BusinessName,BUS-NAME-2,FullBusinessName,MailingAddress,City,State,ZIPCode,BusinessPhone,BusinessType,IssueDate,ExpirationDate,PrimaryStatus,Classifications(s)",
        "1103922,PRIME PLUMBING AND DRAIN INC,,PRIME PLUMBING AND DRAIN INC,123 MAIN ST,ANTIOCH,CA,94509,9255034838,Corporation,01/02/2023,01/31/2027,Clear,C36",
        "1061107,TRIO HEATING AIR PLUMBING & ELECTRICAL,GDM GROUP,TRIO HEATING AIR PLUMBING & ELECTRICAL,456 FIRST ST,SAN JOSE,CA,95112,4158701360,Corporation,04/05/2020,04/30/2028,Clear,B|C10|C20|C36",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls, portal_calls
        if "DownLoadFile.ashx" in request.url.path:
            direct_calls += 1
            assert request.headers["referer"] == portal_url
            assert request.extensions["timeout"]["read"] == board_adapters.CSLB_MASTER_CSV_TIMEOUT_SECONDS
            if direct_calls == 1:
                raise httpx.ReadTimeout("synthetic first-attempt timeout", request=request)
            return httpx.Response(200, text=payload, headers={"content-type": "text/csv"}, request=request)
        portal_calls += 1
        if portal_calls < 3:
            return httpx.Response(
                200,
                text=(
                    f'<input type="hidden" name="__VIEWSTATE" value="view-{portal_calls}">'
                    f'<input type="hidden" name="__EVENTVALIDATION" value="event-{portal_calls}">'
                ),
                request=request,
            )
        return httpx.Response(200, text="Request Rejected", request=request)

    runner = LicenseSourceRunner(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
        min_request_interval_seconds=0,
    )
    selected = selection(
        board_id="CSLB",
        board_name="California Contractors State License Board",
        strategy="cslb_license_master",
        source_url=portal_url,
        categories=["plumbing"],
    )
    first = runner.search(
        BoardSelectionResult(selections=[selected]),
        [search_key("Prime Plumbing and Drain Inc")],
    )[0]
    second = runner.search(
        BoardSelectionResult(selections=[selected]),
        [search_key("Trio Heating Air Plumbing & Electrical")],
    )[0]

    assert portal_calls == 3
    assert direct_calls == 2
    assert first.search_status == "ok"
    assert first.candidates[0].license_number == "1103922"
    assert first.candidates[0].phone == "9255034838"
    assert first.candidates[0].raw_license_status == "Clear"
    assert first.candidates[0].raw_classification == "C36"
    assert first.candidates[0].issued_date == "01/02/2023"
    assert first.candidates[0].expiration_date == "01/31/2027"
    assert first.candidates[0].evidence_url == board_adapters.CSLB_MASTER_CSV_URL
    assert second.search_status == "ok"
    assert second.candidates[0].license_number == "1061107"


def test_cslb_exact_lookup_falls_back_to_official_classification_xlsx() -> None:
    portal_url = "https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList"
    master = "LicenseNo,BusinessName,PrimaryStatus\n1103922,PRIME PLUMBING,CLEAR\n"
    headers = [
        "LicenseNumber", "BusinessName", "Address", "City", "State", "Zip",
        "PhoneNumber", "IssueDate", "ExpirationDate", "Classification", "Status",
    ]
    row = [
        "629538", "CABRILLO PLUMBING & HEATING", "78 DORMAN AVE",
        "SAN FRANCISCO", "CA", "94124", "(415) 821 0560", "09/24/1991",
        "09/30/2027", "C20 | C36", "CLEAR",
    ]
    strings = headers + row
    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
    rows = []
    for row_number, offset in ((1, 0), (2, len(headers))):
        cells = "".join(
            f'<c r="{chr(65 + index)}{row_number}" t="s"><v>{offset + index}</v></c>'
            for index in range(len(headers))
        )
        rows.append(f'<row r="{row_number}">{cells}</row>')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        output.writestr(
            "xl/worksheets/sheet.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows)}</sheetData></worksheet>",
        )

    portal_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal portal_calls
        if str(request.url).rstrip("/") == board_adapters.CSLB_CLASSIFICATION_URL.rstrip("/"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    text='<input type="hidden" name="__VIEWSTATE" value="v"><input type="hidden" name="__EVENTVALIDATION" value="e">',
                    request=request,
                )
            form = parse_qs(request.content.decode())
            assert form["ctl00$MainContent$lbClassification"] == ["C-36"]
            return httpx.Response(
                200,
                content=archive.getvalue(),
                headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                request=request,
            )
        portal_calls += 1
        if portal_calls < 3:
            return httpx.Response(
                200,
                text=f'<input type="hidden" name="__VIEWSTATE" value="v{portal_calls}"><input type="hidden" name="__EVENTVALIDATION" value="e{portal_calls}">',
                request=request,
            )
        return httpx.Response(200, text=master, headers={"content-type": "text/csv"}, request=request)

    runner = LicenseSourceRunner(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
        min_request_interval_seconds=0,
    )
    selected = selection(
        board_id="CSLB", board_name="CSLB", strategy="cslb_license_master",
        source_url=portal_url, categories=["plumbing"],
    )
    clue = board_adapters.LicenseNumberEvidence(
        raw_text="CA License #629538", normalized_number="629538",
        likely_board_id="CSLB", license_family="contractor",
        page_url="https://example.test", fetched_at=FETCHED_AT,
        supporting_snippet="CA License #629538", verified_identity_place_id="place-1",
    )
    result = runner.search(
        BoardSelectionResult(selections=[selected]), [search_key("Cabrillo")], [clue]
    )[0]

    assert result.search_status == "ok", result.notes
    candidate = next(item for item in result.candidates if item.license_number == "629538")
    assert candidate.holder_name == "CABRILLO PLUMBING & HEATING"
    assert candidate.raw_classification == "C20 | C36"
    assert candidate.phone == "(415) 821 0560"
    assert candidate.source_reference == "CSLB classification download"
    assert candidate.discovery_evidence == [clue]


def test_cslb_classification_cache_is_keyed_by_requested_trade_set() -> None:
    plumbing_payload = cslb_xlsx_row([
        "C36-001", "PIPEWORKS LLC", "1 WATER ST", "FRESNO", "CA", "93721",
        "5595550101", "01/01/2020", "01/31/2028", "C36", "CLEAR",
    ])
    hvac_payload = cslb_xlsx_row([
        "C20-002", "AIRWORKS LLC", "2 AIR ST", "FRESNO", "CA", "93721",
        "5595550102", "01/01/2021", "01/31/2029", "C20", "CLEAR",
    ])
    requested_classifications: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<input type="hidden" name="__VIEWSTATE" value="v">'
                '<input type="hidden" name="__EVENTVALIDATION" value="e">',
                request=request,
            )
        classification = parse_qs(request.content.decode())[
            "ctl00$MainContent$lbClassification"
        ][0]
        requested_classifications.append(classification)
        payload = plumbing_payload if classification == "C-36" else hvac_payload
        return httpx.Response(
            200,
            content=payload,
            headers={
                "content-type":
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            },
            request=request,
        )

    adapter = board_adapters.CSLBAdapter(
        httpx.Client(transport=httpx.MockTransport(handler))
    )
    plumbing = selection(
        board_id="CSLB", board_name="CSLB", strategy="cslb_license_master",
        source_url="https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList",
        categories=["plumbing"],
    )
    hvac = plumbing.model_copy(update={"applicable_categories": ["hvac"]})
    plumbing_clue = board_adapters.LicenseNumberEvidence(
        raw_text="CSLB License #C36-001",
        normalized_number="C36-001",
        likely_board_id="CSLB",
        license_family="contractor",
        page_url="https://plumbing.example.test/licenses/",
        fetched_at=FETCHED_AT,
        supporting_snippet="CSLB License #C36-001",
    )
    hvac_clue = plumbing_clue.model_copy(update={
        "raw_text": "CSLB License #C20-002",
        "normalized_number": "C20-002",
        "page_url": "https://hvac.example.test/licenses/",
    })

    plumbing_candidates = adapter.lookup_by_license_number(
        plumbing, [plumbing_clue], FETCHED_AT
    )
    hvac_candidates = adapter.lookup_by_license_number(
        hvac, [hvac_clue], FETCHED_AT
    )
    plumbing_again = adapter.lookup_by_license_number(
        plumbing, [plumbing_clue], FETCHED_AT
    )

    assert [item.license_number for item in plumbing_candidates] == ["C36-001"]
    assert [item.license_number for item in hvac_candidates] == ["C20-002"]
    assert [item.license_number for item in plumbing_again] == ["C36-001"]
    assert requested_classifications == ["C-36", "C-20"]


def test_cslb_name_search_falls_back_to_classification_xlsx_after_truncated_master() -> None:
    headers = [
        "LicenseNumber", "BusinessName", "Address", "City", "State", "Zip",
        "PhoneNumber", "IssueDate", "ExpirationDate", "Classification", "Status",
    ]
    values = headers + [
        "1103922", "PRIME PLUMBING AND DRAIN INC", "1409 JACOBSEN ST",
        "ANTIOCH", "CA", "94509", "(925) 503 4838", "04/25/2023",
        "04/30/2027", "C36", "CLEAR",
    ]
    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in values)
    row_xml = []
    for row_number, offset in ((1, 0), (2, len(headers))):
        cells = "".join(
            f'<c r="{chr(65 + index)}{row_number}" t="s"><v>{offset + index}</v></c>'
            for index in range(len(headers))
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        output.writestr(
            "xl/worksheets/sheet.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>",
        )

    master_attempts = 0
    portal_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal master_attempts, portal_calls
        if "DownLoadFile.ashx" in request.url.path:
            master_attempts += 1
            raise httpx.ReadTimeout("synthetic truncated master transfer", request=request)
        if str(request.url).rstrip("/") == board_adapters.CSLB_CLASSIFICATION_URL.rstrip("/"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    text='<input type="hidden" name="__VIEWSTATE" value="v"><input type="hidden" name="__EVENTVALIDATION" value="e">',
                    request=request,
                )
            return httpx.Response(
                200,
                content=archive.getvalue(),
                headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                request=request,
            )
        portal_calls += 1
        if portal_calls < 3:
            return httpx.Response(
                200,
                text=f'<input type="hidden" name="__VIEWSTATE" value="p{portal_calls}"><input type="hidden" name="__EVENTVALIDATION" value="e{portal_calls}">',
                request=request,
            )
        return httpx.Response(200, text="Request Rejected", request=request)

    selected = selection(
        board_id="CSLB", board_name="CSLB", strategy="cslb_license_master",
        source_url="https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList",
        categories=["plumbing"],
    )
    result = run(
        selected,
        httpx.MockTransport(handler),
        [search_key("Prime Plumbing and Drain Inc")],
    )

    assert master_attempts == board_adapters.CSLB_MASTER_CSV_ATTEMPTS == 2
    assert result.search_status == "ok", result.notes
    assert len(result.candidates) == 1
    assert result.candidates[0].license_number == "1103922"
    assert result.candidates[0].evidence_url == board_adapters.CSLB_CLASSIFICATION_URL
    assert result.candidates[0].raw_classification == "C36"


def test_candidate_model_does_not_claim_an_identity_match() -> None:
    payload = (FIXTURES / "tdlr.json").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, request=request)

    result = run(
        selection(
            board_id="TDLR",
            board_name="TDLR",
            strategy="tdlr_all_licenses_open_data",
            source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
            categories=["electrical"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Electric LLC")],
    )

    fields = result.candidates[0].model_dump().keys()
    assert "matched" not in fields
    assert "accepted" not in fields
    assert "match_confidence" not in fields
    assert "no identity match was accepted" in result.notes[0]


def test_partial_name_row_is_retained_as_a_candidate() -> None:
    payload = (FIXTURES / "tsbpe.csv").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, request=request)

    result = run(
        selection(
            board_id="TSBPE",
            board_name="TSBPE",
            strategy="free_licensee_lists",
            source_url="https://tsbpe.texas.gov/free-licensee-list/",
            categories=["plumbing"],
        ),
        httpx.MockTransport(handler),
        [search_key("Example Plumbing")],
    )

    assert result.search_status == "ok"
    assert len(result.candidates) == 3
