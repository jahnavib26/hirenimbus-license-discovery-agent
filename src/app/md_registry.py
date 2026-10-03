"""Maryland SDAT enrichment from manually captured official evidence.

Maryland Business Express protects both name search and the entity-detail form
with Cloudflare Turnstile.  This module deliberately does not submit either
form.  The live source therefore reports an access-limited ``skipped`` result.
The same establishment path can consume an auditable manual evidence export.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.registry_matching import registry_addresses_equal, registry_legal_name_equal
from app.search_keys import name_relationship


MD_BUSINESS_EXPRESS_URL = "https://egov.maryland.gov/BusinessExpress/EntitySearch"
MD_SDAT_SOURCE_NAME = "Maryland SDAT Business Express"


@dataclass
class _MDTradeName:
    value: str
    status: str | None
    linked_department_id: str
    raw: dict[str, str | int | bool | None]


@dataclass
class _MDRelationship:
    name: str
    role: str
    current: bool
    raw: dict[str, str | int | bool | None]


@dataclass
class _MDCandidate:
    department_id: str
    legal_name: str
    status: str | None
    principal_office_address: str | None
    source_url: str
    lookup_at: str | None
    search_term: str | None
    search_mode: str | None
    raw: dict[str, str | int | bool | None]
    trade_names: list[_MDTradeName] = field(default_factory=list)
    relationships: list[_MDRelationship] = field(default_factory=list)


class MDSDATRegistrySource:
    """Respect Business Express access controls and accept explicit manual evidence."""

    jurisdiction = "MD"
    source_name = MD_SDAT_SOURCE_NAME

    def __init__(
        self,
        *,
        manual_evidence: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._manual_evidence = manual_evidence
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult:
        fetched_at = self._clock()
        if self._manual_evidence is None:
            return RegistryEnrichmentResult(
                jurisdiction=self.jurisdiction,
                registry=self.source_name,
                status="skipped",
                url=MD_BUSINESS_EXPRESS_URL,
                fetched_at=fetched_at,
                notes=[
                    "Maryland Business Express protects entity search and entity-detail submission with Cloudflare Turnstile.",
                    "No published unauthenticated automation contract is configured; protected browser interactions were not automated.",
                    "A manual official-source evidence export may be supplied for auditable enrichment.",
                ],
            )
        try:
            candidates = _parse_manual_evidence(self._manual_evidence)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return RegistryEnrichmentResult(
                jurisdiction=self.jurisdiction,
                registry=self.source_name,
                status="unreachable",
                url=MD_BUSINESS_EXPRESS_URL,
                fetched_at=fetched_at,
                notes=[f"Maryland manual official-source evidence was invalid: {exc}"],
            )
        return _establish_md_candidate(identity, candidates, fetched_at)


def _parse_manual_evidence(raw_evidence: str) -> list[_MDCandidate]:
    payload = json.loads(raw_evidence)
    if not isinstance(payload, dict):
        raise ValueError("manual evidence must be a JSON object")
    source_url = _required_string(payload, "source_url")
    host = (urlparse(source_url).hostname or "").casefold()
    if host != "egov.maryland.gov":
        raise ValueError("manual evidence source_url is not Maryland Business Express")
    if payload.get("capture_method") != "manual_official_source":
        raise ValueError("manual evidence must declare capture_method=manual_official_source")
    lookup_at = _optional_string(payload, "lookup_at")
    search_term = _optional_string(payload, "search_term")
    search_mode = _optional_string(payload, "search_mode")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("manual evidence omitted candidates")

    candidates: list[_MDCandidate] = []
    for raw_candidate in raw_candidates:
        candidate = _string_keyed_object(raw_candidate, "candidate")
        department_id = _required_string(candidate, "Department ID")
        trades: list[_MDTradeName] = []
        for raw_trade in _object_list(candidate.get("Trade Names"), "Trade Names"):
            linked_id = _required_string(raw_trade, "Department ID")
            if linked_id != department_id:
                continue
            trades.append(
                _MDTradeName(
                    value=_required_string(raw_trade, "Trade Name"),
                    status=_optional_string(raw_trade, "Status"),
                    linked_department_id=linked_id,
                    raw=_scalar_fields(raw_trade),
                )
            )
        relationships = [
            _MDRelationship(
                name=_required_string(item, "Name"),
                role=_required_string(item, "Role"),
                current=item.get("Current") is True,
                raw=_scalar_fields(item),
            )
            for item in _object_list(candidate.get("Relationships"), "Relationships")
        ]
        candidates.append(
            _MDCandidate(
                department_id=department_id,
                legal_name=_required_string(candidate, "Business Name"),
                status=_optional_string(candidate, "Business Status"),
                principal_office_address=_optional_string(candidate, "Principal Office"),
                source_url=source_url,
                lookup_at=lookup_at,
                search_term=search_term,
                search_mode=search_mode,
                raw=_scalar_fields(candidate),
                trade_names=trades,
                relationships=relationships,
            )
        )
    return candidates


def _establish_md_candidate(
    identity: BusinessIdentity,
    candidates: list[_MDCandidate],
    fetched_at: datetime,
) -> RegistryEnrichmentResult:
    if not candidates:
        return RegistryEnrichmentResult(
            jurisdiction="MD",
            registry=MD_SDAT_SOURCE_NAME,
            status="not_found",
            url=MD_BUSINESS_EXPRESS_URL,
            fetched_at=fetched_at,
            notes=["The supplied manual official-source lookup recorded no candidate."],
        )

    base_names = _identity_business_names(identity)
    surviving: list[tuple[_MDCandidate, list[RegistryEvidence]]] = []
    all_evidence: list[RegistryEvidence] = []
    conflicts: list[str] = []
    for candidate in candidates:
        evidence = _candidate_evidence(identity, candidate, base_names)
        all_evidence.extend(evidence)
        alias_exact = bool(identity.business_name) and any(
            _active_trade_name(trade)
            and name_relationship(identity.business_name, trade.value) == "exact_normalized"
            for trade in candidate.trade_names
        )
        legal_exact = any(
            registry_legal_name_equal(candidate.legal_name, name)
            for name in base_names
        )
        plausible = any(
            name_relationship(candidate.legal_name, name)
            in {"exact_normalized", "partial", "fuzzy"}
            for name in base_names
        )
        address_exact = _addresses_equal(identity.address, candidate.principal_office_address)
        exact_relationship = alias_exact or legal_exact
        if (
            _address_conflicts(identity.address, candidate.principal_office_address)
            and not exact_relationship
        ):
            conflicts.append(
                f"entity_{candidate.department_id}_principal_office_address_conflicts_with_places_address"
            )
            continue
        if exact_relationship or (plausible and address_exact):
            surviving.append((candidate, evidence))

    if len(surviving) != 1:
        return RegistryEnrichmentResult(
            jurisdiction="MD",
            registry=MD_SDAT_SOURCE_NAME,
            status="ambiguous",
            url=MD_BUSINESS_EXPRESS_URL,
            fetched_at=fetched_at,
            evidence=all_evidence,
            conflicts=list(dict.fromkeys(conflicts)),
            notes=[
                f"Manual Maryland evidence contained {len(candidates)} candidate(s), but {len(surviving)} uniquely satisfied the establishment policy."
            ],
        )

    candidate, evidence = surviving[0]
    qualifying_relationships = [
        relationship
        for relationship in candidate.relationships
        if relationship.current and _qualifying_principal_role(relationship.role)
    ]
    return RegistryEnrichmentResult(
        jurisdiction="MD",
        registry=MD_SDAT_SOURCE_NAME,
        status="ok",
        url=candidate.source_url,
        fetched_at=fetched_at,
        established_entity_id=candidate.department_id,
        established_entity_name=candidate.legal_name,
        entity_status=candidate.status,
        principal_office_address=candidate.principal_office_address,
        legal_names=[
            RegistryName(
                value=candidate.legal_name,
                kind="legal_name",
                raw_field="Business Name",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
            )
        ],
        dbas=[
            RegistryName(
                value=trade.value,
                kind="dba",
                role="current trade name",
                raw_field="Trade Name linked by Department ID",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                notes=[f"Official trade-name status: {trade.status or 'unavailable'}."],
            )
            for trade in candidate.trade_names
            if _active_trade_name(trade)
        ],
        principals=[
            RegistryName(
                value=relationship.name,
                kind="principal",
                role=relationship.role,
                raw_field="Explicit current relationship",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
            )
            for relationship in qualifying_relationships
        ],
        evidence=evidence,
        notes=[
            "One Maryland entity satisfied the conservative establishment policy from manually captured official evidence.",
            "The lookup was manual; no protected Business Express interaction was automated.",
        ],
    )


def _candidate_evidence(
    identity: BusinessIdentity,
    candidate: _MDCandidate,
    base_names: list[str],
) -> list[RegistryEvidence]:
    capture_notes = [
        f"Manual lookup time: {candidate.lookup_at or 'unavailable'}.",
        f"Search mode: {candidate.search_mode or 'unavailable'}.",
        f"Search term: {candidate.search_term or 'unavailable'}.",
    ]
    evidence = [
        RegistryEvidence(
            kind="md_sdat_manual_entity_candidate",
            source_name=MD_SDAT_SOURCE_NAME,
            source_url=candidate.source_url,
            entity_id=candidate.department_id,
            observed=candidate.raw,
            notes=capture_notes,
        )
    ]
    if any(
        registry_legal_name_equal(candidate.legal_name, name)
        for name in base_names
    ):
        evidence.append(
            RegistryEvidence(
                kind="exact_legal_name",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                observed={"Business Name": candidate.legal_name},
            )
        )
    for trade in candidate.trade_names:
        evidence.append(
            RegistryEvidence(
                kind="linked_trade_name",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                observed=trade.raw,
            )
        )
        if (
            identity.business_name
            and _active_trade_name(trade)
            and name_relationship(identity.business_name, trade.value) == "exact_normalized"
        ):
            evidence.append(
                RegistryEvidence(
                    kind="exact_trade_name_relationship",
                    source_name=MD_SDAT_SOURCE_NAME,
                    source_url=candidate.source_url,
                    entity_id=candidate.department_id,
                    observed=trade.raw,
                )
            )
    for relationship in candidate.relationships:
        role = relationship.role.casefold()
        evidence.append(
            RegistryEvidence(
                kind=(
                    "resident_agent"
                    if "resident agent" in role
                    else "explicit_filer_relationship"
                ),
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                observed=relationship.raw,
                notes=(
                    ["Resident-agent facts are evidence only and never principal keys."]
                    if "resident agent" in role
                    else []
                ),
            )
        )
    if _addresses_equal(identity.address, candidate.principal_office_address):
        evidence.append(
            RegistryEvidence(
                kind="exact_principal_office_address",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                observed={"Principal Office": candidate.principal_office_address},
            )
        )
    elif identity.address and candidate.principal_office_address:
        evidence.append(
            RegistryEvidence(
                kind="registry_address_differs_from_places_address",
                source_name=MD_SDAT_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.department_id,
                observed={
                    "places_address": identity.address,
                    "principal_office_address": candidate.principal_office_address,
                },
                notes=[
                    "A differing principal office address is retained as evidence; an exact legal-name or current trade-name relationship may link a branch or service location to this entity."
                ],
            )
        )
    return evidence


def _qualifying_principal_role(role: str) -> bool:
    normalized = " ".join(role.casefold().split())
    if any(term in normalized for term in ("agent", "organizer", "signer", "filer")):
        return False
    return normalized in {
        "officer",
        "director",
        "member",
        "manager",
        "governor",
        "beneficial owner",
        "owner",
        "president",
        "vice president",
        "treasurer",
        "secretary",
    }


def _active_trade_name(trade: _MDTradeName) -> bool:
    status = (trade.status or "").casefold()
    return "active" in status and "inactive" not in status


def _identity_business_names(identity: BusinessIdentity) -> list[str]:
    return list(
        dict.fromkeys(
            value.strip()
            for value in (identity.legal_name, identity.dba, identity.business_name)
            if value and value.strip()
        )
    )


def _required_string(values: dict[str, object], key: str) -> str:
    value = _optional_string(values, key)
    if value is None:
        raise ValueError(f"manual evidence omitted {key}")
    return value


def _optional_string(values: dict[str, object], key: str) -> str | None:
    value = values.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_keyed_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [_string_keyed_object(item, label) for item in value]


def _scalar_fields(values: dict[str, object]) -> dict[str, str | int | bool | None]:
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, (str, int, bool)) or value is None
    }


def _addresses_equal(left: str | None, right: str | None) -> bool:
    return registry_addresses_equal(left, right)


def _address_conflicts(left: str | None, right: str | None) -> bool:
    return bool(left and right) and not _addresses_equal(left, right)
