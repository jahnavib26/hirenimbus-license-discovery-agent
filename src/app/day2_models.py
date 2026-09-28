"""Models for Day 2 planning and raw candidates, separate from Day 1 results."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SearchKeySource = Literal[
    "legal_name",
    "dba",
    "business_name",
    "owner_principal",
]


class BoardSelection(BaseModel):
    """An official licensing source selected for a Day 1 identity."""

    jurisdiction: str
    board_id: str
    board_name: str
    strategy: str
    source_url: str | None = None
    applicable_categories: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BoardSelectionIssue(BaseModel):
    """A jurisdiction/category combination that cannot be routed safely."""

    kind: Literal["unsupported", "ambiguous"]
    jurisdiction: str | None = None
    category: str | None = None
    reason: str


class BoardSelectionResult(BaseModel):
    selections: list[BoardSelection] = Field(default_factory=list)
    issues: list[BoardSelectionIssue] = Field(default_factory=list)


class SearchKey(BaseModel):
    """A normalized search value with its exact Day 1 provenance."""

    original_value: str
    normalized_value: str
    source_field: SearchKeySource


SearchStatus = Literal[
    "ok",
    "not_found",
    "unreachable",
    "captcha_blocked",
    "skipped",
]

CandidateNameRole = Literal["business", "person", "unknown"]


class CandidateLicenseRecord(BaseModel):
    """A raw board row returned as a candidate, not an accepted identity match."""

    board_id: str
    board_name: str
    source_strategy: str
    license_number: str | None = None
    holder_name: str | None = None
    holder_name_role: CandidateNameRole = "unknown"
    raw_license_type: str | None = None
    raw_classification: str | None = None
    raw_license_status: str | None = None
    issued_date: str | None = None
    expiration_date: str | None = None
    address: str | None = None
    state: str | None = None
    phone: str | None = None
    source_fields: dict[str, Any] = Field(default_factory=dict)
    evidence_url: str
    source_reference: str | None = None
    fetched_at: datetime


class BoardSearchResult(BaseModel):
    """Access outcome for one selected source or one skipped Part 1 issue."""

    board_id: str | None = None
    board_name: str | None = None
    jurisdiction: str | None = None
    strategy: str | None = None
    search_status: SearchStatus
    source_url: str | None = None
    fetched_at: datetime
    candidates: list[CandidateLicenseRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


MatchConfidence = Literal["high", "medium", "low"]
NormalizedLicenseStatus = Literal["active", "inactive"]
NameRelationship = Literal["exact_normalized", "partial", "fuzzy", "none"]


class IdentityMatchDecision(BaseModel):
    """A separate decision about one raw candidate license record."""

    candidate: CandidateLicenseRecord
    accepted: bool
    match_confidence: MatchConfidence
    name_relationship: NameRelationship
    matched_identity_field: SearchKeySource | None = None
    matched_identity_value: str | None = None
    matched_candidate_field: str | None = None
    matched_candidate_role: CandidateNameRole | None = None
    supporting_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AcceptedLicenseRecord(CandidateLicenseRecord):
    """A candidate accepted as belonging to the Day 1 identity."""

    normalized_status: NormalizedLicenseStatus | None = None
    match_confidence: Literal["high", "medium"]
    matched_identity_field: SearchKeySource
    matched_identity_value: str
    matched_candidate_field: str
    matched_candidate_role: CandidateNameRole
    supporting_evidence: list[str] = Field(default_factory=list)
    match_notes: list[str] = Field(default_factory=list)


class Day2BoardResult(BaseModel):
    """Final matching view for one unchanged Part 2 board-access result."""

    board_id: str | None = None
    board_name: str | None = None
    jurisdiction: str | None = None
    strategy: str | None = None
    search_status: SearchStatus
    source_url: str | None = None
    fetched_at: datetime
    match_decisions: list[IdentityMatchDecision] = Field(default_factory=list)
    accepted_licenses: list[AcceptedLicenseRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Day2Result(BaseModel):
    """Assembled Day 2 output without making an unlicensed conclusion."""

    identity_place_id: str
    board_results: list[Day2BoardResult] = Field(default_factory=list)
    accepted_licenses: list[AcceptedLicenseRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
