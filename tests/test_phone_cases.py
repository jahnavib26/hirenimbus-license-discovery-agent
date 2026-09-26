import csv
from pathlib import Path

import pytest

from app.phone import InvalidPhoneNumber, parse_us_phone


PHONE_CASES_PATH = Path(__file__).resolve().parents[1] / "data" / "phones.csv"


def _supplied_cases() -> dict[str, str]:
    with PHONE_CASES_PATH.open(newline="", encoding="utf-8-sig") as stream:
        return {row["id"]: row["phone_raw"] for row in csv.DictReader(stream)}


def test_all_supplied_phone_cases_have_expected_validation_outcome() -> None:
    cases = _supplied_cases()
    invalid_ids = {"P20", "P21", "P22", "P23", "P24", "P25"}

    assert set(cases) == {f"P{number:02d}" for number in range(1, 29)}
    for case_id, raw_phone in cases.items():
        if case_id in invalid_ids:
            with pytest.raises(InvalidPhoneNumber):
                parse_us_phone(raw_phone)
        else:
            assert parse_us_phone(raw_phone).e164.startswith("+1")


def test_supplied_equivalent_formats_share_canonical_base_numbers() -> None:
    cases = _supplied_cases()

    for equivalent_ids in (
        ("P01", "P26"),
        ("P03", "P17", "P27"),
        ("P07", "P18"),
        ("P12", "P19"),
    ):
        normalized = {parse_us_phone(cases[case_id]).e164 for case_id in equivalent_ids}
        assert len(normalized) == 1

    assert parse_us_phone(cases["P26"]).extension == "0"


def test_supplied_distinct_secondary_number_remains_distinct() -> None:
    cases = _supplied_cases()

    assert parse_us_phone(cases["P08"]).e164 != parse_us_phone(cases["P28"]).e164

