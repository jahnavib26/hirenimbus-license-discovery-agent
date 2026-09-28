from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import board_adapters
from app.board_adapters import MHIC_BUSINESS_NAME_URL
from app.day2_models import BoardSelection, BoardSelectionResult, SearchKey
from app.license_sources import LicenseSourceRunner


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
        jurisdiction="TX" if board_id in {"TDLR", "TSBPE"} else board_id[:2],
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
    assert result.candidates[0].license_number == "27 05 000100"
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
