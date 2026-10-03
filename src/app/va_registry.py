"""Virginia SCC registry enrichment using official public CIS pages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs

import httpx

from app.models import BusinessIdentity
from app.registry_models import RegistryEnrichmentResult, RegistryEvidence, RegistryName
from app.registry_matching import registry_addresses_equal, registry_legal_name_equal
from app.search_keys import name_relationship


VA_SCC_SEARCH_URL = "https://cis.scc.virginia.gov/EntitySearch/Index"
VA_SCC_COOKIE_URL = "https://cis.scc.virginia.gov/Cookie/StoreCookieConsent"
VA_SCC_SOURCE_NAME = "Virginia SCC Clerk's Information System"


class VASCCRegistrySource:
    """Use Virginia SCC CIS public pages without crossing its access controls."""

    jurisdiction = "VA"
    source_name = VA_SCC_SOURCE_NAME

    def __init__(
        self,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=True, timeout=30.0)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enrich(self, identity: BusinessIdentity) -> RegistryEnrichmentResult:
        fetched_at = self._clock()
        names = _identity_business_names(identity)
        if not names:
            return self._result(
                "skipped",
                fetched_at,
                notes=["The base identity has no established business name for SCC search."],
            )

        try:
            search_page = self._open_search_page()
            if _captcha_present(search_page):
                return self._result(
                    "captcha_blocked",
                    fetched_at,
                    url=str(search_page.url),
                    notes=[
                        "Virginia SCC CIS requires reCAPTCHA before entity search; no automated search was attempted."
                    ],
                )

            token = _verification_token(search_page.text)
            candidates: dict[str, _VASCCCandidate] = {}
            for name in names:
                response = self._client.post(
                    VA_SCC_SEARCH_URL,
                    data=_search_form(name, token),
                )
                if _captcha_present(response):
                    return self._result(
                        "captcha_blocked",
                        fetched_at,
                        url=str(response.url),
                        notes=[
                            "Virginia SCC CIS required CAPTCHA verification; no access control was bypassed."
                        ],
                    )
                response.raise_for_status()
                for summary in _parse_search_results(response.text):
                    detail = self._client.get(summary.detail_url)
                    if _captcha_present(detail):
                        return self._result(
                            "captcha_blocked",
                            fetched_at,
                            url=str(detail.url),
                            notes=[
                                "Virginia SCC CIS required CAPTCHA verification while opening an entity record."
                            ],
                        )
                    detail.raise_for_status()
                    candidate = _parse_entity_detail(
                        detail.text,
                        str(detail.url),
                        fallback=summary,
                    )
                    candidates[candidate.entity_id] = candidate
        except (httpx.HTTPError, ValueError) as exc:
            return self._result(
                "unreachable",
                fetched_at,
                notes=[f"Virginia SCC CIS could not be read safely: {exc}"],
            )

        return _establish_va_candidate(identity, list(candidates.values()), fetched_at)

    def _open_search_page(self) -> httpx.Response:
        response = self._client.get(VA_SCC_SEARCH_URL)
        response.raise_for_status()
        if _cookie_consent_page(response):
            consent = self._client.post(VA_SCC_COOKIE_URL, content=b"")
            consent.raise_for_status()
            response = self._client.get(VA_SCC_SEARCH_URL)
            response.raise_for_status()
        return response

    def _result(
        self,
        status: str,
        fetched_at: datetime,
        *,
        url: str = VA_SCC_SEARCH_URL,
        notes: list[str] | None = None,
    ) -> RegistryEnrichmentResult:
        return RegistryEnrichmentResult(
            jurisdiction=self.jurisdiction,
            registry=self.source_name,
            status=status,
            url=url,
            fetched_at=fetched_at,
            notes=notes or [],
        )


@dataclass
class _Cell:
    text: str = ""
    links: list[str] = field(default_factory=list)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[_Cell]]] = []
        self._depth = 0
        self._table: list[list[_Cell]] | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in {"td", "th"}:
            self._cell = _Cell()
        elif self._cell is not None and tag == "a" and attributes.get("href"):
            self._cell.links.append(str(attributes["href"]))

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text += f" {data}"

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 1 and tag in {"td", "th"} and self._cell is not None:
            self._cell.text = " ".join(self._cell.text.split())
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            if self._depth == 1 and self._table:
                self.tables.append(self._table)
                self._table = None
            self._depth -= 1


@dataclass
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    children: list["_HTMLNode"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Small DOM used for the nested field rows on SCC entity detail pages."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HTMLNode("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HTMLNode(tag, {key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text_parts.append(data)


def _descendants(node: _HTMLNode) -> list[_HTMLNode]:
    descendants: list[_HTMLNode] = []
    for child in node.children:
        descendants.append(child)
        descendants.extend(_descendants(child))
    return descendants


def _node_text(node: _HTMLNode) -> str:
    parts = [*node.text_parts]
    for child in node.children:
        parts.append(_node_text(child))
    return " ".join(" ".join(parts).split())


def _classes(node: _HTMLNode) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _detail_panel_pairs(html: str) -> dict[str, dict[str, str]]:
    parser = _DocumentParser()
    parser.feed(html)
    panels: dict[str, dict[str, str]] = {}
    for panel in _descendants(parser.root):
        if "data_pannel0" not in _classes(panel):
            continue
        heading = next(
            (
                _node_text(node)
                for node in _descendants(panel)
                if "section-title" in _classes(node) and _node_text(node)
            ),
            "",
        )
        if not heading:
            continue
        pairs = panels.setdefault(_header_key(heading), {})
        for row in _descendants(panel):
            if "row" not in _classes(row):
                continue
            columns = [
                child
                for child in row.children
                if any(token.startswith("col-") for token in _classes(child))
            ]
            for index in range(0, len(columns) - 1, 2):
                label = _header_key(_node_text(columns[index]))
                value = _node_text(columns[index + 1])
                if label and value:
                    pairs.setdefault(label, value)
    return panels


@dataclass
class _VASCCCandidate:
    entity_id: str
    legal_name: str
    status: str | None
    principal_office_address: str | None
    detail_url: str
    dbas: list[str] = field(default_factory=list)
    principals: list[tuple[str, str]] = field(default_factory=list)
    registered_agent_name: str | None = None
    registered_office_address: str | None = None


def _identity_business_names(identity: BusinessIdentity) -> list[str]:
    return list(
        dict.fromkeys(
            value.strip()
            for value in (identity.legal_name, identity.dba, identity.business_name)
            if value and value.strip()
        )
    )


def _captcha_present(response: httpx.Response) -> bool:
    body = response.text.casefold()
    return response.status_code in {403, 429} or any(
        marker in body
        for marker in (
            "google.com/recaptcha",
            "grecaptcha.execute",
            "g-recaptcha",
            "please try again. you may be a bot",
        )
    )


def _cookie_consent_page(response: httpx.Response) -> bool:
    return "/cookie/cookieconsent" in str(response.url).casefold() or (
        "cookie consent" in response.text.casefold()
        and "business entity search" not in response.text.casefold()
    )


def _verification_token(html: str) -> str:
    match = re.search(
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
        html,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("SCC search page did not contain an antiforgery token.")
    return match.group(1)


def _search_form(name: str, token: str) -> dict[str, str | bool | int]:
    return {
        "SearchType": "businessname",
        "IsOnline": True,
        "QuickSearch.BESearchLogic": "3",
        "QuickSearch.ExactMatch": "3",
        "QuickSearch.BusinessName": name,
        "__RequestVerificationToken": token,
    }


def _tables(html: str) -> list[list[list[_Cell]]]:
    parser = _TableParser()
    parser.feed(html)
    return parser.tables


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _table_records(table: list[list[_Cell]]) -> list[dict[str, _Cell]]:
    if len(table) < 2:
        return []
    headers = [_header_key(cell.text) for cell in table[0]]
    if not all(headers):
        return []
    return [dict(zip(headers, row)) for row in table[1:] if len(row) == len(headers)]


def _parse_search_results(html: str) -> list[_VASCCCandidate]:
    results: list[_VASCCCandidate] = []
    for table in _tables(html):
        for record in _table_records(table):
            id_cell = _record_cell(record, "entityid", "businessid", "sccid")
            name_cell = _record_cell(record, "entityname", "businessname", "name")
            if id_cell is None or name_cell is None:
                continue
            detail_link = next(
                (
                    link
                    for cell in record.values()
                    for link in cell.links
                    if "entitysearch/businessinformation" in link.casefold()
                ),
                None,
            )
            if detail_link is None:
                continue
            entity_id = id_cell.text or _business_id_from_url(detail_link)
            if not entity_id or not name_cell.text:
                continue
            status = _record_text(record, "entitystatus", "status")
            results.append(
                _VASCCCandidate(
                    entity_id=entity_id,
                    legal_name=name_cell.text,
                    status=status,
                    principal_office_address=None,
                    detail_url=urljoin(VA_SCC_SEARCH_URL, detail_link),
                )
            )
    if not results and not any(
        marker in html.casefold()
        for marker in ("no records found", "no results found", "0 records")
    ):
        raise ValueError("SCC returned an unrecognized entity-search result.")
    return results


def _parse_entity_detail(
    html: str,
    source_url: str,
    *,
    fallback: _VASCCCandidate,
) -> _VASCCCandidate:
    pairs: dict[str, str] = {}
    tables = _tables(html)
    for table in tables:
        for row in table:
            if len(row) == 2 and row[0].text and row[1].text:
                pairs.setdefault(_header_key(row[0].text), row[1].text)

    panels = _detail_panel_pairs(html)
    entity_pairs = panels.get("entityinformation", {})
    for key, value in entity_pairs.items():
        pairs.setdefault(key, value)
    registered_agent_pairs = panels.get("registeredagentinformation", {})
    principal_office_pairs = panels.get("principalofficeaddress", {})

    entity_id = _pair_value(pairs, "entityid", "businessid", "sccid") or fallback.entity_id
    legal_name = _pair_value(pairs, "entityname", "businessname", "legalname") or fallback.legal_name
    if not entity_id or not legal_name:
        raise ValueError("SCC entity detail omitted its entity ID or legal name.")

    dbas: list[str] = []
    principals: list[tuple[str, str]] = []
    for table in tables:
        records = _table_records(table)
        for record in records:
            dba = _record_text(
                record,
                "fictitiousname",
                "assumedname",
                "tradename",
                "doingbusinessas",
            )
            if dba:
                dbas.append(dba)
            principal_name = _record_text(record, "principalname", "officername")
            principal_role = _record_text(record, "title", "role", "principaltype")
            if principal_name and principal_role:
                principals.append((principal_name, principal_role))

    return _VASCCCandidate(
        entity_id=entity_id,
        legal_name=legal_name,
        status=_pair_value(pairs, "entitystatus", "status") or fallback.status,
        principal_office_address=_pair_value(
            principal_office_pairs, "address"
        ) or _pair_value(pairs, "principalofficeaddress", "businessaddress"),
        detail_url=source_url,
        dbas=list(dict.fromkeys(dbas)),
        principals=list(dict.fromkeys(principals)),
        registered_agent_name=_pair_value(
            registered_agent_pairs, "name"
        ) or _pair_value(pairs, "registeredagentname", "registeredagent"),
        registered_office_address=_pair_value(
            registered_agent_pairs, "registeredofficeaddress"
        ) or _pair_value(pairs, "registeredofficeaddress", "registeredagentaddress"),
    )


def _record_cell(record: dict[str, _Cell], *keys: str) -> _Cell | None:
    return next((record[key] for key in keys if key in record), None)


def _record_text(record: dict[str, _Cell], *keys: str) -> str | None:
    cell = _record_cell(record, *keys)
    return cell.text if cell and cell.text else None


def _pair_value(pairs: dict[str, str], *keys: str) -> str | None:
    return next((pairs[key] for key in keys if pairs.get(key)), None)


def _business_id_from_url(url: str) -> str:
    return (parse_qs(urlparse(url).query).get("businessId") or [""])[0]


def _establish_va_candidate(
    identity: BusinessIdentity,
    candidates: list[_VASCCCandidate],
    fetched_at: datetime,
) -> RegistryEnrichmentResult:
    if not candidates:
        return RegistryEnrichmentResult(
            jurisdiction="VA",
            registry=VA_SCC_SOURCE_NAME,
            status="not_found",
            url=VA_SCC_SEARCH_URL,
            fetched_at=fetched_at,
            notes=["Virginia SCC CIS was searched and returned no entity candidate."],
        )

    base_names = _identity_business_names(identity)
    surviving: list[tuple[_VASCCCandidate, list[RegistryEvidence]]] = []
    candidate_evidence: list[RegistryEvidence] = []
    conflicts: list[str] = []
    for candidate in candidates:
        evidence = _candidate_evidence(identity, candidate, base_names)
        candidate_evidence.extend(evidence)
        address_conflict = _address_conflicts(identity.address, candidate.principal_office_address)
        alias_exact = bool(identity.business_name) and any(
            _name_relationship(identity.business_name, dba) == "exact_normalized"
            for dba in candidate.dbas
        )
        legal_exact = any(
            registry_legal_name_equal(candidate.legal_name, name)
            for name in base_names
        )
        plausible = any(
            _name_relationship(candidate.legal_name, name)
            in {"exact_normalized", "partial", "fuzzy"}
            for name in base_names
        )
        address_exact = _addresses_equal(identity.address, candidate.principal_office_address)
        exact_relationship = alias_exact or legal_exact
        if address_conflict and not exact_relationship:
            conflicts.append(
                f"entity_{candidate.entity_id}_principal_office_address_conflicts_with_places_address"
            )
            continue
        if exact_relationship or (plausible and address_exact):
            surviving.append((candidate, evidence))

    if len(surviving) != 1:
        return RegistryEnrichmentResult(
            jurisdiction="VA",
            registry=VA_SCC_SOURCE_NAME,
            status="ambiguous",
            url=VA_SCC_SEARCH_URL,
            fetched_at=fetched_at,
            evidence=candidate_evidence,
            conflicts=list(dict.fromkeys(conflicts)),
            notes=[
                f"Virginia SCC returned {len(candidates)} candidate(s), but {len(surviving)} uniquely satisfied the establishment policy."
            ],
        )

    candidate, evidence = surviving[0]
    legal_name = RegistryName(
        value=candidate.legal_name,
        kind="legal_name",
        raw_field="Entity Name",
        source_name=VA_SCC_SOURCE_NAME,
        source_url=candidate.detail_url,
        entity_id=candidate.entity_id,
    )
    dbas = [
        RegistryName(
            value=value,
            kind="dba",
            raw_field="Fictitious Name",
            source_name=VA_SCC_SOURCE_NAME,
            source_url=candidate.detail_url,
            entity_id=candidate.entity_id,
        )
        for value in candidate.dbas
    ]
    principals = [
        RegistryName(
            value=name,
            kind="principal",
            role=role,
            raw_field="Principal Name / Title",
            source_name=VA_SCC_SOURCE_NAME,
            source_url=candidate.detail_url,
            entity_id=candidate.entity_id,
        )
        for name, role in candidate.principals
        if "registered agent" not in role.casefold()
    ]
    return RegistryEnrichmentResult(
        jurisdiction="VA",
        registry=VA_SCC_SOURCE_NAME,
        status="ok",
        url=candidate.detail_url,
        fetched_at=fetched_at,
        established_entity_id=candidate.entity_id,
        established_entity_name=candidate.legal_name,
        entity_status=candidate.status,
        principal_office_address=candidate.principal_office_address,
        legal_names=[legal_name],
        dbas=dbas,
        principals=principals,
        evidence=evidence,
        notes=["One Virginia SCC entity satisfied the conservative establishment policy."],
    )


def _candidate_evidence(
    identity: BusinessIdentity,
    candidate: _VASCCCandidate,
    base_names: list[str],
) -> list[RegistryEvidence]:
    evidence = [
        RegistryEvidence(
            kind="scc_entity_candidate",
            source_name=VA_SCC_SOURCE_NAME,
            source_url=candidate.detail_url,
            entity_id=candidate.entity_id,
            observed={
                "entity_name": candidate.legal_name,
                "entity_status": candidate.status,
                "principal_office_address": candidate.principal_office_address,
            },
        )
    ]
    if any(
        registry_legal_name_equal(candidate.legal_name, name)
        for name in base_names
    ):
        evidence.append(
            RegistryEvidence(
                kind="exact_legal_name",
                source_name=VA_SCC_SOURCE_NAME,
                source_url=candidate.detail_url,
                entity_id=candidate.entity_id,
                observed={"entity_name": candidate.legal_name},
            )
        )
    if identity.business_name:
        for dba in candidate.dbas:
            if _name_relationship(identity.business_name, dba) == "exact_normalized":
                evidence.append(
                    RegistryEvidence(
                        kind="exact_fictitious_name_relationship",
                        source_name=VA_SCC_SOURCE_NAME,
                        source_url=candidate.detail_url,
                        entity_id=candidate.entity_id,
                        observed={"fictitious_name": dba},
                    )
                )
    if _addresses_equal(identity.address, candidate.principal_office_address):
        evidence.append(
            RegistryEvidence(
                kind="exact_principal_office_address",
                source_name=VA_SCC_SOURCE_NAME,
                source_url=candidate.detail_url,
                entity_id=candidate.entity_id,
                observed={"principal_office_address": candidate.principal_office_address},
            )
        )
    elif identity.address and candidate.principal_office_address:
        evidence.append(
            RegistryEvidence(
                kind="registry_address_differs_from_places_address",
                source_name=VA_SCC_SOURCE_NAME,
                source_url=candidate.detail_url,
                entity_id=candidate.entity_id,
                observed={
                    "places_address": identity.address,
                    "principal_office_address": candidate.principal_office_address,
                },
                notes=[
                    "A differing registry office address is retained as evidence; exact legal or fictitious-name relationships may link a branch or service location to this entity."
                ],
            )
        )
    if candidate.registered_agent_name or candidate.registered_office_address:
        evidence.append(
            RegistryEvidence(
                kind="registered_agent",
                source_name=VA_SCC_SOURCE_NAME,
                source_url=candidate.detail_url,
                entity_id=candidate.entity_id,
                observed={
                    "name": candidate.registered_agent_name,
                    "address": candidate.registered_office_address,
                },
                notes=["Registered-agent facts are evidence only and never principal keys."],
            )
        )
    return evidence


def _addresses_equal(left: str | None, right: str | None) -> bool:
    return registry_addresses_equal(left, right)


def _address_conflicts(left: str | None, right: str | None) -> bool:
    return bool(left and right) and not _addresses_equal(left, right)


def _name_relationship(left: str, right: str) -> str:
    return name_relationship(left, right)
