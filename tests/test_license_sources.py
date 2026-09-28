from datetime import datetime, timezone

import httpx
import pytest

from app import license_sources
from app.day2_models import (
    BoardSelection,
    BoardSelectionIssue,
    BoardSelectionResult,
    CandidateLicenseRecord,
    SearchKey,
)
from app.license_sources import LicenseSourceRunner


def test_unsupported_or_ambiguous_part_1_mapping_is_skipped() -> None:
    plan = BoardSelectionResult(
        issues=[
            BoardSelectionIssue(
                kind="ambiguous",
                jurisdiction="DC",
                category="cleaning",
                reason="boards.md provides only general DC guidance.",
            )
        ]
    )
    fetched_at = datetime(2026, 9, 27, tzinfo=timezone.utc)

    result = LicenseSourceRunner(clock=lambda: fetched_at).search(plan, [])[0]

    assert result.search_status == "skipped"
    assert result.jurisdiction == "DC"
    assert result.board_id is None
    assert result.candidates == []
    assert result.fetched_at == fetched_at


def test_selection_without_a_safe_official_adapter_is_skipped() -> None:
    plan = BoardSelectionResult(
        selections=[
            BoardSelection(
                jurisdiction="MD",
                board_id="MD_ELECTRICIANS",
                board_name="Maryland State Board of Electricians",
                strategy="official_electrician_query",
                applicable_categories=["electrical"],
            )
        ]
    )

    result = LicenseSourceRunner().search(plan, [])[0]

    assert result.search_status == "skipped"
    assert result.board_id == "MD_ELECTRICIANS"
    assert result.candidates == []


def test_successful_source_results_are_cached_in_process() -> None:
    fetched_at = datetime(2026, 9, 27, tzinfo=timezone.utc)

    class FakeAdapter:
        calls = 0

        def fetch(self, selection, search_keys, observed_at):
            self.calls += 1
            return [
                CandidateLicenseRecord(
                    board_id=selection.board_id,
                    board_name=selection.board_name,
                    source_strategy=selection.strategy,
                    holder_name="Example Services",
                    holder_name_role="business",
                    evidence_url="https://official.example/licenses",
                    fetched_at=observed_at,
                )
            ]

    adapter = FakeAdapter()
    runner = LicenseSourceRunner(
        adapters={"test_source": adapter},
        clock=lambda: fetched_at,
        min_request_interval_seconds=0,
    )
    plan = BoardSelectionResult(
        selections=[
            BoardSelection(
                jurisdiction="TX",
                board_id="TEST",
                board_name="Test Board",
                strategy="test_source",
            )
        ]
    )
    keys = [
        SearchKey(
            original_value="Example Services",
            normalized_value="example services",
            source_field="business_name",
        )
    ]

    first = runner.search(plan, keys)
    second = runner.search(plan, keys)

    assert adapter.calls == 1
    assert first == second


def test_http_requests_observe_minimum_interval(monkeypatch) -> None:
    monotonic_values = iter([0.0, 0.0, 0.1, 0.25])
    sleep_calls: list[float] = []
    monkeypatch.setattr(license_sources.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(license_sources.time, "sleep", sleep_calls.append)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[], request=request)

    runner = LicenseSourceRunner(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval_seconds=0.25,
    )
    plan = BoardSelectionResult(
        selections=[
            BoardSelection(
                jurisdiction="TX",
                board_id="TDLR",
                board_name="TDLR",
                strategy="tdlr_all_licenses_open_data",
                applicable_categories=["electrical"],
            )
        ]
    )
    keys = [
        SearchKey(
            original_value=value,
            normalized_value=value.casefold(),
            source_field="business_name",
        )
        for value in ("Example Electric", "Example Electrical")
    ]

    result = runner.search(plan, keys)[0]

    assert result.search_status == "not_found"
    assert len(requests) == 2
    assert sleep_calls == [pytest.approx(0.15)]
