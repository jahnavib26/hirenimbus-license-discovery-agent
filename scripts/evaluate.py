#!/usr/bin/env python3
"""Run the unchanged phone-to-license pipeline over the supplied evaluation CSV."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.pipeline import LicensePipeline, PipelineResult
from app.search_keys import normalize_search_name


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "phones.csv"
DEFAULT_CACHE = ROOT / ".cache" / "evaluation_cache.json"
DEFAULT_GROUND_TRUTH = ROOT / "data" / "evaluation_ground_truth.json"
_TRAILING_LEGAL_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "ltd",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate find_licenses over every row in data/phones.csv."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument(
        "--reuse-cache",
        action="store_true",
        help="Allow an existing evaluation cache; the default refuses stale state.",
    )
    return parser


def _case_record(row: dict[str, str], result: PipelineResult) -> dict[str, Any]:
    identity = result.identity_result.identity
    license_result = result.license_result
    boards = license_result.board_results if license_result else []
    accepted = license_result.accepted_licenses if license_result else []
    identity_error = result.identity_result.error

    return {
        "case_id": row["id"],
        "original_phone": row["phone_raw"],
        "market_hint": row.get("market_hint") or None,
        "input_notes": row.get("notes_for_candidate") or None,
        "normalized_phone": result.input_phone.e164 if result.input_phone else None,
        "invalid_input": bool(
            identity_error and identity_error.kind == "invalid_input"
        ),
        "identity_found": result.identity_result.found,
        "identity_confidence": result.identity_result.confidence,
        "business_name": identity.business_name if identity else None,
        "business_address": identity.address if identity else None,
        "business_states": identity.states if identity else [],
        "normalized_categories": identity.normalized_categories if identity else [],
        "identity_place_id": identity.place_id if identity else None,
        "identity_evidence": [
            item.model_dump(mode="json") for item in result.identity_result.evidence
        ],
        "candidate_assessments": [
            item.model_dump(mode="json") for item in result.identity_result.assessments
        ],
        "unresolved_candidate_hypotheses": [
            item.model_dump(mode="json")
            for item in result.identity_result.candidate_hypotheses
        ],
        "pipeline_status": result.pipeline_status,
        "cache_status": result.cache.status,
        "boards_attempted": [
            board.board_id for board in boards if board.board_id is not None
        ],
        "registry_results": [result.model_dump(mode="json") for result in result.registry_results],
        "search_keys": [key.model_dump(mode="json") for key in result.search_keys],
        "license_number_evidence": [
            item.model_dump(mode="json") for item in result.license_number_evidence
        ],
        "board_results": [
            {
                "board_id": board.board_id,
                "board_name": board.board_name,
                "jurisdiction": board.jurisdiction,
                "strategy": board.strategy,
                "search_status": board.search_status,
                "issue_kind": board.issue_kind,
                "source_url": board.source_url,
                "fetched_at": board.fetched_at.isoformat(),
                "match_decisions": [
                    decision.model_dump(mode="json")
                    for decision in board.match_decisions
                ],
                "notes": board.notes,
            }
            for board in boards
        ],
        "accepted_licenses": [
            {
                "board_id": item.board_id,
                "board_name": item.board_name,
                "license_number": item.license_number,
                "holder_name": item.holder_name,
                "raw_license_status": item.raw_license_status,
                "normalized_status": item.normalized_status,
                "match_confidence": item.match_confidence,
                "matched_identity_field": item.matched_identity_field,
                "matched_identity_value": item.matched_identity_value,
                "supporting_evidence": item.supporting_evidence,
                "match_notes": item.match_notes,
                "evidence_url": item.evidence_url,
                "source_reference": item.source_reference,
                "fetched_at": item.fetched_at.isoformat(),
            }
            for item in accepted
        ],
        "identity_error": identity_error.model_dump() if identity_error else None,
        "pipeline_notes": result.notes,
        "identity_notes": result.identity_result.notes,
        "license_notes": license_result.notes if license_result else [],
        "verification": {
            "identity_correctness": "unknown",
            "license_truth": "unknown",
            "reason": "No supported manual verification labels were supplied.",
        },
    }


def _primary_valid_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary: list[dict[str, Any]] = []
    first_case_by_phone: dict[str, str] = {}
    for case in cases:
        if "evaluation_error" in case or case.get("invalid_input"):
            case["primary_unique_valid_phone"] = False
            continue
        normalized_phone = case.get("normalized_phone")
        if not normalized_phone:
            case["primary_unique_valid_phone"] = False
            continue
        first_case_id = first_case_by_phone.get(normalized_phone)
        if first_case_id is not None:
            case["primary_unique_valid_phone"] = False
            case["duplicate_of_case_id"] = first_case_id
            continue
        first_case_by_phone[normalized_phone] = case["case_id"]
        case["primary_unique_valid_phone"] = True
        primary.append(case)
    return primary


def _license_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item["case_id"]),
        str(item["board_id"]).strip().casefold(),
        str(item["license_number"]).strip().casefold(),
    )


def _identity_name_key(value: str) -> str:
    """Normalize display names without treating a trailing legal suffix as identity."""

    parts = normalize_search_name(value).split()
    while parts and parts[-1] in _TRAILING_LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def _verified_identity_names(expected: dict[str, Any]) -> list[str]:
    """Return the primary name plus explicitly verified aliases."""

    names = [str(expected["business_name"])]
    for alias in expected.get("verified_aliases", []):
        if isinstance(alias, dict) and alias.get("business_name"):
            names.append(str(alias["business_name"]))
    return names


def _verified_metrics(
    cases: list[dict[str, Any]], ground_truth: dict[str, Any]
) -> dict[str, Any]:
    primary = _primary_valid_cases(cases)
    identities = ground_truth.get("identities", {})
    verified_licenses = ground_truth.get("verified_licenses", [])

    correct_identity_cases: list[str] = []
    incorrect_identity_cases: list[str] = []
    coverage_miss_cases: list[str] = []
    unknown_identity_cases: list[str] = []
    for case in primary:
        case_id = case["case_id"]
        expected = identities.get(case_id)
        if not isinstance(expected, dict) or not expected.get("business_name"):
            case["verification"] = {
                "identity_correctness": "unknown",
                "license_truth": "unknown",
                "reason": "No manually established identity ground truth was supplied.",
            }
            unknown_identity_cases.append(case_id)
            continue
        if not case["identity_found"]:
            case["verification"] = {
                "identity_correctness": "coverage_miss",
                "expected_business_name": expected["business_name"],
                "license_truth": "unknown",
            }
            coverage_miss_cases.append(case_id)
            continue
        predicted_name = case.get("business_name") or ""
        verified_names = _verified_identity_names(expected)
        predicted_key = _identity_name_key(predicted_name)
        matched_name = next(
            (
                verified_name
                for verified_name in verified_names
                if predicted_key == _identity_name_key(verified_name)
            ),
            None,
        )
        is_correct = matched_name is not None
        case["verification"] = {
            "identity_correctness": "correct" if is_correct else "incorrect",
            "expected_business_name": expected["business_name"],
            "verified_business_names": verified_names,
            "matched_verified_business_name": matched_name,
            "predicted_business_name": predicted_name,
            "comparison": (
                "exact against the primary name or an explicitly verified alias after "
                "standard name normalization and removal of trailing legal-entity suffixes"
            ),
            "license_truth": "unknown",
        }
        (correct_identity_cases if is_correct else incorrect_identity_cases).append(
            case_id
        )

    expanded_truth = any("case_ids" in item for item in verified_licenses)
    if expanded_truth:
        expanded_metrics = _expanded_license_metrics(cases, ground_truth)
        precision_value = expanded_metrics["precision"]
        correct_predictions = expanded_metrics["verified_correct_predictions"]
        judgeable_predictions = expanded_metrics["judgeable_prediction_denominator"]
        unverified_prediction_count = expanded_metrics["unverified_predictions"]
        recall_value = expanded_metrics["recall"]
        recovered_count = expanded_metrics["verified_correct_predictions"]
    else:
        primary_by_id = {case["case_id"]: case for case in primary}
        accepted_predictions = [
            {"case_id": case_id, **license_item}
            for case_id, case in primary_by_id.items()
            for license_item in case.get("accepted_licenses", [])
        ]
        reference_keys = {_license_key(item) for item in verified_licenses}
        prediction_keys = {_license_key(item) for item in accepted_predictions}
        verified_true_predictions = prediction_keys & reference_keys
        unverified_predictions = prediction_keys - reference_keys
        recovered_references = reference_keys & prediction_keys
        precision_value = (
            len(verified_true_predictions) / len(verified_true_predictions)
            if verified_true_predictions else None
        )
        correct_predictions = len(verified_true_predictions)
        judgeable_predictions = len(verified_true_predictions)
        unverified_prediction_count = len(unverified_predictions)
        recall_value = len(recovered_references) / len(reference_keys) if reference_keys else None
        recovered_count = len(recovered_references)

    coverage_numerator = sum(case["identity_found"] for case in primary)
    accuracy_denominator = len(correct_identity_cases) + len(incorrect_identity_cases)
    valid_rows = [
        case
        for case in cases
        if "evaluation_error" not in case and not case.get("invalid_input")
    ]

    return {
        "identity_coverage_yield": {
            "value": coverage_numerator / len(primary) if primary else None,
            "resolved_unique_phones": coverage_numerator,
            "unique_valid_phone_denominator": len(primary),
            "resolved_case_ids": [
                case["case_id"] for case in primary if case["identity_found"]
            ],
        },
        "identity_accuracy": {
            "value": (
                len(correct_identity_cases) / accuracy_denominator
                if accuracy_denominator
                else None
            ),
            "correct_predictions": len(correct_identity_cases),
            "judgeable_prediction_denominator": accuracy_denominator,
            "judgeable_case_ids": correct_identity_cases + incorrect_identity_cases,
            "incorrect_case_ids": incorrect_identity_cases,
            "coverage_miss_case_ids": coverage_miss_cases,
            "unknown_ground_truth_case_ids": unknown_identity_cases,
        },
        "license_precision": {
            "value": precision_value,
            "verified_correct_predictions": correct_predictions,
            "judgeable_prediction_denominator": judgeable_predictions,
            "unverified_prediction_count": unverified_prediction_count,
            "reason_if_unavailable": (
                "No judgeable accepted-license predictions were produced."
                if judgeable_predictions == 0
                else None
            ),
        },
        "license_recall": {
            "value": recall_value,
            "recovered_verified_licenses": recovered_count,
            "verified_reference_denominator": len(verified_licenses),
            "reference_set": verified_licenses,
        },
        "secondary_row_level_identity_coverage": {
            "value": (
                sum(case["identity_found"] for case in valid_rows) / len(valid_rows)
                if valid_rows
                else None
            ),
            "resolved_valid_rows": sum(case["identity_found"] for case in valid_rows),
            "valid_row_denominator": len(valid_rows),
            "note": "Secondary only; formatting duplicates are included.",
        },
    }


def _aggregate(
    cases: list[dict[str, Any]], ground_truth: dict[str, Any]
) -> dict[str, Any]:
    completed = [case for case in cases if "evaluation_error" not in case]
    board_results = [
        board for case in completed for board in case.get("board_results", [])
    ]
    accepted = [
        license_item
        for case in completed
        for license_item in case.get("accepted_licenses", [])
    ]
    normalized = [
        case["normalized_phone"]
        for case in completed
        if case.get("normalized_phone") is not None
    ]

    expanded = any("case_ids" in item for item in ground_truth.get("verified_licenses", []))
    verified_metrics = _verified_metrics(cases, ground_truth)
    aggregate = {
        "total_input_cases": len(cases),
        "pipeline_results_returned": len(completed),
        "evaluation_execution_errors": len(cases) - len(completed),
        "valid_input_cases": sum(not case["invalid_input"] for case in completed),
        "invalid_input_cases": sum(case["invalid_input"] for case in completed),
        "unique_normalized_phones": len(set(normalized)),
        "identity_found": dict(
            Counter(str(case["identity_found"]).lower() for case in completed)
        ),
        "identity_confidence": dict(
            Counter(case["identity_confidence"] for case in completed)
        ),
        "pipeline_status": dict(
            Counter(case["pipeline_status"] for case in completed)
        ),
        "cache_status": dict(Counter(case["cache_status"] for case in completed)),
        "board_search_status": dict(
            Counter(board["search_status"] for board in board_results)
        ),
        "board_result_count": len(board_results),
        "cases_with_accepted_licenses": sum(
            bool(case["accepted_licenses"]) for case in completed
        ),
        "accepted_license_count": len(accepted),
        "accepted_license_match_confidence": dict(
            Counter(item["match_confidence"] for item in accepted)
        ),
        "verified_metrics": verified_metrics,
        "phase2_metrics": _phase2_metrics(cases, ground_truth, verified_metrics),
        "benchmark_comparison": _benchmark_comparison(verified_metrics, expanded),
    }
    if expanded:
        aggregate["expanded_license_metrics"] = _expanded_license_metrics(
            cases, ground_truth
        )
    return aggregate


def _expanded_license_metrics(
    cases: list[dict[str, Any]], ground_truth: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate globally unique licenses while honoring documented case aliases."""
    primary = _primary_valid_cases(cases)
    references = ground_truth.get("verified_licenses", [])
    reference_by_key = {
        _global_license_key(item): item for item in references
    }
    predictions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in primary:
        for item in case.get("accepted_licenses", []):
            predictions.setdefault(_global_license_key(item), []).append(
                {"case_id": case["case_id"], **item}
            )

    recovered: list[str] = []
    for key, reference in reference_by_key.items():
        allowed = set(reference.get("case_ids", []))
        if any(item["case_id"] in allowed for item in predictions.get(key, [])):
            recovered.append(reference["global_id"])
    recovered_set = set(recovered)
    missed = [
        item["global_id"] for item in references
        if item["global_id"] not in recovered_set
    ]
    true_prediction_keys = {
        key for key, items in predictions.items()
        if key in reference_by_key
        and any(item["case_id"] in set(reference_by_key[key].get("case_ids", [])) for item in items)
    }
    explicit_negative_keys = {
        _global_id_key(value)
        for value in ground_truth.get("explicit_non_positives", [])
    }
    incorrect_prediction_keys = set(predictions) & explicit_negative_keys
    unverified_prediction_keys = (
        set(predictions) - true_prediction_keys - incorrect_prediction_keys
    )
    judgeable_count = len(true_prediction_keys) + len(incorrect_prediction_keys)
    return {
        "unique_valid_phone_denominator": len(primary),
        "global_verified_license_denominator": len(reference_by_key),
        "globally_unique_accepted_predictions": len(predictions),
        "verified_correct_predictions": len(true_prediction_keys),
        "verified_incorrect_predictions": len(incorrect_prediction_keys),
        "unverified_predictions": len(unverified_prediction_keys),
        "judgeable_prediction_denominator": judgeable_count,
        "precision": len(true_prediction_keys) / judgeable_count if judgeable_count else None,
        "recall": len(recovered) / len(reference_by_key) if reference_by_key else None,
        "recovered_license_ids": recovered,
        "missed_license_ids": missed,
        "verified_correct_prediction_ids": _prediction_ids(true_prediction_keys),
        "verified_incorrect_prediction_ids": _prediction_ids(incorrect_prediction_keys),
        "unverified_prediction_ids": _prediction_ids(unverified_prediction_keys),
        "precision_note": (
            "Precision uses only manually judgeable predictions; out-of-set predictions are unverified, not presumed false."
        ),
    }


def _global_license_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item["board_id"]).strip().casefold(),
        str(item["license_number"]).strip().casefold(),
    )


def _global_id_key(value: str) -> tuple[str, str]:
    board_id, separator, license_number = str(value).partition(":")
    if not separator:
        raise ValueError(f"Invalid global license ID: {value!r}")
    return board_id.strip().casefold(), license_number.strip().casefold()


def _prediction_ids(keys: set[tuple[str, str]]) -> list[str]:
    return [f"{board.upper()}:{number.upper()}" for board, number in sorted(keys)]


def _phase2_metrics(
    cases: list[dict[str, Any]],
    ground_truth: dict[str, Any],
    verified_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Summarize registry recovery and truthful source outcomes for Phase 2."""

    primary = [case for case in cases if case.get("primary_unique_valid_phone")]
    references = ground_truth.get("verified_licenses", [])
    reference_by_global_key = {_global_license_key(item): item for item in references}
    accepted = [
        {"case_id": case["case_id"], **item}
        for case in primary
        for item in case.get("accepted_licenses", [])
    ]
    verified_accepted = []
    for item in accepted:
        reference = reference_by_global_key.get(_global_license_key(item))
        allowed_case_ids = set(reference.get("case_ids", [reference.get("case_id")])) if reference else set()
        if reference and item["case_id"] in allowed_case_ids:
            verified_accepted.append(item)

    recovery_counts = {
        "registry_legal_name": 0,
        "official_dba_trade_fictitious_name": 0,
        "explicit_registry_principal": 0,
    }
    recovery_cases: dict[str, list[str]] = {key: [] for key in recovery_counts}
    primary_by_id = {case["case_id"]: case for case in primary}
    for item in verified_accepted:
        case = primary_by_id[item["case_id"]]
        matched_value = item.get("matched_identity_value")
        matched_field = item.get("matched_identity_field")
        matched_key = next(
            (
                key
                for key in case.get("search_keys", [])
                if key.get("origin") == "registry"
                and key.get("source_field") == matched_field
                and key.get("original_value") == matched_value
            ),
            None,
        )
        if matched_key is None:
            continue
        channel = {
            "legal_name": "registry_legal_name",
            "dba": "official_dba_trade_fictitious_name",
            "owner_principal": "explicit_registry_principal",
        }.get(str(matched_field))
        if channel:
            recovery_counts[channel] += 1
            recovery_cases[channel].append(item["case_id"])

    source_records = [
        {"case_id": case["case_id"], "source_kind": "registry", **result}
        for case in primary
        for result in case.get("registry_results", [])
    ] + [
        {"case_id": case["case_id"], "source_kind": "board", **result}
        for case in primary
        for result in case.get("board_results", [])
    ]
    source_outcomes: dict[str, dict[str, Any]] = {}
    for status in (
        "not_found",
        "ambiguous",
        "captcha_blocked",
        "unreachable",
        "skipped",
    ):
        matching = [item for item in source_records if item.get("search_status", item.get("status")) == status]
        source_outcomes[status] = {
            "count": len(matching),
            "case_ids": list(dict.fromkeys(item["case_id"] for item in matching)),
        }
    unsupported = [
        item for item in source_records if item.get("issue_kind") == "unsupported"
    ]
    source_outcomes["unsupported"] = {
        "count": len(unsupported),
        "case_ids": list(dict.fromkeys(item["case_id"] for item in unsupported)),
    }

    recall = verified_metrics["license_recall"]
    precision = verified_metrics["license_precision"]
    known_case_ids = list(dict.fromkeys(
        str(case_id)
        for item in references
        for case_id in item.get("case_ids", [item.get("case_id")])
        if case_id is not None
    ))
    tx_board_ids = {"tdlr", "tsbpe"}
    tx_references = [
        item for item in references
        if str(item.get("board_id", "")).strip().casefold() in tx_board_ids
    ]
    tx_reference_case_ids = list(dict.fromkeys(
        str(case_id)
        for item in tx_references
        for case_id in item.get("case_ids", [item.get("case_id")])
        if case_id is not None
    ))
    return {
        "known_license_cases": len(known_case_ids),
        "known_license_case_ids": known_case_ids,
        "known_licenses": len(reference_by_global_key),
        "accepted_license_predictions": len(accepted),
        "accepted_verified_licenses": len({
            _global_license_key(item) for item in verified_accepted
        }),
        "precision": precision["value"],
        "recall": recall["value"],
        "remaining_verified_licenses_not_recovered": (
            len(reference_by_global_key) - recall["recovered_verified_licenses"]
        ),
        "recovered_through_registry_legal_name": recovery_counts["registry_legal_name"],
        "recovered_through_official_dba_trade_fictitious_name": recovery_counts[
            "official_dba_trade_fictitious_name"
        ],
        "recovered_through_explicit_registry_principal": recovery_counts[
            "explicit_registry_principal"
        ],
        "registry_recovery_case_ids": recovery_cases,
        "tx_outside_phase2_registry_expansion": {
            "known_license_cases": len(tx_reference_case_ids),
            "known_license_case_ids": tx_reference_case_ids,
            "known_licenses": len(tx_references),
            "note": "Texas board behavior is preserved and no Texas registry enrichment is run.",
        },
        "source_outcomes_primary_unique_valid_cases": source_outcomes,
    }


def _benchmark_comparison(
    verified_metrics: dict[str, Any], expanded: bool
) -> dict[str, Any]:
    recall = verified_metrics["license_recall"]
    return {
        "historical_phase1": {
            "recovered_verified_licenses": 1,
            "verified_license_denominator": 8,
        },
        "historical_phase2": {
            "recovered_verified_licenses": 1,
            "verified_license_denominator": 8,
        },
        "current_expanded_benchmark": (
            {
                "recovered_verified_licenses": recall["recovered_verified_licenses"],
                "verified_license_denominator": recall["verified_reference_denominator"],
            }
            if expanded
            else None
        ),
        "note": "Historical Phase 1 and Phase 2 retain their original eight-license denominators.",
    }


def _places_diagnostic_class(message: str) -> str:
    normalized = message.casefold()
    if "api key is not set" in normalized or "api key is required" in normalized:
        return "configuration_missing"
    if re.search(r"http\s+403\b", normalized):
        return "http_403"
    if re.search(r"http\s+429\b", normalized):
        return "http_429"
    if re.search(r"http\s+5\d\d\b", normalized):
        return "http_5xx"
    if "transport failure" in normalized:
        return "transport_failure"
    return "provider_failure"


def _run_metadata(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Report whether the run reached identity resolution and board evaluation."""
    valid_cases = [
        case for case in cases
        if case.get("normalized_phone") is not None and not case.get("invalid_input")
    ]
    lookup_failures = [
        case for case in valid_cases
        if (case.get("identity_error") or {}).get("kind") == "lookup_failure"
    ]
    board_evaluations = sum(len(case.get("board_results", [])) for case in cases)
    source_blocked = bool(valid_cases) and len(lookup_failures) == len(valid_cases) and board_evaluations == 0

    if source_blocked:
        places_status = "unavailable"
    elif not valid_cases:
        places_status = "not_checked"
    elif lookup_failures:
        places_status = "degraded"
    else:
        places_status = "available"

    failure_classes = Counter(
        _places_diagnostic_class(str((case.get("identity_error") or {}).get("message", "")))
        for case in lookup_failures
    )
    return {
        "benchmark_status": "source_blocked" if source_blocked else "completed",
        "comparable": not source_blocked,
        "source_health": {
            "google_places": {
                "status": places_status,
                "valid_identity_inputs": len(valid_cases),
                "identity_lookup_failures": len(lookup_failures),
                "failure_classes": dict(failure_classes),
            },
            "board_evaluations": board_evaluations,
        },
    }


def _write_report(report: dict[str, Any], requested_path: Path) -> Path:
    """Write blocked runs beside the benchmark path without replacing it."""
    output_path = requested_path.resolve()
    if not report.get("run_metadata", {}).get("comparable", True):
        timestamp = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
        stamp = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = output_path.suffix or ".json"
        stem = output_path.stem if output_path.suffix else output_path.name
        candidate = output_path.with_name(f"{stem}.source_blocked.{stamp}{suffix}")
        sequence = 2
        while candidate.exists():
            candidate = output_path.with_name(
                f"{stem}.source_blocked.{stamp}.{sequence}{suffix}"
            )
            sequence += 1
        output_path = candidate

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return output_path


def evaluate(input_path: Path, ground_truth_path: Path) -> dict[str, Any]:
    with input_path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    ground_truth = json.loads(ground_truth_path.read_text(encoding="utf-8"))
    pipeline = LicensePipeline()

    cases: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        case_id = row.get("id") or f"row-{index}"
        print(f"[{index}/{len(rows)}] {case_id}", file=sys.stderr, flush=True)
        try:
            result = pipeline.find_licenses(
                row["phone_raw"], market_hint=row.get("market_hint") or None
            )
        except Exception as exc:  # Preserve one unexpected failure without losing later cases.
            cases.append(
                {
                    "case_id": case_id,
                    "original_phone": row.get("phone_raw"),
                    "market_hint": row.get("market_hint") or None,
                    "input_notes": row.get("notes_for_candidate") or None,
                    "evaluation_error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "verification": {
                        "identity_correctness": "unknown",
                        "license_truth": "unknown",
                    },
                }
            )
        else:
            cases.append(_case_record(row, result))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(input_path.relative_to(ROOT)),
        "ground_truth_file": str(ground_truth_path.relative_to(ROOT)),
        "methodology": {
            "pipeline_call": "one reusable LicensePipeline.find_licenses(phone, refresh=False, market_hint=row.market_hint) per row",
            "manual_verification_available": True,
            "primary_metric_unit": "unique normalized valid phone",
            "unknown_cases_excluded_from_correctness_precision_recall": True,
            "cache_note": (
                "The run starts with an isolated cache; duplicate normalized phones may "
                "produce cache hits during the same run."
            ),
        },
        "run_metadata": _run_metadata(cases),
        "cases": cases,
        "aggregate": _aggregate(cases, ground_truth),
    }


def main() -> int:
    args = _parser().parse_args()
    cache_path = args.cache_path.resolve()
    if cache_path.exists() and not args.reuse_cache:
        print(
            f"Refusing existing evaluation cache: {cache_path}. "
            "Choose a fresh --cache-path or pass --reuse-cache.",
            file=sys.stderr,
        )
        return 2

    os.environ["HIRENIMBUS_CACHE_PATH"] = str(cache_path)
    report = evaluate(args.input.resolve(), args.ground_truth.resolve())
    if args.output:
        output_path = _write_report(report, args.output)
        print(f"Wrote {output_path}", file=sys.stderr)
    else:
        print(json.dumps(report, indent=2, sort_keys=False), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
