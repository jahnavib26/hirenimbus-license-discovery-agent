from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.day2_models import BoardSearchResult, CandidateLicenseRecord
from app.license_matching import (
    assemble_day2_result,
    match_candidate,
    normalize_license_status,
)
from app.models import BusinessIdentity


FETCHED_AT = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)


def identity(**overrides: object) -> BusinessIdentity:
    values: dict[str, object] = {
        "business_name": "Example Services",
        "phone": "+15124567890",
        "place_id": "place-1",
    }
    values.update(overrides)
    return BusinessIdentity(**values)


def candidate(**overrides: object) -> CandidateLicenseRecord:
    values: dict[str, object] = {
        "board_id": "TEST",
        "board_name": "Test Licensing Board",
        "source_strategy": "test_source",
        "license_number": "LIC-100",
        "holder_name": "Example Services",
        "holder_name_role": "business",
        "evidence_url": "https://official.example/licenses/LIC-100",
        "source_reference": "official test row 100",
        "fetched_at": FETCHED_AT,
    }
    values.update(overrides)
    return CandidateLicenseRecord(**values)


def test_exact_normalized_legal_name_match_is_accepted() -> None:
    decision = match_candidate(
        identity(legal_name="Example Holdings, L.L.C.", business_name=None),
        candidate(holder_name="EXAMPLE HOLDINGS LLC"),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "medium"
    assert decision.name_relationship == "exact_normalized"
    assert decision.matched_identity_field == "legal_name"


@pytest.mark.parametrize(
    ("identity_values", "expected_field"),
    [
        ({"dba": "Example Electric", "business_name": None}, "dba"),
        ({"business_name": "Example Public Name"}, "business_name"),
    ],
)
def test_dba_or_public_business_name_match_is_accepted_when_established(
    identity_values: dict[str, object], expected_field: str
) -> None:
    matched_name = identity_values[expected_field]

    decision = match_candidate(
        identity(**identity_values),
        candidate(holder_name=str(matched_name)),
    )

    assert decision.accepted is True
    assert decision.matched_identity_field == expected_field


def test_board_associated_company_name_can_match_established_business_name() -> None:
    decision = match_candidate(
        identity(business_name="Example Plumbing LLC"),
        candidate(
            holder_name="Ada Owner",
            source_fields={"PLUMB_COMPANY": "EXAMPLE PLUMBING L.L.C."},
        ),
    )

    assert decision.accepted is True
    assert decision.matched_identity_field == "business_name"
    assert decision.matched_candidate_field == "source_fields.PLUMB_COMPANY"


def test_owner_match_is_considered_only_when_day1_established_owner() -> None:
    absent_owner = match_candidate(
        identity(business_name="Different Business", owner_principal=None),
        candidate(holder_name="Ada Owner"),
    )
    established_owner = match_candidate(
        identity(
            business_name="Different Business",
            owner_principal="Ada Owner",
        ),
        candidate(holder_name="Ada Owner", phone="512-456-7890"),
    )

    assert absent_owner.accepted is False
    assert absent_owner.matched_identity_field is None
    assert established_owner.accepted is True
    assert established_owner.match_confidence == "high"
    assert established_owner.matched_identity_field == "owner_principal"


def test_ambiguous_owner_only_match_is_not_blindly_accepted() -> None:
    decision = match_candidate(
        identity(business_name="Different Business", owner_principal="John Smith"),
        candidate(holder_name="JOHN SMITH"),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert "ambiguous" in decision.notes[0]


def test_person_side_name_uses_conservative_policy_for_business_identity() -> None:
    decision = match_candidate(
        identity(business_name="Ada Owner", owner_principal=None),
        candidate(holder_name="Ada Owner", holder_name_role="person"),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert decision.matched_identity_field == "business_name"
    assert decision.matched_candidate_role == "person"
    assert "person/owner" in decision.notes[0]


def test_source_owner_name_does_not_become_a_business_name_match() -> None:
    decision = match_candidate(
        identity(business_name="Ada Owner", owner_principal=None),
        candidate(
            holder_name="Different Company",
            holder_name_role="business",
            source_fields={"OWNER_NAME": "Ada Owner"},
        ),
    )

    assert decision.accepted is False
    assert decision.matched_candidate_field == "source_fields.OWNER_NAME"
    assert decision.matched_candidate_role == "person"


@pytest.mark.parametrize(
    ("identity_name", "candidate_name"),
    [
        ("Acme, L.L.C.", "ACME LLC"),
        ("Acme Incorporated", "Acme Inc."),
        ("Acme Corporation", "Acme Corp."),
        ("Acme Company", "Acme Co."),
    ],
)
def test_legal_suffix_variants_match(
    identity_name: str, candidate_name: str
) -> None:
    decision = match_candidate(
        identity(legal_name=identity_name, business_name=None),
        candidate(holder_name=candidate_name),
    )

    assert decision.accepted is True
    assert decision.name_relationship == "exact_normalized"


def test_fuzzy_or_partial_name_only_is_rejected() -> None:
    decision = match_candidate(
        identity(business_name="Example Electrical Services"),
        candidate(holder_name="Example Electrical"),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert decision.name_relationship == "partial"


def test_typo_fuzzy_name_only_is_auditable_but_rejected() -> None:
    decision = match_candidate(
        identity(business_name="Example Electric"),
        candidate(holder_name="Example Electrik"),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert decision.name_relationship == "fuzzy"
    assert decision.matched_candidate_role == "business"


def test_supporting_phone_strengthens_exact_name_to_high_confidence() -> None:
    decision = match_candidate(
        identity(business_name="Example Electric"),
        candidate(holder_name="Example Electric", phone="(512) 456-7890"),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "high"
    assert decision.supporting_evidence == ["exact_phone"]


def test_supporting_address_strengthens_exact_name_to_high_confidence() -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electric",
            address="1 Main Street, Austin, TX 78701",
        ),
        candidate(
            holder_name="Example Electric",
            address="1 MAIN STREET AUSTIN TX 78701",
        ),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "high"
    assert decision.supporting_evidence == ["exact_address"]


def test_supporting_state_and_trade_strengthen_exact_name() -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electric",
            states=["TX"],
            normalized_categories=["electrical"],
        ),
        candidate(
            holder_name="Example Electric",
            state="TX",
            raw_license_type="Electrical Contractor",
        ),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "medium"
    assert decision.supporting_evidence == [
        "observed_address_state_consistent",
        "trade_category_consistent",
    ]


def test_conflicting_phone_rejects_an_otherwise_exact_name() -> None:
    decision = match_candidate(
        identity(business_name="Example Electric"),
        candidate(holder_name="Example Electric", phone="737-456-7890"),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert decision.conflicts == ["candidate_phone_conflicts_with_day1_phone"]


def test_exact_business_name_and_official_mailing_address_override_phone_conflict() -> None:
    matched_candidate = candidate(
        board_id="TDLR",
        holder_name="Example Electric",
        phone="512-442-6782",
        address="4300 South Congress Avenue, Austin TX 78745",
        source_fields={
            "business_address_line1": "4300 SOUTH CONGRESS AVENUE",
            "business_city_state_zip": "AUSTIN TX 78745",
            "mailing_address_line1": "1506 FERGUSON LN STE 102",
            "mailing_address_city_state_zip": "AUSTIN TX 78754",
        },
    )
    day1_identity = identity(
        business_name="Example Electric",
        phone="512-488-1120",
        address="1506 Ferguson Ln Ste 102, Austin, TX 78754, USA",
    )

    decision = match_candidate(day1_identity, matched_candidate)

    assert decision.accepted is True
    assert decision.match_confidence == "medium"
    assert decision.supporting_evidence == ["exact_address"]
    assert decision.conflicts == ["candidate_phone_conflicts_with_day1_phone"]
    assert "+15124881120" in decision.notes[0]
    assert "+15124426782" in decision.notes[0]

    source_result = BoardSearchResult(
        board_id="TDLR",
        board_name="Texas Department of Licensing and Regulation",
        jurisdiction="TX",
        strategy="tdlr_all_licenses_open_data",
        search_status="ok",
        fetched_at=FETCHED_AT,
        candidates=[matched_candidate],
    )
    accepted = assemble_day2_result(day1_identity, [source_result]).accepted_licenses[0]
    assert accepted.match_notes == decision.notes


def test_exact_name_and_conflicting_phone_with_nonmatching_address_is_rejected() -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electric",
            address="1506 Ferguson Ln Ste 102, Austin, TX 78754",
        ),
        candidate(
            board_id="TDLR",
            holder_name="Example Electric",
            phone="512-442-6782",
            source_fields={
                "mailing_address_line1": "4300 SOUTH CONGRESS AVENUE",
                "mailing_address_city_state_zip": "AUSTIN TX 78745",
            },
        ),
    )

    assert decision.accepted is False
    assert decision.match_confidence == "low"
    assert "exact_address" not in decision.supporting_evidence
    assert decision.conflicts == ["candidate_phone_conflicts_with_day1_phone"]


def test_partial_name_cannot_use_exact_address_to_override_phone_conflict() -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electrical Services",
            address="1 Main Street, Austin, TX 78701",
        ),
        candidate(
            board_id="TDLR",
            holder_name="Example Electrical",
            phone="512-442-6782",
            source_fields={
                "mailing_address_line1": "1 MAIN STREET",
                "mailing_address_city_state_zip": "AUSTIN TX 78701",
            },
        ),
    )

    assert decision.accepted is False
    assert decision.name_relationship == "partial"
    assert decision.supporting_evidence == ["exact_address"]
    assert decision.conflicts == ["candidate_phone_conflicts_with_day1_phone"]


def test_exact_name_and_matching_official_address_without_phone_conflict_is_unchanged() -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electric",
            address="1 Main Street, Austin, TX 78701, USA",
        ),
        candidate(
            board_id="TDLR",
            holder_name="Example Electric",
            source_fields={
                "mailing_address_line1": "1 MAIN STREET",
                "mailing_address_city_state_zip": "AUSTIN TX 78701",
            },
        ),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "high"
    assert decision.supporting_evidence == ["exact_address"]
    assert decision.conflicts == []


@pytest.mark.parametrize(
    ("line_key", "city_state_zip_key"),
    [
        ("business_address_line1", "business_city_state_zip"),
        ("mailing_address_line1", "mailing_address_city_state_zip"),
    ],
)
def test_both_tdlr_official_address_kinds_can_support_matching(
    line_key: str,
    city_state_zip_key: str,
) -> None:
    decision = match_candidate(
        identity(
            business_name="Example Electric",
            address="1 Main Street, Austin, TX 78701",
        ),
        candidate(
            board_id="TDLR",
            holder_name="Example Electric",
            source_fields={
                line_key: "1 MAIN STREET",
                city_state_zip_key: "AUSTIN TX 78701",
            },
        ),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "high"
    assert decision.supporting_evidence == ["exact_address"]


def test_exact_name_with_limited_evidence_is_medium_confidence() -> None:
    decision = match_candidate(
        identity(business_name="Example Electric"),
        candidate(holder_name="Example Electric"),
    )

    assert decision.accepted is True
    assert decision.match_confidence == "medium"
    assert decision.supporting_evidence == []


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("Current", "active"),
        ("ACTIVE", "active"),
        ("Expired", "inactive"),
        ("Suspended", "inactive"),
        ("Inactive", "inactive"),
        ("Revoked", "inactive"),
        ("Not Current", "inactive"),
        ("non-current", "inactive"),
        ("Not Active", "inactive"),
        ("unknown board code", None),
        (None, None),
    ],
)
def test_license_status_normalization_preserves_unknowns(
    raw_status: str | None, expected: str | None
) -> None:
    assert normalize_license_status(raw_status) == expected
