"""End-to-end Day 1 -> Day 2 orchestration with a last-good cache."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from app.board_selection import select_boards
from app.cache import JsonLastGoodCache
from app.ca_registry import CASOSRegistrySource
from app.categories import CategoryMapper
from app.day2_models import Day2Result, LicenseNumberEvidence, SearchKey
from app.license_discovery import extract_license_numbers
from app.dc_registry import DCDLCPRegistrySource
from app.license_matching import assemble_day2_result
from app.license_sources import LicenseSourceRunner
from app.md_registry import MDSDATRegistrySource
from app.models import (
    BusinessIdentity,
    Evidence,
    IdentityCandidateHypothesis,
    IdentityLookupResult,
    NormalizedPhone,
)
from app.phone import InvalidPhoneNumber, parse_us_phone
from app.places import GooglePlacesClient, PlacesLookupError
from app.resolver import IdentityResolver
from app.registry_models import RegistryEnrichmentResult
from app.registry_sources import registry_result_is_established
from app.search_keys import generate_expanded_search_keys
from app.va_registry import VASCCRegistrySource
from app.website import (
    FirstPartyWebsiteClient,
    WebsiteClient,
    fetch_bounded_first_party_pages,
)


PipelineStatus = Literal["complete", "partial", "failed"]
CacheStatus = Literal[
    "hit",
    "miss_saved",
    "miss_not_saved",
    "refresh_saved",
    "refresh_partial_preserved",
    "refresh_failed_preserved",
]


class CacheMetadata(BaseModel):
    status: CacheStatus
    last_good_preserved: bool


class PipelineResult(BaseModel):
    input_phone: NormalizedPhone | None
    pipeline_status: PipelineStatus
    identity_result: IdentityLookupResult
    license_result: Day2Result | None = None
    registry_results: list[RegistryEnrichmentResult] = Field(default_factory=list)
    search_keys: list[SearchKey] = Field(default_factory=list)
    license_number_evidence: list[LicenseNumberEvidence] = Field(default_factory=list)
    cache: CacheMetadata
    notes: list[str] = Field(default_factory=list)


class IdentityResolverLike(Protocol):
    def resolve(self, raw_phone: str) -> IdentityLookupResult: ...


class LastGoodCacheLike(Protocol):
    def get(self, normalized_phone: str) -> dict[str, object] | None: ...

    def put(self, normalized_phone: str, result: dict[str, object]) -> None: ...


class RegistrySourceLike(Protocol):
    jurisdiction: str

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult: ...


class LicensePipeline:
    """Coordinate existing Day 1 and Day 2 behavior without duplicating it."""

    def __init__(
        self,
        *,
        identity_resolver: IdentityResolverLike | None = None,
        source_runner: LicenseSourceRunner | None = None,
        cache: LastGoodCacheLike | None = None,
        registry_sources: list[RegistrySourceLike] | None = None,
        website_client: WebsiteClient | None = None,
    ) -> None:
        self._identity_resolver = identity_resolver
        self._source_runner = source_runner
        self._website_client = website_client
        self._registry_sources = (
            registry_sources
            if registry_sources is not None
            else [
                VASCCRegistrySource(),
                DCDLCPRegistrySource(),
                MDSDATRegistrySource(),
                CASOSRegistrySource(),
            ]
        )
        self._cache = (
            cache if cache is not None else JsonLastGoodCache(_default_cache_path())
        )

    def find_licenses(
        self, phone: str, refresh: bool = False, market_hint: str | None = None
    ) -> PipelineResult:
        try:
            input_phone = parse_us_phone(phone)
        except InvalidPhoneNumber:
            identity_result = self._resolver().resolve(phone)
            return PipelineResult(
                input_phone=identity_result.input_phone,
                pipeline_status="failed",
                identity_result=identity_result,
                cache=CacheMetadata(
                    status="miss_not_saved", last_good_preserved=False
                ),
                notes=["Invalid input was not cached."],
            )

        # Namespace Phase 2 records so a Phase 1 payload cannot short-circuit
        # registry enrichment and expanded-key matching.
        hint_key = (market_hint or "").strip().casefold()
        cache_key = (
            f"phase2:expanded:{input_phone.e164}:{hint_key}"
            if hint_key
            else f"phase2:{input_phone.e164}"
        )
        cached_payload = self._cache.get(cache_key)
        if cached_payload is not None and not refresh:
            try:
                cached_result = PipelineResult.model_validate(cached_payload)
            except ValidationError:
                cached_payload = None
            else:
                return cached_result.model_copy(
                    update={
                        "input_phone": input_phone,
                        "cache": CacheMetadata(
                            status="hit", last_good_preserved=True
                        )
                    },
                    deep=True,
                )

        identity_result = self._resolver().resolve(phone)
        license_result: Day2Result | None = None
        registry_results: list[RegistryEnrichmentResult] = []
        search_registry_results: list[RegistryEnrichmentResult] = []
        search_keys: list[SearchKey] = []
        license_number_evidence: list[LicenseNumberEvidence] = []
        if identity_result.found is False and identity_result.candidate_hypotheses:
            promoted, hypothesis_results, selected_results = self._corroborate_hypotheses(
                identity_result, market_hint
            )
            registry_results.extend(hypothesis_results)
            if promoted is not None:
                identity_result = promoted
                search_registry_results.extend(selected_results)
        if identity_result.error is not None:
            pipeline_status: PipelineStatus = "failed"
        elif not identity_result.found:
            pipeline_status = (
                "partial"
                if any(item.status in {"unreachable", "captcha_blocked", "skipped"} for item in registry_results)
                else "complete"
            )
        else:
            identity = identity_result.identity
            if identity is None:
                pipeline_status = "failed"
            else:
                jurisdiction_evidence = _market_jurisdictions(market_hint)
                license_number_evidence = self._website_license_evidence(identity)
                for clue in license_number_evidence:
                    jurisdiction = _BOARD_JURISDICTIONS.get(clue.likely_board_id)
                    if jurisdiction:
                        jurisdiction_evidence.setdefault(
                            jurisdiction,
                            f"verified first-party website displayed a {clue.license_family} identifier routed to {clue.likely_board_id}",
                        )
                plan = select_boards(identity, jurisdiction_evidence)
                applicable_jurisdictions = {
                    *(_jurisdiction(state) for state in identity.states),
                    *jurisdiction_evidence,
                } & {"VA", "MD", "DC", "CA"}
                for source in self._registry_sources:
                    if _jurisdiction(source.jurisdiction) not in applicable_jurisdictions:
                        continue
                    if any(item.registry == getattr(source, "source_name", type(source).__name__)
                           and item.candidate_place_id == identity.place_id
                           for item in search_registry_results):
                        continue
                    try:
                        enriched = source.enrich(identity)
                        registry_results.append(enriched)
                        search_registry_results.append(enriched)
                    except Exception as exc:
                        failure = RegistryEnrichmentResult(
                                jurisdiction=_jurisdiction(source.jurisdiction),
                                registry=type(source).__name__,
                                status="unreachable",
                                notes=[f"Registry enrichment failed: {exc}"],
                            )
                        registry_results.append(failure)
                        search_registry_results.append(failure)
                search_keys = generate_expanded_search_keys(identity, search_registry_results)
                if license_number_evidence:
                    source_results = self._runner().search(
                        plan, search_keys, license_number_evidence
                    )
                else:
                    # Preserve the established two-argument runner seam used by
                    # downstream integrations that have no number evidence.
                    source_results = self._runner().search(plan, search_keys)
                license_result = assemble_day2_result(
                    identity,
                    source_results,
                    search_keys=search_keys,
                    registry_results=search_registry_results,
                )
                pipeline_status = _classify_day2(license_result, registry_results)

        result = PipelineResult(
            input_phone=input_phone,
            pipeline_status=pipeline_status,
            identity_result=identity_result,
            license_result=license_result,
            registry_results=registry_results,
            search_keys=search_keys,
            license_number_evidence=license_number_evidence,
            cache=CacheMetadata(
                status="miss_not_saved", last_good_preserved=False
            ),
        )

        if pipeline_status == "complete":
            cache_status: CacheStatus = "refresh_saved" if refresh else "miss_saved"
            result.cache = CacheMetadata(
                status=cache_status, last_good_preserved=False
            )
            self._cache.put(cache_key, result.model_dump(mode="json"))
            return result

        if cached_payload is not None and refresh:
            cache_status = (
                "refresh_partial_preserved"
                if pipeline_status == "partial"
                else "refresh_failed_preserved"
            )
            result.cache = CacheMetadata(
                status=cache_status, last_good_preserved=True
            )
        return result

    def _corroborate_hypotheses(
        self,
        lookup: IdentityLookupResult,
        market_hint: str | None,
    ) -> tuple[IdentityLookupResult | None, list[RegistryEnrichmentResult], list[RegistryEnrichmentResult]]:
        all_results: list[RegistryEnrichmentResult] = []
        qualifying: list[
            tuple[
                IdentityCandidateHypothesis,
                tuple[str, str, str],
                list[RegistryEnrichmentResult],
            ]
        ] = []
        hint_jurisdictions = _market_jurisdictions(market_hint)
        for hypothesis in lookup.candidate_hypotheses:
            identity = hypothesis.identity
            jurisdictions = {
                *(_jurisdiction(state) for state in identity.states),
                *hint_jurisdictions,
            } & {"VA", "MD", "DC", "CA"}
            candidate_results: list[RegistryEnrichmentResult] = []
            for source in self._registry_sources:
                jurisdiction = _jurisdiction(source.jurisdiction)
                if jurisdiction not in jurisdictions:
                    continue
                try:
                    result = source.enrich(identity)
                except Exception as exc:
                    result = RegistryEnrichmentResult(
                        jurisdiction=jurisdiction,
                        registry=getattr(source, "source_name", type(source).__name__),
                        status="unreachable",
                        notes=[f"Registry enrichment failed: {exc}"],
                    )
                result.candidate_place_id = identity.place_id
                candidate_results.append(result)
                all_results.append(result)
            hard_results = [item for item in candidate_results if _hard_registry_corroboration(item)]
            entity_keys = {_registry_entity_key(item) for item in hard_results}
            if len(entity_keys) == 1:
                qualifying.append((hypothesis, next(iter(entity_keys)), hard_results))

        # A Places search result remains only an acquisition hypothesis. If more
        # than one candidate was returned, each must resolve to the same official
        # registry entity before we can promote the shared identity.
        if not lookup.candidate_hypotheses or len(qualifying) != len(lookup.candidate_hypotheses):
            return None, all_results, []
        entity_keys = {entity_key for _, entity_key, _ in qualifying}
        if len(entity_keys) != 1:
            return None, all_results, []

        hypothesis, entity_key, hard_results = max(
            qualifying,
            key=lambda item: _hypothesis_evidence_strength(item[0]),
        )
        selected_results = [
            result for result in hard_results
            if _registry_entity_key(result) == entity_key
        ]
        base_evidence = [
            item for item in lookup.evidence
            if item.source != "official_business_website"
        ]
        hypothesis_evidence = [
            item
            for candidate_hypothesis in lookup.candidate_hypotheses
            for item in candidate_hypothesis.evidence
        ]
        promoted = IdentityLookupResult(
            found=True,
            confidence="medium",
            input_phone=lookup.input_phone,
            identity=hypothesis.identity,
            evidence=[
                *base_evidence,
                *hypothesis_evidence,
                *[
                    Evidence(
                        source="official_business_registry",
                        source_id=result.established_entity_id,
                        url=result.url,
                        observed={
                            "registry": result.registry,
                            "legal_name": result.established_entity_name,
                            "establishment_evidence_kinds": [
                                item.kind for item in result.evidence
                                if item.kind in _HARD_REGISTRY_EVIDENCE_KINDS
                            ],
                        },
                    )
                    for result in selected_results
                ],
            ],
            notes=[
                "A Google Places phone-search candidate remained untrusted while registry acquisition ran; promotion required a unique, conflict-free official exact legal-name, current DBA/trade-name, or exact business-address relationship.",
                *(["Multiple Places candidates were promoted only because official evidence linked each one to the same registry entity."] if len(lookup.candidate_hypotheses) > 1 else []),
            ],
            assessments=lookup.assessments,
        )
        return promoted, all_results, selected_results

    def _resolver(self) -> IdentityResolverLike:
        if self._identity_resolver is None:
            self._identity_resolver = _default_identity_resolver()
        return self._identity_resolver

    def _runner(self) -> LicenseSourceRunner:
        if self._source_runner is None:
            self._source_runner = LicenseSourceRunner()
        return self._source_runner

    def _website_license_evidence(
        self, identity: BusinessIdentity
    ) -> list[LicenseNumberEvidence]:
        if not identity.website:
            return []
        client = self._website_client or FirstPartyWebsiteClient()
        found: dict[tuple[str, str], LicenseNumberEvidence] = {}
        pages, _failed_fetches = fetch_bounded_first_party_pages(
            client, identity.website
        )
        for page in pages:
            for clue in extract_license_numbers(
                page.visible_text,
                page_url=page.final_url,
                fetched_at=datetime.now(timezone.utc),
                verified_identity_place_id=identity.place_id,
            ):
                found.setdefault((clue.likely_board_id, clue.normalized_number), clue)
        return list(found.values())


def find_licenses(
    phone: str, refresh: bool = False, market_hint: str | None = None
) -> PipelineResult:
    """Resolve a phone, search relevant boards, and retain only complete results."""

    return LicensePipeline().find_licenses(
        phone, refresh=refresh, market_hint=market_hint
    )


_BOARD_JURISDICTIONS = {
    "TSBPE": "TX", "TDLR": "TX", "DPOR": "VA", "CSLB": "CA",
    "DC_INDUSTRIAL_TRADES": "DC", "MD_ELECTRICIANS": "MD",
}


def _market_jurisdictions(market_hint: str | None) -> dict[str, str]:
    """Map explicit evaluation/business-market metadata; never infer from phone."""
    normalized = (market_hint or "").strip().casefold()
    if normalized == "dc metro":
        return {state: "explicit DC Metro market metadata" for state in ("DC", "MD", "VA")}
    if normalized == "austin":
        return {"TX": "explicit Austin market metadata"}
    if normalized in {"sf bay", "san francisco bay area"}:
        return {"CA": "explicit SF Bay market metadata"}
    return {}


_HARD_REGISTRY_EVIDENCE_KINDS = {
    "exact_legal_name",
    "exact_trade_name_relationship",
    "exact_fictitious_name_relationship",
    "exact_business_address",
    "exact_principal_office_address",
}


def _hard_registry_corroboration(result: RegistryEnrichmentResult) -> bool:
    """Only exact, provenance-backed registry links can promote a hypothesis."""

    if not registry_result_is_established(result):
        return False
    return any(
        evidence.kind in _HARD_REGISTRY_EVIDENCE_KINDS
        and evidence.source_name
        and evidence.source_url
        and evidence.entity_id == result.established_entity_id
        and bool(evidence.observed)
        for evidence in result.evidence
    )


def _registry_entity_key(result: RegistryEnrichmentResult) -> tuple[str, str, str]:
    return (
        result.jurisdiction.strip().casefold(),
        result.registry.strip().casefold(),
        result.established_entity_id or "",
    )


def _hypothesis_evidence_strength(hypothesis: IdentityCandidateHypothesis) -> int:
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
        + int(bool(observed.get("input_phone_published")))
    )


def _classify_day2(
    result: Day2Result,
    registry_results: list[RegistryEnrichmentResult] | None = None,
) -> PipelineStatus:
    statuses = {board.search_status for board in result.board_results}
    if statuses & {"unreachable", "captcha_blocked", "skipped"}:
        return "partial"
    if any(result.status in {"unreachable", "captcha_blocked", "skipped"} for result in registry_results or []):
        return "partial"
    return "complete"


def _jurisdiction(value: str) -> str:
    normalized = value.strip().casefold()
    aliases = {
        "california": "CA", "district of columbia": "DC", "maryland": "MD",
        "virginia": "VA", "texas": "TX",
    }
    return normalized.upper() if len(normalized) == 2 else aliases.get(normalized, value.strip().upper())


def _default_cache_path() -> Path:
    configured = os.environ.get("HIRENIMBUS_CACHE_PATH")
    return Path(configured) if configured else Path.cwd() / ".cache" / "license_results.json"


def _default_identity_resolver() -> IdentityResolver:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    places_client = (
        GooglePlacesClient(api_key)
        if api_key
        else _UnavailablePlacesClient("GOOGLE_PLACES_API_KEY is not set.")
    )
    taxonomy_path = Path(__file__).resolve().parents[2] / "data" / "category_taxonomy.json"
    mapper = (
        CategoryMapper.from_json(taxonomy_path)
        if taxonomy_path.exists()
        else CategoryMapper()
    )
    return IdentityResolver(
        places_client,
        mapper,
        website_client=FirstPartyWebsiteClient(),
    )


class _UnavailablePlacesClient:
    def __init__(self, message: str) -> None:
        self._message = message

    def search_by_phone(self, normalized_phone: str) -> list[str]:
        raise PlacesLookupError(self._message)

    def get_place_details(self, place_id: str) -> object:
        raise PlacesLookupError(self._message)
