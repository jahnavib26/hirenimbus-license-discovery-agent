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
    DPORAdapter,
    MHICAdapter,
    SourceAccessError,
    TDLRAdapter,
    TSBPEAdapter,
)
from app.day2_models import (
    BoardSearchResult,
    BoardSelection,
    BoardSelectionResult,
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
            "cslb_license_master": CSLBAdapter(self._client),
            "mhic_public_query": MHICAdapter(self._client),
        }
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._successful_cache: dict[tuple[object, ...], BoardSearchResult] = {}

    def search(
        self,
        plan: BoardSelectionResult,
        search_keys: list[SearchKey],
    ) -> list[BoardSearchResult]:
        results: list[BoardSearchResult] = []

        for selection in plan.selections:
            cache_key = (
                selection.model_dump_json(),
                tuple(
                    (key.source_field, key.original_value, key.normalized_value)
                    for key in search_keys
                ),
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
                        source_url=selection.source_url,
                        fetched_at=fetched_at,
                        notes=["No safe official adapter is configured for this selection."],
                    )
                )
                continue
            if not search_keys:
                results.append(
                    BoardSearchResult(
                        board_id=selection.board_id,
                        board_name=selection.board_name,
                        jurisdiction=selection.jurisdiction,
                        strategy=selection.strategy,
                        search_status="skipped",
                        source_url=selection.source_url,
                        fetched_at=fetched_at,
                        notes=["Day 1 established no name field that can be used as a search key."],
                    )
                )
                continue

            try:
                candidates = adapter.fetch(selection, search_keys, fetched_at)
            except CaptchaBlockedError as exc:
                results.append(
                    _failed_result(selection, "captcha_blocked", fetched_at, str(exc))
                )
            except (
                httpx.HTTPError,
                SourceAccessError,
                csv.Error,
                UnicodeError,
                ValueError,
            ) as exc:
                results.append(_failed_result(selection, "unreachable", fetched_at, str(exc)))
            else:
                result = BoardSearchResult(
                    board_id=selection.board_id,
                    board_name=selection.board_name,
                    jurisdiction=selection.jurisdiction,
                    strategy=selection.strategy,
                    search_status="ok" if candidates else "not_found",
                    source_url=selection.source_url,
                    fetched_at=fetched_at,
                    candidates=candidates,
                    notes=(
                        ["Returned rows are candidates only; no identity match was accepted."]
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
                    fetched_at=self._clock(),
                    notes=[issue.reason],
                )
            )

        return results


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
