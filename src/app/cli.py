"""Command-line interface for Day 1 identity lookup."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from app.categories import CategoryMapper
from app.models import IdentityLookupResult, LookupError
from app.phone import InvalidPhoneNumber, parse_us_phone
from app.places import GooglePlacesClient
from app.resolver import IdentityResolver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve a U.S. phone to a business identity.")
    parser.add_argument("phone", help="U.S. phone number in any common representation")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] in {"lookup-identity", "lookup_identity"}:
        args_list = args_list[1:]
    args = _parser().parse_args(args_list)

    try:
        input_phone = parse_us_phone(args.phone)
    except InvalidPhoneNumber as exc:
        result = IdentityLookupResult(
            found=False,
            confidence="low",
            notes=["Input validation failed before any external lookup."],
            error=LookupError(kind="invalid_input", message=str(exc)),
        )
        print(result.model_dump_json(indent=2))
        return 2

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        result = IdentityLookupResult(
            found=False,
            confidence="low",
            input_phone=input_phone,
            notes=["The external lookup was not attempted."],
            error=LookupError(
                kind="lookup_failure", message="GOOGLE_PLACES_API_KEY is not set."
            ),
        )
        print(result.model_dump_json(indent=2))
        return 1

    taxonomy_path = Path(__file__).resolve().parents[2] / "data" / "category_taxonomy.json"
    category_mapper = (
        CategoryMapper.from_json(taxonomy_path) if taxonomy_path.exists() else CategoryMapper()
    )
    result = IdentityResolver(GooglePlacesClient(api_key), category_mapper).resolve(args.phone)
    print(result.model_dump_json(indent=2))
    if result.error:
        return 2 if result.error.kind == "invalid_input" else 1
    return 0


def console_main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    console_main()
