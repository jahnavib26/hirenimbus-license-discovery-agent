import json
from datetime import datetime, timezone
from pathlib import Path

from app.md_registry import MDSDATRegistrySource
from app.models import BusinessIdentity
from app.search_keys import generate_expanded_search_keys


FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
FETCHED_AT = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)


def evidence() -> str:
    return (FIXTURES / "synthetic_manual_md_sdat_evidence.json").read_text(
        encoding="utf-8"
    )


def identity(**overrides: object) -> BusinessIdentity:
    values: dict[str, object] = {
        "business_name": "Harbor Home Services",
        "phone": "+14105551234",
        "address": "100 Harbor St, Baltimore, MD 21201",
        "place_id": "md-place-1",
        "states": ["MD"],
        "normalized_categories": ["renovation"],
    }
    values.update(overrides)
    return BusinessIdentity(**values)


def test_md_live_source_reports_turnstile_access_limit_as_skipped() -> None:
    result = MDSDATRegistrySource(clock=lambda: FETCHED_AT).enrich(identity())

    assert result.status == "skipped"
    assert result.fetched_at == FETCHED_AT
    assert result.legal_names == []
    assert result.dbas == []
    assert result.principals == []
    assert "Turnstile" in " ".join(result.notes)
    assert "not_found" not in " ".join(result.notes)


def test_md_manual_evidence_parses_legal_trade_and_explicit_roles() -> None:
    base = identity()
    result = MDSDATRegistrySource(
        manual_evidence=evidence(), clock=lambda: FETCHED_AT
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])

    assert result.status == "ok"
    assert result.established_entity_id == "SYNTHMD01"
    assert [name.value for name in result.legal_names] == ["Harbor Renovations LLC"]
    assert [(name.value, name.role) for name in result.dbas] == [
        ("Harbor Home Services", "current trade name")
    ]
    assert [(name.value, name.role) for name in result.principals] == [
        ("Morgan Manager", "Manager")
    ]
    assert "Robin Resident Agent" not in [name.value for name in result.principals]
    assert "Olivia Organizer" not in [name.value for name in result.principals]
    assert any(item.kind == "resident_agent" for item in result.evidence)
    assert any(item.kind == "exact_trade_name_relationship" for item in result.evidence)
    assert any(
        key.original_value == "Harbor Renovations LLC" and key.origin == "registry"
        for key in expanded
    )


def test_md_ambiguous_candidates_contribute_no_registry_keys() -> None:
    payload = json.loads(evidence())
    duplicate = dict(payload["candidates"][0])
    duplicate["Department ID"] = "SYNTHMD02"
    duplicate["Trade Names"] = [
        {**duplicate["Trade Names"][0], "Department ID": "SYNTHMD02"}
    ]
    payload["candidates"].append(duplicate)
    base = identity(address=None)

    result = MDSDATRegistrySource(
        manual_evidence=json.dumps(payload), clock=lambda: FETCHED_AT
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.legal_names == []
    assert result.dbas == []
    assert result.principals == []
    assert all(key.origin == "base_identity" for key in expanded)


def test_md_trade_name_join_requires_exact_department_id() -> None:
    payload = json.loads(evidence())
    payload["candidates"][0]["Trade Names"][0]["Department ID"] = "SYNTHMD99"
    base = identity(business_name="Harbor Renovations LLC", address=None)

    result = MDSDATRegistrySource(
        manual_evidence=json.dumps(payload), clock=lambda: FETCHED_AT
    ).enrich(base)

    assert result.status == "ok"
    assert result.dbas == []
