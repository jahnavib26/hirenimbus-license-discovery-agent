#!/usr/bin/env python3
"""Run the unchanged phone-to-license pipeline over the supplied evaluation CSV."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.pipeline import PipelineResult, find_licenses
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
        "identity_place_id": identity.place_id if identity else None,
        "pipeline_status": result.pipeline_status,
        "cache_status": result.cache.status,
        "boards_attempted": [
            board.board_id for board in boards if board.board_id is not None
        ],
        "board_results": [
            {
                "board_id": board.board_id,
                "board_name": board.board_name,
                "jurisdiction": board.jurisdiction,
                "strategy": board.strategy,
                "search_status": board.search_status,
                "source_url": board.source_url,
                "fetched_at": board.fetched_at.isoformat(),
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

    coverage_numerator = sum(case["identity_found"] for case in primary)
    accuracy_denominator = len(correct_identity_cases) + len(incorrect_identity_cases)
    precision_denominator = len(verified_true_predictions)
    recall_denominator = len(reference_keys)
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
            "value": (
                len(verified_true_predictions) / precision_denominator
                if precision_denominator
                else None
            ),
            "verified_correct_predictions": len(verified_true_predictions),
            "judgeable_prediction_denominator": precision_denominator,
            "unverified_prediction_count": len(unverified_predictions),
            "reason_if_unavailable": (
                "No judgeable accepted-license predictions were produced."
                if precision_denominator == 0
                else None
            ),
        },
        "license_recall": {
            "value": (
                len(recovered_references) / recall_denominator
                if recall_denominator
                else None
            ),
            "recovered_verified_licenses": len(recovered_references),
            "verified_reference_denominator": recall_denominator,
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

    return {
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
        "verified_metrics": _verified_metrics(cases, ground_truth),
    }


def evaluate(input_path: Path, ground_truth_path: Path) -> dict[str, Any]:
    with input_path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    ground_truth = json.loads(ground_truth_path.read_text(encoding="utf-8"))

    cases: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        case_id = row.get("id") or f"row-{index}"
        print(f"[{index}/{len(rows)}] {case_id}", file=sys.stderr, flush=True)
        try:
            result = find_licenses(row["phone_raw"])
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
            "pipeline_call": "find_licenses(phone, refresh=False)",
            "manual_verification_available": True,
            "primary_metric_unit": "unique normalized valid phone",
            "unknown_cases_excluded_from_correctness_precision_recall": True,
            "cache_note": (
                "The run starts with an isolated cache; duplicate normalized phones may "
                "produce cache hits during the same run."
            ),
        },
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
    serialized = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.output:
        output_path = args.output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized, encoding="utf-8")
        print(f"Wrote {output_path}", file=sys.stderr)
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
