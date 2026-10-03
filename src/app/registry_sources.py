"""Shared contracts and establishment checks for official registry sources."""

from __future__ import annotations

from typing import Protocol

from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult


class RegistrySource(Protocol):
    """Read-only source contract for a jurisdiction's official registry."""

    @property
    def jurisdiction(self) -> str: ...

    @property
    def source_name(self) -> str: ...

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult: ...


def registry_result_is_established(result: RegistryEnrichmentResult) -> bool:
    """Require an unambiguous successful entity and explicit establishment facts."""

    return result.entity_established and not result.conflicts and bool(result.evidence)
