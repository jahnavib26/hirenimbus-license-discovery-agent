"""California Secretary of State registry enrichment via ordinary public access."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.registry_matching import registry_addresses_equal, registry_legal_name_equal
from app.search_keys import name_relationship


CA_BIZFILE_URL = "https://bizfileonline.sos.ca.gov/"
CA_SOS_SOURCE_NAME = "California Secretary of State bizfile Online"


@dataclass
class _CAPerson:
    name: str
    role: str
    raw: dict[str, str | int | bool | None]


@dataclass
class _CACandidate:
    entity_number: str
    legal_name: str
    status: str | None
    business_address: str | None
    source_url: str
    lookup_at: str | None
    search_term: str | None
    search_mode: str | None
    raw: dict[str, str | int | bool | None]
    people: list[_CAPerson] = field(default_factory=list)
    agent_name: str | None = None
    agent_address: str | None = None


class CASOSRegistrySource:
    """Use safe bizfile access and stop when WAF or UI-only access intervenes."""

    jurisdiction = "CA"
    source_name = CA_SOS_SOURCE_NAME

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        manual_evidence: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=True, timeout=30.0)
        self._manual_evidence = manual_evidence
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult:
        fetched_at = self._clock()
        if self._manual_evidence is not None:
            try:
                candidates = _parse_manual_evidence(self._manual_evidence)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return self._result(
                    "unreachable",
                    fetched_at,
                    notes=[f"California manual official-source evidence was invalid: {exc}"],
                )
            return _establish_ca_candidate(identity, candidates, fetched_at)

        try:
            response = self._client.get(CA_BIZFILE_URL)
            if _waf_blocked(response):
                return self._result(
                    "unreachable",
                    fetched_at,
                    url=str(response.url),
                    notes=[
                        "California bizfile Online blocked ordinary backend access with WAF/anti-bot behavior; no bypass was attempted."
                    ],
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return self._result(
                "unreachable",
                fetched_at,
                notes=[f"California bizfile Online could not be reached safely: {exc}"],
            )
        return self._result(
            "skipped",
            fetched_at,
            url=str(response.url),
            notes=[
                "The public bizfile UI was reachable, but no documented unauthenticated entity-search API was configured.",
                "No UI automation or anti-bot circumvention was attempted.",
                "County fictitious-business-name filings are outside Phase 2.",
            ],
        )

    def _result(
        self,
        status: str,
        fetched_at: datetime,
        *,
        url: str = CA_BIZFILE_URL,
        notes: list[str] | None = None,
    ) -> RegistryEnrichmentResult:
        return RegistryEnrichmentResult(
            jurisdiction=self.jurisdiction,
            registry=self.source_name,
            status=status,
            url=url,
            fetched_at=fetched_at,
            notes=notes or [],
        )


def _waf_blocked(response: httpx.Response) -> bool:
    body = response.text.casefold()
    return response.status_code in {403, 429} or any(
        marker in body
        for marker in (
            "access denied",
            "request blocked",
            "cloudflare",
            "captcha",
            "bot detection",
        )
    )


def _parse_manual_evidence(raw_evidence: str) -> list[_CACandidate]:
    payload = json.loads(raw_evidence)
    if not isinstance(payload, dict):
        raise ValueError("manual evidence must be a JSON object")
    source_url = _required_string(payload, "source_url")
    host = (urlparse(source_url).hostname or "").casefold()
    if host != "bizfileonline.sos.ca.gov":
        raise ValueError("manual evidence source_url is not California bizfile Online")
    if payload.get("capture_method") != "manual_official_source":
        raise ValueError("manual evidence must declare capture_method=manual_official_source")
    lookup_at = _optional_string(payload, "lookup_at")
    search_term = _optional_string(payload, "search_term")
    search_mode = _optional_string(payload, "search_mode")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("manual evidence omitted candidates")

    candidates: list[_CACandidate] = []
    for raw_candidate in raw_candidates:
        candidate = _string_keyed_object(raw_candidate, "candidate")
        people = [
            _CAPerson(
                name=_required_string(item, "Name"),
                role=_required_string(item, "Role"),
                raw=_scalar_fields(item),
            )
            for item in _object_list(candidate.get("People"), "People")
        ]
        agent = candidate.get("Agent for Service of Process")
        agent_record = (
            _string_keyed_object(agent, "Agent for Service of Process")
            if agent is not None
            else {}
        )
        candidates.append(
            _CACandidate(
                entity_number=_required_string(candidate, "Entity Number"),
                legal_name=_required_string(candidate, "Entity Name"),
                status=_optional_string(candidate, "Status"),
                business_address=_optional_string(candidate, "Business Address"),
                source_url=source_url,
                lookup_at=lookup_at,
                search_term=search_term,
                search_mode=search_mode,
                raw=_scalar_fields(candidate),
                people=people,
                agent_name=_optional_string(agent_record, "Name"),
                agent_address=_optional_string(agent_record, "Address"),
            )
        )
    return candidates


def _establish_ca_candidate(
    identity: BusinessIdentity,
    candidates: list[_CACandidate],
    fetched_at: datetime,
) -> RegistryEnrichmentResult:
    if not candidates:
        return RegistryEnrichmentResult(
            jurisdiction="CA",
            registry=CA_SOS_SOURCE_NAME,
            status="not_found",
            url=CA_BIZFILE_URL,
            fetched_at=fetched_at,
            notes=["The supplied manual official-source lookup recorded no candidate."],
        )

    base_names = _identity_business_names(identity)
    surviving: list[tuple[_CACandidate, list[RegistryEvidence]]] = []
    all_evidence: list[RegistryEvidence] = []
    conflicts: list[str] = []
    for candidate in candidates:
        evidence = _candidate_evidence(identity, candidate, base_names)
        all_evidence.extend(evidence)
        legal_exact = any(
            registry_legal_name_equal(candidate.legal_name, name)
            for name in base_names
        )
        plausible = any(
            name_relationship(candidate.legal_name, name)
            in {"exact_normalized", "partial", "fuzzy"}
            for name in base_names
        )
        address_exact = _addresses_equal(identity.address, candidate.business_address)
        if (
            _address_conflicts(identity.address, candidate.business_address)
            and not legal_exact
        ):
            conflicts.append(
                f"entity_{candidate.entity_number}_business_address_conflicts_with_places_address"
            )
            continue
        if legal_exact or (plausible and address_exact):
            surviving.append((candidate, evidence))

    if len(surviving) != 1:
        return RegistryEnrichmentResult(
            jurisdiction="CA",
            registry=CA_SOS_SOURCE_NAME,
            status="ambiguous",
            url=CA_BIZFILE_URL,
            fetched_at=fetched_at,
            evidence=all_evidence,
            conflicts=list(dict.fromkeys(conflicts)),
            notes=[
                f"California evidence contained {len(candidates)} candidate(s), but {len(surviving)} uniquely satisfied the establishment policy."
            ],
        )

    candidate, evidence = surviving[0]
    principals = [person for person in candidate.people if _qualifying_principal_role(person.role)]
    return RegistryEnrichmentResult(
        jurisdiction="CA",
        registry=CA_SOS_SOURCE_NAME,
        status="ok",
        url=candidate.source_url,
        fetched_at=fetched_at,
        established_entity_id=candidate.entity_number,
        established_entity_name=candidate.legal_name,
        entity_status=candidate.status,
        principal_office_address=candidate.business_address,
        legal_names=[
            RegistryName(
                value=candidate.legal_name,
                kind="legal_name",
                raw_field="Entity Name",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
            )
        ],
        principals=[
            RegistryName(
                value=person.name,
                kind="principal",
                role=person.role,
                raw_field="People / explicit role",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
            )
            for person in principals
        ],
        evidence=evidence,
        notes=[
            "One California entity satisfied the conservative establishment policy from official evidence.",
            "County fictitious-business-name filings are outside Phase 2 and no DBA was inferred.",
        ],
    )


def _candidate_evidence(
    identity: BusinessIdentity,
    candidate: _CACandidate,
    base_names: list[str],
) -> list[RegistryEvidence]:
    evidence = [
        RegistryEvidence(
            kind="ca_sos_entity_candidate",
            source_name=CA_SOS_SOURCE_NAME,
            source_url=candidate.source_url,
            entity_id=candidate.entity_number,
            observed=candidate.raw,
            notes=[
                f"Manual lookup time: {candidate.lookup_at or 'unavailable'}.",
                f"Search mode: {candidate.search_mode or 'unavailable'}.",
                f"Search term: {candidate.search_term or 'unavailable'}.",
            ],
        )
    ]
    if any(
        registry_legal_name_equal(candidate.legal_name, name)
        for name in base_names
    ):
        evidence.append(
            RegistryEvidence(
                kind="exact_legal_name",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
                observed={"Entity Name": candidate.legal_name},
            )
        )
    if _addresses_equal(identity.address, candidate.business_address):
        evidence.append(
            RegistryEvidence(
                kind="exact_business_address",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
                observed={"Business Address": candidate.business_address},
            )
        )
    elif identity.address and candidate.business_address:
        evidence.append(
            RegistryEvidence(
                kind="registry_address_differs_from_places_address",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
                observed={
                    "places_address": identity.address,
                    "business_address": candidate.business_address,
                },
                notes=[
                    "A differing registry business address is retained as evidence; an exact legal-name relationship may link a branch or service location to this entity."
                ],
            )
        )
    for person in candidate.people:
        evidence.append(
            RegistryEvidence(
                kind="explicit_person_role",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
                observed=person.raw,
            )
        )
    if candidate.agent_name or candidate.agent_address:
        evidence.append(
            RegistryEvidence(
                kind="agent_for_service_of_process",
                source_name=CA_SOS_SOURCE_NAME,
                source_url=candidate.source_url,
                entity_id=candidate.entity_number,
                observed={
                    "Name": candidate.agent_name,
                    "Address": candidate.agent_address,
                },
                notes=["Agent-for-service facts are evidence only and never principal keys."],
            )
        )
    return evidence


def _qualifying_principal_role(role: str) -> bool:
    normalized = " ".join(role.casefold().split())
    if "agent" in normalized:
        return False
    return normalized in {
        "officer",
        "director",
        "member",
        "manager",
        "president",
        "vice president",
        "secretary",
        "treasurer",
        "chief executive officer",
        "chief financial officer",
    }


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
