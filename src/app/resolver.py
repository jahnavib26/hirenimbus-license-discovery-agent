"""Deterministic identity resolution from verified Place Details."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Literal
import unicodedata
from urllib.parse import urlsplit

from app.categories import CategoryMapper
from app.models import (
    BusinessIdentity,
    CandidateAssessment,
    Evidence,
    IdentityCandidateHypothesis,
    IdentityLookupResult,
    LookupError,
    PlaceDetails,
)
from app.phone import InvalidPhoneNumber, normalize_us_phone, parse_us_phone
from app.places import PlacesClient, PlacesLookupError
from app.search_keys import name_relationship, normalize_search_name
from app.website import (
    WebsiteClient,
    WebsiteFetchError,
    WebsitePage,
    fetch_bounded_first_party_pages,
    is_first_party_url,
    is_known_third_party_url,
)


class IdentityResolver:
    def __init__(
        self,
        places_client: PlacesClient,
        category_mapper: CategoryMapper | None = None,
        website_client: WebsiteClient | None = None,
    ) -> None:
        self._places = places_client
        self._categories = category_mapper or CategoryMapper()
        self._websites = website_client

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

        details: list[PlaceDetails] = []
        detail_notes: list[str] = []
        try:
            candidate_ids = list(dict.fromkeys(self._places.search_by_phone(normalized_phone)))
        except PlacesLookupError as exc:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                notes=["The external lookup failed; this is not a no-result outcome."],
                error=LookupError(kind="lookup_failure", message=str(exc)),
            )
        for place_id in candidate_ids:
            try:
                details.append(self._places.get_place_details(place_id))
            except PlacesLookupError as exc:
                # Keep other independent Place candidates when one details request fails.
                detail_notes.append(f"Place Details failed for candidate {place_id}: {exc}")

        if not details:
            if candidate_ids:
                return IdentityLookupResult(
                    found=False,
                    confidence="low",
                    input_phone=input_phone,
                    notes=["Places returned candidate IDs, but no candidate details were retrievable.", *detail_notes],
                    error=LookupError(
                        kind="lookup_failure",
                        message="All Places candidate detail requests failed.",
                    ),
                )
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
            corroborated, website_evidence, website_notes, hypotheses = self._website_candidates(
                evaluated, normalized_phone
            )
        else:
            corroborated, website_evidence, website_notes, hypotheses = [], [], [], []

        if not exact:
            if len(corroborated) == 1:
                place = corroborated[0]
                identity = self._identity(place, normalized_phone, website_evidence)
                return IdentityLookupResult(
                    found=True,
                    confidence="medium",
                    input_phone=input_phone,
                    identity=identity,
                    evidence=[*evidence, *website_evidence],
                    assessments=assessments,
                    notes=[
                        *detail_notes,
                        "First-party evidence matched the input phone and Places listing using exact name/address corroboration or a schema.org exact-name legal-entity claim for subsequent registry corroboration.",
                        "That corroboration overcame the different phone returned by Google Places; the Places phone mismatch remains visible in the evidence and candidate assessment.",
                        *website_notes,
                        "The Google display name is not asserted to be a legal name or DBA.",
                    ],
                )
            if len(corroborated) > 1:
                website_notes.append(
                    "Multiple Places candidates satisfied the complete first-party website corroboration rule, so identity remains ambiguous."
                )
            return IdentityLookupResult(
                found=False,
                confidence="low",
                input_phone=input_phone,
                evidence=[*evidence, *website_evidence],
                assessments=assessments,
                candidate_hypotheses=hypotheses,
                notes=[
                    *detail_notes,
                    "Candidates were returned, but none had a non-conflicting phone that exactly normalized to the input.",
                    "Search ranking was not treated as proof of identity.",
                    *website_notes,
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
                    *detail_notes,
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
                    *detail_notes,
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
                notes=[*detail_notes, "The exact-phone listing has no business name, so identity is insufficient."],
            )

        notes = [
            *detail_notes,
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

    def _website_candidates(
        self,
        evaluated: list[tuple[PlaceDetails, str]],
        normalized_phone: str,
    ) -> tuple[list[PlaceDetails], list[Evidence], list[str], list[IdentityCandidateHypothesis]]:
        corroborated: list[PlaceDetails] = []
        evidence: list[Evidence] = []
        notes: list[str] = []
        hypotheses: list[IdentityCandidateHypothesis] = []
        for place, phone_result in evaluated:
            if phone_result == "exact" or not place.display_name:
                continue
            place_evidence = Evidence(
                source="google_places_phone_search",
                source_id=place.place_id,
                url=place.google_maps_uri,
                observed={
                    "candidate_discovery_path": "Google Places phone text search",
                    "input_phone": normalized_phone,
                    "places_phone_verification": phone_result,
                    "places_returned_phones": _returned_phones(place),
                    "registry_search_only": True,
                },
            )
            page_proofs: list[tuple[int, Evidence]] = []
            failed_fetches = 0
            pages: list[WebsitePage] = []
            if place.website_uri and self._websites is not None:
                if is_known_third_party_url(place.website_uri):
                    notes.append(
                        f"The website returned for Place {place.place_id} was not accepted as first-party."
                    )
                else:
                    pages, failed_fetches = self._fetch_first_party_pages(place.website_uri)
                    if not pages:
                        notes.append(
                            f"The first-party website fallback could not safely verify Place {place.place_id}; "
                            "the conservative Places result remains an untrusted registry-search hypothesis."
                        )
            candidate_conflict = False
            candidate_complete = False
            # Website facts improve registry acquisition, but only an exact
            # published-phone relationship can use the legacy direct path.
            for page in pages:
                if not is_first_party_url(place.website_uri or "", page.final_url):
                    notes.append(
                        f"The website returned for Place {place.place_id} was not accepted as first-party."
                    )
                    continue
                page_text = _normalize_page_text(page.visible_text)
                name = _normalize_page_text(place.display_name)
                address = _normalize_address(place.formatted_address) if place.formatted_address else None
                visible_name = _contains_exact_tokens(page_text, name)
                visible_address = bool(address and _contains_exact_tokens(page_text, address))
                matching_structures = [
                    item for item in page.structured_identities
                    if item.name
                    and name_relationship(item.name, place.display_name) == "exact_normalized"
                ]
                structured_with_phone = [
                    item for item in matching_structures
                    if item.telephone
                    and _normalize_observed_phone(item.telephone) == normalized_phone
                ]
                legal_names = {
                    normalize_search_name(item.legal_name)
                    for item in matching_structures
                    if item.legal_name
                }
                structured_phones = {
                    _normalize_observed_phone(item.telephone)
                    for item in matching_structures
                    if item.telephone
                }
                structured_addresses = {
                    _normalize_address(item.address)
                    for item in matching_structures
                    if item.address
                }
                if (
                    len(legal_names) > 1
                    or (structured_with_phone and len(structured_phones) > 1)
                    or (address and len(structured_addresses) > 1)
                ):
                    notes.append(
                        f"Conflicting schema.org legal names were published for Place {place.place_id}; "
                        "structured claims were retained as evidence but not selected for registry search."
                    )
                    candidate_conflict = True
                structured_match = next(iter(structured_with_phone), None)
                structured_name_claim = matching_structures[0] if matching_structures else None
                structured_address_match = bool(
                    structured_match and structured_match.address and address
                    and _normalize_address(structured_match.address) == address
                )
                visible_complete = visible_name and visible_address
                structured_complete = bool(
                    structured_match
                    and (structured_address_match or structured_match.legal_name)
                )
                observed_phone = next(
                    (phone for phone in page.phones if phone.normalized == normalized_phone),
                    None,
                )
                website_name_match = visible_name or bool(matching_structures)
                website_address_match = visible_address or any(
                    item.address and address
                    and _normalize_address(item.address) == address
                    for item in matching_structures
                )
                has_structured_legal_name = len(legal_names) == 1
                extracted_fields = ["name"] if website_name_match else []
                if observed_phone:
                    extracted_fields.append("telephone")
                if website_address_match:
                    extracted_fields.append("address")
                elif has_structured_legal_name:
                    extracted_fields.append("legalName")
                proof = Evidence(
                    source="official_business_website",
                    source_id=place.place_id,
                    url=page.final_url,
                    observed={
                        "public_name": place.display_name,
                        "website_phone": observed_phone.raw if observed_phone else None,
                        "website_phone_normalized": observed_phone.normalized if observed_phone else None,
                        "input_phone_published": observed_phone is not None,
                        "address": place.formatted_address,
                        "places_returned_phones": _returned_phones(place),
                        "places_phone_verification": phone_result,
                        "canonical_domain": _canonical_domain(page.final_url),
                        "website_exact_name_match": website_name_match,
                        "website_exact_address_match": website_address_match,
                        "structured_type": structured_name_claim.source_type if structured_name_claim else None,
                        "structured_name": structured_name_claim.name if structured_name_claim else None,
                        "structured_legal_name": (
                            structured_name_claim.legal_name if len(legal_names) == 1 and structured_name_claim else None
                        ),
                        "structured_telephone": structured_name_claim.telephone if structured_name_claim else None,
                        "structured_address": structured_name_claim.address if structured_name_claim else None,
                        "structured_field_source": (
                            "schema.org JSON-LD" if structured_name_claim else "visible first-party page"
                        ),
                        "extracted_fields": extracted_fields,
                    },
                )
                website_strength = (
                    int(visible_name or bool(matching_structures))
                    + int(visible_address or any(
                        item.address and address
                        and _normalize_address(item.address) == address
                        for item in matching_structures
                    ))
                    + int(len(legal_names) == 1)
                )
                page_proofs.append((website_strength, proof))
                candidate_complete = candidate_complete or bool(
                    observed_phone and (visible_complete or structured_complete)
                )
            if page_proofs:
                page_proofs.sort(key=lambda item: item[0], reverse=True)
                candidate_proof = page_proofs[0][1]
                evidence.append(candidate_proof)
                hypothesis_evidence = [place_evidence, candidate_proof]
            else:
                candidate_proof = None
                hypothesis_evidence = [place_evidence]
            if candidate_complete and not candidate_conflict:
                corroborated.append(place)
            if failed_fetches:
                notes.append(
                    f"{failed_fetches} bounded first-party page fetch(es) failed for Place {place.place_id}; other pages and candidates were evaluated independently."
                )
            hypotheses.append(IdentityCandidateHypothesis(
                identity=self._identity(
                    place,
                    normalized_phone,
                    [candidate_proof] if candidate_proof else [],
                ),
                evidence=hypothesis_evidence,
                notes=[
                    "This Google Places phone-search candidate is an acquisition hypothesis only; its Place Details phone was not an exact match.",
                    "First-party name, legal-name, address, and domain observations may prioritize registry acquisition but do not establish identity.",
                ],
            ))
        hypotheses.sort(
            key=lambda hypothesis: _hypothesis_acquisition_strength(hypothesis),
            reverse=True,
        )
        return corroborated, evidence, notes, hypotheses

    def _fetch_first_party_pages(self, website_url: str) -> tuple[list[WebsitePage], int]:
        if self._websites is None or is_known_third_party_url(website_url):
            return [], 0
        return fetch_bounded_first_party_pages(self._websites, website_url)

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

    def _identity(
        self,
        place: PlaceDetails,
        phone: str,
        website_evidence: list[Evidence] | None = None,
    ) -> BusinessIdentity:
        raw_categories = _unique(filter(None, [place.primary_type, *place.types]))
        legal_name = next((
            item.observed.get("structured_legal_name")
            for item in website_evidence or []
            if item.source_id == place.place_id
            and isinstance(item.observed.get("structured_legal_name"), str)
            and item.observed.get("structured_name")
            and name_relationship(
                str(item.observed["structured_name"]), place.display_name or ""
            ) == "exact_normalized"
        ), None)
        return BusinessIdentity(
            business_name=place.display_name,
            legal_name=legal_name,
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
        elif phone_result == "mismatch":
            conflicts.append(
                "The phone returned by Google Places normalizes to a different number than the input."
            )
        return CandidateAssessment(
            source_id=place.place_id,
            phone_verification=phone_result,
            conflicts=conflicts,
        )


def _hypothesis_acquisition_strength(
    hypothesis: IdentityCandidateHypothesis,
) -> int:
    """Order registry acquisition by first-party evidence, never Places ranking."""

    website = next(
        (item for item in hypothesis.evidence if item.source == "official_business_website"),
        None,
    )
    if website is None:
        return 0
    observed = website.observed
    return (
        4 * int(bool(observed.get("structured_legal_name")))
        + 2 * int(bool(observed.get("website_exact_address_match")))
        + int(bool(observed.get("website_exact_name_match")))
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


_ADDRESS_ALIASES = {
    "apartment": "unit",
    "apt": "unit",
    "avenue": "ave",
    "boulevard": "blvd",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
    "ste": "unit",
    "suite": "unit",
}


def _normalize_page_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"#\s*([a-z0-9]+)", r" unit \1 ", normalized)
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    token_text = " ".join(_ADDRESS_ALIASES.get(token, token) for token in tokens)
    for spaced, canonical in (
        ("l l c", "llc"),
        ("i n c", "inc"),
        ("c o r p", "corp"),
        ("c o", "co"),
    ):
        token_text = re.sub(rf"\b{spaced}\b", canonical, token_text)
    token_text = re.sub(r"\bcompany\b", "co", token_text)
    token_text = re.sub(r"\bcorporation\b", "corp", token_text)
    token_text = re.sub(r"\bincorporated\b", "inc", token_text)
    return token_text


def _normalize_address(value: str) -> str:
    tokens = _normalize_page_text(value).split()
    if tokens[-3:] == ["united", "states", "america"]:
        tokens = tokens[:-3]
    elif tokens[-2:] == ["united", "states"]:
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in {"us", "usa"}:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _contains_exact_tokens(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _returned_phones(place: PlaceDetails) -> list[str]:
    return list(
        dict.fromkeys(
            value
            for value in (
                place.international_phone_number,
                place.national_phone_number,
            )
            if value
        )
    )


def _canonical_domain(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.casefold().removeprefix("www.") if host else None
