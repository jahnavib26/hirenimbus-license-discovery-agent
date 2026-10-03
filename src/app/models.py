"""Explicit public and provider-facing data models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Confidence = Literal["high", "medium", "low"]


class NormalizedPhone(BaseModel):
    e164: str
    extension: str | None = None


class LookupError(BaseModel):
    kind: Literal["invalid_input", "lookup_failure"]
    message: str


class Evidence(BaseModel):
    source: str
    source_id: str | None = None
    url: str | None = None
    observed: dict[str, Any] = Field(default_factory=dict)


class CandidateAssessment(BaseModel):
    source_id: str
    phone_verification: Literal["exact", "mismatch", "conflict", "unavailable"]
    conflicts: list[str] = Field(default_factory=list)


class BusinessIdentity(BaseModel):
    business_name: str | None = None
    legal_name: str | None = None
    dba: str | None = None
    owner_principal: str | None = None
    phone: str
    address: str | None = None
    service_area: bool | None = None
    states: list[str] = Field(default_factory=list)
    business_status: str | None = None
    website: str | None = None
    place_id: str
    moved_to_place_id: str | None = None
    raw_categories: list[str] = Field(default_factory=list)
    normalized_categories: list[str] = Field(default_factory=list)


class IdentityCandidateHypothesis(BaseModel):
    """Places phone-search candidate awaiting official registry corroboration."""

    identity: BusinessIdentity
    evidence: list[Evidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IdentityLookupResult(BaseModel):
    found: bool
    confidence: Confidence
    input_phone: NormalizedPhone | None = None
    identity: BusinessIdentity | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    candidate_hypotheses: list[IdentityCandidateHypothesis] = Field(default_factory=list)
    assessments: list[CandidateAssessment] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: LookupError | None = None


class PlaceDetails(BaseModel):
    place_id: str
    display_name: str | None = None
    national_phone_number: str | None = None
    international_phone_number: str | None = None
    formatted_address: str | None = None
    address_components: list[dict[str, Any]] = Field(default_factory=list)
    business_status: str | None = None
    website_uri: str | None = None
    primary_type: str | None = None
    types: list[str] = Field(default_factory=list)
    pure_service_area_business: bool | None = None
    moved_place_id: str | None = None
    google_maps_uri: str | None = None

    @classmethod
    def from_google(cls, payload: dict[str, Any]) -> "PlaceDetails":
        display_name = payload.get("displayName")
        if isinstance(display_name, dict):
            display_name = display_name.get("text")
        return cls(
            place_id=payload.get("id", ""),
            display_name=display_name,
            national_phone_number=payload.get("nationalPhoneNumber"),
            international_phone_number=payload.get("internationalPhoneNumber"),
            formatted_address=payload.get("formattedAddress"),
            address_components=payload.get("addressComponents") or [],
            business_status=payload.get("businessStatus"),
            website_uri=payload.get("websiteUri"),
            primary_type=payload.get("primaryType"),
            types=payload.get("types") or [],
            pure_service_area_business=payload.get("pureServiceAreaBusiness"),
            moved_place_id=payload.get("movedPlaceId"),
            google_maps_uri=payload.get("googleMapsUri"),
        )
