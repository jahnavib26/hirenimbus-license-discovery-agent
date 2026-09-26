"""U.S. phone parsing and canonicalization."""

from __future__ import annotations

import re

import phonenumbers

from app.models import NormalizedPhone


_PHONE_INPUT_PATTERN = re.compile(
    r"^\s*(?:tel:\s*)?\+?[\d().\-\s]+"
    r"(?:\s*(?:(?:x|ext\.?|extension|#)\s*\d+|;ext=\d+))?\s*$",
    re.IGNORECASE,
)


class InvalidPhoneNumber(ValueError):
    """Raised when an input is not a valid U.S. phone number."""


def parse_us_phone(value: str) -> NormalizedPhone:
    """Validate a complete U.S. phone input and preserve its extension.

    A narrow shape check prevents ``phonenumbers`` from silently ignoring junk
    or repairing malformed prefixes. The library remains responsible for actual
    numbering-plan parsing and validation.
    """

    if not isinstance(value, str) or not value.strip():
        raise InvalidPhoneNumber("A non-empty phone number is required.")
    if not _PHONE_INPUT_PATTERN.fullmatch(value):
        raise InvalidPhoneNumber("The input contains unsupported or malformed phone syntax.")
    if value.count("(") != value.count(")"):
        raise InvalidPhoneNumber("The input contains unbalanced phone punctuation.")
    if "(" in value and value.index("(") > value.index(")"):
        raise InvalidPhoneNumber("The input contains malformed phone punctuation.")

    try:
        parsed = phonenumbers.parse(value.strip(), "US")
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumber("The input could not be parsed as a phone number.") from exc

    if parsed.country_code != 1 or phonenumbers.region_code_for_number(parsed) != "US":
        raise InvalidPhoneNumber("Only valid U.S. phone numbers are supported.")
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber("The input is not a valid U.S. phone number.")

    national_number = f"{parsed.national_number:010d}"
    exchange = national_number[3:6]
    line_number = int(national_number[6:])
    if exchange == "555" and 100 <= line_number <= 199:
        raise InvalidPhoneNumber("The input uses the reserved fictional 555-01xx range.")

    return NormalizedPhone(
        e164=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        extension=parsed.extension or None,
    )


def normalize_us_phone(value: str) -> str:
    """Return only the canonical base number for lookup and comparison."""

    return parse_us_phone(value).e164
