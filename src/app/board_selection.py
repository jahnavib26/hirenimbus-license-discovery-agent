"""Select official licensing sources from established Day 1 facts only."""

from __future__ import annotations

from collections.abc import Iterable

from app.day2_models import (
    BoardSelection,
    BoardSelectionIssue,
    BoardSelectionResult,
)
from app.models import BusinessIdentity


_STATE_CODES = {
    "california": "CA",
    "district of columbia": "DC",
    "maryland": "MD",
    "texas": "TX",
    "virginia": "VA",
}


def select_boards(
    identity: BusinessIdentity,
    additional_jurisdictions: dict[str, str] | None = None,
) -> BoardSelectionResult:
    """Consider board mappings using Day 1's observed address-state geography."""

    result = BoardSelectionResult()
    observed_states = _unique(_jurisdiction(value) for value in identity.states if value.strip())
    for jurisdiction in (additional_jurisdictions or {}):
        normalized = _jurisdiction(jurisdiction)
        if normalized not in observed_states:
            observed_states.append(normalized)
    categories = _unique(
        value.strip().casefold()
        for value in identity.normalized_categories
        if value.strip()
    )

    if not observed_states:
        result.issues.append(
            BoardSelectionIssue(
                kind="ambiguous",
                reason="Day 1 observed no address state; no board mapping can be considered.",
            )
        )
        return result

    for jurisdiction in observed_states:
        evidence_note = (additional_jurisdictions or {}).get(jurisdiction)
        before = len(result.selections)
        if jurisdiction == "TX":
            _select_texas(categories, result)
        elif jurisdiction == "VA":
            result.selections.append(
                BoardSelection(
                    jurisdiction="VA",
                    board_id="DPOR",
                    board_name="Virginia Department of Professional and Occupational Regulation",
                    strategy="dpor_regulant_lists",
                    source_url="https://www.dpor.virginia.gov/RegulantLists",
                    applicable_categories=categories,
                    notes=[
                        _observed_state_note("VA"),
                        "boards.md identifies contractor classes (2705 A/B/C) and the combined tradesman list (2710) as relevant.",
                        "boards.md does not define a finer category-to-list mapping, so Part 1 does not invent one.",
                    ],
                )
            )
        elif jurisdiction == "CA":
            result.selections.append(
                BoardSelection(
                    jurisdiction="CA",
                    board_id="CSLB",
                    board_name="California Contractors State License Board",
                    strategy="cslb_license_master",
                    source_url="https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList",
                    applicable_categories=categories,
                    notes=[_observed_state_note("CA")],
                )
            )
        elif jurisdiction == "MD":
            _select_maryland(categories, result)
        elif jurisdiction == "DC":
            _select_dc(categories, result)
        else:
            _add_unsupported(jurisdiction, categories, result)
        if evidence_note:
            for selection in result.selections[before:]:
                selection.notes.append(f"Additional jurisdiction evidence: {evidence_note}")

    return result


def _select_texas(categories: list[str], result: BoardSelectionResult) -> None:
    if not categories:
        result.issues.append(
            BoardSelectionIssue(
                kind="ambiguous",
                jurisdiction="TX",
                reason="Texas board selection requires an established normalized category.",
            )
        )
        return

    if "plumbing" in categories:
        result.selections.append(
            BoardSelection(
                jurisdiction="TX",
                board_id="TSBPE",
                board_name="Texas State Board of Plumbing Examiners",
                strategy="free_licensee_lists",
                source_url="https://tsbpe.texas.gov/free-licensee-list/",
                applicable_categories=["plumbing"],
                notes=[_observed_state_note("TX")],
            )
        )

    tdlr_categories = [
        category for category in categories if category in {"electrical", "hvac"}
    ]
    if tdlr_categories:
        result.selections.append(
            BoardSelection(
                jurisdiction="TX",
                board_id="TDLR",
                board_name="Texas Department of Licensing and Regulation",
                strategy="tdlr_all_licenses_open_data",
                source_url="https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7",
                applicable_categories=tdlr_categories,
                notes=[_observed_state_note("TX")],
            )
        )

    for category in categories:
        if category not in {"plumbing", "electrical", "hvac"}:
            result.issues.append(
                BoardSelectionIssue(
                    kind="unsupported",
                    jurisdiction="TX",
                    category=category,
                    reason="boards.md does not define a Texas board mapping for this category.",
                )
            )


def _select_maryland(categories: list[str], result: BoardSelectionResult) -> None:
    if not categories:
        result.issues.append(
            BoardSelectionIssue(
                kind="ambiguous",
                jurisdiction="MD",
                reason="Maryland board selection requires an established normalized category.",
            )
        )
        return

    if "electrical" in categories:
        result.selections.append(
            BoardSelection(
                jurisdiction="MD",
                board_id="MD_ELECTRICIANS",
                board_name="Maryland State Board of Electricians",
                strategy="official_electrician_query",
                source_url=(
                    "https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_Search/"
                    "OP_search.cgi?calling_app=ME::ME_personal_name"
                ),
                applicable_categories=["electrical"],
                notes=[
                    _observed_state_note("MD"),
                    "The official public form searches a personal last name and requires a human CAPTCHA before submitting.",
                    "Part 1 may inspect the public form but does not submit a query or bypass CAPTCHA.",
                ],
            )
        )

    if "renovation" in categories:
        result.selections.append(
            BoardSelection(
                jurisdiction="MD",
                board_id="MHIC",
                board_name="Maryland Home Improvement Commission",
                strategy="mhic_public_query",
                source_url="https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_search/OP_search.cgi?calling_app=HIC::HIC_qselect",
                applicable_categories=["renovation"],
                notes=[_observed_state_note("MD")],
            )
        )

    for category in categories:
        if category not in {"electrical", "renovation"}:
            result.issues.append(
                BoardSelectionIssue(
                    kind="unsupported",
                    jurisdiction="MD",
                    category=category,
                    reason="boards.md does not clearly define a Maryland board mapping for this category.",
                )
            )


def _select_dc(categories: list[str], result: BoardSelectionResult) -> None:
    if not categories:
        result.issues.append(
            BoardSelectionIssue(
                kind="ambiguous",
                jurisdiction="DC",
                reason=(
                    "District of Columbia Industrial Trades selection requires an "
                    "established normalized category."
                ),
            )
        )
        return

    supported_categories = ["plumbing", "electrical", "hvac"]
    if any(category in categories for category in supported_categories):
        result.selections.append(
            BoardSelection(
                jurisdiction="DC",
                board_id="DC_INDUSTRIAL_TRADES",
                board_name="District of Columbia Board of Industrial Trades",
                strategy="dc_opla_industrial_trades",
                source_url=(
                    "https://govservices.dcra.dc.gov/oplaportal/"
                    "Home/GetLicenseSearchDetails"
                ),
                applicable_categories=supported_categories,
                notes=[
                    _observed_state_note("DC"),
                    "The official Board of Industrial Trades covers plumbing, electrical, and refrigeration/air-conditioning trades.",
                ],
            )
        )

    for category in categories:
        if category not in {"plumbing", "electrical", "hvac"}:
            result.issues.append(
                BoardSelectionIssue(
                    kind="unsupported",
                    jurisdiction="DC",
                    category=category,
                    reason=(
                        "The narrow District of Columbia Industrial Trades route "
                        "does not cover this normalized category."
                    ),
                )
            )


def _add_unsupported(
    jurisdiction: str,
    categories: list[str],
    result: BoardSelectionResult,
) -> None:
    for category in categories or [None]:
        result.issues.append(
            BoardSelectionIssue(
                kind="unsupported",
                jurisdiction=jurisdiction,
                category=category,
                reason="boards.md does not define a licensing-source mapping for this jurisdiction.",
            )
        )


def _jurisdiction(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) == 2:
        return normalized.upper()
    return _STATE_CODES.get(normalized, value.strip())


def _observed_state_note(state: str) -> str:
    return (
        f"This board mapping was considered because Day 1 observed address state = {state}; "
        f"this is not evidence that the business is licensed in {state}."
    )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
