"""Bounded first-party website retrieval for Day 1 corroboration."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app.phone import InvalidPhoneNumber, normalize_us_phone


class WebsiteFetchError(RuntimeError):
    """A candidate website could not be fetched within the safety bounds."""


@dataclass(frozen=True)
class ObservedWebsitePhone:
    raw: str
    normalized: str


@dataclass(frozen=True)
class WebsitePage:
    requested_url: str
    final_url: str
    visible_text: str
    phones: tuple[ObservedWebsitePhone, ...]


class WebsiteClient:
    def fetch(self, url: str) -> WebsitePage: ...


HostResolver = Callable[[str], Sequence[str]]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_BLOCKED_THIRD_PARTY_DOMAINS = {
    "angi.com",
    "facebook.com",
    "google.com",
    "homeadvisor.com",
    "houzz.com",
    "instagram.com",
    "linkedin.com",
    "nextdoor.com",
    "thumbtack.com",
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
        return WebsitePage(
            requested_url=requested_url,
            final_url=current_url,
            visible_text=visible_text,
            phones=phones,
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

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        if normalized_tag == "a":
            href = dict(attrs).get("href") or ""
            if href.casefold().startswith("tel:"):
                self.tel_values.append(href[4:])

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.text_parts.append(data.strip())
