import httpx
import pytest

from app.places import GooglePlacesClient, PlacesLookupError
from app.resolver import IdentityResolver


def test_search_uses_places_new_phone_text_flow_and_minimal_fields() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://places.googleapis.com/v1/places:searchText"
        assert request.headers["x-goog-api-key"] == "secret"
        assert request.headers["x-goog-fieldmask"] == "places.id"
        assert request.read()
        payload = __import__("json").loads(request.content)
        queries.append(payload["textQuery"])
        assert {key: value for key, value in payload.items() if key != "textQuery"} == {
            "regionCode": "US",
            "languageCode": "en",
            "includePureServiceAreaBusinesses": True,
            "pageSize": 20,
        }
        return httpx.Response(200, json={"places": [{"id": "abc"}]})

    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.search_by_phone("+15125551234") == ["abc"]
    assert queries == [
        "+15125551234", "+1 5125551234", "512-555-1234", "(512) 555-1234"
    ]


def test_search_queries_standard_phone_formats_even_when_compact_has_candidates() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        queries.append(payload["textQuery"])
        places = [{"id": "fallback-place"}] if len(queries) == 3 else []
        return httpx.Response(200, json={"places": places})

    client = GooglePlacesClient(
        "secret", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.search_by_phone("+17034779016") == ["fallback-place"]
    assert queries == [
        "+17034779016", "+1 7034779016", "703-477-9016", "(703) 477-9016"
    ]


def test_spaced_phone_fallback_can_recover_a_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = __import__("json").loads(request.content)["textQuery"]
        places = [{"id": "brownlee"}] if query == "(703) 477-9016" else []
        return httpx.Response(200, json={"places": places})

    client = GooglePlacesClient(
        "secret", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.search_by_phone("+17034779016") == ["brownlee"]


def test_search_deduplicates_fallback_place_ids() -> None:
    responses = iter(
        [
            {"places": [{"id": "same-place"}, {"id": "same-place"}]},
            {"places": []},
            {"places": [{"id": "same-place"}]},
            {"places": []},
        ]
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=next(responses))
    )
    client = GooglePlacesClient(
        "secret", http_client=httpx.Client(transport=transport)
    )

    assert client.search_by_phone("+17034779016") == ["same-place"]


def test_compact_candidates_do_not_prevent_formatted_phone_searches() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(__import__("json").loads(request.content)["textQuery"])
        return httpx.Response(200, json={"places": [{"id": "compact-place"}]})

    client = GooglePlacesClient(
        "secret", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.search_by_phone("+17034779016") == ["compact-place"]
    assert queries == [
        "+17034779016", "+1 7034779016", "703-477-9016", "(703) 477-9016"
    ]


def test_fallback_candidate_still_requires_an_exact_returned_phone() -> None:
    text_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            query = __import__("json").loads(request.content)["textQuery"]
            text_queries.append(query)
            places = [] if len(text_queries) == 1 else [{"id": "mismatched"}]
            return httpx.Response(200, json={"places": places})
        return httpx.Response(
            200,
            json={
                "id": "mismatched",
                "displayName": {"text": "Different Phone Plumbing"},
                "internationalPhoneNumber": "+1 737-555-1200",
            },
        )

    client = GooglePlacesClient(
        "secret", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    result = IdentityResolver(client).resolve("512-555-1234")

    assert result.found is False
    assert result.assessments[0].phone_verification == "mismatch"
    assert text_queries == [
        "+15125551234", "+1 5125551234", "512-555-1234", "(512) 555-1234"
    ]


def test_details_requests_only_identity_fields_and_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == "https://places.googleapis.com/v1/places/abc"
        fields = request.headers["x-goog-fieldmask"]
        assert "reviews" not in fields
        assert "photos" not in fields
        assert "nationalPhoneNumber" in fields
        assert "pureServiceAreaBusiness" in fields
        assert "movedPlaceId" in fields
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "displayName": {"text": "Example Plumbing", "languageCode": "en"},
                "nationalPhoneNumber": "(512) 555-1234",
            },
        )

    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    details = client.get_place_details("abc")

    assert details.place_id == "abc"
    assert details.display_name == "Example Plumbing"
    assert details.national_phone_number == "(512) 555-1234"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "Google Places access denied (HTTP 403)."),
        (429, "Google Places rate limited (HTTP 429)."),
        (503, "Google Places service error (HTTP 503)."),
    ],
)
def test_http_failure_diagnostic_class_is_safe(status: int, expected: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text="sensitive response body")
    )
    client = GooglePlacesClient(
        "private-test-key", http_client=httpx.Client(transport=transport)
    )

    with pytest.raises(PlacesLookupError) as exc_info:
        client._search_text("512-555-0100")

    assert str(exc_info.value) == expected
    assert "private-test-key" not in str(exc_info.value)
    assert "sensitive response body" not in str(exc_info.value)


def test_transport_failure_has_safe_diagnostic() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-test-key and sensitive transport detail")

    client = GooglePlacesClient(
        "private-test-key", http_client=httpx.Client(transport=httpx.MockTransport(fail))
    )

    with pytest.raises(PlacesLookupError) as exc_info:
        client._search_text("512-555-0100")

    assert str(exc_info.value) == "Google Places transport failure."
    assert "private-test-key" not in str(exc_info.value)
    assert "sensitive transport detail" not in str(exc_info.value)


def test_malformed_details_are_reported_as_provider_failure() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "abc", "types": {"bad": "shape"}})
    )
    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=transport))

    with pytest.raises(PlacesLookupError, match="invalid place details"):
        client.get_place_details("abc")


def test_malformed_search_candidate_is_not_silently_a_no_result() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"places": [{"displayName": "No ID"}]})
    )
    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=transport))

    with pytest.raises(PlacesLookupError, match="invalid search candidate"):
        client.search_by_phone("+15125551234")


def test_details_place_id_must_match_requested_candidate() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "different-id"})
    )
    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=transport))

    with pytest.raises(PlacesLookupError, match="different place ID"):
        client.get_place_details("abc")
