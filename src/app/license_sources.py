"""Run Part 1 plans against the narrow official-source adapters."""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

import httpx

from app.board_adapters import (
    BoardAdapter,
    CSLBAdapter,
    CaptchaBlockedError,
    DCIndustrialTradesAdapter,
    DPORAdapter,
    MHICAdapter,
    MarylandElectriciansAdapter,
    SourceAccessError,
    TDLRAdapter,
    TSBPEAdapter,
)
from app.day2_models import (
    BoardSearchResult,
    BoardSelection,
    BoardSelectionResult,
    LicenseNumberEvidence,
    SearchKey,
    SearchStatus,
)


class LicenseSourceRunner:
    """Execute Part 1 selections without accepting any candidate as a match."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        adapters: Mapping[str, BoardAdapter] | None = None,
        clock: Callable[[], datetime] | None = None,
        min_request_interval_seconds: float = 0.1,
    ) -> None:
        if min_request_interval_seconds < 0:
            raise ValueError("The minimum request interval cannot be negative.")
        raw_client = client or httpx.Client(follow_redirects=True, timeout=30.0)
        self._client = _RateLimitedClient(raw_client, min_request_interval_seconds)
        self._adapters: Mapping[str, BoardAdapter] = adapters or {
            "tdlr_all_licenses_open_data": TDLRAdapter(self._client),
            "free_licensee_lists": TSBPEAdapter(self._client),
            "dpor_regulant_lists": DPORAdapter(self._client),
            "dc_opla_industrial_trades": DCIndustrialTradesAdapter(self._client),
            "cslb_license_master": CSLBAdapter(self._client),
            "official_electrician_query": MarylandElectriciansAdapter(self._client),
            "mhic_public_query": MHICAdapter(self._client),
        }
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._successful_cache: dict[tuple[object, ...], BoardSearchResult] = {}

    def search(
        self,
        plan: BoardSelectionResult,
        search_keys: list[SearchKey],
        license_number_evidence: list[LicenseNumberEvidence] | None = None,
    ) -> list[BoardSearchResult]:
        results: list[BoardSearchResult] = []

        for selection in plan.selections:
            audited_search_keys = (
                [key.model_copy(deep=True) for key in search_keys]
                if selection.jurisdiction.strip().upper() == "MD"
                else []
            )
            exact_evidence = [
                item for item in (license_number_evidence or [])
                if item.likely_board_id == selection.board_id
            ]
            cache_key = (
                selection.model_dump_json(),
                tuple(
                (key.source_field, key.original_value, key.normalized_value)
                    + (key.origin, key.registry_name, key.entity_id, key.source_url, key.registry_role)
                    for key in search_keys
                ),
                tuple((item.likely_board_id, item.normalized_number, item.page_url) for item in exact_evidence),
            )
            cached = self._successful_cache.get(cache_key)
            if cached is not None:
                results.append(cached.model_copy(deep=True))
                continue

            fetched_at = self._clock()
            adapter = self._adapters.get(selection.strategy)
            if adapter is None:
                results.append(
                    BoardSearchResult(
                        board_id=selection.board_id,
                        board_name=selection.board_name,
                        jurisdiction=selection.jurisdiction,
                        strategy=selection.strategy,
                        search_status="skipped",
                        issue_kind="no_safe_adapter",
                        source_url=selection.source_url,
                        fetched_at=fetched_at,
                        search_keys=audited_search_keys,
                        notes=["No safe official adapter is configured for this selection."],
                    )
                )
                continue
            if not search_keys and not exact_evidence:
                results.append(
                    BoardSearchResult(
                        board_id=selection.board_id,
                        board_name=selection.board_name,
                        jurisdiction=selection.jurisdiction,
                        strategy=selection.strategy,
                        search_status="skipped",
                        source_url=selection.source_url,
                        fetched_at=fetched_at,
                        search_keys=audited_search_keys,
                        notes=["No established name or discovered board-specific license number is available."],
                    )
                )
                continue

            try:
                candidates = []
                exact_lookup = getattr(adapter, "lookup_by_license_number", None)
                if exact_evidence and callable(exact_lookup):
                    candidates.extend(exact_lookup(selection, exact_evidence, fetched_at))
                if search_keys:
                    candidates.extend(adapter.fetch(selection, search_keys, fetched_at))
                candidates = _deduplicate_candidates(candidates)
            except CaptchaBlockedError as exc:
                failed = _failed_result(selection, "captcha_blocked", fetched_at, str(exc))
                failed.search_keys = audited_search_keys
                results.append(failed)
            except (
                httpx.HTTPError,
                SourceAccessError,
                csv.Error,
                UnicodeError,
                ValueError,
            ) as exc:
                failed = _failed_result(selection, "unreachable", fetched_at, str(exc))
                failed.search_keys = audited_search_keys
                results.append(failed)
            else:
                result = BoardSearchResult(
                    board_id=selection.board_id,
                    board_name=selection.board_name,
                    jurisdiction=selection.jurisdiction,
                    strategy=selection.strategy,
                    search_status="ok" if candidates else "not_found",
                    source_url=selection.source_url,
                    fetched_at=fetched_at,
                    search_keys=audited_search_keys,
                    candidates=candidates,
                    notes=(
                        ["Returned official rows are candidates only; no identity match was accepted."]
                        if candidates
                        else [
                            "The official source was reached but returned no candidate rows; this is not an unlicensed conclusion."
                        ]
                    ),
                )
                self._successful_cache[cache_key] = result.model_copy(deep=True)
                results.append(result)

        for issue in plan.issues:
            results.append(
                BoardSearchResult(
                    jurisdiction=issue.jurisdiction,
                    search_status="skipped",
                    issue_kind=issue.kind,
                    fetched_at=self._clock(),
                    notes=[issue.reason],
                )
            )

        return results


def _deduplicate_candidates(candidates):
    unique = {}
    for candidate in candidates:
        key = (candidate.board_id, (candidate.license_number or "").strip().casefold(), (candidate.holder_name or "").strip().casefold())
        existing = unique.get(key)
        if existing is None:
            unique[key] = candidate
        elif candidate.discovery_evidence:
            existing.discovery_evidence = list({(item.page_url, item.normalized_number): item for item in [*existing.discovery_evidence, *candidate.discovery_evidence]}.values())
    return list(unique.values())


def _failed_result(
    selection: BoardSelection,
    status: SearchStatus,
    fetched_at: datetime,
    message: str,
) -> BoardSearchResult:
    return BoardSearchResult(
        board_id=selection.board_id,
        board_name=selection.board_name,
        jurisdiction=selection.jurisdiction,
        strategy=selection.strategy,
        search_status=status,
        source_url=selection.source_url,
        fetched_at=fetched_at,
        notes=[message],
    )


class _RateLimitedClient:
    """Apply a small process-local delay between actual board HTTP requests."""

    def __init__(self, client: httpx.Client, minimum_interval: float) -> None:
        self._client = client
        self._minimum_interval = minimum_interval
        self._last_request_at: float | None = None

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self._wait()
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        self._wait()
        return self._client.post(url, **kwargs)

    def _wait(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = self._minimum_interval - (now - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()
