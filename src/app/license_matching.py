"""Conservative candidate matching, status normalization, and Day 2 assembly."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from app.categories import CategoryMapper
from app.day2_models import (
    AcceptedLicenseRecord,
    BoardSearchResult,
    CandidateNameRole,
    CandidateLicenseRecord,
    Day2BoardResult,
    Day2Result,
    IdentityMatchDecision,
    NormalizedLicenseStatus,
    NameRelationship,
    SearchKey,
)
from app.models import BusinessIdentity
from app.phone import InvalidPhoneNumber, normalize_us_phone
from app.search_keys import generate_search_keys, name_relationship, normalize_search_name


_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "data" / "category_taxonomy.json"

_CANDIDATE_BUSINESS_NAME_FIELDS = {
    "businessname",
    "busname",
    "busname1",
    "busname2",
    "companyname",
    "dba",
    "plumbcompany",
    "tradename",
}

_CANDIDATE_PERSON_NAME_FIELDS = {
    "individualname",
    "licenseename",
    "ownername",
}

_ACTIVE_STATUSES = {
    "active",
    "approved",
    "current",
    "renewed",
    "valid",
}

_INACTIVE_STATUSES = {
    "cancelled",
    "canceled",
    "delinquent",
    "expired",
    "inactive",
    "lapsed",
    "revoked",
    "suspended",
    "void",
}

_STATE_CODES = {
    "california": "CA",
    "district of columbia": "DC",
    "maryland": "MD",
    "texas": "TX",
    "virginia": "VA",
}

_BUSINESS_IDENTITY_FIELDS = {"legal_name", "dba", "business_name"}
_PHONE_CONFLICT = "candidate_phone_conflicts_with_day1_phone"


def normalize_license_status(raw_status: str | None) -> NormalizedLicenseStatus | None:
    """Map explicit board standing words without inferring from dates."""

    if raw_status is None or not raw_status.strip():
        return None
    normalized = normalize_search_name(raw_status)
    tokens = set(normalized.split())
    if any(
        phrase in normalized
        for phrase in ("not active", "not current", "non active", "non current")
    ) or tokens & _INACTIVE_STATUSES:
        return "inactive"
    if tokens & _ACTIVE_STATUSES:
        return "active"
    return None


def match_candidate(
    identity: BusinessIdentity,
    candidate: CandidateLicenseRecord,
) -> IdentityMatchDecision:
    """Decide whether one candidate belongs to established Day 1 identity fields."""

    identity_keys = generate_search_keys(identity)
    candidate_names = _candidate_names(candidate)
    exact = _exact_name_relationship(identity_keys, candidate_names)
    nonexact = exact or _nonexact_name_relationship(identity_keys, candidate_names)
    supporting_evidence, strong_support = _supporting_evidence(identity, candidate)
    conflicts = _conflicts(identity, candidate)

    if exact is not None:
        identity_key, candidate_field, candidate_role = exact
        if conflicts:
            if (
                conflicts == [_PHONE_CONFLICT]
                and identity_key.source_field in _BUSINESS_IDENTITY_FIELDS
                and candidate_role == "business"
                and "exact_address" in supporting_evidence
            ):
                identity_phone = _normalized_phone(identity.phone)
                candidate_phone = _normalized_phone(candidate.phone)
                return _decision(
                    candidate,
                    accepted=True,
                    confidence="medium",
                    relationship="exact_normalized",
                    match=exact,
                    supporting_evidence=supporting_evidence,
                    conflicts=conflicts,
                    notes=[
                        "An exact normalized business name and exact official address "
                        "provide stronger corroboration despite a different board "
                        f"contact phone (Day 1: {identity_phone}; board: {candidate_phone})."
                    ],
                )
            return _decision(
                candidate,
                accepted=False,
                confidence="low",
                relationship="exact_normalized",
                match=exact,
                supporting_evidence=supporting_evidence,
                conflicts=conflicts,
                notes=["An exact normalized name was found, but material conflicting evidence prevents acceptance."],
            )

        if identity_key.source_field == "owner_principal" or candidate_role == "person":
            if not strong_support:
                return _decision(
                    candidate,
                    accepted=False,
                    confidence="low",
                    relationship="exact_normalized",
                    match=exact,
                    supporting_evidence=supporting_evidence,
                    conflicts=[],
                    notes=[
                        "A person/owner name alone is ambiguous; exact phone or address corroboration is required."
                    ],
                )
            return _decision(
                candidate,
                accepted=True,
                confidence="high",
                relationship="exact_normalized",
                match=exact,
                supporting_evidence=supporting_evidence,
                conflicts=[],
                notes=["Established person/owner name is corroborated by strong evidence."],
            )

        return _decision(
            candidate,
            accepted=True,
            confidence="high" if strong_support else "medium",
            relationship="exact_normalized",
            match=exact,
            supporting_evidence=supporting_evidence,
            conflicts=[],
            notes=[
                "An established Day 1 business name exactly matches a board-provided name after normalization."
            ],
        )

    if nonexact is not None:
        identity_key, candidate_field, candidate_role, relationship = nonexact
        return _decision(
            candidate,
            accepted=False,
            confidence="low",
            relationship=relationship,
            match=(identity_key, candidate_field, candidate_role),
            supporting_evidence=supporting_evidence,
            conflicts=conflicts,
            notes=[
                "A fuzzy or partial name relationship is insufficient for acceptance."
            ],
        )

    return IdentityMatchDecision(
        candidate=candidate,
        accepted=False,
        match_confidence="low",
        name_relationship="none",
        supporting_evidence=supporting_evidence,
        conflicts=conflicts,
        notes=["No established Day 1 name field matches a board-provided candidate name."],
    )


def assemble_day2_result(
    identity: BusinessIdentity,
    board_results: list[BoardSearchResult],
) -> Day2Result:
    """Preserve board access outcomes while assembling accepted license matches."""

    final_boards: list[Day2BoardResult] = []
    accepted_all: list[AcceptedLicenseRecord] = []

    for board_result in board_results:
        decisions = [
            match_candidate(identity, candidate) for candidate in board_result.candidates
        ]
        accepted = [
            _accepted_license(decision)
            for decision in decisions
            if decision.accepted
        ]
        accepted_all.extend(accepted)
        notes = list(board_result.notes)
        if board_result.candidates and not accepted:
            notes.append(
                "The board returned candidates, but none met the conservative identity-match policy."
            )
        final_boards.append(
            Day2BoardResult(
                board_id=board_result.board_id,
                board_name=board_result.board_name,
                jurisdiction=board_result.jurisdiction,
                strategy=board_result.strategy,
                search_status=board_result.search_status,
                source_url=board_result.source_url,
                fetched_at=board_result.fetched_at,
                match_decisions=decisions,
                accepted_licenses=accepted,
                notes=notes,
            )
        )

    notes = [
        "Board search status describes source access; it does not describe license standing or identity-match confidence."
    ]
    if not accepted_all:
        notes.append(
            "No license candidate was accepted for this identity; this does not mean the business is unlicensed."
        )
    if any(result.search_status in {"unreachable", "captcha_blocked", "skipped"} for result in board_results):
        notes.append("One or more relevant searches were incomplete or intentionally skipped.")

    return Day2Result(
        identity_place_id=identity.place_id,
        board_results=final_boards,
        accepted_licenses=accepted_all,
        notes=notes,
    )


def _decision(
    candidate: CandidateLicenseRecord,
    *,
    accepted: bool,
    confidence: str,
    relationship: NameRelationship,
    match: tuple[SearchKey, str, CandidateNameRole],
    supporting_evidence: list[str],
    conflicts: list[str],
    notes: list[str],
) -> IdentityMatchDecision:
    identity_key, candidate_field, candidate_role = match
    return IdentityMatchDecision(
        candidate=candidate,
        accepted=accepted,
        match_confidence=confidence,
        name_relationship=relationship,
        matched_identity_field=identity_key.source_field,
        matched_identity_value=identity_key.original_value,
        matched_candidate_field=candidate_field,
        matched_candidate_role=candidate_role,
        supporting_evidence=supporting_evidence,
        conflicts=conflicts,
        notes=notes,
    )


def _accepted_license(decision: IdentityMatchDecision) -> AcceptedLicenseRecord:
    if (
        not decision.accepted
        or decision.match_confidence == "low"
        or decision.matched_identity_field is None
        or decision.matched_identity_value is None
        or decision.matched_candidate_field is None
        or decision.matched_candidate_role is None
    ):
        raise ValueError("Only accepted high/medium decisions can become licenses.")
    return AcceptedLicenseRecord(
        **decision.candidate.model_dump(),
        normalized_status=normalize_license_status(
            decision.candidate.raw_license_status
        ),
        match_confidence=decision.match_confidence,
        matched_identity_field=decision.matched_identity_field,
        matched_identity_value=decision.matched_identity_value,
        matched_candidate_field=decision.matched_candidate_field,
        matched_candidate_role=decision.matched_candidate_role,
        supporting_evidence=decision.supporting_evidence,
        match_notes=decision.notes,
    )


def _candidate_names(
    candidate: CandidateLicenseRecord,
) -> list[tuple[str, str, CandidateNameRole]]:
    names: list[tuple[str, str, CandidateNameRole]] = []
    if candidate.holder_name and candidate.holder_name.strip():
        names.append(
            ("holder_name", candidate.holder_name, candidate.holder_name_role)
        )
    for field, value in candidate.source_fields.items():
        field_key = _field_key(field)
        role: CandidateNameRole | None = None
        if field_key in _CANDIDATE_BUSINESS_NAME_FIELDS:
            role = "business"
        elif field_key in _CANDIDATE_PERSON_NAME_FIELDS:
            role = "person"
        if role is not None and value is not None and str(value).strip():
            names.append((f"source_fields.{field}", str(value), role))
    return list(dict.fromkeys(names))


def _exact_name_relationship(
    identity_keys: list[SearchKey],
    candidate_names: list[tuple[str, str, CandidateNameRole]],
) -> tuple[SearchKey, str, CandidateNameRole] | None:
    for identity_key in identity_keys:
        for candidate_field, candidate_value, candidate_role in candidate_names:
            if name_relationship(candidate_value, identity_key.normalized_value) == "exact_normalized":
                return identity_key, candidate_field, candidate_role
    return None


def _nonexact_name_relationship(
    identity_keys: list[SearchKey],
    candidate_names: list[tuple[str, str, CandidateNameRole]],
) -> tuple[SearchKey, str, CandidateNameRole, NameRelationship] | None:
    for identity_key in identity_keys:
        for candidate_field, candidate_value, candidate_role in candidate_names:
            relationship = name_relationship(
                identity_key.normalized_value, candidate_value
            )
            if relationship in {"partial", "fuzzy"}:
                return identity_key, candidate_field, candidate_role, relationship
    return None


def _supporting_evidence(
    identity: BusinessIdentity,
    candidate: CandidateLicenseRecord,
) -> tuple[list[str], bool]:
    evidence: list[str] = []
    strong = False

    identity_phone = _normalized_phone(identity.phone)
    candidate_phone = _normalized_phone(candidate.phone)
    if identity_phone and candidate_phone and identity_phone == candidate_phone:
        evidence.append("exact_phone")
        strong = True

    if identity.address:
        identity_address = _normalize_address(identity.address)
        if identity_address and any(
            identity_address == _normalize_address(address)
            for address in _candidate_addresses(candidate)
        ):
            evidence.append("exact_address")
            strong = True

    if candidate.state and _state(candidate.state) in {
        _state(value) for value in identity.states
    }:
        evidence.append("observed_address_state_consistent")

    candidate_categories = _taxonomy().map(
        [value for value in (candidate.raw_license_type, candidate.raw_classification) if value]
    )
    if any(category in identity.normalized_categories for category in candidate_categories):
        evidence.append("trade_category_consistent")

    return evidence, strong


def _conflicts(
    identity: BusinessIdentity,
    candidate: CandidateLicenseRecord,
) -> list[str]:
    conflicts: list[str] = []
    identity_phone = _normalized_phone(identity.phone)
    candidate_phone = _normalized_phone(candidate.phone)
    if identity_phone and candidate_phone and identity_phone != candidate_phone:
        conflicts.append(_PHONE_CONFLICT)
    return conflicts


def _candidate_addresses(candidate: CandidateLicenseRecord) -> list[str]:
    addresses = [candidate.address] if candidate.address else []
    source_by_key = {
        _field_key(str(field)): str(value).strip()
        for field, value in candidate.source_fields.items()
        if value is not None and str(value).strip()
    }
    for prefix in ("business", "mailing"):
        city_state_zip_key = (
            "businesscitystatezip"
            if prefix == "business"
            else "mailingaddresscitystatezip"
        )
        parts = [
            source_by_key.get(f"{prefix}addressline1"),
            source_by_key.get(f"{prefix}addressline2"),
            source_by_key.get(city_state_zip_key),
        ]
        address = ", ".join(part for part in parts if part)
        if address:
            addresses.append(address)
    return list(dict.fromkeys(addresses))


def _normalized_phone(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_us_phone(value)
    except InvalidPhoneNumber:
        return None


def _normalize_free_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _normalize_address(value: str) -> str:
    tokens = _normalize_free_text(value).split()
    if tokens[-3:] == ["united", "states", "america"]:
        tokens = tokens[:-3]
    elif tokens[-2:] == ["united", "states"]:
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in {"us", "usa"}:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _state(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) == 2:
        return normalized.upper()
    return _STATE_CODES.get(normalized, value.strip().upper())


@lru_cache(maxsize=1)
def _taxonomy() -> CategoryMapper:
    return CategoryMapper.from_json(_TAXONOMY_PATH)
