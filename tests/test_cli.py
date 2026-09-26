import json

from app import cli
from app.models import IdentityLookupResult


def test_cli_reports_missing_credentials_as_lookup_failure(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: False)

    exit_code = cli.run(["lookup-identity", "5125551234"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["error"]["kind"] == "lookup_failure"
    assert output["found"] is False
    assert output["input_phone"] == {"e164": "+15125551234", "extension": None}


def test_cli_validates_input_before_checking_credentials(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: False)

    exit_code = cli.run(["lookup-identity", "123"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["error"]["kind"] == "invalid_input"
    assert output["notes"] == ["Input validation failed before any external lookup."]


def test_cli_loads_api_key_from_local_dotenv(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / ".env").write_text("GOOGLE_PLACES_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    captured: dict[str, str] = {}

    class FakeGooglePlacesClient:
        def __init__(self, api_key: str) -> None:
            captured["api_key"] = api_key

    class FakeResolver:
        def __init__(self, places_client: object, category_mapper: object) -> None:
            captured["mapped_category"] = category_mapper.map(["plumber"])[0]

        def resolve(self, raw_phone: str) -> IdentityLookupResult:
            return IdentityLookupResult(
                found=False,
                confidence="low",
                notes=["Test no-result."],
            )

    monkeypatch.setattr(cli, "GooglePlacesClient", FakeGooglePlacesClient)
    monkeypatch.setattr(cli, "IdentityResolver", FakeResolver)

    exit_code = cli.run(["5125551234"])

    assert exit_code == 0
    assert captured == {"api_key": "test-key", "mapped_category": "plumbing"}
    output = json.loads(capsys.readouterr().out)
    assert output["found"] is False
    assert {"found", "confidence", "identity", "evidence", "notes"} <= output.keys()
