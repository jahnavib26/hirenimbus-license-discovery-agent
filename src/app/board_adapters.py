"""Narrow adapters for official sources selected by Day 2 Part 1."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterable
from datetime import datetime
from html.parser import HTMLParser
from typing import Protocol

import httpx

from app.day2_models import BoardSelection, CandidateLicenseRecord, SearchKey
from app.search_keys import name_relationship, normalize_search_name


TDLR_DATA_URL = "https://data.texas.gov/resource/7358-krk7.json"
TSBPE_RMP_URL = "https://tsbpe.texas.gov/download-csv/RMP/"
MHIC_BUSINESS_NAME_URL = (
    "https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_Search/"
    "OP_search.cgi?calling_app=HIC::HIC_business_name"
)
DPOR_LISTS = (
    (
        "2705 A",
        "https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2705a__crnt.txt",
    ),
    (
        "2705 B",
        "https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2705b__crnt.txt",
    ),
    (
        "2705 C",
        "https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2705c__crnt.txt",
    ),
    (
        "2710",
        "https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2710__crnt.txt",
    ),
)


class CaptchaBlockedError(Exception):
    """The official source requires a human CAPTCHA interaction."""


class SourceAccessError(Exception):
    """The official source responded but could not be used safely."""


class BoardAdapter(Protocol):
    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]: ...


class TDLRAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]:
        candidates: list[CandidateLicenseRecord] = []
        for value in _unique(key.original_value for key in search_keys):
            response = self._client.get(
                TDLR_DATA_URL,
                params={"$q": value, "$limit": "1000"},
            )
            _ensure_accessible(response)
            try:
                rows = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise SourceAccessError("TDLR returned invalid JSON.") from exc
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise SourceAccessError("TDLR returned an unexpected JSON shape.")
            if rows:
                _validate_schema(
                    {str(field) for row in rows for field in row},
                    (
                        ("license_type",),
                        ("license_number",),
                        ("business_name", "owner_name"),
                    ),
                    "TDLR JSON",
                )
            for row in rows:
                if not _tdlr_category_applies(row, selection.applicable_categories):
                    continue
                candidates.append(
                    _tdlr_candidate(selection, row, str(response.request.url), fetched_at)
                )
        return _deduplicate(candidates)


class TSBPEAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]:
        response = self._client.get(TSBPE_RMP_URL)
        _ensure_accessible(response)
        rows = _csv_rows(
            _download_text(response),
            required_fields=(
                ("RANK",),
                ("LICENSE_NBR",),
                ("LIC_STATUS",),
                ("FIRST_NAME",),
                ("LAST_NAME",),
                ("PLUMB_COMPANY",),
            ),
            source_name="TSBPE CSV",
        )
        normalized_keys = _normalized_keys(search_keys)
        candidates: list[CandidateLicenseRecord] = []
        for row in rows:
            person_name = _join(
                [
                    _pick(row, "FIRST_NAME"),
                    _pick(row, "MIDDLE_NAME"),
                    _pick(row, "LAST_NAME"),
                    _pick(row, "SUFFIX"),
                ],
                " ",
            )
            if not _matches(normalized_keys, [_pick(row, "PLUMB_COMPANY"), person_name]):
                continue
            candidates.append(
                CandidateLicenseRecord(
                    board_id=selection.board_id,
                    board_name=selection.board_name,
                    source_strategy=selection.strategy,
                    license_number=_pick(row, "LICENSE_NBR"),
                    holder_name=person_name,
                    holder_name_role="person",
                    raw_license_type=_pick(row, "RANK"),
                    raw_license_status=_pick(row, "LIC_STATUS"),
                    issued_date=_pick(row, "LICENSE_DATE"),
                    expiration_date=_pick(row, "EXPIRATION_DTE"),
                    address=_join(
                        [
                            _pick(row, "ADDR1"),
                            _pick(row, "ADDR2"),
                            _pick(row, "ADDR3"),
                            _pick(row, "CITY"),
                            _pick(row, "STATE"),
                            _pick(row, "ZIP"),
                        ]
                    ),
                    state=_pick(row, "STATE"),
                    phone=_pick(row, "PHONE"),
                    source_fields=_nonempty(row),
                    evidence_url=str(response.request.url),
                    source_reference="TSBPE Responsible Master Plumber List",
                    fetched_at=fetched_at,
                )
            )
        return _deduplicate(candidates)


class DPORAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]:
        normalized_keys = _normalized_keys(search_keys)
        candidates: list[CandidateLicenseRecord] = []
        for list_name, url in DPOR_LISTS:
            response = self._client.get(url)
            _ensure_accessible(response)
            for row in _tab_rows(
                response.text,
                required_fields=(
                    ("BOARD",),
                    ("OCCUPATION",),
                    ("CERTIFICATE #",),
                    ("INDIVIDUAL NAME", "BUSINESS NAME"),
                    ("EXPIRATION DATE",),
                ),
                source_name=f"DPOR regulant list {list_name}",
            ):
                if not _matches(
                    normalized_keys,
                    [_pick(row, "INDIVIDUAL NAME"), _pick(row, "BUSINESS NAME")],
                ):
                    continue
                license_number = _join(
                    [
                        _pick(row, "BOARD"),
                        _pick(row, "OCCUPATION"),
                        _pick(row, "CERTIFICATE #"),
                    ],
                    " ",
                )
                candidates.append(
                    CandidateLicenseRecord(
                        board_id=selection.board_id,
                        board_name=selection.board_name,
                        source_strategy=selection.strategy,
                        license_number=license_number,
                        holder_name=(
                            _pick(row, "BUSINESS NAME")
                            or _pick(row, "INDIVIDUAL NAME")
                        ),
                        holder_name_role=(
                            "business" if _pick(row, "BUSINESS NAME") else "person"
                        ),
                        raw_license_type=_pick(row, "LICENSE RANK") or list_name,
                        raw_classification=_pick(row, "LICENSE SPECIALTY"),
                        raw_license_status=_pick(row, "LICENSE STATUS", "STATUS"),
                        issued_date=_pick(row, "CERTIFICATION DATE"),
                        expiration_date=_pick(row, "EXPIRATION DATE"),
                        address=_join(
                            [
                                _pick(row, "FIRST LINE ADDRESS"),
                                _pick(row, "SECOND LINE ADDRESS"),
                                _pick(row, "P O BOX #"),
                                _pick(row, "CITY"),
                                _pick(row, "STATE"),
                                _pick(row, "FIVE DIGIT ZIP CODE"),
                                _pick(row, "ZIP CODE EXTENSION"),
                            ]
                        ),
                        state=_pick(row, "STATE"),
                        phone=_pick(row, "PHONE", "TELEPHONE"),
                        source_fields=_nonempty(row),
                        evidence_url=str(response.request.url),
                        source_reference=f"DPOR regulant list {list_name}",
                        fetched_at=fetched_at,
                    )
                )
        return _deduplicate(candidates)


class CSLBAdapter:
    """Use CSLB's official public portal flow to retrieve its master CSV."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]:
        if not selection.source_url:
            raise SourceAccessError("CSLB selection has no official source URL.")
        response = self._client.get(selection.source_url)
        _ensure_accessible(response)

        if _looks_tabular(response):
            download = response
        else:
            first_form = _parse_webform(response.text)
            selected = self._client.post(
                selection.source_url,
                data={
                    **first_form,
                    "__EVENTTARGET": "ctl00$MainContent$ddlStatus",
                    "__EVENTARGUMENT": "",
                    "ctl00$MainContent$ddlStatus": "M",
                },
            )
            _ensure_accessible(selected)
            second_form = _parse_webform(selected.text)
            download = self._client.post(
                selection.source_url,
                data={
                    **second_form,
                    "__EVENTTARGET": "ctl00$MainContent$lbMasterCSV",
                    "__EVENTARGUMENT": "",
                    "ctl00$MainContent$ddlStatus": "M",
                },
            )
            _ensure_accessible(download)
        if not _looks_tabular(download):
            raise SourceAccessError("CSLB did not return its License Master CSV.")

        normalized_keys = _normalized_keys(search_keys)
        candidates: list[CandidateLicenseRecord] = []
        for row in _csv_rows(
            _download_text(download),
            required_fields=(
                ("LICENSE NUMBER", "LICENSE_NUMBER", "LIC_NUMBER", "LIC-NUMBER"),
                ("BUSINESS NAME", "BUS_NAME", "BUS_NAME_1", "BUS-NAME-1"),
                ("LICENSE STATUS", "LICENSE_STATUS", "PRIM_STAT_CODE", "PRIM-STAT-CODE"),
            ),
            source_name="CSLB License Master CSV",
        ):
            names = [
                _pick(row, "BUSINESS NAME", "BUS_NAME", "BUS_NAME_1", "BUS-NAME-1"),
                _pick(row, "BUS_NAME_2", "BUS-NAME-2", "DBA"),
            ]
            if not _matches(normalized_keys, names):
                continue
            phone = _pick(row, "PHONE", "TELEPHONE", "BUS_PHONE")
            if not phone:
                phone = "".join(
                    filter(
                        None,
                        [
                            _pick(row, "BUS-PHONE-NO-AREA"),
                            _pick(row, "BUS-PHONE-NO-PRE"),
                            _pick(row, "BUS-PHONE-NO"),
                        ],
                    )
                ) or None
            candidates.append(
                CandidateLicenseRecord(
                    board_id=selection.board_id,
                    board_name=selection.board_name,
                    source_strategy=selection.strategy,
                    license_number=_pick(
                        row, "LICENSE NUMBER", "LICENSE_NUMBER", "LIC_NUMBER", "LIC-NUMBER"
                    ),
                    holder_name=names[0] or names[1],
                    holder_name_role="business",
                    raw_license_type=_pick(row, "BUSINESS TYPE", "BUS_TYPE_CODE", "BUS-TYPE-CODE"),
                    raw_classification=_pick(
                        row, "CLASSIFICATION", "CLASSIFICATIONS", "PRIN_CLASS", "PRIN-CLASS"
                    ),
                    raw_license_status=_pick(
                        row, "LICENSE STATUS", "LICENSE_STATUS", "PRIM_STAT_CODE", "PRIM-STAT-CODE"
                    ),
                    issued_date=_pick(
                        row,
                        "ISSUE DATE",
                        "ISSUE_DATE",
                        "ISSUE-DATE",
                        "ORIG_ISSUE_DATE",
                        "ORIG-ISSUE-DATE",
                    ),
                    expiration_date=_pick(
                        row,
                        "EXPIRATION DATE",
                        "EXPIRATION_DATE",
                        "EXPIRATION-DATE",
                        "EXP_DATE",
                        "EXP-DATE",
                    ),
                    address=_join(
                        [
                            _pick(row, "ADDRESS", "MAILING_ADDR1", "MAILING-ADDR1"),
                            _pick(row, "MAILING_ADDR2", "MAILING-ADDR2"),
                            _pick(row, "CITY", "MAILING_CITY", "MAILING-CITY"),
                            _pick(row, "STATE", "MAILING_STATE", "MAILING-STATE"),
                            _pick(row, "ZIP", "MAILING_ZIP", "MAILING-ZIP"),
                        ]
                    ),
                    state=_pick(row, "STATE", "MAILING_STATE", "MAILING-STATE"),
                    phone=phone,
                    source_fields=_nonempty(row),
                    evidence_url=str(download.request.url),
                    source_reference="CSLB License Master CSV",
                    fetched_at=fetched_at,
                )
            )
        return _deduplicate(candidates)


class MHICAdapter:
    """Check the supplied official MHIC query and never cross its CAPTCHA."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        selection: BoardSelection,
        search_keys: list[SearchKey],
        fetched_at: datetime,
    ) -> list[CandidateLicenseRecord]:
        del selection, search_keys, fetched_at
        response = self._client.get(MHIC_BUSINESS_NAME_URL)
        _ensure_accessible(response)
        raise SourceAccessError(
            "The accessible MHIC page did not expose a safe query contract."
        )


class _WebFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden: dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "input":
            return
        values = dict(attrs)
        if values.get("type", "").casefold() != "hidden" or not values.get("name"):
            return
        self.hidden[values["name"]] = values.get("value") or ""


def _parse_webform(html: str) -> dict[str, str]:
    parser = _WebFormParser()
    parser.feed(html)
    required = {"__VIEWSTATE", "__EVENTVALIDATION"}
    if not required <= parser.hidden.keys():
        raise SourceAccessError("CSLB returned an unexpected portal form.")
    return parser.hidden


def _ensure_accessible(response: httpx.Response) -> None:
    body = response.text.casefold()
    if any(
        marker in body
        for marker in (
            "g-recaptcha",
            "recaptcha",
            "captcha",
            "please check the box above",
        )
    ):
        raise CaptchaBlockedError("The official source requires CAPTCHA interaction.")
    response.raise_for_status()


def _tdlr_category_applies(row: dict[str, object], categories: list[str]) -> bool:
    license_type = str(row.get("license_type") or "").casefold()
    if "electrical" in categories and "electric" in license_type:
        return True
    if "hvac" in categories and any(
        term in license_type for term in ("a/c", "air conditioning", "air conditioner")
    ):
        return True
    return False


def _tdlr_candidate(
    selection: BoardSelection,
    row: dict[str, object],
    evidence_url: str,
    fetched_at: datetime,
) -> CandidateLicenseRecord:
    raw = {str(key): value for key, value in row.items()}
    city_state_zip = _pick(raw, "business_city_state_zip", "mailing_address_city_state_zip")
    state_match = re.search(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", city_state_zip or "")
    return CandidateLicenseRecord(
        board_id=selection.board_id,
        board_name=selection.board_name,
        source_strategy=selection.strategy,
        license_number=_pick(raw, "license_number"),
        holder_name=_pick(raw, "business_name", "owner_name"),
        holder_name_role="business" if _pick(raw, "business_name") else "person",
        raw_license_type=_pick(raw, "license_type"),
        raw_classification=_pick(raw, "license_subtype"),
        raw_license_status=_pick(raw, "license_status"),
        expiration_date=_pick(raw, "license_expiration_date_mmddccyy"),
        address=_join(
            [
                _pick(raw, "business_address_line1", "mailing_address_line1"),
                _pick(raw, "business_address_line2", "mailing_address_line2"),
                city_state_zip,
            ]
        ),
        state=state_match.group(1) if state_match else None,
        phone=_pick(raw, "business_telephone", "owner_telephone"),
        source_fields=_nonempty(raw),
        evidence_url=evidence_url,
        source_reference="TDLR All Licenses Socrata dataset 7358-krk7",
        fetched_at=fetched_at,
    )


def _download_text(response: httpx.Response) -> str:
    content = response.content
    if content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = [
                    name
                    for name in archive.namelist()
                    if name.casefold().endswith((".csv", ".txt"))
                ]
                if not names:
                    raise SourceAccessError("Official archive contained no CSV or text file.")
                content = archive.read(names[0])
        except zipfile.BadZipFile as exc:
            raise SourceAccessError("Official source returned an invalid archive.") from exc
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _looks_tabular(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    disposition = response.headers.get("content-disposition", "").casefold()
    return (
        response.content.startswith(b"PK")
        or "csv" in content_type
        or ".csv" in disposition
    )


def _csv_rows(
    text: str,
    *,
    required_fields: tuple[tuple[str, ...], ...],
    source_name: str,
) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SourceAccessError(f"{source_name} had no header row.")
    _validate_schema(set(reader.fieldnames), required_fields, source_name)
    rows = list(reader)
    if any(None in row for row in rows):
        raise SourceAccessError(f"{source_name} contained malformed rows.")
    return [{str(key): value or "" for key, value in row.items()} for row in rows]


def _tab_rows(
    text: str,
    *,
    required_fields: tuple[tuple[str, ...], ...],
    source_name: str,
) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if not reader.fieldnames:
        raise SourceAccessError(f"{source_name} had no header row.")
    _validate_schema(set(reader.fieldnames), required_fields, source_name)
    rows = list(reader)
    if any(None in row for row in rows):
        raise SourceAccessError(f"{source_name} contained malformed rows.")
    return [{str(key): value or "" for key, value in row.items()} for row in rows]


def _normalized_keys(search_keys: list[SearchKey]) -> set[str]:
    return {key.normalized_value for key in search_keys if key.normalized_value}


def _matches(normalized_keys: set[str], values: Iterable[str | None]) -> bool:
    return any(
        name_relationship(normalized_key, value) != "none"
        for normalized_key in normalized_keys
        for value in values
        if value and value.strip()
    )


def _validate_schema(
    fields: set[str | None],
    required_fields: tuple[tuple[str, ...], ...],
    source_name: str,
) -> None:
    if not all(isinstance(field, str) and field.strip() for field in fields):
        raise SourceAccessError(f"{source_name} contained an invalid header row.")
    normalized_fields = {_field_key(field) for field in fields}
    missing = [
        alternatives
        for alternatives in required_fields
        if not any(_field_key(name) in normalized_fields for name in alternatives)
    ]
    if missing:
        expected = ["/".join(alternatives) for alternatives in missing]
        raise SourceAccessError(
            f"{source_name} did not contain expected fields: {', '.join(expected)}."
        )


def _pick(row: dict[str, object], *names: str) -> str | None:
    normalized = {_field_key(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(_field_key(name))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _join(values: Iterable[str | None], separator: str = ", ") -> str | None:
    present = [value.strip() for value in values if value and value.strip()]
    return separator.join(present) or None


def _nonempty(row: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in row.items()
        if value is not None and str(value).strip()
    }


def _deduplicate(
    candidates: list[CandidateLicenseRecord],
) -> list[CandidateLicenseRecord]:
    unique: dict[tuple[str | None, str | None, str], CandidateLicenseRecord] = {}
    for candidate in candidates:
        key = (
            candidate.license_number,
            candidate.holder_name,
            candidate.source_reference or candidate.evidence_url,
        )
        unique.setdefault(key, candidate)
    return list(unique.values())


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
