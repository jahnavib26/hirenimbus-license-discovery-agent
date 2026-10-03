from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from app.board_adapters import DPORAdapter, TSBPEAdapter
from app.board_selection import select_boards
from app.day2_models import BoardSelection, BoardSelectionResult, CandidateLicenseRecord, LicenseNumberEvidence, SearchKey
from app.license_discovery import extract_license_numbers, normalize_license_number
from app.license_matching import assemble_day2_result, match_candidate
from app.license_sources import LicenseSourceRunner
from app.models import BusinessIdentity
from app.pipeline import _market_jurisdictions
from scripts.evaluate import _expanded_license_metrics


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity(*, states=None, categories=None, name="Example Services"):
    return BusinessIdentity(
        business_name=name, phone="+15125550123", states=states or [],
        place_id="place-1", normalized_categories=categories or [],
    )


def clue(board="TDLR", number="30557", family="electrical", place_id=None):
    return LicenseNumberEvidence(
        raw_text=f"TECL {number}", normalized_number=number,
        likely_board_id=board, license_family=family,
        page_url="https://example.test/licenses", fetched_at=NOW,
        supporting_snippet=f"Texas license TECL {number}",
        verified_identity_place_id=place_id,
    )


def test_extracts_marked_first_party_license_numbers_and_normalizes_tdlr_hvac():
    found = extract_license_numbers(
        "Licensed and insured. M-20628 | TECL 30557 | TACLA135747C",
        page_url="https://example.test", fetched_at=NOW,
    )
    assert {(item.likely_board_id, item.normalized_number) for item in found} == {
        ("TSBPE", "20628"), ("TDLR", "30557"),
        ("TDLR", "TACLA00135747C"),
    }


def test_extracts_ca_license_label_as_cslb_evidence():
    found = extract_license_numbers(
        "Cabrillo Plumbing, Heating & Air — CA License #629538",
        page_url="https://example.test", fetched_at=NOW,
    )
    assert [(item.likely_board_id, item.normalized_number) for item in found] == [
        ("CSLB", "629538")
    ]


def test_extractor_rejects_unlabelled_numeric_noise():
    assert extract_license_numbers(
        "Call 512-555-0123. ZIP 78701. Founded 1998. Order 30557.",
        page_url="https://example.test", fetched_at=NOW,
    ) == []


def test_board_specific_normalization_does_not_generic_zero_pad():
    assert normalize_license_number("TDLR", "air_conditioning", "TACLB112806E") == "TACLB00112806E"
    assert normalize_license_number("TDLR", "electrical", "TECL 39935") == "39935"
    assert normalize_license_number("CSLB", "contractor", "629538") == "629538"


class ExactAdapter:
    def __init__(self):
        self.exact_calls = []
        self.name_calls = 0

    def lookup_by_license_number(self, selection, evidence, fetched_at):
        self.exact_calls.append(evidence)
        return []

    def fetch(self, selection, search_keys, fetched_at):
        self.name_calls += 1
        return []


def test_exact_number_lookup_is_first_class_and_name_search_still_enumerates():
    adapter = ExactAdapter()
    runner = LicenseSourceRunner(
        adapters={"synthetic": adapter}, clock=lambda: NOW,
        min_request_interval_seconds=0,
    )
    plan = BoardSelectionResult(selections=[BoardSelection(
        jurisdiction="TX", board_id="TDLR", board_name="TDLR",
        strategy="synthetic", applicable_categories=["electrical"],
    )])
    runner.search(plan, [SearchKey(original_value="Example", normalized_value="example", source_field="business_name")], [clue()])
    assert adapter.exact_calls == [[clue()]]
    assert adapter.name_calls == 1


def test_website_number_alone_never_becomes_an_accepted_license():
    result = assemble_day2_result(identity(states=["TX"], categories=["electrical"]), [])
    assert result.accepted_licenses == []


def test_explicit_market_routing_supports_service_area_and_controlled_dc_metro():
    tx = select_boards(identity(categories=["electrical"]), _market_jurisdictions("Austin"))
    assert [item.board_id for item in tx.selections] == ["TDLR"]
    assert _market_jurisdictions(None) == {}
    assert set(_market_jurisdictions("DC Metro")) == {"DC", "MD", "VA"}


def test_tsbpe_exact_lookup_preserves_company_affiliation_and_rmp():
    payload = "RANK,LICENSE_NBR,LIC_STATUS,FIRST_NAME,LAST_NAME,PLUMB_COMPANY\nRMP,20628,Active,Ada,Plumber,Example Services\n"
    def handler(request):
        return httpx.Response(200, text=payload, request=request)
    adapter = TSBPEAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    selection = BoardSelection(jurisdiction="TX", board_id="TSBPE", board_name="TSBPE", strategy="free_licensee_lists")
    rows = adapter.lookup_by_license_number(selection, [clue("TSBPE", "20628", "master_plumber")], NOW)
    assert rows[0].source_fields["PLUMB_COMPANY"] == "Example Services"
    assert rows[0].raw_license_type == "RMP"
    accepted = assemble_day2_result(identity(states=["TX"], categories=["plumbing"]), [
        __import__("app.day2_models", fromlist=["BoardSearchResult"]).BoardSearchResult(
            board_id="TSBPE", board_name="TSBPE", jurisdiction="TX", strategy="free_licensee_lists",
            search_status="ok", fetched_at=NOW, candidates=rows,
        )
    ])
    assert [item.license_number for item in accepted.accepted_licenses] == ["20628"]


def test_tsbpe_person_without_company_affiliation_is_not_accepted():
    candidate = CandidateLicenseRecord(
        board_id="TSBPE", board_name="TSBPE", source_strategy="fixture",
        license_number="999", holder_name="Ada Plumber", holder_name_role="person",
        evidence_url="https://tsbpe.texas.gov", fetched_at=NOW,
    )
    board_result_cls = __import__("app.day2_models", fromlist=["BoardSearchResult"]).BoardSearchResult
    result = assemble_day2_result(identity(states=["TX"], categories=["plumbing"]), [
        board_result_cls(board_id="TSBPE", board_name="TSBPE", jurisdiction="TX", strategy="fixture", search_status="ok", fetched_at=NOW, candidates=[candidate])
    ])
    assert result.accepted_licenses == []


def test_dpor_skips_one_malformed_row_and_keeps_valid_rows(monkeypatch):
    payload = "BOARD\tOCCUPATION\tCERTIFICATE #\tBUSINESS NAME\tEXPIRATION DATE\n27\t05\t123\tExample Services\t2030-01-01\nmalformed\textra\trow\twith\ttoo\tmany\n"
    url = "https://dpor.example.test/list.tsv"
    monkeypatch.setattr("app.board_adapters.DPOR_LISTS", (("2705", url),))
    adapter = DPORAdapter(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=payload, request=request))))
    selection = BoardSelection(jurisdiction="VA", board_id="DPOR", board_name="DPOR", strategy="dpor_regulant_lists")
    rows = adapter.fetch(selection, [SearchKey(original_value="Example Services", normalized_value="example services", source_field="business_name")], NOW)
    assert [item.license_number for item in rows] == ["2705123"]


def test_expanded_metrics_globally_deduplicate_shared_case_aliases():
    truth = {"verified_licenses": [
        {"global_id": "TSBPE:20628", "case_ids": ["P08", "P28"], "board_id": "TSBPE", "license_number": "20628"},
        {"global_id": "CSLB:629538", "case_ids": ["P12", "P13"], "board_id": "CSLB", "license_number": "629538"},
    ]}
    cases = []
    for case_id, phone, board, number in [
        ("P08", "+15120000008", "TSBPE", "20628"),
        ("P28", "+15120000028", "TSBPE", "20628"),
        ("P13", "+16500000013", "CSLB", "629538"),
    ]:
        cases.append({"case_id": case_id, "normalized_phone": phone, "invalid_input": False, "accepted_licenses": [{"board_id": board, "license_number": number}]})
    metrics = _expanded_license_metrics(cases, truth)
    assert metrics["global_verified_license_denominator"] == 2
    assert metrics["globally_unique_accepted_predictions"] == 2
    assert metrics["recovered_license_ids"] == ["TSBPE:20628", "CSLB:629538"]


def test_out_of_set_prediction_is_unverified_not_a_false_positive():
    truth = {
        "verified_licenses": [
            {"global_id": "TDLR:1", "case_ids": ["P01"], "board_id": "TDLR", "license_number": "1"}
        ],
        "explicit_non_positives": ["TDLR:999"],
    }
    cases = [{
        "case_id": "P01", "normalized_phone": "+15120000001", "invalid_input": False,
        "accepted_licenses": [
            {"board_id": "TDLR", "license_number": "1"},
            {"board_id": "DC_INDUSTRIAL_TRADES", "license_number": "UNREVIEWED"},
        ],
    }]
    metrics = _expanded_license_metrics(cases, truth)
    assert metrics["precision"] == 1.0
    assert metrics["judgeable_prediction_denominator"] == 1
    assert metrics["verified_incorrect_predictions"] == 0
    assert metrics["unverified_prediction_ids"] == ["DC_INDUSTRIAL_TRADES:UNREVIEWED"]


def test_explicit_manual_negative_is_a_judgeable_incorrect_prediction():
    truth = {"verified_licenses": [], "explicit_non_positives": ["TDLR:999"]}
    cases = [{
        "case_id": "P01", "normalized_phone": "+15120000001", "invalid_input": False,
        "accepted_licenses": [{"board_id": "TDLR", "license_number": "999"}],
    }]
    metrics = _expanded_license_metrics(cases, truth)
    assert metrics["precision"] == 0.0
    assert metrics["verified_incorrect_prediction_ids"] == ["TDLR:999"]


def _bridge_candidate(evidence, **overrides):
    values = dict(
        board_id="TDLR", board_name="TDLR", source_strategy="official",
        license_number="30557", holder_name="Different Legal Entity LLC",
        holder_name_role="business", raw_license_type="Electrical Contractor",
        state="TX", evidence_url="https://data.texas.gov", fetched_at=NOW,
        discovery_evidence=[evidence],
    )
    values.update(overrides)
    return CandidateLicenseRecord(**values)


def test_verified_first_party_exact_number_official_bridge_is_accepted():
    decision = match_candidate(
        identity(states=["TX"], categories=["electrical"], name="Abacus"),
        _bridge_candidate(clue(place_id="place-1")),
    )
    assert decision.accepted is True
    assert "website_exact_license_number" in decision.supporting_evidence
    assert "website_source:https://example.test/licenses" in decision.supporting_evidence


def test_cslb_exact_number_bridge_accepts_business_with_different_name_address_and_phone():
    evidence = clue("CSLB", "806952", "contractor", "place-1")
    business = identity(states=["CA"], categories=["plumbing"], name="Roto-Rooter")
    business.phone = "+15124567890"
    business.address = "1 Places Main Street, San Francisco, CA 94124"
    decision = match_candidate(
        business,
        _bridge_candidate(
            evidence,
            board_id="CSLB",
            board_name="CSLB",
            license_number="806952",
            raw_license_type="Corporation",
            raw_classification="A| C36",
            state="OH",
            phone="+19252701399",
            address="900 Corporate Avenue, Cleveland, OH 44101",
        ),
    )
    assert decision.accepted is True
    assert decision.candidate.holder_name_role == "business"
    assert "website_exact_license_number" in decision.supporting_evidence
    assert "official_exact_license_number" in decision.supporting_evidence
    assert "exact_address" not in decision.supporting_evidence
    assert decision.conflicts == ["candidate_phone_conflicts_with_day1_phone"]


def test_cslb_exact_number_bridge_still_accepts_matching_business_address():
    evidence = clue("CSLB", "629538", "contractor", "place-1")
    business = identity(states=["CA"], categories=["plumbing"], name="Cabrillo Plumbing, Heating & Air")
    business.address = "78 Dorman Ave, San Francisco, CA 94124, USA"
    decision = match_candidate(
        business,
        _bridge_candidate(
            evidence,
            board_id="CSLB", board_name="CSLB", license_number="629538",
            holder_name="CABRILLO PLUMBING & HEATING", raw_license_type="Corporation",
            raw_classification="C20 | C36", state="CA",
            address="78 DORMAN AVE, SAN FRANCISCO, CA, 94124",
        ),
    )
    assert decision.accepted is True
    assert "exact_address" in decision.supporting_evidence


def test_exact_number_bridge_does_not_accept_different_name_person_candidate():
    evidence = clue("CSLB", "629538", "contractor", "place-1")
    decision = match_candidate(
        identity(states=["CA"], categories=["plumbing"], name="Cabrillo Plumbing"),
        _bridge_candidate(
            evidence,
            board_id="CSLB",
            board_name="CSLB",
            license_number="629538",
            holder_name="Ada Owner",
            holder_name_role="person",
            raw_license_type="Plumbing Contractor",
            state="CA",
        ),
    )
    assert decision.accepted is False


def test_exact_hvac_bridge_uses_established_business_name_trade_signal():
    evidence = clue("TDLR", "TACLA00135747C", "air_conditioning", "place-1")
    decision = match_candidate(
        identity(
            states=["TX"], categories=["plumbing", "electrical"],
            name="Abacus Plumbing, Air Conditioning, & Electrical",
        ),
        _bridge_candidate(
            evidence, license_number="TACLA00135747C",
            raw_license_type="A/C Contractor", raw_classification="AC",
        ),
    )
    assert decision.accepted is True


def test_unverified_website_number_is_rejected():
    mismatched_provenance = match_candidate(
        identity(states=["TX"], categories=["electrical"], name="Abacus"),
        _bridge_candidate(clue(place_id=None)),
    )
    missing_verified_identity = match_candidate(
        identity(states=["TX"], categories=["electrical"], name="Abacus").model_copy(
            update={"place_id": None}
        ),
        _bridge_candidate(clue(place_id=None)),
    )
    assert mismatched_provenance.accepted is False
    assert missing_verified_identity.accepted is False


def test_website_and_official_number_mismatch_is_rejected():
    decision = match_candidate(
        identity(states=["TX"], categories=["electrical"], name="Abacus"),
        _bridge_candidate(clue(place_id="place-1"), license_number="99999"),
    )
    assert decision.accepted is False


def test_exact_number_bridge_rejects_inconsistent_jurisdiction_or_trade():
    wrong_trade = match_candidate(
        identity(states=["TX"], categories=["plumbing"], name="Abacus"),
        _bridge_candidate(clue(place_id="place-1")),
    )
    wrong_state = match_candidate(
        identity(states=["CA"], categories=["electrical"], name="Abacus"),
        _bridge_candidate(clue(place_id="place-1")),
    )
    assert wrong_trade.accepted is False
    assert wrong_state.accepted is False


def test_business_name_legal_suffix_plus_exact_address_is_accepted():
    decision = match_candidate(
        BusinessIdentity(
            business_name="Brownlee Plumbing", phone="+17034779016",
            address="7702 Backlick Rd M, Springfield, VA 22150, USA",
            states=["VA"], place_id="place-1", normalized_categories=["plumbing"],
        ),
        CandidateLicenseRecord(
            board_id="DPOR", board_name="DPOR", source_strategy="official",
            license_number="2705159268", holder_name="BROWNLEE PLUMBING LLC",
            holder_name_role="business", raw_license_type="Contractor",
            raw_classification="Plumbing", state="VA",
            address="7702 Backlick Rd Unit M, Springfield, VA 22150-0000",
            evidence_url="https://dpor.virginia.gov", fetched_at=NOW,
        ),
        search_keys=[SearchKey(original_value="Brownlee Plumbing", normalized_value="brownlee plumbing", source_field="business_name")],
    )
    assert decision.accepted is True
    assert decision.supporting_evidence == [
        "exact_address", "observed_address_state_consistent", "trade_category_consistent"
    ]


def test_tdlr_hvac_exact_lookup_reconstructs_canonical_number():
    def handler(request):
        assert request.url.params["license_number"] == "135747"
        return httpx.Response(200, json=[{
            "license_type": "A/C Contractor", "license_number": "135747",
            "business_name": "NEW ABACUS LLC", "license_subtype": "AC",
        }], request=request)
    from app.board_adapters import TDLRAdapter
    adapter = TDLRAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    selection = BoardSelection(
        jurisdiction="TX", board_id="TDLR", board_name="TDLR",
        strategy="tdlr_all_licenses_open_data", applicable_categories=["hvac"],
    )
    evidence = clue("TDLR", "TACLA00135747C", "air_conditioning", "place-1")
    rows = adapter.lookup_by_license_number(selection, [evidence], NOW)
    assert rows[0].license_number == "TACLA00135747C"
    assert rows[0].source_fields["license_number"] == "135747"


def test_frozen_ground_truth_has_23_unique_licenses_and_excludes_hold_items():
    root = Path(__file__).resolve().parents[1]
    expanded = json.loads((root / "data/evaluation_ground_truth_expanded.json").read_text())
    historical = json.loads((root / "data/evaluation_ground_truth.json").read_text())
    keys = {(item["board_id"], item["license_number"]) for item in expanded["verified_licenses"]}
    assert len(keys) == 23
    assert len(historical["verified_licenses"]) == 8
    assert ("TSBPE", "45535") not in keys
    assert ("MD_ELECTRICIANS", "93215") not in keys
