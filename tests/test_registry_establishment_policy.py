from datetime import datetime, timezone

import pytest

from app.ca_registry import _CACandidate, _establish_ca_candidate
from app.dc_registry import _DCRecord, _DCCandidate, _establish_dc_candidate
from app.md_registry import _MDCandidate, _MDTradeName, _establish_md_candidate
from app.models import BusinessIdentity
from app.registry_matching import registry_addresses_equal, registry_legal_name_equal
from app.va_registry import _VASCCCandidate, _establish_va_candidate


FETCHED_AT = datetime(2026, 10, 2, tzinfo=timezone.utc)
BRANCH_ADDRESS = "100 Market Street, Capital City, XX 12345"
REGISTRY_ADDRESS = "500 Corporate Plaza, Capital City, XX 12345"
JURISDICTIONS = ("VA", "MD", "DC", "CA")


def identity(name: str, address: str = BRANCH_ADDRESS) -> BusinessIdentity:
    return BusinessIdentity(
        business_name=name,
        phone="+12025550123",
        address=address,
        place_id="synthetic-place",
        states=["XX"],
        normalized_categories=["plumbing"],
    )


def candidate(
    jurisdiction: str,
    entity_id: str,
    legal_name: str,
    *,
    alias: str | None = None,
    address: str = REGISTRY_ADDRESS,
):
    if jurisdiction == "VA":
        return _VASCCCandidate(
            entity_id=entity_id,
            legal_name=legal_name,
            status="Active",
            principal_office_address=address,
            detail_url=f"https://cis.scc.virginia.gov/entity/{entity_id}",
            dbas=[alias] if alias else [],
        )
    if jurisdiction == "MD":
        return _MDCandidate(
            department_id=entity_id,
            legal_name=legal_name,
            status="Active",
            principal_office_address=address,
            source_url="https://egov.maryland.gov/BusinessExpress/EntitySearch",
            lookup_at="2026-10-02T00:00:00Z",
            search_term=legal_name,
            search_mode="Business Name",
            raw={"Business Name": legal_name},
            trade_names=(
                [_MDTradeName(alias, "Active", entity_id, {"Trade Name": alias})]
                if alias else []
            ),
        )
    if jurisdiction == "DC":
        corporate = _DCRecord(
            {
                "FILE_NUMBER": entity_id,
                "BUSINESS_NAME": legal_name,
                "ENTITY_STATUS": "Active",
                "BUSNIESS_ADDRESS_LINE1": address,
                "BUSINESS_CITY": "Capital City",
                "BUSINESS_STATE": "XX",
                "ZIPCODE": "12345",
                "BUSINESS_COUNTRY": "USA",
            },
            f"https://maps2.dcgis.dc.gov/example/{entity_id}/0/query",
        )
        trades = (
            [_DCRecord({
                "TRADE_NAME": alias,
                "FILE_NUMBER": f"TRADE-{entity_id}",
                "INITIAL_FILENUMBER": entity_id,
                "TRADENAME_STATUS": "Active Trade Name",
            }, f"https://maps2.dcgis.dc.gov/example/{entity_id}/1/query")]
            if alias else []
        )
        return _DCCandidate(
            file_number=entity_id,
            legal_name=legal_name,
            status="Active",
            business_address=address,
            corporate=corporate,
            trade_names=trades,
        )
    if jurisdiction == "CA":
        return _CACandidate(
            entity_number=entity_id,
            legal_name=legal_name,
            status="Active",
            business_address=address,
            source_url="https://bizfileonline.sos.ca.gov/search/business",
            lookup_at="2026-10-02T00:00:00Z",
            search_term=legal_name,
            search_mode="Business Name",
            raw={"Entity Name": legal_name},
        )
    raise AssertionError(jurisdiction)


def establish(jurisdiction: str, base: BusinessIdentity, candidates: list[object]):
    if jurisdiction == "VA":
        return _establish_va_candidate(base, candidates, FETCHED_AT)
    if jurisdiction == "MD":
        return _establish_md_candidate(base, candidates, FETCHED_AT)
    if jurisdiction == "DC":
        return _establish_dc_candidate(base, candidates, FETCHED_AT)
    if jurisdiction == "CA":
        return _establish_ca_candidate(base, candidates, FETCHED_AT)
    raise AssertionError(jurisdiction)


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_exact_legal_name_establishes_despite_branch_address_difference(
    jurisdiction: str,
) -> None:
    result = establish(
        jurisdiction,
        identity("Harbor Home Services LLC"),
        [candidate(jurisdiction, "ENTITY-1", "Harbor Home Services LLC")],
    )

    assert result.status == "ok"
    assert result.established_entity_id == "ENTITY-1"
    assert any(item.kind == "exact_legal_name" for item in result.evidence)
    address_evidence = next(
        item for item in result.evidence
        if item.kind == "registry_address_differs_from_places_address"
    )
    assert address_evidence.observed["places_address"] == BRANCH_ADDRESS
    assert result.conflicts == []


@pytest.mark.parametrize("jurisdiction", ("VA", "MD", "DC"))
def test_current_official_alias_establishes_despite_branch_address_difference(
    jurisdiction: str,
) -> None:
    result = establish(
        jurisdiction,
        identity("Harbor Home Services"),
        [candidate(
            jurisdiction,
            "ENTITY-1",
            "Harbor Parent Holdings LLC",
            alias="Harbor Home Services",
        )],
    )

    assert result.status == "ok"
    assert result.established_entity_id == "ENTITY-1"
    assert any(
        item.kind in {
            "exact_fictitious_name_relationship",
            "exact_trade_name_relationship",
        }
        for item in result.evidence
    )
    assert any(
        item.kind == "registry_address_differs_from_places_address"
        for item in result.evidence
    )


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_fuzzy_name_with_different_address_does_not_establish(
    jurisdiction: str,
) -> None:
    result = establish(
        jurisdiction,
        identity("Harbor Plumbing"),
        [candidate(jurisdiction, "ENTITY-1", "Harbor Plumbing Services LLC")],
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert any("address_conflicts_with_places_address" in item for item in result.conflicts)


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_multiple_exact_registry_candidates_remain_ambiguous(
    jurisdiction: str,
) -> None:
    base = identity("Harbor Home Services LLC")
    candidates = [
        candidate(jurisdiction, entity_id, "Harbor Home Services LLC")
        for entity_id in ("ENTITY-1", "ENTITY-2")
    ]

    result = establish(jurisdiction, base, candidates)

    assert result.status == "ambiguous"
    assert result.established_entity_id is None


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
@pytest.mark.parametrize(
    ("marketing_name", "legal_name"),
    [
        ("ABC Plumbing", "ABC Plumbing LLC"),
        ("ABC Plumbing", "ABC Plumbing Corporation"),
        ("A.B.C. Plumbing", "ABC Plumbing, Inc."),
        ("ABC Plumbing", "ABC Plumbing Corp"),
    ],
)
def test_registry_legal_suffix_equivalence_establishes_and_preserves_source_name(
    jurisdiction: str,
    marketing_name: str,
    legal_name: str,
) -> None:
    result = establish(
        jurisdiction,
        identity(marketing_name),
        [candidate(jurisdiction, "ENTITY-1", legal_name)],
    )

    assert result.status == "ok"
    assert result.established_entity_id == "ENTITY-1"
    assert any(item.kind == "exact_legal_name" for item in result.evidence)
    assert [name.value for name in result.legal_names] == [legal_name]


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_legal_suffix_equivalence_does_not_ignore_meaningful_words(
    jurisdiction: str,
) -> None:
    result = establish(
        jurisdiction,
        identity("ABC Plumbing"),
        [candidate(jurisdiction, "ENTITY-1", "ABC Plumbing Services LLC")],
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_multiple_suffix_equivalent_legal_candidates_remain_ambiguous(
    jurisdiction: str,
) -> None:
    result = establish(
        jurisdiction,
        identity("ABC Plumbing"),
        [
            candidate(jurisdiction, "ENTITY-1", "ABC Plumbing LLC"),
            candidate(jurisdiction, "ENTITY-2", "ABC Plumbing Inc"),
        ],
    )

    assert result.status == "ambiguous"
    assert result.established_entity_id is None


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("123 Main St, Capital City, VA 12345", "123 Main Street, Capital City, VA 12345"),
        ("123 Oak Rd, Capital City, VA 12345", "123 Oak Road, Capital City, VA 12345"),
        ("123 Lake Ave, Capital City, VA 12345", "123 Lake Avenue, Capital City, VA 12345"),
        ("123 Garden Pl, Capital City, VA 12345", "123 Garden Place, Capital City, VA 12345"),
        ("123 River Blvd, Capital City, VA 12345", "123 River Boulevard, Capital City, VA 12345"),
        ("123 MAIN ST.,  Capital City, VA 12345", "123 Main Street Capital City VA 12345"),
        (
            "123 Main Street Suite 4, Capital City, VA 12345",
            "123 Main St, Capital City, VA 12345",
        ),
        (
            "123 Main Street Apt #4, Capital City, VA 12345",
            "123 Main St, Capital City, VA 12345",
        ),
        (
            "123 Main Street Unit 4, Capital City, VA 12345",
            "123 Main St, Capital City, VA 12345",
        ),
    ],
)
def test_registry_address_postal_format_variants_are_equal(left: str, right: str) -> None:
    assert registry_addresses_equal(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("123 Main St, Capital City, VA 12345", "124 Main St, Capital City, VA 12345"),
        ("123 Main St, Capital City, VA 12345", "123 Oak St, Capital City, VA 12345"),
        ("123 Main St, Capital City, VA 12345", "123 Main St, Other City, VA 12345"),
        ("123 Main St, Capital City, VA 12345", "123 Main St, Capital City, MD 12345"),
        (
            "123 Main St Suite 4, Capital City, VA 12345",
            "123 Main Street Suite 5, Capital City, VA 12345",
        ),
        (
            "123 Main St Suite, Capital City, VA 12345",
            "123 Main Street, Capital City, VA 12345",
        ),
    ],
)
def test_registry_address_conflicts_remain_unequal(left: str, right: str) -> None:
    assert not registry_addresses_equal(left, right)


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_suffix_equivalent_name_and_postal_address_establish_unique_entity(
    jurisdiction: str,
) -> None:
    places_address = "123 Main Street Suite 4, Capital City, XX 12345"
    official_address = "123 Main St, Capital City, XX 12345"
    result = establish(
        jurisdiction,
        identity("ABC Plumbing", places_address),
        [candidate(jurisdiction, "ENTITY-1", "ABC Plumbing, LLC", address=official_address)],
    )

    assert result.status == "ok"
    assert result.established_entity_id == "ENTITY-1"
    assert [name.value for name in result.legal_names] == ["ABC Plumbing, LLC"]
    assert any(item.kind == "exact_legal_name" for item in result.evidence)
    assert any(
        item.kind in {"exact_business_address", "exact_principal_office_address"}
        for item in result.evidence
    )


@pytest.mark.parametrize("jurisdiction", JURISDICTIONS)
def test_plausible_name_uses_postal_address_corroboration(
    jurisdiction: str,
) -> None:
    places_address = "123 Main Street Suite 4, Capital City, XX 12345"
    official_address = "123 Main St, Capital City, XX 12345"
    result = establish(
        jurisdiction,
        identity("Harbor Plumbing", places_address),
        [candidate(jurisdiction, "ENTITY-1", "Harbor Plumbing Services LLC", address=official_address)],
    )

    assert result.status == "ok"
    assert result.established_entity_id == "ENTITY-1"
    assert any(
        item.kind in {"exact_business_address", "exact_principal_office_address"}
        for item in result.evidence
    )


def test_registry_suffix_rule_is_exact_and_not_global_fuzzy_matching() -> None:
    assert registry_legal_name_equal("ABC Plumbing", "ABC Plumbing LLC")
    assert registry_legal_name_equal("ABC Plumbing", "ABC Plumbing Corporation")
    assert not registry_legal_name_equal("ABC Plumbing", "ABC Plumbing Services LLC")
    assert not registry_legal_name_equal("ABC Plumbing", "ABC Plumbing Supply LLC")
