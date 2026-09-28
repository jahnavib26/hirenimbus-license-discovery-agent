from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.board_selection import select_boards
from app.license_matching import assemble_day2_result
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity
from app.search_keys import generate_search_keys


FIXTURES = Path(__file__).parent / "fixtures" / "license_sources"
FETCHED_AT = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)


def test_day2_pipeline_from_board_selection_through_final_result() -> None:
    identity = BusinessIdentity(
        business_name="Example Plumbing LLC",
        phone="+15124567890",
        place_id="place-1",
        states=["TX"],
        normalized_categories=["plumbing"],
    )
    payload = (FIXTURES / "tsbpe.csv").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://tsbpe.texas.gov/download-csv/RMP/"
        return httpx.Response(200, text=payload, request=request)

    plan = select_boards(identity)
    search_keys = generate_search_keys(identity)
    source_results = LicenseSourceRunner(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
        min_request_interval_seconds=0,
    ).search(plan, search_keys)
    result = assemble_day2_result(identity, source_results)

    assert [selection.board_id for selection in plan.selections] == ["TSBPE"]
    assert [key.original_value for key in search_keys] == ["Example Plumbing LLC"]
    assert source_results[0].search_status == "ok"
    assert len(source_results[0].candidates) == 2
    assert len(result.board_results[0].match_decisions) == 2
    assert [decision.accepted for decision in result.board_results[0].match_decisions] == [
        True,
        False,
    ]
    assert result.board_results[0].match_decisions[1].name_relationship == "fuzzy"

    accepted = result.accepted_licenses[0]
    assert accepted.license_number == "1001"
    assert accepted.matched_candidate_field == "source_fields.PLUMB_COMPANY"
    assert accepted.matched_candidate_role == "business"
    assert accepted.match_confidence == "medium"
    assert accepted.normalized_status == "active"
    assert accepted.issued_date == "03/06/1987"
    assert accepted.expiration_date == "11/30/2026"
    assert accepted.evidence_url == "https://tsbpe.texas.gov/download-csv/RMP/"
    assert accepted.fetched_at == FETCHED_AT
