from app.board_selection import select_boards
from app.models import BusinessIdentity


def identity(
    *,
    states: list[str],
    categories: list[str],
    address: str | None = None,
    service_area: bool | None = None,
) -> BusinessIdentity:
    return BusinessIdentity(
        business_name="Example Services",
        phone="+15125551234",
        place_id="place-1",
        states=states,
        normalized_categories=categories,
        address=address,
        service_area=service_area,
    )


def test_tx_plumbing_selects_tsbpe() -> None:
    result = select_boards(identity(states=["TX"], categories=["plumbing"]))

    assert [selection.board_id for selection in result.selections] == ["TSBPE"]
    assert result.selections[0].applicable_categories == ["plumbing"]
    assert result.selections[0].notes == [
        "This board mapping was considered because Day 1 observed address state = TX; "
        "this is not evidence that the business is licensed in TX."
    ]
    assert result.issues == []


def test_tx_electrical_selects_tdlr() -> None:
    result = select_boards(identity(states=["TX"], categories=["electrical"]))

    assert [selection.board_id for selection in result.selections] == ["TDLR"]
    assert result.selections[0].applicable_categories == ["electrical"]


def test_tx_hvac_selects_tdlr() -> None:
    result = select_boards(identity(states=["TX"], categories=["hvac"]))

    assert [selection.board_id for selection in result.selections] == ["TDLR"]
    assert result.selections[0].applicable_categories == ["hvac"]


def test_unsupported_mapping_is_explicit_and_does_not_guess() -> None:
    result = select_boards(identity(states=["TX"], categories=["roofing"]))

    assert result.selections == []
    assert len(result.issues) == 1
    assert result.issues[0].kind == "unsupported"
    assert result.issues[0].jurisdiction == "TX"
    assert result.issues[0].category == "roofing"


def test_missing_category_is_ambiguous_and_does_not_guess() -> None:
    result = select_boards(identity(states=["TX"], categories=[]))

    assert result.selections == []
    assert result.issues[0].kind == "ambiguous"


def test_address_and_service_area_do_not_add_jurisdictions() -> None:
    result = select_boards(
        identity(
            states=["TX"],
            categories=["plumbing"],
            address="1 Border Road, Texarkana, AR 71854",
            service_area=True,
        )
    )

    assert [selection.jurisdiction for selection in result.selections] == ["TX"]
    assert [selection.board_id for selection in result.selections] == ["TSBPE"]


def test_supported_non_texas_sources_use_documented_strategies() -> None:
    va = select_boards(identity(states=["VA"], categories=["plumbing"]))
    ca = select_boards(identity(states=["CA"], categories=["roofing"]))
    md = select_boards(identity(states=["MD"], categories=["electrical", "renovation"]))

    assert [(item.board_id, item.strategy) for item in va.selections] == [
        ("DPOR", "dpor_regulant_lists")
    ]
    assert [(item.board_id, item.strategy) for item in ca.selections] == [
        ("CSLB", "cslb_license_master")
    ]
    assert [(item.board_id, item.strategy) for item in md.selections] == [
        ("MD_ELECTRICIANS", "official_electrician_query"),
        ("MHIC", "mhic_public_query"),
    ]


def test_dc_plumbing_selects_all_supported_industrial_trades() -> None:
    result = select_boards(
        identity(
            states=["DC"],
            categories=["plumbing", "cleaning"],
        )
    )

    assert [(item.board_id, item.strategy) for item in result.selections] == [
        ("DC_INDUSTRIAL_TRADES", "dc_opla_industrial_trades")
    ]
    assert result.selections[0].applicable_categories == [
        "plumbing",
        "electrical",
        "hvac",
    ]
    assert len(result.issues) == 1
    assert result.issues[0].kind == "unsupported"
    assert result.issues[0].category == "cleaning"


def test_dc_arbitrary_category_remains_explicitly_unsupported() -> None:
    result = select_boards(identity(states=["DC"], categories=["cleaning"]))

    assert result.selections == []
    assert len(result.issues) == 1
    assert result.issues[0].kind == "unsupported"
    assert result.issues[0].jurisdiction == "DC"
    assert result.issues[0].category == "cleaning"
    assert "does not cover" in result.issues[0].reason


def test_dc_missing_category_remains_ambiguous() -> None:
    result = select_boards(identity(states=["DC"], categories=[]))

    assert result.selections == []
    assert len(result.issues) == 1
    assert result.issues[0].kind == "ambiguous"
    assert result.issues[0].jurisdiction == "DC"
    assert result.issues[0].category is None
