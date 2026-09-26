from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.categories import CategoryMapper
from app.models import PlaceDetails
from app.places import PlacesLookupError
from app.resolver import IdentityResolver


@dataclass
class FakePlacesClient:
    candidate_ids: list[str] = field(default_factory=list)
    details: dict[str, PlaceDetails] = field(default_factory=dict)
    failure: bool = False
    search_calls: list[str] = field(default_factory=list)
    detail_calls: list[str] = field(default_factory=list)

    def search_by_phone(self, normalized_phone: str) -> list[str]:
        self.search_calls.append(normalized_phone)
        if self.failure:
            raise PlacesLookupError("Google Places request failed.")
        return self.candidate_ids

    def get_place_details(self, place_id: str) -> PlaceDetails:
        self.detail_calls.append(place_id)
        return self.details[place_id]


def place(place_id: str = "place-1", **overrides: object) -> PlaceDetails:
    values: dict[str, object] = {
        "place_id": place_id,
        "display_name": "Example Plumbing",
        "international_phone_number": "+1 512-555-1234",
        "formatted_address": "1 Main St, Austin, TX 78701, USA",
        "address_components": [
            {
                "longText": "Texas",
                "shortText": "TX",
                "types": ["administrative_area_level_1", "political"],
            }
        ],
        "business_status": "OPERATIONAL",
        "website_uri": "https://example.test",
        "primary_type": "plumber",
        "types": ["plumber", "point_of_interest"],
        "google_maps_uri": "https://maps.google.com/?cid=1",
    }
    values.update(overrides)
    return PlaceDetails(**values)


def resolver_for(*places: PlaceDetails) -> tuple[IdentityResolver, FakePlacesClient]:
    client = FakePlacesClient(
        candidate_ids=[item.place_id for item in places],
        details={item.place_id: item for item in places},
    )
    return IdentityResolver(client), client


def test_invalid_input_never_calls_external_lookup() -> None:
    client = FakePlacesClient(candidate_ids=["should-not-run"])

    result = IdentityResolver(client).resolve("123")

    assert result.found is False
    assert result.confidence == "low"
    assert result.error and result.error.kind == "invalid_input"
    assert client.search_calls == []
    assert client.detail_calls == []


def test_reserved_fictional_number_never_calls_external_lookup() -> None:
    client = FakePlacesClient(candidate_ids=["should-not-run"])

    result = IdentityResolver(client).resolve("202-555-0100")

    assert result.found is False
    assert result.error and result.error.kind == "invalid_input"
    assert client.search_calls == []
    assert client.detail_calls == []


def test_exact_returned_phone_is_required() -> None:
    resolver, _ = resolver_for(place(international_phone_number="+1 737-555-1200"))

    result = resolver.resolve("512-555-1234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None
    assert result.assessments[0].phone_verification == "mismatch"


def test_national_returned_phone_can_match_exactly() -> None:
    resolver, _ = resolver_for(
        place(international_phone_number=None, national_phone_number="(512) 555-1234")
    )

    result = resolver.resolve("+15125551234")

    assert result.found is True
    assert result.identity and result.identity.phone == "+15125551234"


def test_conflicting_returned_phones_are_not_resolved() -> None:
    resolver, _ = resolver_for(
        place(
            international_phone_number="+1 512-555-1234",
            national_phone_number="(737) 555-1200",
        )
    )

    result = resolver.resolve("5125551234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.assessments[0].phone_verification == "conflict"
    assert result.assessments[0].conflicts


def test_coherent_google_only_identity_is_medium_confidence() -> None:
    resolver, _ = resolver_for(place())

    result = resolver.resolve("5125551234")

    assert result.found is True
    assert result.confidence == "medium"
    assert result.identity
    assert result.identity.business_name == "Example Plumbing"
    assert result.identity.legal_name is None
    assert result.identity.dba is None
    assert result.identity.owner_principal is None
    assert result.identity.states == ["TX"]


def test_medium_confidence_for_coherent_but_sparse_exact_match() -> None:
    sparse = place(
        formatted_address=None,
        address_components=[],
        website_uri=None,
        primary_type=None,
        types=[],
    )
    resolver, _ = resolver_for(sparse)

    result = resolver.resolve("5125551234")

    assert result.found is True
    assert result.confidence == "medium"


def test_low_confidence_exact_match_without_business_name_is_unresolved() -> None:
    resolver, _ = resolver_for(place(display_name=None))

    result = resolver.resolve("5125551234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None


def test_multiple_exact_candidates_are_ambiguous_and_not_guessed() -> None:
    resolver, _ = resolver_for(place("place-1"), place("place-2", display_name="Other Plumbing"))

    result = resolver.resolve("5125551234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None
    assert len(result.evidence) == 2
    assert all(item.phone_verification == "exact" for item in result.assessments)


def test_multiple_candidates_resolve_when_only_one_phone_matches() -> None:
    resolver, _ = resolver_for(
        place("matching"),
        place(
            "nonmatching",
            display_name="Nearby Plumbing",
            international_phone_number="+1 737-555-1200",
        ),
    )

    result = resolver.resolve("5125551234")

    assert result.found is True
    assert result.confidence == "medium"
    assert result.identity and result.identity.place_id == "matching"
    assert [item.phone_verification for item in result.assessments] == [
        "exact",
        "mismatch",
    ]


def test_exact_candidate_is_vetoed_by_another_material_phone_conflict() -> None:
    resolver, _ = resolver_for(
        place("matching"),
        place(
            "conflicting",
            display_name="Different Plumbing",
            international_phone_number="+1 512-555-1234",
            national_phone_number="(737) 555-1200",
        ),
    )

    result = resolver.resolve("5125551234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None
    assert [item.phone_verification for item in result.assessments] == [
        "exact",
        "conflict",
    ]
    assert any("vetoed" in note for note in result.notes)


def test_no_result_is_not_an_error() -> None:
    resolver, client = resolver_for()

    result = resolver.resolve("5125551234")

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None
    assert result.error is None
    assert client.search_calls == ["+15125551234"]


def test_external_api_failure_is_distinct_from_no_result() -> None:
    client = FakePlacesClient(failure=True)

    result = IdentityResolver(client).resolve("5125551234")

    assert result.found is False
    assert result.error and result.error.kind == "lookup_failure"
    assert "not a no-result" in result.notes[0]


def test_closed_and_moved_business_can_still_be_identified() -> None:
    closed = place(
        business_status="CLOSED_PERMANENTLY",
        moved_place_id="new-place-id",
    )
    resolver, _ = resolver_for(closed)

    result = resolver.resolve("5125551234")

    assert result.found is True
    assert result.identity
    assert result.identity.business_status == "CLOSED_PERMANENTLY"
    assert result.identity.moved_to_place_id == "new-place-id"
    assert any("permanently closed" in note for note in result.notes)
    assert any("new-place-id" in note for note in result.notes)


def test_service_area_business_does_not_require_visible_address() -> None:
    service_area = place(
        formatted_address=None,
        address_components=[],
        pure_service_area_business=True,
    )
    resolver, _ = resolver_for(service_area)

    result = resolver.resolve("5125551234")

    assert result.found is True
    assert result.confidence == "medium"
    assert result.identity and result.identity.service_area is True
    assert result.identity.address is None
    assert any("hidden address is expected" in note for note in result.notes)


def test_missing_optional_fields_stay_empty_not_fabricated() -> None:
    sparse = place(
        national_phone_number=None,
        formatted_address=None,
        address_components=[],
        business_status=None,
        website_uri=None,
        primary_type=None,
        types=[],
        pure_service_area_business=None,
        google_maps_uri=None,
    )
    resolver, _ = resolver_for(sparse)

    result = resolver.resolve("5125551234")

    assert result.identity
    assert result.identity.address is None
    assert result.identity.service_area is None
    assert result.identity.states == []
    assert result.identity.website is None
    assert result.identity.raw_categories == []
    assert result.identity.normalized_categories == []


def test_evidence_has_provenance_and_supported_signals() -> None:
    resolver, _ = resolver_for(place())

    result = resolver.resolve("5125551234")

    evidence = result.evidence[0]
    assert evidence.source == "google_places_details"
    assert evidence.source_id == "place-1"
    assert evidence.url == "https://maps.google.com/?cid=1"
    assert evidence.observed["public_name"] == "Example Plumbing"
    assert evidence.observed["international_phone"] == "+1 512-555-1234"
    assert evidence.observed["international_phone_normalized"] == "+15125551234"
    assert evidence.observed["raw_categories"] == ["plumber", "point_of_interest"]
    assert "phone_match" not in evidence.observed
    assert "phone_verification" not in evidence.observed
    assert result.assessments[0].phone_verification == "exact"


def test_category_mapping_only_uses_explicit_taxonomy_entries() -> None:
    client = FakePlacesClient(candidate_ids=["place-1"], details={"place-1": place()})
    mapper = CategoryMapper({"plumber": "Plumbing"})

    result = IdentityResolver(client, mapper).resolve("5125551234")

    assert result.identity
    assert result.identity.raw_categories == ["plumber", "point_of_interest"]
    assert result.identity.normalized_categories == ["Plumbing"]


def test_duplicate_candidate_ids_only_fetch_details_once() -> None:
    candidate = place()
    client = FakePlacesClient(
        candidate_ids=["place-1", "place-1"], details={"place-1": candidate}
    )

    result = IdentityResolver(client).resolve("5125551234")

    assert result.found is True
    assert client.detail_calls == ["place-1"]


def test_extension_is_preserved_but_base_number_is_used_for_lookup() -> None:
    resolver, client = resolver_for(place())

    result = resolver.resolve("512-555-1234 x42")

    assert result.found is True
    assert result.input_phone
    assert result.input_phone.e164 == "+15125551234"
    assert result.input_phone.extension == "42"
    assert client.search_calls == ["+15125551234"]
    assert result.identity and result.identity.phone == "+15125551234"


def test_different_phones_can_resolve_independently_to_same_business() -> None:
    first_resolver, _ = resolver_for(place(international_phone_number="+1 512-555-1234"))
    second_resolver, _ = resolver_for(place(international_phone_number="+1 512-555-1235"))

    first = first_resolver.resolve("5125551234")
    second = second_resolver.resolve("5125551235")

    assert first.found is True and second.found is True
    assert first.identity and second.identity
    assert first.identity.place_id == second.identity.place_id == "place-1"
    assert first.identity.phone == "+15125551234"
    assert second.identity.phone == "+15125551235"


@pytest.mark.parametrize("status", ["OPERATIONAL", "CLOSED_TEMPORARILY"])
def test_business_status_is_preserved(status: str) -> None:
    resolver, _ = resolver_for(place(business_status=status))

    result = resolver.resolve("5125551234")

    assert result.identity and result.identity.business_status == status
