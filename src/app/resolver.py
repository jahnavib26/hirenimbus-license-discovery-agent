"""Deterministic identity resolution from verified Place Details."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from app.categories import CategoryMapper
from app.models import (
    BusinessIdentity,
    CandidateAssessment,
    Evidence,
    IdentityLookupResult,
    LookupError,
    PlaceDetails,
)
from app.phone import InvalidPhoneNumber, normalize_us_phone, parse_us_phone
from app.places import PlacesClient, PlacesLookupError


class IdentityResolver:
    def __init__(
        self,
        places_client: PlacesClient,
        category_mapper: CategoryMapper | None = None,
    ) -> None:
        self._places = places_client
        self._categories = category_mapper or CategoryMapper()

    def resolve(self, raw_phone: str) -> IdentityLookupResult:
        try:
            input_phone = parse_us_phone(raw_phone)
        except InvalidPhoneNumber as exc:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                notes=["Input validation failed before any external lookup."],
                error=LookupError(kind="invalid_input", message=str(exc)),
            )

        normalized_phone = input_phone.e164

        try:
            candidate_ids = list(dict.fromkeys(self._places.search_by_phone(normalized_phone)))
            details = [self._places.get_place_details(place_id) for place_id in candidate_ids]
        except PlacesLookupError as exc:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                notes=["The external lookup failed; this is not a no-result outcome."],
                error=LookupError(kind="lookup_failure", message=str(exc)),
            )

        if not details:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                notes=["Google Places returned no candidates for the valid phone number."],
            )

        evaluated = [
            (place, self._phone_evaluation(place, normalized_phone)) for place in details
        ]
        evidence = [self._evidence(place) for place, _ in evaluated]
        assessments = [
            self._assessment(place, phone_result) for place, phone_result in evaluated
        ]
        exact = [place for place, phone_result in evaluated if phone_result == "exact"]
        has_meaningful_conflict = any(
            phone_result == "conflict" for _, phone_result in evaluated
        )

        if not exact:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                evidence=evidence,
                assessments=assessments,
                notes=[
                    "Candidates were returned, but none had a non-conflicting phone that exactly normalized to the input.",
                    "Search ranking was not treated as proof of identity.",
                ],
            )

        if has_meaningful_conflict:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                evidence=evidence,
                assessments=assessments,
                notes=[
                    "An exact-phone candidate exists, but another returned phone value creates a meaningful conflict.",
                    "The conflict vetoed automatic identity resolution.",
                ],
            )

        if len(exact) > 1:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                evidence=evidence,
                assessments=assessments,
                notes=[
                    "Multiple Places listings have the exact input phone; identity is ambiguous.",
                    "No candidate was selected from search ranking.",
                ],
            )

        place = exact[0]
        identity = self._identity(place, normalized_phone)
        if not identity.business_name:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                evidence=evidence,
                assessments=assessments,
                notes=["The exact-phone listing has no business name, so identity is insufficient."],
            )

        notes = [
            "A single Place Details record exactly matches the normalized input phone.",
            "The identity is verified from one source, so confidence is medium.",
            "The Google display name is not asserted to be a legal name or DBA.",
        ]
        if place.pure_service_area_business:
            notes.append("Google marks this as a pure service-area business; a hidden address is expected.")
        if place.business_status == "CLOSED_PERMANENTLY":
            notes.append("Google marks the identified listing as permanently closed.")
        if place.moved_place_id:
            notes.append(f"Google links the listing to moved place ID {place.moved_place_id}.")

        return IdentityLookupResult(
            found=True,
            confidence="medium",
            input_phone=input_phone,
            identity=identity,
            evidence=evidence,
            assessments=assessments,
            notes=notes,
        )

    @staticmethod
    def _phone_evaluation(
        place: PlaceDetails, expected: str
    ) -> Literal["exact", "mismatch", "conflict", "unavailable"]:
        returned = [
            value
            for value in (place.international_phone_number, place.national_phone_number)
            if value
        ]
        normalized: list[str] = []
        invalid_returned_phone = False
        for value in returned:
            try:
                normalized.append(normalize_us_phone(value))
            except InvalidPhoneNumber:
                invalid_returned_phone = True

        if not returned:
            return "unavailable"
        if expected not in normalized:
            return "mismatch" if normalized else "unavailable"
        if invalid_returned_phone or any(value != expected for value in normalized):
            return "conflict"
        return "exact"

    def _identity(self, place: PlaceDetails, phone: str) -> BusinessIdentity:
        raw_categories = _unique(filter(None, [place.primary_type, *place.types]))
        return BusinessIdentity(
            business_name=place.display_name,
            phone=phone,
            address=place.formatted_address,
            service_area=place.pure_service_area_business,
            states=_states(place.address_components),
            business_status=place.business_status,
            website=place.website_uri,
            place_id=place.place_id,
            moved_to_place_id=place.moved_place_id,
            raw_categories=raw_categories,
            normalized_categories=self._categories.map(raw_categories),
        )

    @staticmethod
    def _evidence(place: PlaceDetails) -> Evidence:
        raw_categories = _unique(filter(None, [place.primary_type, *place.types]))
        national_normalized = _normalize_observed_phone(place.national_phone_number)
        international_normalized = _normalize_observed_phone(
            place.international_phone_number
        )
        observed = {
            "public_name": place.display_name,
            "national_phone": place.national_phone_number,
            "national_phone_normalized": national_normalized,
            "international_phone": place.international_phone_number,
            "international_phone_normalized": international_normalized,
            "address": place.formatted_address,
            "service_area": place.pure_service_area_business,
            "business_status": place.business_status,
            "website": place.website_uri,
            "raw_categories": raw_categories,
            "moved_to_place_id": place.moved_place_id,
        }
        return Evidence(
            source="google_places_details",
            source_id=place.place_id,
            url=place.google_maps_uri,
            observed={key: value for key, value in observed.items() if value is not None},
        )

    @staticmethod
    def _assessment(
        place: PlaceDetails,
        phone_result: Literal["exact", "mismatch", "conflict", "unavailable"],
    ) -> CandidateAssessment:
        conflicts: list[str] = []
        if phone_result == "conflict":
            conflicts.append(
                "Returned phone values are invalid or normalize to conflicting base numbers."
            )
        return CandidateAssessment(
            source_id=place.place_id,
            phone_verification=phone_result,
            conflicts=conflicts,
        )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _states(components: list[dict[str, object]]) -> list[str]:
    states: list[str] = []
    for component in components:
        types = component.get("types", [])
        if isinstance(types, list) and "administrative_area_level_1" in types:
            state = component.get("shortText") or component.get("longText")
            if isinstance(state, str) and state not in states:
                states.append(state)
    return states


def _normalize_observed_phone(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_us_phone(value)
    except InvalidPhoneNumber:
        return None
