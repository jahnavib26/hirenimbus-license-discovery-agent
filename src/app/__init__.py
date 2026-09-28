"""HireNimbus identity-resolution and license-discovery package."""

from app.resolver import IdentityResolver
from app.pipeline import find_licenses

__all__ = ["IdentityResolver", "find_licenses"]
