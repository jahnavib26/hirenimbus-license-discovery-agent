from datetime import datetime, timezone

import pytest

from app.day2_models import BoardSearchResult, CandidateLicenseRecord
from app.license_matching import assemble_day2_result
from app.models import BusinessIdentity


FETCHED_AT = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def identity() -> BusinessIdentity:
    return BusinessIdentity(
        business_name="Example Services LLC",
        phone="+15124567890",
        place_id="place-1",
    )


def candidate(raw_status: str = "Expired") -> CandidateLicenseRecord:
    return CandidateLicenseRecord(
        board_id="TEST",
        board_name="Test Licensing Board",
        source_strategy="official_test_source",
        license_number="LIC-100",
        holder_name="EXAMPLE SERVICES L.L.C.",
        holder_name_role="business",
        raw_license_status=raw_status,
        issued_date="01/15/2010",
        expiration_date="06/30/2027",
        evidence_url="https://official.example/licenses/LIC-100",
        source_reference="official row LIC-100",
        fetched_at=FETCHED_AT,
    )


@pytest.mark.parametrize("raw_status", ["Expired", "Suspended", "Inactive"])
def test_inactive_standing_does_not_prevent_identity_acceptance(
    raw_status: str,
) -> None:
    source_result = BoardSearchResult(
        board_id="TEST",
        board_name="Test Licensing Board",
        jurisdiction="TX",
        strategy="official_test_source",
        search_status="ok",
        source_url="https://official.example/licenses",
        fetched_at=FETCHED_AT,
        candidates=[candidate(raw_status)],
    )

    result = assemble_day2_result(identity(), [source_result])

    accepted = result.accepted_licenses[0]
    assert accepted.raw_license_status == raw_status
    assert accepted.normalized_status == "inactive"
    assert accepted.match_confidence == "medium"


def test_final_result_preserves_evidence_timestamp_and_match_confidence() -> None:
    source_result = BoardSearchResult(
        board_id="TEST",
        board_name="Test Licensing Board",
        jurisdiction="TX",
        strategy="official_test_source",
        search_status="ok",
        source_url="https://official.example/licenses",
        fetched_at=FETCHED_AT,
        candidates=[candidate("Current")],
    )

    result = assemble_day2_result(identity(), [source_result])
    accepted = result.accepted_licenses[0]

    assert accepted.evidence_url == "https://official.example/licenses/LIC-100"
    assert accepted.source_reference == "official row LIC-100"
    assert accepted.fetched_at == FETCHED_AT
    assert accepted.raw_license_status == "Current"
    assert accepted.normalized_status == "active"
    assert accepted.issued_date == "01/15/2010"
    assert accepted.expiration_date == "06/30/2027"
    assert accepted.match_confidence == "medium"


def test_board_search_statuses_remain_independent_and_distinct() -> None:
    statuses = ["not_found", "unreachable", "captcha_blocked", "skipped"]
    source_results = [
        BoardSearchResult(
            board_id=f"BOARD-{index}",
            board_name=f"Board {index}",
            jurisdiction="TX",
            strategy="test",
            search_status=status,
            fetched_at=FETCHED_AT,
        )
        for index, status in enumerate(statuses)
    ]

    result = assemble_day2_result(identity(), source_results)

    assert [board.search_status for board in result.board_results] == statuses
    assert result.accepted_licenses == []
    assert any("does not mean the business is unlicensed" in note for note in result.notes)


def test_ok_status_remains_ok_when_candidates_fail_identity_matching() -> None:
    unrelated = candidate()
    unrelated.holder_name = "Unrelated Company"
    source_result = BoardSearchResult(
        board_id="TEST",
        board_name="Test Licensing Board",
        jurisdiction="TX",
        strategy="official_test_source",
        search_status="ok",
        fetched_at=FETCHED_AT,
        candidates=[unrelated],
    )

    result = assemble_day2_result(identity(), [source_result])

    assert result.board_results[0].search_status == "ok"
    assert result.board_results[0].accepted_licenses == []
    assert result.board_results[0].match_decisions[0].accepted is False
    assert any("none met" in note for note in result.board_results[0].notes)


def test_no_result_never_claims_business_is_unlicensed() -> None:
    source_result = BoardSearchResult(
        board_id="TEST",
        board_name="Test Licensing Board",
        jurisdiction="TX",
        strategy="official_test_source",
        search_status="not_found",
        fetched_at=FETCHED_AT,
        notes=[
            "The official source was reached but returned no candidate rows; this is not an unlicensed conclusion."
        ],
    )

    result = assemble_day2_result(identity(), [source_result])

    assert result.accepted_licenses == []
    assert any("does not mean the business is unlicensed" in note for note in result.notes)
    assert not any(note == "The business is unlicensed." for note in result.notes)
