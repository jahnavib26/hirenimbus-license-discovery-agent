"""End-to-end Day 1 -> Day 2 orchestration with a last-good cache."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from app.board_selection import select_boards
from app.cache import JsonLastGoodCache
from app.categories import CategoryMapper
from app.day2_models import Day2Result
from app.license_matching import assemble_day2_result
from app.license_sources import LicenseSourceRunner
from app.models import IdentityLookupResult, NormalizedPhone
from app.phone import InvalidPhoneNumber, parse_us_phone
from app.places import GooglePlacesClient, PlacesLookupError
from app.resolver import IdentityResolver
from app.search_keys import generate_search_keys
from app.website import FirstPartyWebsiteClient


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
    cache: CacheMetadata
    notes: list[str] = Field(default_factory=list)


class IdentityResolverLike(Protocol):
    def resolve(self, raw_phone: str) -> IdentityLookupResult: ...


class LastGoodCacheLike(Protocol):
    def get(self, normalized_phone: str) -> dict[str, object] | None: ...

    def put(self, normalized_phone: str, result: dict[str, object]) -> None: ...


class LicensePipeline:
    """Coordinate existing Day 1 and Day 2 behavior without duplicating it."""

    def __init__(
        self,
        *,
        identity_resolver: IdentityResolverLike | None = None,
        source_runner: LicenseSourceRunner | None = None,
        cache: LastGoodCacheLike | None = None,
    ) -> None:
        self._identity_resolver = identity_resolver
        self._source_runner = source_runner
        self._cache = (
            cache if cache is not None else JsonLastGoodCache(_default_cache_path())
        )

    def find_licenses(self, phone: str, refresh: bool = False) -> PipelineResult:
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

        cache_key = input_phone.e164
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
        if identity_result.error is not None:
            pipeline_status: PipelineStatus = "failed"
        elif not identity_result.found:
            pipeline_status = "complete"
        else:
            identity = identity_result.identity
            if identity is None:
                pipeline_status = "failed"
            else:
                plan = select_boards(identity)
                search_keys = generate_search_keys(identity)
                source_results = self._runner().search(plan, search_keys)
                license_result = assemble_day2_result(identity, source_results)
                pipeline_status = _classify_day2(license_result)

        result = PipelineResult(
            input_phone=input_phone,
            pipeline_status=pipeline_status,
            identity_result=identity_result,
            license_result=license_result,
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

    def _resolver(self) -> IdentityResolverLike:
        if self._identity_resolver is None:
            self._identity_resolver = _default_identity_resolver()
        return self._identity_resolver

    def _runner(self) -> LicenseSourceRunner:
        if self._source_runner is None:
            self._source_runner = LicenseSourceRunner()
        return self._source_runner


def find_licenses(phone: str, refresh: bool = False) -> PipelineResult:
    """Resolve a phone, search relevant boards, and retain only complete results."""

    return LicensePipeline().find_licenses(phone, refresh=refresh)


def _classify_day2(result: Day2Result) -> PipelineStatus:
    statuses = {board.search_status for board in result.board_results}
    if statuses & {"unreachable", "captcha_blocked"}:
        return "partial"
    return "complete"


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
