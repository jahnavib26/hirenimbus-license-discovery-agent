"""Conservative discovery of license-number clues on established first-party pages."""

from __future__ import annotations

import re
from datetime import datetime

from app.day2_models import LicenseNumberEvidence


# Every pattern requires a board/family marker or a nearby licensing term. Bare
# numeric strings are deliberately ignored (phones, ZIPs, dates, order IDs, etc.).
_MARKED_PATTERNS = (
    ("TSBPE", "master_plumber", re.compile(r"\bM\s*[-#:]?\s*(\d{4,6})\b", re.I)),
    ("TDLR", "electrical", re.compile(r"\b(?:TECL|T.E.C.L.)\s*[-#:]?\s*(\d{4,8})\b", re.I)),
    ("TDLR", "air_conditioning", re.compile(r"\b(TACLA|TACLB)\s*[-#:]?\s*(\d{5,8})\s*([A-Z])\b", re.I)),
    ("CSLB", "contractor", re.compile(r"\bCSLB(?:\s+license)?\s*[-#:]?\s*(\d{5,8})\b", re.I)),
    ("DPOR", "contractor", re.compile(r"\b(?:VA\s+)?DPOR\s*[-#:]?\s*(\d{8,12})\b", re.I)),
    ("DC_INDUSTRIAL_TRADES", "industrial_trade", re.compile(r"\b(?:DC\s+)?(?:license|lic\.?)[\s#:]*(ECC\d{6,}|PC\d{2,}|RC\d{3,})\b", re.I)),
)
_GENERIC_LICENSE = re.compile(
    r"\b(?:contractor(?:'s)?\s+license|license|lic\.)\s*(?:no\.?|number|#)\s*[:#-]?\s*(\d{5,10})\b",
    re.I,
)


def normalize_license_number(board_id: str, family: str, raw: str) -> str:
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if board_id == "TSBPE" and family == "master_plumber":
        return compact.removeprefix("M")
    if board_id == "TDLR" and family == "electrical":
        return compact.removeprefix("TECL")
    if board_id == "TDLR" and family == "air_conditioning":
        match = re.fullmatch(r"(TACLA|TACLB)(\d+)([A-Z])", compact)
        if not match:
            raise ValueError("Invalid TDLR air-conditioning license number.")
        prefix, digits, suffix = match.groups()
        # TDLR's canonical TACLA/TACLB form uses an eight-digit numeric body.
        return f"{prefix}{digits.zfill(8)}{suffix}"
    return compact


def extract_license_numbers(
    text: str, *, page_url: str, fetched_at: datetime,
    verified_identity_place_id: str | None = None,
) -> list[LicenseNumberEvidence]:
    found: dict[tuple[str, str], LicenseNumberEvidence] = {}
    for board_id, family, pattern in _MARKED_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0)
            if family == "air_conditioning":
                raw_number = "".join(match.groups())
            else:
                raw_number = match.group(1)
            normalized = normalize_license_number(board_id, family, raw_number)
            evidence = _evidence(text, match.start(), match.end(), raw, normalized, board_id, family, page_url, fetched_at, verified_identity_place_id)
            found.setdefault((board_id, normalized), evidence)

    # A generic contractor-license label is useful only for the board family
    # explicitly named in the same local context.
    for match in _GENERIC_LICENSE.finditer(text):
        context = text[max(0, match.start() - 80) : min(len(text), match.end() + 80)]
        board_id = "CSLB" if re.search(
            r"\b(?:CSLB|California|CA\s+(?:Contractor\s+)?License)\b", context, re.I
        ) else None
        if board_id:
            normalized = normalize_license_number(board_id, "contractor", match.group(1))
            evidence = _evidence(text, match.start(), match.end(), match.group(0), normalized, board_id, "contractor", page_url, fetched_at, verified_identity_place_id)
            found.setdefault((board_id, normalized), evidence)
    return list(found.values())


def _evidence(text: str, start: int, end: int, raw: str, normalized: str, board_id: str, family: str, page_url: str, fetched_at: datetime, verified_identity_place_id: str | None) -> LicenseNumberEvidence:
    snippet = " ".join(text[max(0, start - 80) : min(len(text), end + 80)].split())
    return LicenseNumberEvidence(raw_text=raw, normalized_number=normalized, likely_board_id=board_id, license_family=family, page_url=page_url, fetched_at=fetched_at, supporting_snippet=snippet, verified_identity_place_id=verified_identity_place_id)
