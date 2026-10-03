import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.ca_registry import CA_BIZFILE_URL, CASOSRegistrySource
from app.models import BusinessIdentity
from app.search_keys import generate_expanded_search_keys


FIXTURES = Path(__file__).parent / "fixtures" / "registry_sources"
FETCHED_AT = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)


def evidence() -> str:
    return (FIXTURES / "synthetic_manual_ca_sos_evidence.json").read_text(
        encoding="utf-8"
    )


def identity(**overrides: object) -> BusinessIdentity:
    values: dict[str, object] = {
        "business_name": "Example Builders",
        "phone": "+19165551234",
        "address": "1 Main St, Sacramento, CA 95814",
        "place_id": "ca-place-1",
        "states": ["CA"],
        "normalized_categories": ["renovation"],
    }
    values.update(overrides)
    return BusinessIdentity(**values)


def test_ca_preserves_explicit_principal_roles_and_excludes_service_agent() -> None:
    base = identity()
    result = CASOSRegistrySource(
        manual_evidence=evidence(), clock=lambda: FETCHED_AT
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])

    assert result.status == "ok"
    assert result.established_entity_id == "SYNTHCA001"
    assert [(name.value, name.role) for name in result.principals] == [
        ("Opal Officer", "Officer"),
        ("Dana Director", "Director"),
        ("Mina Member", "Member"),
        ("Mara Manager", "Manager"),
    ]
    assert "Alex Service Agent" not in [name.value for name in result.principals]
    assert any(
        item.kind == "agent_for_service_of_process" for item in result.evidence
    )
    assert {key.registry_role for key in expanded if key.source_field == "owner_principal"} == {
        "Officer",
        "Director",
        "Member",
        "Manager",
    }
    assert result.dbas == []
    assert "outside Phase 2" in " ".join(result.notes)


def test_ca_waf_block_is_unreachable_without_bypass() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, text="Access Denied", request=request)

    result = CASOSRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity())

    assert result.status == "unreachable"
    assert "no bypass" in " ".join(result.notes)
    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", CA_BIZFILE_URL)
    ]


def test_ca_reachable_ui_without_safe_search_contract_is_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>bizfile Online</html>", request=request)

    result = CASOSRegistrySource(
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: FETCHED_AT,
    ).enrich(identity())

    assert result.status == "skipped"
    assert "no documented unauthenticated" in " ".join(result.notes)


def test_ca_ambiguous_candidates_contribute_no_registry_keys() -> None:
    payload = json.loads(evidence())
    duplicate = dict(payload["candidates"][0])
    duplicate["Entity Number"] = "SYNTHCA002"
    payload["candidates"].append(duplicate)
    base = identity(address=None, business_name="Example Builders LLC")

    result = CASOSRegistrySource(
        manual_evidence=json.dumps(payload), clock=lambda: FETCHED_AT
    ).enrich(base)
    expanded = generate_expanded_search_keys(base, [result])

    assert result.status == "ambiguous"
    assert result.established_entity_id is None
    assert result.legal_names == []
    assert result.principals == []
    assert all(key.origin == "base_identity" for key in expanded)
