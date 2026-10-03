"""Small, mockable adapter for Google Places API (New)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.models import PlaceDetails


class PlacesLookupError(RuntimeError):
    """A provider, transport, or response failure."""


class PlacesClient(Protocol):
    def search_by_phone(self, normalized_phone: str) -> Sequence[str]: ...

    def get_place_details(self, place_id: str) -> PlaceDetails: ...


class GooglePlacesClient:
    BASE_URL = "https://places.googleapis.com/v1"
    SEARCH_PAGE_SIZE = 20
    DETAILS_FIELDS = (
        "id,displayName,nationalPhoneNumber,internationalPhoneNumber,"
        "formattedAddress,addressComponents,businessStatus,websiteUri,"
        "primaryType,types,pureServiceAreaBusiness,movedPlaceId,googleMapsUri"
    )

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_key:
            raise ValueError("A Google Places API key is required.")
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=timeout_seconds)

    def search_by_phone(self, normalized_phone: str) -> list[str]:
        digits = normalized_phone.removeprefix("+1")
        queries = (
            normalized_phone,
            f"+1 {digits}",
            f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
            f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
        )
        place_ids = [
            place_id
            for query in dict.fromkeys(queries)
            for place_id in self._search_text(query)
        ]
        return list(dict.fromkeys(place_ids))

    def _search_text(self, phone_query: str) -> list[str]:
        payload = {
            "textQuery": phone_query,
            "regionCode": "US",
            "languageCode": "en",
            "includePureServiceAreaBusinesses": True,
            "pageSize": self.SEARCH_PAGE_SIZE,
        }
        data = self._request_json(
            "POST",
            f"{self.BASE_URL}/places:searchText",
            headers=self._headers("places.id"),
            json=payload,
        )
        places = data.get("places", [])
        if not isinstance(places, list):
            raise PlacesLookupError("Google Places returned an invalid search response.")
        place_ids: list[str] = []
        for item in places:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise PlacesLookupError("Google Places returned an invalid search candidate.")
            if not item["id"]:
                raise PlacesLookupError("Google Places returned an empty place ID.")
            place_ids.append(item["id"])
        return place_ids

    def get_place_details(self, place_id: str) -> PlaceDetails:
        if not place_id:
            raise PlacesLookupError("Google Places returned an empty place ID.")
        data = self._request_json(
            "GET",
            f"{self.BASE_URL}/places/{quote(place_id, safe='')}",
            headers=self._headers(self.DETAILS_FIELDS),
        )
        try:
            details = PlaceDetails.from_google(data)
        except (TypeError, ValidationError) as exc:
            raise PlacesLookupError("Google Places returned invalid place details.") from exc
        if not details.place_id:
            raise PlacesLookupError("Google Places details omitted the place ID.")
        if details.place_id != place_id:
            raise PlacesLookupError("Google Places details returned a different place ID.")
        return details

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
        }

    def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            raise PlacesLookupError("Google Places transport failure.") from exc

        if not response.is_success:
            status = response.status_code
            if status == 403:
                message = "Google Places access denied (HTTP 403)."
            elif status == 429:
                message = "Google Places rate limited (HTTP 429)."
            elif 500 <= status <= 599:
                message = f"Google Places service error (HTTP {status})."
            else:
                message = f"Google Places HTTP error (HTTP {status})."
            raise PlacesLookupError(message)

        try:
            data = response.json()
        except ValueError as exc:
            raise PlacesLookupError("Google Places returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise PlacesLookupError("Google Places returned an invalid response.")
        return data
