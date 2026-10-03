"""District of Columbia registry enrichment from official DLCP open data."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.registry_matching import registry_addresses_equal, registry_legal_name_equal
from app.search_keys import name_relationship


DC_REGISTRY_SOURCE_NAME = "DC DLCP Open Data registries"
_DC_ARCGIS_SERVICE = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Business_Licensing_and_Grants_WebMercator/FeatureServer"
)
DC_CORPORATE_QUERY_URL = f"{_DC_ARCGIS_SERVICE}/0/query"
DC_TRADE_NAME_QUERY_URL = f"{_DC_ARCGIS_SERVICE}/1/query"
DC_BENEFICIAL_OWNER_QUERY_URL = f"{_DC_ARCGIS_SERVICE}/2/query"
DC_PAGE_SIZE = 250
_CORPORATE_FIELDS = (
    "FILE_NUMBER", "BUSINESS_NAME", "ENTITY_STATUS",
    "BUSNIESS_ADDRESS_LINE1", "BUSNIESS_ADDRESS_LINE2",
    "BUSNIESS_ADDRESS_LINE3", "BUSNIESS_ADDRESS_LINE4",
    "BUSINESS_CITY", "BUSINESS_STATE", "ZIPCODE", "BUSINESS_COUNTRY",
    "RA_NAME", "RA_ADDRESS1", "RA_ADDRESS2", "RA_ADDRESS3", "RA_ADDRESS4",
    "RA_CITY", "RA_STATE", "RA_ZIPCODE",
)
_TRADE_NAME_FIELDS = (
    "TRADE_NAME", "FILE_NUMBER", "INITIAL_FILENUMBER", "TRADENAME_STATUS",
)
_BENEFICIAL_OWNER_FIELDS = ("NAME", "INITIALFILENUMBER", "STATUS")


@dataclass
class _DCRecord:
    fields: dict[str, str | int | bool | None]
    source_url: str


@dataclass
class _DCCandidate:
    file_number: str
    legal_name: str
    status: str | None
    business_address: str | None
    corporate: _DCRecord
    trade_names: list[_DCRecord] = field(default_factory=list)
    beneficial_owners: list[_DCRecord] = field(default_factory=list)


class DCDLCPRegistrySource:
    """Join official corporate, trade-name, and beneficial-owner open data."""

    jurisdiction = "DC"
    source_name = DC_REGISTRY_SOURCE_NAME

    def __init__(
        self,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=True, timeout=30.0)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult:
        fetched_at = self._clock()
        names = _identity_business_names(identity)
        if not names:
            return self._result(
                "skipped",
                fetched_at,
                notes=["The base identity has no established business name for DC registry search."],
            )

        try:
            corporate_records: dict[str, _DCRecord] = {}
            for name in names:
                for record in self._query(
                    DC_CORPORATE_QUERY_URL,
                    _contains("BUSINESS_NAME", name),
                    required_fields=_CORPORATE_FIELDS,
                ):
                    file_number = _text(record.fields, "FILE_NUMBER")
                    if file_number:
                        corporate_records[file_number] = record

                matches = self._query(
                    DC_TRADE_NAME_QUERY_URL,
                    _contains("TRADE_NAME", name),
                    required_fields=_TRADE_NAME_FIELDS,
                )
                for trade_record in matches:
                    owner_file = _text(trade_record.fields, "INITIAL_FILENUMBER")
                    if not owner_file:
                        continue
                    for corporate_record in self._query(
                        DC_CORPORATE_QUERY_URL,
                        _equals("FILE_NUMBER", owner_file),
                        required_fields=_CORPORATE_FIELDS,
                    ):
                        corporate_records[owner_file] = corporate_record

            candidates: list[_DCCandidate] = []
            for file_number, corporate in corporate_records.items():
                trades = self._query(
                    DC_TRADE_NAME_QUERY_URL,
                    _equals("INITIAL_FILENUMBER", file_number),
                    required_fields=_TRADE_NAME_FIELDS,
                )
                owners = self._query(
                    DC_BENEFICIAL_OWNER_QUERY_URL,
                    _equals("INITIALFILENUMBER", file_number),
                    required_fields=_BENEFICIAL_OWNER_FIELDS,
                )
                candidates.append(_candidate(corporate, trades, owners))
        except (httpx.HTTPError, ValueError) as exc:
            return self._result(
                "unreachable",
                fetched_at,
                notes=[f"DC DLCP open data could not be read safely: {exc}"],
            )

        return _establish_dc_candidate(identity, candidates, fetched_at)

    def _query(
        self,
        url: str,
        where: str,
        *,
        required_fields: tuple[str, ...],
    ) -> list[_DCRecord]:
        records: list[_DCRecord] = []
        offset = 0
        while True:
            params = {
                "where": where,
                "outFields": ",".join(required_fields),
                "returnGeometry": "false",
                "resultOffset": str(offset),
                "resultRecordCount": str(DC_PAGE_SIZE),
                "f": "json",
            }
            response = self._get_page_with_retry(url, params)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("DC open data returned invalid JSON.") from exc
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("DC open data returned an ArcGIS query error.")
            features = payload.get("features")
            if not isinstance(features, list) or not all(
                isinstance(feature, dict) for feature in features
            ):
                raise ValueError("DC open data returned an unexpected feature shape.")
            for feature in features:
                attributes = feature.get("attributes")
                if not isinstance(attributes, dict):
                    raise ValueError("DC open data omitted feature attributes.")
                fields = {
                    str(key): value
                    for key, value in attributes.items()
                    if isinstance(value, (str, int, bool)) or value is None
                }
                if not all(field in fields for field in required_fields):
                    raise ValueError(
                        "DC open data omitted required fields: "
                        + ", ".join(required_fields)
                    )
                records.append(_DCRecord(fields, str(response.request.url)))
            if not payload.get("exceededTransferLimit"):
                return records
            if not features:
                raise ValueError("DC open-data pagination stopped unexpectedly.")
            offset += len(features)

    def _get_page_with_retry(
        self, url: str, params: dict[str, str]
    ) -> httpx.Response:
        for attempt in range(2):
            try:
                return self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 1:
                    raise
        raise AssertionError("unreachable retry state")

    def _result(
        self,
        status: str,
        fetched_at: datetime,
        *,
        notes: list[str] | None = None,
    ) -> RegistryEnrichmentResult:
        return RegistryEnrichmentResult(
            jurisdiction=self.jurisdiction,
            registry=self.source_name,
            status=status,
            url=DC_CORPORATE_QUERY_URL,
            fetched_at=fetched_at,
            notes=notes or [],
        )


def _candidate(
    corporate: _DCRecord,
    trades: list[_DCRecord],
    owners: list[_DCRecord],
) -> _DCCandidate:
    file_number = _required_text(corporate.fields, "FILE_NUMBER")
    legal_name = _required_text(corporate.fields, "BUSINESS_NAME")
    return _DCCandidate(
        file_number=file_number,
        legal_name=legal_name,
        status=_text(corporate.fields, "ENTITY_STATUS"),
        business_address=_corporate_address(corporate.fields),
        corporate=corporate,
        trade_names=[
            record
            for record in trades
            if _text(record.fields, "INITIAL_FILENUMBER") == file_number
        ],
        beneficial_owners=[
            record
            for record in owners
            if _text(record.fields, "INITIALFILENUMBER") == file_number
        ],
    )


def _establish_dc_candidate(
    identity: BusinessIdentity,
    candidates: list[_DCCandidate],
    fetched_at: datetime,
) -> RegistryEnrichmentResult:
    if not candidates:
        return RegistryEnrichmentResult(
            jurisdiction="DC",
            registry=DC_REGISTRY_SOURCE_NAME,
            status="not_found",
            url=DC_CORPORATE_QUERY_URL,
            fetched_at=fetched_at,
            notes=["The official DC corporate and trade-name registries returned no linked entity candidate."],
        )

    base_names = _identity_business_names(identity)
    surviving: list[tuple[_DCCandidate, list[RegistryEvidence]]] = []
    candidate_evidence: list[RegistryEvidence] = []
    conflicts: list[str] = []
    for candidate in candidates:
        evidence = _candidate_evidence(identity, candidate, base_names)
        candidate_evidence.extend(evidence)
        alias_exact = bool(identity.business_name) and any(
            name_relationship(
                identity.business_name,
                _required_text(record.fields, "TRADE_NAME"),
            )
            == "exact_normalized"
            for record in candidate.trade_names
            if _trade_name_category(record) == "current"
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
        address_exact = _addresses_equal(identity.address, candidate.business_address)
        exact_relationship = alias_exact or legal_exact
        if (
            _address_conflicts(identity.address, candidate.business_address)
            and not exact_relationship
        ):
            conflicts.append(
                f"entity_{candidate.file_number}_business_address_conflicts_with_places_address"
            )
            continue
        if exact_relationship or (plausible and address_exact):
            surviving.append((candidate, evidence))

    if len(surviving) != 1:
        return RegistryEnrichmentResult(
            jurisdiction="DC",
            registry=DC_REGISTRY_SOURCE_NAME,
            status="ambiguous",
            url=DC_CORPORATE_QUERY_URL,
            fetched_at=fetched_at,
            evidence=candidate_evidence,
            conflicts=list(dict.fromkeys(conflicts)),
            notes=[
                f"DC open data returned {len(candidates)} candidate(s), but {len(surviving)} uniquely satisfied the establishment policy."
            ],
        )

    candidate, evidence = surviving[0]
    legal_name = RegistryName(
        value=candidate.legal_name,
        kind="legal_name",
        raw_field="BUSINESS_NAME",
        source_name="DC Corporate Registration",
        source_url=candidate.corporate.source_url,
        entity_id=candidate.file_number,
    )
    searchable_trade_names = sorted(
        (
            record
            for record in candidate.trade_names
            if _trade_name_category(record) in {"current", "historical"}
        ),
        key=lambda record: _trade_name_category(record) != "current",
    )
    dbas = [
        RegistryName(
            value=_required_text(record.fields, "TRADE_NAME"),
            kind="dba",
            role=(
                "current trade name"
                if _trade_name_category(record) == "current"
                else "historical trade name"
            ),
            raw_field="TRADE_NAME linked by INITIAL_FILENUMBER",
            source_name="DC Trade Name",
            source_url=record.source_url,
            entity_id=candidate.file_number,
            notes=[
                f"Official trade-name status: {_trade_name_status(record) or 'unavailable'}.",
                (
                    "Current trade name."
                    if _trade_name_category(record) == "current"
                    else "Historical trade name; retained for license discovery after independent corporate establishment."
                ),
            ],
        )
        for record in searchable_trade_names
    ]
    principals = [
        RegistryName(
            value=_required_text(record.fields, "NAME"),
            kind="principal",
            role="beneficial owner",
            raw_field="NAME linked by INITIALFILENUMBER",
            source_name="DC Beneficial Owners",
            source_url=record.source_url,
            entity_id=candidate.file_number,
            notes=[
                "last reported; relationship currentness unavailable",
                f"Linked corporate standing in this record: {_text(record.fields, 'STATUS') or 'unavailable'}.",
            ],
        )
        for record in candidate.beneficial_owners
    ]
    return RegistryEnrichmentResult(
        jurisdiction="DC",
        registry=DC_REGISTRY_SOURCE_NAME,
        status="ok",
        url=candidate.corporate.source_url,
        fetched_at=fetched_at,
        established_entity_id=candidate.file_number,
        established_entity_name=candidate.legal_name,
        entity_status=candidate.status,
        principal_office_address=candidate.business_address,
        legal_names=[legal_name],
        dbas=dbas,
        principals=principals,
        evidence=evidence,
        notes=["One DC corporate file satisfied the conservative establishment policy."],
    )


def _candidate_evidence(
    identity: BusinessIdentity,
    candidate: _DCCandidate,
    base_names: list[str],
) -> list[RegistryEvidence]:
    evidence = [
        RegistryEvidence(
            kind="dc_corporate_candidate",
            source_name="DC Corporate Registration",
            source_url=candidate.corporate.source_url,
            entity_id=candidate.file_number,
            observed=candidate.corporate.fields,
        )
    ]
    if any(
        registry_legal_name_equal(candidate.legal_name, name)
        for name in base_names
    ):
        evidence.append(
            RegistryEvidence(
                kind="exact_legal_name",
                source_name="DC Corporate Registration",
                source_url=candidate.corporate.source_url,
                entity_id=candidate.file_number,
                observed={"BUSINESS_NAME": candidate.legal_name},
            )
        )
    for record in candidate.trade_names:
        category = _trade_name_category(record)
        raw_status = _trade_name_status(record) or "unavailable"
        evidence.append(
            RegistryEvidence(
                kind="linked_trade_name",
                source_name="DC Trade Name",
                source_url=record.source_url,
                entity_id=candidate.file_number,
                observed=record.fields,
                notes=[
                    f"Trade-name status classification: {category}.",
                    f"Official trade-name status: {raw_status}.",
                    *(
                        ["Transferred trade names are evidence only and never search keys."]
                        if category == "transferred"
                        else []
                    ),
                ],
            )
        )
        if identity.business_name and name_relationship(
            identity.business_name, _required_text(record.fields, "TRADE_NAME")
        ) == "exact_normalized":
            relationship_kind = {
                "current": "exact_trade_name_relationship",
                "historical": "historical_trade_name_relationship_evidence_only",
                "transferred": "transferred_trade_name_relationship_evidence_only",
                "other": "unclassified_trade_name_relationship_evidence_only",
            }[category]
            evidence.append(
                RegistryEvidence(
                    kind=relationship_kind,
                    source_name="DC Trade Name",
                    source_url=record.source_url,
                    entity_id=candidate.file_number,
                    observed=record.fields,
                    notes=[
                        (
                            "Only a current trade-name relationship may establish the corporate entity."
                            if category == "current"
                            else "This trade-name relationship is evidence only and cannot establish the corporate entity."
                        )
                    ],
                )
            )
    for record in candidate.beneficial_owners:
        evidence.append(
            RegistryEvidence(
                kind="linked_beneficial_owner",
                source_name="DC Beneficial Owners",
                source_url=record.source_url,
                entity_id=candidate.file_number,
                observed=record.fields,
                notes=["last reported; relationship currentness unavailable"],
            )
        )
    if _addresses_equal(identity.address, candidate.business_address):
        evidence.append(
            RegistryEvidence(
                kind="exact_business_address",
                source_name="DC Corporate Registration",
                source_url=candidate.corporate.source_url,
                entity_id=candidate.file_number,
                observed={"business_address": candidate.business_address},
            )
        )
    elif identity.address and candidate.business_address:
        evidence.append(
            RegistryEvidence(
                kind="registry_address_differs_from_places_address",
                source_name="DC Corporate Registration",
                source_url=candidate.corporate.source_url,
                entity_id=candidate.file_number,
                observed={
                    "places_address": identity.address,
                    "business_address": candidate.business_address,
                },
                notes=[
                    "A differing corporate office address is retained as evidence; an exact legal-name or current trade-name relationship may link a branch or service location to this entity."
                ],
            )
        )
    registered_agent = _text(candidate.corporate.fields, "RA_NAME")
    registered_address = _registered_agent_address(candidate.corporate.fields)
    if registered_agent or registered_address:
        evidence.append(
            RegistryEvidence(
                kind="registered_agent",
                source_name="DC Corporate Registration",
                source_url=candidate.corporate.source_url,
                entity_id=candidate.file_number,
                observed={
                    "RA_NAME": registered_agent,
                    "registered_agent_address": registered_address,
                },
                notes=["Registered-agent facts are evidence only and never principal keys."],
            )
        )
    return evidence


def _identity_business_names(identity: BusinessIdentity) -> list[str]:
    return list(
        dict.fromkeys(
            value.strip()
            for value in (identity.legal_name, identity.dba, identity.business_name)
            if value and value.strip()
        )
    )


def _contains(field: str, value: str) -> str:
    escaped = value.upper().replace("'", "''")
    return f"UPPER({field}) LIKE '%{escaped}%'"


def _equals(field: str, value: str) -> str:
    escaped = value.replace("'", "''")
    return f"{field} = '{escaped}'"


def _text(fields: dict[str, object], name: str) -> str | None:
    value = fields.get(name)
    return str(value).strip() if value is not None and str(value).strip() else None


def _required_text(fields: dict[str, object], name: str) -> str:
    value = _text(fields, name)
    if value is None:
        raise ValueError(f"DC open data omitted required value {name}.")
    return value


def _corporate_address(fields: dict[str, object]) -> str | None:
    return _join(
        [
            _text(fields, "BUSNIESS_ADDRESS_LINE1"),
            _text(fields, "BUSNIESS_ADDRESS_LINE2"),
            _text(fields, "BUSNIESS_ADDRESS_LINE3"),
            _text(fields, "BUSNIESS_ADDRESS_LINE4"),
            _text(fields, "BUSINESS_CITY"),
            _text(fields, "BUSINESS_STATE"),
            _text(fields, "ZIPCODE"),
            _text(fields, "BUSINESS_COUNTRY"),
        ]
    )


def _registered_agent_address(fields: dict[str, object]) -> str | None:
    return _join(
        [
            _text(fields, "RA_ADDRESS1"),
            _text(fields, "RA_ADDRESS2"),
            _text(fields, "RA_ADDRESS3"),
            _text(fields, "RA_ADDRESS4"),
            _text(fields, "RA_CITY"),
            _text(fields, "RA_STATE"),
            _text(fields, "RA_ZIPCODE"),
        ]
    )


def _join(values: list[str | None]) -> str | None:
    present = [value for value in values if value]
    return ", ".join(present) or None


def _trade_name_status(record: _DCRecord) -> str | None:
    return _text(record.fields, "TRADENAME_STATUS")


def _trade_name_category(record: _DCRecord) -> str:
    status = (_trade_name_status(record) or "").casefold()
    if "transferred" in status:
        return "transferred"
    if any(value in status for value in ("inactive", "expired", "cancelled", "revoked")):
        return "historical"
    if "active" in status:
        return "current"
    return "other"


def _addresses_equal(left: str | None, right: str | None) -> bool:
    return registry_addresses_equal(left, right)


def _address_conflicts(left: str | None, right: str | None) -> bool:
    return bool(left and right) and not _addresses_equal(left, right)
