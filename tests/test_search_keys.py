import pytest

from app.models import BusinessIdentity
from datetime import datetime, timezone

from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.search_keys import generate_expanded_search_keys, generate_search_keys, normalize_search_name


def identity(**names: str | None) -> BusinessIdentity:
    return BusinessIdentity(
        phone="+15125551234",
        place_id="place-1",
        **names,
    )


def test_search_keys_use_only_established_day_1_fields_in_preferred_order() -> None:
    business = identity(
        legal_name="Example Holdings, L.L.C.",
        dba="Example Electric",
        business_name="Example Public Name",
        owner_principal="Ada Owner",
    )

    keys = generate_search_keys(business)

    assert [key.source_field for key in keys] == [
        "legal_name",
        "dba",
        "business_name",
        "owner_principal",
    ]
    assert [key.original_value for key in keys] == [
        "Example Holdings, L.L.C.",
        "Example Electric",
        "Example Public Name",
        "Ada Owner",
    ]


def test_owner_key_is_omitted_when_owner_was_not_established() -> None:
    keys = generate_search_keys(identity(business_name="Example Plumbing"))

    assert [key.source_field for key in keys] == ["business_name"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ACME, L.L.C.", "acme llc"),
        ("Acme LLC", "acme llc"),
        ("Acme Incorporated", "acme inc"),
        ("Acme Inc.", "acme inc"),
        ("Acme Corporation", "acme corp"),
        ("Acme Corp.", "acme corp"),
        ("Acme Company", "acme co"),
        ("Acme Co.", "acme co"),
        ("  ACME---Services,   Inc.  ", "acme services inc"),
    ],
)
def test_legal_suffix_punctuation_case_and_whitespace_normalization(
    value: str, expected: str
) -> None:
    assert normalize_search_name(value) == expected


def test_original_search_value_is_preserved_exactly() -> None:
    original = "  Example & Sons, Incorporated  "

    key = generate_search_keys(identity(legal_name=original))[0]

    assert key.original_value == original
    assert key.normalized_value == "example sons inc"


def test_expanded_keys_include_only_established_registry_names_and_keep_people_last() -> None:
    result = RegistryEnrichmentResult(
        jurisdiction="TX", registry="TX SOS", status="ok",
        fetched_at=datetime.now(timezone.utc), established_entity_id="123",
        established_entity_name="Example Plumbing LLC",
        legal_names=[RegistryName(value="Example Plumbing LLC", kind="legal_name", source_name="TX SOS")],
        dbas=[RegistryName(value="Example Pipe", kind="dba", source_name="TX SOS")],
        principals=[RegistryName(value="Ada Owner", kind="principal", role="manager", source_name="TX SOS")],
        evidence=[RegistryEvidence(kind="exact_name", source_name="TX SOS")],
    )
    unestablished = RegistryEnrichmentResult(
        jurisdiction="MD", registry="MD SDAT", status="ambiguous",
        legal_names=[RegistryName(value="Wrong Name", kind="legal_name", source_name="MD SDAT")],
    )
    keys = generate_expanded_search_keys(
        identity(business_name="Places Name", legal_name="Base Legal", dba="Base DBA", owner_principal="Base Person"),
        [result, unestablished],
    )
    assert [key.original_value for key in keys] == [
        "Base Legal", "Base DBA", "Places Name", "Example Plumbing LLC", "Example Pipe", "Base Person", "Ada Owner"
    ]
    assert keys[3].origin == "registry"
    assert keys[3].entity_id == "123"
    assert keys[6].registry_role == "manager"
