from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.categories import CategoryMapper
from app.models import PlaceDetails
from app.places import PlacesLookupError
from app.resolver import IdentityResolver
from app.website import (
    ObservedWebsitePhone,
    StructuredBusinessIdentity,
    WebsiteFetchError,
    WebsitePage,
)


@dataclass
class FakePlacesClient:
    candidate_ids: list[str] = field(default_factory=list)
    details: dict[str, PlaceDetails] = field(default_factory=dict)
    failure: bool = False
    failed_details: set[str] = field(default_factory=set)
    search_calls: list[str] = field(default_factory=list)
    detail_calls: list[str] = field(default_factory=list)

    def search_by_phone(self, normalized_phone: str) -> list[str]:
        self.search_calls.append(normalized_phone)
        if self.failure:
            raise PlacesLookupError("Google Places request failed.")
        return self.candidate_ids

    def get_place_details(self, place_id: str) -> PlaceDetails:
        self.detail_calls.append(place_id)
        if place_id in self.failed_details:
            raise PlacesLookupError("details unavailable")
        return self.details[place_id]


@dataclass
class FakeWebsiteClient:
    pages: dict[str, WebsitePage] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> WebsitePage:
        self.calls.append(url)
        if url in self.failures or url not in self.pages:
            raise WebsiteFetchError("website unavailable")
        return self.pages[url]


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


def website_page(
    url: str,
    *,
    text: str,
    phone: str = "(512) 555-1234",
    final_url: str | None = None,
) -> WebsitePage:
    return WebsitePage(
        requested_url=url,
        final_url=final_url or url,
        visible_text=text,
        phones=(
            ObservedWebsitePhone(raw=phone, normalized="+15125551234"),
        ),
    )


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


def test_exact_website_phone_name_and_full_address_corroborate_candidate() -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    places = FakePlacesClient(
        candidate_ids=[candidate.place_id],
        details={candidate.place_id: candidate},
    )
    websites = FakeWebsiteClient(
        pages={
            "https://example.test": website_page(
                "https://example.test",
                text=(
                    "Example Plumbing. Call (512) 555-1234. "
                    "1 Main Street, Austin, TX 78701."
                ),
            )
        }
    )

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is True
    assert result.confidence == "medium"
    assert result.identity and result.identity.place_id == "place-1"
    assert result.assessments[0].phone_verification == "mismatch"
    assert result.assessments[0].conflicts == [
        "The phone returned by Google Places normalizes to a different number than the input."
    ]
    assert [item.source for item in result.evidence] == [
        "google_places_details",
        "official_business_website",
    ]
    website_evidence = result.evidence[1]
    assert website_evidence.url == "https://example.test"
    assert website_evidence.observed["website_phone"] == "(512) 555-1234"
    assert website_evidence.observed["website_phone_normalized"] == "+15125551234"
    assert website_evidence.observed["places_returned_phones"] == [
        "+1 737-555-1200"
    ]
    assert website_evidence.observed["places_phone_verification"] == "mismatch"
    assert any("overcame" in note for note in result.notes)
    assert any("mismatch remains visible" in note for note in result.notes)


def test_mismatched_places_phone_becomes_untrusted_registry_hypothesis() -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    resolver, _ = resolver_for(candidate)

    result = resolver.resolve("512-555-1234")

    assert result.found is False
    assert result.identity is None
    assert len(result.candidate_hypotheses) == 1
    hypothesis = result.candidate_hypotheses[0]
    assert hypothesis.identity.place_id == candidate.place_id
    assert any(item.source == "google_places_phone_search" for item in hypothesis.evidence)
    phone_search_evidence = next(
        item for item in hypothesis.evidence
        if item.source == "google_places_phone_search"
    )
    assert phone_search_evidence.observed["places_phone_verification"] == "mismatch"
    assert phone_search_evidence.observed["registry_search_only"] is True
    assert result.assessments[0].phone_verification == "mismatch"


def test_first_party_name_address_and_legal_name_strengthen_but_do_not_promote_hypothesis() -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    page = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="Example Plumbing 1 Main Street Austin TX 78701",
        phones=(),
        structured_identities=(StructuredBusinessIdentity(
            name="Example Plumbing",
            legal_name="Example Plumbing LLC",
            address="1 Main Street, Austin, TX 78701",
            source_type="Plumber",
        ),),
    )
    resolver = IdentityResolver(
        FakePlacesClient(
            candidate_ids=[candidate.place_id],
            details={candidate.place_id: candidate},
        ),
        website_client=FakeWebsiteClient(pages={candidate.website_uri: page}),
    )

    result = resolver.resolve("512-555-1234")

    assert result.found is False
    assert result.identity is None
    assert len(result.candidate_hypotheses) == 1
    hypothesis = result.candidate_hypotheses[0]
    assert hypothesis.identity.legal_name == "Example Plumbing LLC"
    website_evidence = next(
        item for item in hypothesis.evidence
        if item.source == "official_business_website"
    )
    assert website_evidence.observed["website_exact_name_match"] is True
    assert website_evidence.observed["website_exact_address_match"] is True
    assert website_evidence.observed["input_phone_published"] is False
    assert website_evidence.observed["structured_legal_name"] == "Example Plumbing LLC"
    assert website_evidence.observed["canonical_domain"] == "example.test"


def test_multiple_mismatched_places_candidates_remain_separate_hypotheses() -> None:
    resolver, _ = resolver_for(
        place("place-one", international_phone_number="+1 737-555-1200"),
        place(
            "place-two",
            display_name="Unrelated Services",
            international_phone_number="+1 737-555-1201",
        ),
    )

    result = resolver.resolve("512-555-1234")

    assert result.found is False
    assert result.identity is None
    assert [item.identity.place_id for item in result.candidate_hypotheses] == [
        "place-one", "place-two"
    ]
    assert {item.identity.business_name for item in result.candidate_hypotheses} == {
        "Example Plumbing", "Unrelated Services"
    }


@pytest.mark.parametrize(
    "text",
    [
        "Call (512) 555-1234 for service.",
        "Example Plumbing. Call (512) 555-1234. 9 Other Road, Austin, TX 78702.",
        "Different Plumbing. Call (512) 555-1234. 1 Main St, Austin, TX 78701.",
    ],
    ids=["phone-only", "phone-name-wrong-address", "phone-address-wrong-name"],
)
def test_incomplete_website_corroboration_is_rejected(text: str) -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    places = FakePlacesClient(
        candidate_ids=[candidate.place_id],
        details={candidate.place_id: candidate},
    )
    websites = FakeWebsiteClient(
        pages={
            "https://example.test": website_page(
                "https://example.test", text=text
            )
        }
    )

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is False
    assert result.confidence == "low"
    assert result.identity is None


def test_multiple_website_corroborated_candidates_remain_ambiguous() -> None:
    first = place(
        "place-1",
        international_phone_number="+1 737-555-1200",
        website_uri="https://first.example.test",
    )
    second = place(
        "place-2",
        international_phone_number="+1 737-555-1201",
        website_uri="https://second.example.test",
    )
    places = FakePlacesClient(
        candidate_ids=[first.place_id, second.place_id],
        details={first.place_id: first, second.place_id: second},
    )
    text = "Example Plumbing (512) 555-1234 1 Main Street Austin TX 78701"
    websites = FakeWebsiteClient(
        pages={
            first.website_uri: website_page(first.website_uri, text=text),
            second.website_uri: website_page(second.website_uri, text=text),
        }
    )

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is False
    assert result.identity is None
    assert any("ambiguous" in note for note in result.notes)


def test_third_party_website_is_not_accepted() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        website_uri="https://www.facebook.com/example-plumbing",
    )
    places = FakePlacesClient(
        candidate_ids=[candidate.place_id],
        details={candidate.place_id: candidate},
    )
    websites = FakeWebsiteClient(
        pages={
            candidate.website_uri: website_page(
                candidate.website_uri,
                text="Example Plumbing (512) 555-1234 1 Main St Austin TX 78701",
            )
        }
    )

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is False
    assert any("not accepted as first-party" in note for note in result.notes)


def test_website_fetch_failure_keeps_existing_conservative_result() -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    places = FakePlacesClient(
        candidate_ids=[candidate.place_id],
        details={candidate.place_id: candidate},
    )
    websites = FakeWebsiteClient(failures={candidate.website_uri})

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is False
    assert result.error is None
    assert result.assessments[0].phone_verification == "mismatch"
    assert any("conservative Places result" in note for note in result.notes)


def test_first_party_contact_page_can_corroborate_stale_places_phone() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        website_uri="https://example.test/location/",
    )
    places = FakePlacesClient(
        candidate_ids=[candidate.place_id], details={candidate.place_id: candidate}
    )
    contact_url = "https://example.test/contact/"
    websites = FakeWebsiteClient(
        pages={
            contact_url: website_page(
                contact_url,
                text="Example Plumbing (512) 555-1234 1 Main St Austin TX 78701",
            )
        }
    )

    result = IdentityResolver(places, website_client=websites).resolve("512-555-1234")

    assert result.found is True
    assert result.identity and result.identity.place_id == candidate.place_id
    assert result.evidence[-1].url == contact_url
    assert websites.calls == [
        "https://example.test/location/",
        contact_url,
        "https://example.test/contact-us/",
        "https://example.test/about/",
        "https://example.test/locations/",
        "https://example.test/",
    ]


def test_one_places_details_failure_does_not_discard_an_independent_candidate() -> None:
    candidate = place("survivor", website_uri=None)
    places = FakePlacesClient(
        candidate_ids=["unavailable", candidate.place_id],
        details={candidate.place_id: candidate},
        failed_details={"unavailable"},
    )

    result = IdentityResolver(places).resolve("512-555-1234")

    assert result.found is True
    assert result.identity and result.identity.place_id == candidate.place_id
    assert [assessment.source_id for assessment in result.assessments] == [candidate.place_id]


def test_homepage_identity_link_is_followed_once_within_same_origin() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        website_uri="https://example.test/",
    )
    contact_url = "https://example.test/customer-service-area/"
    homepage = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="Example Plumbing homepage",
        phones=(),
        internal_identity_links=(contact_url, "https://directory.test/about/"),
    )
    websites = FakeWebsiteClient(pages={
        candidate.website_uri: homepage,
        contact_url: website_page(
            contact_url,
            text="Example Plumbing (512) 555-1234 1 Main St Austin TX 78701",
        ),
    })

    result = IdentityResolver(
        FakePlacesClient(
            candidate_ids=[candidate.place_id], details={candidate.place_id: candidate}
        ),
        website_client=websites,
    ).resolve("512-555-1234")

    assert result.found is True
    assert result.evidence[-1].url == contact_url
    assert contact_url in websites.calls
    assert "https://directory.test/about/" not in websites.calls


def test_apex_homepage_safe_www_redirect_follows_www_contact_link() -> None:
    apex = "https://example.test/"
    www = "https://www.example.test/"
    contact = "https://www.example.test/contact/"
    websites = FakeWebsiteClient(pages={
        apex: WebsitePage(
            requested_url=apex,
            final_url=www,
            visible_text="Example Plumbing",
            phones=(),
            internal_identity_links=(contact,),
        ),
        contact: website_page(contact, text="Example Plumbing contact page"),
    })

    pages, failures = IdentityResolver(
        FakePlacesClient(), website_client=websites
    )._fetch_first_party_pages(apex)

    assert failures == 4  # The four other bounded, conventional routes are absent in this fake.
    assert contact in websites.calls
    assert any(page.final_url == contact for page in pages)


def test_root_redirect_to_home_follows_only_two_same_party_identity_links() -> None:
    root = "https://example.test/"
    home = "https://example.test/home/"
    contact = "https://example.test/get-in-touch/"
    about = "https://example.test/company/"
    third = "https://example.test/our-team/"
    websites = FakeWebsiteClient(pages={
        root: WebsitePage(
            requested_url=root,
            final_url=home,
            visible_text="Example Plumbing",
            phones=(),
            internal_identity_links=(contact, about, third),
        ),
        contact: website_page(contact, text="Example Plumbing contact page"),
        about: website_page(about, text="Example Plumbing about page"),
        third: website_page(third, text="Example Plumbing locations page"),
    })

    pages, failures = IdentityResolver(
        FakePlacesClient(), website_client=websites
    )._fetch_first_party_pages(root)

    assert failures == 4  # The four conventional routes are absent in this fake.
    assert contact in websites.calls
    assert about in websites.calls
    assert third not in websites.calls
    assert len(pages) <= 8


def test_homepage_discovery_rejects_cross_party_links() -> None:
    root = "https://example.test/"
    outside = "https://directory.test/contact/"
    websites = FakeWebsiteClient(pages={
        root: WebsitePage(
            requested_url=root,
            final_url=root,
            visible_text="Example Plumbing",
            phones=(),
            internal_identity_links=(outside,),
        ),
    })

    pages, failures = IdentityResolver(
        FakePlacesClient(), website_client=websites
    )._fetch_first_party_pages(root)

    assert len(pages) == 1
    assert failures == 4  # Only the homepage exists in this fake.
    assert outside not in websites.calls


def test_one_candidate_fetch_failure_does_not_erase_another_corroborated_candidate() -> None:
    qualifying = place(
        "qualifying",
        international_phone_number="+1 737-555-1200",
        website_uri="https://qualifying.example.test",
    )
    unavailable = place(
        "unavailable",
        display_name="Other Plumbing",
        international_phone_number="+1 737-555-1201",
        website_uri="https://unavailable.example.test",
    )
    places = FakePlacesClient(
        candidate_ids=[qualifying.place_id, unavailable.place_id],
        details={
            qualifying.place_id: qualifying,
            unavailable.place_id: unavailable,
        },
    )
    websites = FakeWebsiteClient(
        pages={
            qualifying.website_uri: website_page(
                qualifying.website_uri,
                text="Example Plumbing (512) 555-1234 1 Main St Austin TX 78701",
            )
        },
        failures={unavailable.website_uri},
    )

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is True
    assert result.identity and result.identity.place_id == qualifying.place_id
    assert any("conservative Places result" in note for note in result.notes)


def test_schema_org_identity_can_corroborate_phone_name_and_address_and_extract_legal_name() -> None:
    candidate = place(international_phone_number="+1 737-555-1200")
    places = FakePlacesClient(candidate_ids=[candidate.place_id], details={candidate.place_id: candidate})
    page = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="",
        phones=(ObservedWebsitePhone(raw="+1 512-555-1234", normalized="+15125551234"),),
        structured_identities=(StructuredBusinessIdentity(
            name="Example Plumbing",
            legal_name="Example Plumbing LLC",
            telephone="+1 512-555-1234",
            address="1 Main St, Austin, TX 78701, USA",
            source_type="LocalBusiness",
        ),),
    )
    websites = FakeWebsiteClient(pages={candidate.website_uri: page})

    result = IdentityResolver(places, website_client=websites).resolve("512-555-1234")

    assert result.found is True
    assert result.identity and result.identity.legal_name == "Example Plumbing LLC"
    observed = result.evidence[-1].observed
    assert observed["structured_field_source"] == "schema.org JSON-LD"
    assert observed["structured_legal_name"] == "Example Plumbing LLC"
    assert observed["canonical_domain"] == "example.test"
    assert observed["extracted_fields"] == ["name", "telephone", "address"]


def test_hidden_places_address_can_use_schema_name_phone_and_legal_name() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        formatted_address=None,
        address_components=[],
        pure_service_area_business=True,
    )
    places = FakePlacesClient(candidate_ids=[candidate.place_id], details={candidate.place_id: candidate})
    page = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="",
        phones=(ObservedWebsitePhone(raw="+1 512-555-1234", normalized="+15125551234"),),
        structured_identities=(StructuredBusinessIdentity(
            name="Example Plumbing", legal_name="Example Plumbing LLC",
            telephone="+1 512-555-1234", source_type="Plumber",
        ),),
    )

    result = IdentityResolver(places, website_client=FakeWebsiteClient(pages={candidate.website_uri: page})).resolve("512-555-1234")

    assert result.found is True
    assert result.identity and result.identity.legal_name == "Example Plumbing LLC"
    assert result.evidence[-1].observed["extracted_fields"] == ["name", "telephone", "legalName"]


def test_weak_structured_phone_and_name_without_address_or_legal_name_stays_hypothesis() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        formatted_address=None,
        address_components=[],
    )
    page = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="",
        phones=(ObservedWebsitePhone(raw="+1 512-555-1234", normalized="+15125551234"),),
        structured_identities=(StructuredBusinessIdentity(
            name="Example Plumbing", telephone="+1 512-555-1234", source_type="Plumber",
        ),),
    )

    result = IdentityResolver(
        FakePlacesClient(candidate_ids=[candidate.place_id], details={candidate.place_id: candidate}),
        website_client=FakeWebsiteClient(pages={candidate.website_uri: page}),
    ).resolve("512-555-1234")

    assert result.found is False
    assert result.identity is None
    assert len(result.candidate_hypotheses) == 1


def test_conflicting_structured_legal_names_remain_unresolved() -> None:
    candidate = place(
        international_phone_number="+1 737-555-1200",
        formatted_address=None,
        address_components=[],
    )
    page = WebsitePage(
        requested_url=candidate.website_uri,
        final_url=candidate.website_uri,
        visible_text="Example Plumbing 1 Main St Austin TX 78701",
        phones=(ObservedWebsitePhone(raw="+1 512-555-1234", normalized="+15125551234"),),
        structured_identities=(
            StructuredBusinessIdentity(name="Example Plumbing", legal_name="Example A LLC", telephone="+1 512-555-1234"),
            StructuredBusinessIdentity(name="Example Plumbing", legal_name="Example B LLC", telephone="+1 512-555-1234"),
        ),
    )

    result = IdentityResolver(
        FakePlacesClient(candidate_ids=[candidate.place_id], details={candidate.place_id: candidate}),
        website_client=FakeWebsiteClient(pages={candidate.website_uri: page}),
    ).resolve("512-555-1234")

    assert result.found is False
    assert result.identity is None
    assert len(result.candidate_hypotheses) == 1
    assert result.candidate_hypotheses[0].identity.legal_name is None
    assert any("Conflicting schema.org legal names" in note for note in result.notes)


def test_exact_places_phone_match_does_not_invoke_website_fallback() -> None:
    matching = place("matching")
    mismatch = place(
        "mismatch",
        display_name="Nearby Plumbing",
        international_phone_number="+1 737-555-1200",
        website_uri="https://nearby.example.test",
    )
    places = FakePlacesClient(
        candidate_ids=[matching.place_id, mismatch.place_id],
        details={matching.place_id: matching, mismatch.place_id: mismatch},
    )
    websites = FakeWebsiteClient(failures={mismatch.website_uri})

    result = IdentityResolver(places, website_client=websites).resolve(
        "512-555-1234"
    )

    assert result.found is True
    assert result.identity and result.identity.place_id == "matching"
    assert websites.calls == []


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
