"""State-independent models for registry enrichment and auditable evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


RegistrySearchStatus = Literal[
    "ok", "not_found", "ambiguous", "unreachable", "captcha_blocked", "skipped"
]
RegistryNameKind = Literal["legal_name", "dba", "principal"]


class RegistryName(BaseModel):
    value: str
    kind: RegistryNameKind
    role: str | None = None
    raw_field: str | None = None
    source_name: str
    source_url: str | None = None
    entity_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class RegistryEvidence(BaseModel):
    """An auditable fact used to establish a registry entity as the business."""

    kind: str
    source_name: str
    source_url: str | None = None
    entity_id: str | None = None
    observed: dict[str, str | int | bool | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class RegistryEnrichmentResult(BaseModel):
    jurisdiction: str
    registry: str
    status: RegistrySearchStatus
    url: str | None = None
    fetched_at: datetime | None = None
    candidate_place_id: str | None = None
    established_entity_id: str | None = None
    established_entity_name: str | None = None
    entity_status: str | None = None
    principal_office_address: str | None = None
    legal_names: list[RegistryName] = Field(default_factory=list)
    dbas: list[RegistryName] = Field(default_factory=list)
    principals: list[RegistryName] = Field(default_factory=list)
    evidence: list[RegistryEvidence] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def entity_established(self) -> bool:
        return self.status == "ok" and bool(
            self.established_entity_id and self.established_entity_name
        )
