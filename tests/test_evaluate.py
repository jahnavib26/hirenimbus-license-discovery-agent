from scripts.evaluate import _verified_metrics


def case(
    case_id: str,
    phone: str,
    *,
    found: bool,
    business_name: str | None = None,
    accepted_licenses: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "normalized_phone": phone,
        "invalid_input": False,
        "identity_found": found,
        "business_name": business_name,
        "accepted_licenses": accepted_licenses or [],
    }


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
