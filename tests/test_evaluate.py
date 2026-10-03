import json
from pathlib import Path

import scripts.evaluate as evaluate_module
from app.models import (
    BusinessIdentity,
    CandidateAssessment,
    Evidence,
    IdentityCandidateHypothesis,
    IdentityLookupResult,
    NormalizedPhone,
)
from app.pipeline import CacheMetadata, PipelineResult
from scripts.evaluate import (
    _aggregate,
    _case_record,
    _phase2_metrics,
    _run_metadata,
    _verified_metrics,
    _write_report,
)


def case(
    case_id: str,
    phone: str,
    *,
    found: bool,
    business_name: str | None = None,
    accepted_licenses: list[dict[str, str]] | None = None,
    states: list[str] | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "normalized_phone": phone,
        "invalid_input": False,
        "identity_found": found,
        "business_name": business_name,
        "business_states": states or [],
        "accepted_licenses": accepted_licenses or [],
    }


def test_case_record_preserves_unresolved_identity_diagnostics() -> None:
    evidence = Evidence(
        source="official_business_website",
        source_id="candidate-1",
        url="https://example.test/contact/",
        observed={"website_phone": "512-555-1234"},
    )
    hypothesis = IdentityCandidateHypothesis(
        identity=BusinessIdentity(
            business_name="Example Plumbing",
            phone="+15125551234",
            place_id="candidate-1",
        ),
        evidence=[evidence],
        notes=["Awaiting registry corroboration."],
    )
    result = PipelineResult(
        input_phone=NormalizedPhone(e164="+15125551234"),
        pipeline_status="complete",
        identity_result=IdentityLookupResult(
            found=False,
            confidence="low",
            input_phone=NormalizedPhone(e164="+15125551234"),
            evidence=[evidence],
            assessments=[CandidateAssessment(
                source_id="candidate-1",
                phone_verification="mismatch",
                conflicts=["returned phone did not match"],
            )],
            candidate_hypotheses=[hypothesis],
        ),
        cache=CacheMetadata(status="miss_saved", last_good_preserved=False),
    )

    record = _case_record(
        {"id": "PXX", "phone_raw": "512-555-1234"}, result
    )

    assert record["identity_evidence"] == [evidence.model_dump(mode="json")]
    assert record["candidate_assessments"][0]["source_id"] == "candidate-1"
    assert record["unresolved_candidate_hypotheses"][0]["identity"][
        "business_name"
    ] == "Example Plumbing"
    assert record["unresolved_candidate_hypotheses"][0]["evidence"] == [
        evidence.model_dump(mode="json")
    ]


def test_evaluate_reuses_one_pipeline_so_bulk_adapters_persist_across_cases(
    tmp_path: Path, monkeypatch
) -> None:
    input_path = tmp_path / "phones.csv"
    input_path.write_text(
        "id,phone_raw,market_hint\nP01,512-555-0101,Austin\nP02,512-555-0102,Austin\n"
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text('{"identities": {}, "verified_licenses": []}\n')
    constructions = 0

    class ReusablePipeline:
        def __init__(self) -> None:
            nonlocal constructions
            constructions += 1
            self.calls: list[str] = []

        def find_licenses(self, phone: str, **_kwargs: object) -> PipelineResult:
            self.calls.append(phone)
            normalized = "+1512555" + phone[-4:]
            return PipelineResult(
                input_phone=NormalizedPhone(e164=normalized),
                pipeline_status="complete",
                identity_result=IdentityLookupResult(
                    found=False,
                    confidence="low",
                    input_phone=NormalizedPhone(e164=normalized),
                ),
                cache=CacheMetadata(
                    status="miss_saved", last_good_preserved=False
                ),
            )

    monkeypatch.setattr(evaluate_module, "LicensePipeline", ReusablePipeline)
    monkeypatch.setattr(evaluate_module, "ROOT", tmp_path)

    report = evaluate_module.evaluate(input_path, truth_path)

    assert constructions == 1
    assert report["aggregate"]["pipeline_results_returned"] == 2
    assert report["methodology"]["pipeline_call"].startswith(
        "one reusable LicensePipeline"
    )


def test_primary_metrics_exclude_duplicates_unknowns_and_coverage_misses() -> None:
    cases = [
        case("P01", "+17030000001", found=False),
        case("P02", "+17030000002", found=True, business_name="Known"),
        case("P17", "+17030000001", found=False),
        case("P10", "+17030000010", found=True, business_name="Unknown Co"),
    ]
    ground_truth = {
        "identities": {
            "P01": {"business_name": "Missing Business"},
            "P02": {"business_name": "Known L.L.C."},
        },
        "verified_licenses": [],
    }

    metrics = _verified_metrics(cases, ground_truth)

    assert metrics["identity_coverage_yield"] == {
        "value": 2 / 3,
        "resolved_unique_phones": 2,
        "unique_valid_phone_denominator": 3,
        "resolved_case_ids": ["P02", "P10"],
    }
    assert metrics["identity_accuracy"]["correct_predictions"] == 1
    assert metrics["identity_accuracy"]["judgeable_prediction_denominator"] == 1
    assert metrics["identity_accuracy"]["judgeable_case_ids"] == ["P02"]
    assert metrics["identity_accuracy"]["coverage_miss_case_ids"] == ["P01"]
    assert metrics["identity_accuracy"]["unknown_ground_truth_case_ids"] == ["P10"]
    assert cases[2]["duplicate_of_case_id"] == "P01"


def test_zero_accepted_predictions_makes_precision_unavailable_but_recall_zero() -> None:
    cases = [case("P08", "+15120000008", found=True, business_name="Abacus")]
    reference = {"case_id": "P08", "board_id": "TDLR", "license_number": "30557"}

    metrics = _verified_metrics(
        cases,
        {
            "identities": {"P08": {"business_name": "Abacus"}},
            "verified_licenses": [reference],
        },
    )

    assert metrics["license_precision"]["value"] is None
    assert metrics["license_precision"]["judgeable_prediction_denominator"] == 0
    assert metrics["license_recall"]["value"] == 0
    assert metrics["license_recall"]["recovered_verified_licenses"] == 0
    assert metrics["license_recall"]["verified_reference_denominator"] == 1
    assert metrics["license_recall"]["reference_set"] == [reference]


def test_explicitly_verified_identity_alias_counts_without_fuzzy_matching() -> None:
    cases = [
        case(
            "P16",
            "+14156562130",
            found=True,
            business_name="Roto-Rooter Plumbing & Water Cleanup",
        ),
        case(
            "P99",
            "+14156562131",
            found=True,
            business_name="Roto-Rooter Plumbing",
        ),
    ]
    identity_truth = {
        "business_name": "Roto-Rooter San Francisco",
        "verified_aliases": [
            {
                "business_name": "Roto-Rooter Plumbing & Water Cleanup",
                "evidence_url": "https://www.rotorooter.com/sanfrancisco/",
            }
        ],
    }

    metrics = _verified_metrics(
        cases,
        {
            "identities": {"P16": identity_truth, "P99": identity_truth},
            "verified_licenses": [],
        },
    )

    assert metrics["identity_accuracy"]["correct_predictions"] == 1
    assert metrics["identity_accuracy"]["judgeable_prediction_denominator"] == 2
    assert metrics["identity_accuracy"]["incorrect_case_ids"] == ["P99"]
    assert cases[0]["verification"]["matched_verified_business_name"] == (
        "Roto-Rooter Plumbing & Water Cleanup"
    )


def test_phase2_metrics_attribute_only_verified_registry_recoveries() -> None:
    cases = [
        {
            **case(
                "P01",
                "+17030000001",
                found=True,
                business_name="Example Plumbing",
                accepted_licenses=[
                    {
                        "board_id": "DPOR",
                        "license_number": "123",
                        "matched_identity_field": "legal_name",
                        "matched_identity_value": "Example Plumbing LLC",
                    }
                ],
            ),
            "primary_unique_valid_phone": True,
            "registry_results": [{"status": "ok"}],
            "search_keys": [
                {
                    "original_value": "Example Plumbing LLC",
                    "source_field": "legal_name",
                    "origin": "registry",
                }
            ],
            "board_results": [
                {"search_status": "ok", "issue_kind": None}
            ],
        },
        {
            **case("P02", "+17030000002", found=True, business_name="Blocked"),
            "primary_unique_valid_phone": True,
            "registry_results": [{"status": "captcha_blocked"}],
            "search_keys": [],
            "board_results": [
                {"search_status": "skipped", "issue_kind": "unsupported"}
            ],
        },
    ]
    ground_truth = {
        "identities": {},
        "verified_licenses": [
            {"case_id": "P01", "board_id": "DPOR", "license_number": "123"}
        ],
    }
    verified = _verified_metrics(cases, ground_truth)
    metrics = _phase2_metrics(cases, ground_truth, verified)

    assert metrics["known_license_cases"] == 1
    assert metrics["accepted_verified_licenses"] == 1
    assert metrics["recovered_through_registry_legal_name"] == 1
    assert metrics["recovered_through_official_dba_trade_fictitious_name"] == 0
    assert metrics["source_outcomes_primary_unique_valid_cases"]["captcha_blocked"] == {
        "count": 1,
        "case_ids": ["P02"],
    }
    assert metrics["source_outcomes_primary_unique_valid_cases"]["unsupported"] == {
        "count": 1,
        "case_ids": ["P02"],
    }
    assert metrics["tx_outside_phase2_registry_expansion"] == {
        "known_license_cases": 0,
        "known_license_case_ids": [],
        "known_licenses": 0,
        "note": "Texas board behavior is preserved and no Texas registry enrichment is run.",
    }


def test_phase2_metrics_report_tx_references_outside_registry_scope() -> None:
    cases = [
        {
            **case(
                "P08",
                "+15120000008",
                found=False,
            ),
            "primary_unique_valid_phone": True,
            "registry_results": [],
            "search_keys": [],
            "board_results": [],
        }
    ]
    ground_truth = {
        "identities": {},
        "verified_licenses": [
            {"case_id": "P08", "board_id": "TSBPE", "license_number": "1"},
            {"case_id": "P08", "board_id": "TDLR", "license_number": "2"},
        ],
    }

    verified = _verified_metrics(cases, ground_truth)
    metrics = _phase2_metrics(cases, ground_truth, verified)

    assert metrics["tx_outside_phase2_registry_expansion"] == {
        "known_license_cases": 1,
        "known_license_case_ids": ["P08"],
        "known_licenses": 2,
        "note": "Texas board behavior is preserved and no Texas registry enrichment is run.",
    }


def test_frozen_expanded_tx_scope_comes_from_board_ids_when_identities_are_unresolved() -> None:
    root = Path(__file__).resolve().parents[1]
    ground_truth = json.loads(
        (root / "data" / "evaluation_ground_truth_expanded.json").read_text()
    )
    tx_references = [
        item for item in ground_truth["verified_licenses"]
        if item["board_id"].casefold() in {"tdlr", "tsbpe"}
    ]
    tx_case_ids = list(dict.fromkeys(
        case_id for item in tx_references for case_id in item["case_ids"]
    ))
    cases = [
        {
            **case(case_id, f"+1202555{index:04d}", found=False),
            "primary_unique_valid_phone": True,
            "registry_results": [],
            "search_keys": [],
            "board_results": [],
        }
        for index, case_id in enumerate(tx_case_ids, start=1)
    ]

    verified = _verified_metrics(cases, ground_truth)
    metrics = _phase2_metrics(cases, ground_truth, verified)

    assert metrics["tx_outside_phase2_registry_expansion"] == {
        "known_license_cases": len(tx_case_ids),
        "known_license_case_ids": tx_case_ids,
        "known_licenses": len(tx_references),
        "note": "Texas board behavior is preserved and no Texas registry enrichment is run.",
    }


def test_all_valid_places_lookup_failures_mark_run_source_blocked() -> None:
    metadata = _run_metadata([
        {
            "case_id": "P02",
            "normalized_phone": "+17030000002",
            "invalid_input": False,
            "identity_found": False,
            "identity_error": {
                "kind": "lookup_failure",
                "message": "Google Places access denied (HTTP 403).",
            },
            "board_results": [],
        },
        {
            "case_id": "P03",
            "normalized_phone": "+17030000003",
            "invalid_input": False,
            "identity_found": False,
            "identity_error": {
                "kind": "lookup_failure",
                "message": "Google Places access denied (HTTP 403).",
            },
            "board_results": [],
        },
    ])

    assert metadata == {
        "benchmark_status": "source_blocked",
        "comparable": False,
        "source_health": {
            "google_places": {
                "status": "unavailable",
                "valid_identity_inputs": 2,
                "identity_lookup_failures": 2,
                "failure_classes": {"http_403": 2},
            },
            "board_evaluations": 0,
        },
    }


def test_partial_places_failure_does_not_mark_entire_run_source_blocked() -> None:
    metadata = _run_metadata([
        {
            "case_id": "P02",
            "normalized_phone": "+17030000002",
            "invalid_input": False,
            "identity_found": False,
            "identity_error": {"kind": "lookup_failure", "message": "HTTP 429"},
            "board_results": [],
        },
        {
            "case_id": "P03",
            "normalized_phone": "+17030000003",
            "invalid_input": False,
            "identity_found": False,
            "identity_error": None,
            "board_results": [],
        },
    ])

    assert metadata["benchmark_status"] == "completed"
    assert metadata["comparable"] is True
    assert metadata["source_health"]["google_places"]["status"] == "degraded"


def test_source_blocked_report_is_written_beside_existing_comparable_report(tmp_path: Path) -> None:
    requested_path = tmp_path / "expanded_evaluation_report.json"
    original = '{"run_metadata": {"comparable": true}, "result": "last-good"}\n'
    requested_path.write_text(original)
    report = {
        "generated_at": "2026-10-02T12:30:00+00:00",
        "run_metadata": {"benchmark_status": "source_blocked", "comparable": False},
        "aggregate": {"expanded_license_metrics": {"recall": 0.0}},
    }

    written_path = _write_report(report, requested_path)

    assert written_path != requested_path
    assert ".source_blocked.20261002T123000Z" in written_path.name
    assert requested_path.read_text() == original
    assert json.loads(written_path.read_text()) == report


def test_expanded_alias_recovered_only_by_second_case_counts_in_both_recall_metrics() -> None:
    cases = [
        {
            **case("P12", "+14150000012", found=False),
            "identity_confidence": "low", "pipeline_status": "complete",
            "cache_status": "miss_saved", "board_results": [], "registry_results": [],
            "search_keys": [], "license_notes": [], "identity_notes": [],
            "accepted_licenses": [],
        },
        {
            **case("P13", "+16500000013", found=True, business_name="Cabrillo"),
            "identity_confidence": "medium", "pipeline_status": "complete",
            "cache_status": "miss_saved", "board_results": [], "registry_results": [],
            "search_keys": [], "license_notes": [], "identity_notes": [],
            "accepted_licenses": [{"board_id": "CSLB", "license_number": "629538", "match_confidence": "high"}],
        },
    ]
    truth = {
        "identities": {"P12": {"business_name": "Cabrillo"}, "P13": {"business_name": "Cabrillo"}},
        "verified_licenses": [{
            "global_id": "CSLB:629538", "case_ids": ["P12", "P13"],
            "board_id": "CSLB", "license_number": "629538",
        }],
    }

    aggregate = _aggregate(cases, truth)

    assert aggregate["verified_metrics"]["license_recall"]["recovered_verified_licenses"] == 1
    assert aggregate["verified_metrics"]["license_recall"]["value"] == 1.0
    assert aggregate["expanded_license_metrics"]["verified_correct_predictions"] == 1
    assert aggregate["expanded_license_metrics"]["recall"] == 1.0
    comparison = aggregate["benchmark_comparison"]
    assert comparison["historical_phase1"] == {
        "recovered_verified_licenses": 1,
        "verified_license_denominator": 8,
    }
    assert comparison["historical_phase2"] == {
        "recovered_verified_licenses": 1,
        "verified_license_denominator": 8,
    }
    assert comparison["current_expanded_benchmark"] == {
        "recovered_verified_licenses": 1,
        "verified_license_denominator": 1,
    }
