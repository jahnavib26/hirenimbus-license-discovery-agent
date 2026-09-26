import pytest

from app.phone import InvalidPhoneNumber, normalize_us_phone, parse_us_phone


@pytest.mark.parametrize(
    "raw",
    [
        "(512) 555-1234",
        "5125551234",
        "+1 512 555 1234",
        "1-512-555-1234",
        "tel:512-555-1234",
        " 512-555-1234 x0 ",
    ],
)
def test_equivalent_phone_formats_normalize(raw: str) -> None:
    assert normalize_us_phone(raw) == "+15125551234"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "12345", "555-1234", "+44 20 7946 0958", "not a phone"],
)
def test_invalid_phone_inputs_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumber):
        normalize_us_phone(raw)


def test_extension_is_preserved_separately_from_base_number() -> None:
    parsed = parse_us_phone("tel:+1 (512) 555-1234;ext=0042")

    assert parsed.e164 == "+15125551234"
    assert parsed.extension == "0042"


def test_distinct_valid_numbers_remain_distinct() -> None:
    assert normalize_us_phone("512-555-1234") != normalize_us_phone("512-555-1235")


@pytest.mark.parametrize(
    "raw",
    [
        "abc 5125551234",
        "++15125551234",
        "15125551234x",
        "(512 555-1234",
        "512-555-1234 garbage",
        "0000000000",
    ],
)
def test_malformed_or_obviously_invalid_numbers_are_not_coerced(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumber):
        parse_us_phone(raw)
