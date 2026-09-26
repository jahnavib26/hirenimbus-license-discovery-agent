import httpx
import pytest

from app.places import GooglePlacesClient, PlacesLookupError


def test_search_uses_places_new_phone_text_flow_and_minimal_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://places.googleapis.com/v1/places:searchText"
        assert request.headers["x-goog-api-key"] == "secret"
        assert request.headers["x-goog-fieldmask"] == "places.id"
        assert request.read()
        payload = __import__("json").loads(request.content)
        assert payload == {
            "textQuery": "+15125551234",
            "regionCode": "US",
            "languageCode": "en",
            "includePureServiceAreaBusinesses": True,
            "pageSize": 20,
        }
        return httpx.Response(200, json={"places": [{"id": "abc"}]})

    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.search_by_phone("+15125551234") == ["abc"]


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


def test_http_failure_is_wrapped_without_leaking_provider_response() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(403, text="sensitive details"))
    client = GooglePlacesClient("secret", http_client=httpx.Client(transport=transport))

    with pytest.raises(PlacesLookupError, match="Google Places request failed"):
        client.search_by_phone("+15125551234")


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
