"""Bounded first-party website retrieval for Day 1 corroboration."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.phone import InvalidPhoneNumber, normalize_us_phone


class WebsiteFetchError(RuntimeError):
    """A candidate website could not be fetched within the safety bounds."""


@dataclass(frozen=True)
class ObservedWebsitePhone:
    raw: str
    normalized: str


@dataclass(frozen=True)
class StructuredBusinessIdentity:
    """A home-services schema.org business entity found in first-party JSON-LD."""

    name: str | None = None
    legal_name: str | None = None
    telephone: str | None = None
    address: str | None = None
    source_type: str | None = None


@dataclass(frozen=True)
class WebsitePage:
    requested_url: str
    final_url: str
    visible_text: str
    phones: tuple[ObservedWebsitePhone, ...]
    structured_identities: tuple[StructuredBusinessIdentity, ...] = ()
    internal_identity_links: tuple[str, ...] = ()


class WebsiteClient:
    def fetch(self, url: str) -> WebsitePage: ...


def first_party_evidence_urls(url: str) -> tuple[str, ...]:
    """Return a small, deterministic set of ordinary first-party evidence pages."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return (url,)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return tuple(dict.fromkeys((
        url,
        urljoin(f"{origin}/", "contact/"),
        urljoin(f"{origin}/", "contact-us/"),
        urljoin(f"{origin}/", "about/"),
        urljoin(f"{origin}/", "locations/"),
    )))


def relevant_same_origin_links(
    base_url: str, links: Sequence[str], *, limit: int = 2
) -> tuple[str, ...]:
    """Select a few obvious identity pages; never turn this into a site crawl."""

    base = urlsplit(base_url)
    try:
        origin = (base.scheme.casefold(), (base.hostname or "").casefold(), base.port)
    except ValueError:
        return ()
    selected: list[str] = []
    for link in links:
        try:
            absolute = urljoin(base_url, link)
            parsed = urlsplit(absolute)
            candidate_origin = (
                parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port
            )
        except ValueError:
            continue
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or candidate_origin != origin
        ):
            continue
        clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if clean not in selected:
            selected.append(clean)
        if len(selected) >= limit:
            break
    return tuple(selected)


def fetch_bounded_first_party_pages(
    client: WebsiteClient, website_url: str
) -> tuple[list[WebsitePage], int]:
    """Fetch fixed identity routes plus at most two links from the verified homepage."""

    if is_known_third_party_url(website_url):
        return [], 0
    targets = list(first_party_evidence_urls(website_url))
    parsed = urlsplit(website_url)
    if parsed.path not in {"", "/"}:
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        if origin not in targets:
            targets.append(origin)
    pages: list[WebsitePage] = []
    failed = 0
    processed: set[str] = set()
    followed_homepage_links = False
    while targets and len(processed) < 8:
        target = targets.pop(0)
        if target in processed:
            continue
        processed.add(target)
        try:
            page = client.fetch(target)
        except WebsiteFetchError:
            failed += 1
            continue
        if not is_first_party_url(website_url, page.final_url):
            continue
        pages.append(page)
        requested = urlsplit(target)
        site_origin = urlsplit(website_url)
        requested_homepage = (
            requested.scheme.casefold() == site_origin.scheme.casefold()
            and requested.netloc.casefold() == site_origin.netloc.casefold()
            and requested.path in {"", "/"}
            and not requested.query
        )
        if not followed_homepage_links and requested_homepage:
            followed_homepage_links = True
            for link in relevant_same_origin_links(
                page.final_url, page.internal_identity_links
            ):
                if link not in processed and link not in targets:
                    targets.append(link)
    return pages, failed


HostResolver = Callable[[str], Sequence[str]]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_BLOCKED_THIRD_PARTY_DOMAINS = {
    "bbb.org",
    "chamberofcommerce.com",
    "checkatrade.com",
    "citysearch.com",
    "angi.com",
    "facebook.com",
    "google.com",
    "homeadvisor.com",
    "houzz.com",
    "instagram.com",
    "linkedin.com",
    "mapquest.com",
    "manta.com",
    "merchantcircle.com",
    "nextdoor.com",
    "spokeo.com",
    "thumbtack.com",
    "trustpilot.com",
    "whitepages.com",
    "yellowpages.com",
    "yelp.com",
}
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]*)?\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)"
)


class FirstPartyWebsiteClient:
    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = 1_000_000,
        max_redirects: int = 3,
        host_resolver: HostResolver | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Website timeout must be positive.")
        if max_response_bytes <= 0:
            raise ValueError("Website response-size cap must be positive.")
        if max_redirects < 0:
            raise ValueError("Website redirect limit cannot be negative.")
        self._client = http_client or httpx.Client(follow_redirects=False)
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._host_resolver = host_resolver or _resolve_host

    def fetch(self, url: str) -> WebsitePage:
        requested_url = _validated_public_url(url, self._host_resolver)
        if is_known_third_party_url(requested_url):
            raise WebsiteFetchError("The Places website URL is a third-party domain.")

        current_url = requested_url
        redirects = 0
        while True:
            try:
                with self._client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                    timeout=self._timeout,
                    headers={"User-Agent": "HireNimbusIdentityAudit/1.0"},
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebsiteFetchError(
                                "The candidate website returned an invalid redirect."
                            )
                        if redirects >= self._max_redirects:
                            raise WebsiteFetchError(
                                "The candidate website exceeded the redirect limit."
                            )
                        next_url = _validated_public_url(
                            urljoin(current_url, location), self._host_resolver
                        )
                        if not is_first_party_url(requested_url, next_url):
                            raise WebsiteFetchError(
                                "The candidate website redirected outside its first-party domain."
                            )
                        if urlsplit(current_url).scheme == "https" and urlsplit(
                            next_url
                        ).scheme == "http":
                            raise WebsiteFetchError(
                                "The candidate website attempted an HTTPS downgrade."
                            )
                        current_url = next_url
                        redirects += 1
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    if content_type and not any(
                        value in content_type
                        for value in ("text/html", "application/xhtml+xml")
                    ):
                        raise WebsiteFetchError(
                            "The candidate website did not return an HTML page."
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > self._max_response_bytes:
                            raise WebsiteFetchError(
                                "The candidate website exceeded the response-size cap."
                            )
                    encoding = response.encoding or "utf-8"
            except WebsiteFetchError:
                raise
            except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                raise WebsiteFetchError(
                    "The candidate website could not be fetched safely."
                ) from exc
            break

        parser = _VisiblePageParser()
        try:
            parser.feed(bytes(content).decode(encoding, errors="replace"))
            parser.close()
        except (UnicodeError, ValueError) as exc:
            raise WebsiteFetchError(
                "The candidate website returned invalid HTML."
            ) from exc
        visible_text = " ".join(parser.text_parts)
        phones = _extract_phones(visible_text, parser.tel_values)
        structured_phones = _extract_phones(
            "", [item.telephone for item in parser.structured_identities if item.telephone]
        )
        phones = tuple(dict.fromkeys((*phones, *structured_phones)))
        return WebsitePage(
            requested_url=requested_url,
            final_url=current_url,
            visible_text=visible_text,
            phones=phones,
            structured_identities=tuple(parser.structured_identities),
            internal_identity_links=tuple(parser.internal_identity_links),
        )


def is_first_party_url(requested_url: str, final_url: str) -> bool:
    requested_host = _hostname(requested_url)
    final_host = _hostname(final_url)
    if not requested_host or not final_host:
        return False
    if is_known_third_party_url(requested_url) or is_known_third_party_url(final_url):
        return False
    requested_anchor = _host_anchor(requested_host)
    final_anchor = _host_anchor(final_host)
    return (
        requested_anchor == final_anchor
        or requested_anchor.endswith(f".{final_anchor}")
        or final_anchor.endswith(f".{requested_anchor}")
    )


def is_known_third_party_url(url: str) -> bool:
    host = _hostname(url)
    return bool(
        host
        and any(
            host == domain or host.endswith(f".{domain}")
            for domain in _BLOCKED_THIRD_PARTY_DOMAINS
        )
    )


def _validated_public_url(url: str, host_resolver: HostResolver) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebsiteFetchError("The candidate website URL is not public HTTP(S).")
    if parsed.username or parsed.password:
        raise WebsiteFetchError("The candidate website URL contains credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebsiteFetchError("The candidate website URL has an invalid port.") from exc
    if port not in {None, 80, 443}:
        raise WebsiteFetchError("The candidate website URL uses a nonstandard port.")

    host = parsed.hostname.rstrip(".").casefold()
    if (
        host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
    ):
        raise WebsiteFetchError("The candidate website host is not public.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = host_resolver(host)
        except (OSError, ValueError) as exc:
            raise WebsiteFetchError(
                "The candidate website host could not be resolved."
            ) from exc
        if not addresses:
            raise WebsiteFetchError("The candidate website host could not be resolved.")
        try:
            resolved = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise WebsiteFetchError(
                "The candidate website host resolved to an invalid address."
            ) from exc
        if any(not address.is_global for address in resolved):
            raise WebsiteFetchError(
                "The candidate website host resolved to a non-public address."
            )
    else:
        if not literal.is_global:
            raise WebsiteFetchError("The candidate website host is not public.")
    return parsed._replace(netloc=parsed.netloc.rstrip("."), fragment="").geturl()


def _resolve_host(host: str) -> list[str]:
    return list(
        dict.fromkeys(
            result[4][0]
            for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        )
    )


def _hostname(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.rstrip(".").casefold() if host else None


def _host_anchor(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _extract_phones(
    visible_text: str, tel_values: list[str]
) -> tuple[ObservedWebsitePhone, ...]:
    raw_values = [*tel_values, *(_PHONE_PATTERN.findall(visible_text))]
    observed: list[ObservedWebsitePhone] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_values:
        cleaned = unquote(raw).strip()
        try:
            normalized = normalize_us_phone(cleaned)
        except InvalidPhoneNumber:
            continue
        key = (cleaned, normalized)
        if key not in seen:
            seen.add(key)
            observed.append(ObservedWebsitePhone(raw=cleaned, normalized=normalized))
    return tuple(observed)


class _VisiblePageParser(HTMLParser):
    _HIDDEN_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.tel_values: list[str] = []
        self._hidden_depth = 0
        self._jsonld_depth = 0
        self._jsonld_parts: list[str] = []
        self.structured_identities: list[StructuredBusinessIdentity] = []
        self.internal_identity_links: list[str] = []
        self._active_anchor: tuple[str, list[str]] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        attrs_dict = dict(attrs)
        if normalized_tag == "script" and "ld+json" in (attrs_dict.get("type") or "").casefold():
            self._jsonld_depth += 1
        if normalized_tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        if normalized_tag == "a":
            href = dict(attrs).get("href") or ""
            if href.casefold().startswith("tel:"):
                self.tel_values.append(href[4:])
            elif href and self._active_anchor is None:
                self._active_anchor = (href, [])

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._active_anchor is not None:
            href, text_parts = self._active_anchor
            identity_terms = re.compile(
                r"(?:contact|location|service[-_ ]?area|about|company)", re.I
            )
            if identity_terms.search(href) or identity_terms.search(" ".join(text_parts)):
                self.internal_identity_links.append(href)
            self._active_anchor = None
        if tag.casefold() == "script" and self._jsonld_depth:
            self._jsonld_depth -= 1
            if not self._jsonld_depth:
                self.structured_identities.extend(_parse_jsonld_identities("".join(self._jsonld_parts)))
                self._jsonld_parts = []
        if tag.casefold() in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._jsonld_depth:
            self._jsonld_parts.append(data)
            return
        if self._active_anchor is not None and not self._hidden_depth and data.strip():
            self._active_anchor[1].append(data.strip())
        if not self._hidden_depth and data.strip():
            self.text_parts.append(data.strip())


def _parse_jsonld_identities(raw: str) -> list[StructuredBusinessIdentity]:
    import json

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    entities: list[dict[str, object]] = []
    home_service_types = {
        "organization",
        "localbusiness",
        "plumber",
        "electrician",
        "hvacbusiness",
        "homeandconstructionbusiness",
        "generalcontractor",
        "roofingcontractor",
        "professionalservice",
    }
    nested_entity_keys = {
        "@graph", "mainEntity", "mainEntityOfPage", "provider",
        "parentOrganization", "department", "subOrganization", "branchOf",
        "location", "item", "itemListElement",
    }

    def visit(value: object, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, dict):
            raw_types = value.get("@type")
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            business_type = next((
                item.rsplit("/", 1)[-1]
                for item in types
                if isinstance(item, str)
                and item.rsplit("/", 1)[-1].casefold() in home_service_types
            ), None)
            if business_type:
                entities.append(value)
            for key in nested_entity_keys:
                if key in value:
                    visit(value[key], depth + 1)

    visit(payload)
    result: list[StructuredBusinessIdentity] = []
    for entity in entities:
        address_value = entity.get("address")
        if isinstance(address_value, list):
            address_value = next((item for item in address_value if isinstance(item, (str, dict))), None)
        if isinstance(address_value, dict):
            address = ", ".join(
                str(address_value[key]).strip()
                for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                if isinstance(address_value.get(key), (str, int)) and str(address_value[key]).strip()
            ) or None
        else:
            address = address_value.strip() if isinstance(address_value, str) and address_value.strip() else None
        raw_types = entity.get("@type")
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        business_type = next((
            item.rsplit("/", 1)[-1]
            for item in types
            if isinstance(item, str) and item.rsplit("/", 1)[-1].casefold() in home_service_types
        ), None)
        result.append(StructuredBusinessIdentity(
            name=_first_structured_text(entity.get("name")),
            legal_name=_first_structured_text(entity.get("legalName")),
            telephone=_first_structured_text(entity.get("telephone")),
            address=address,
            source_type=business_type,
        ))
    return result


def _structured_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_structured_text(value: object) -> str | None:
    if isinstance(value, list):
        return next((text for item in value if (text := _structured_text(item))), None)
    return _structured_text(value)
