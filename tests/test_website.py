from __future__ import annotations

import httpx
import pytest

from app.website import FirstPartyWebsiteClient, WebsiteFetchError


def public_host(_: str) -> list[str]:
    return ["93.184.216.34"]


def test_http_to_https_same_site_redirect_is_allowed() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(
                301,
                headers={"location": "https://example.com/"},
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><body>Example Plumbing "
                '<a href="tel:+15125551234">Call</a> '
                "1 Main Street Austin TX 78701</body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    client = FirstPartyWebsiteClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        host_resolver=public_host,
    )

    page = client.fetch("http://www.example.com/")

    assert requests == ["http://www.example.com/", "https://example.com/"]
    assert page.final_url == "https://example.com/"
    assert [phone.normalized for phone in page.phones] == ["+15125551234"]


def test_redirect_outside_first_party_domain_is_rejected() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://directory.example.net/listing"},
            request=request,
        )

    client = FirstPartyWebsiteClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        host_resolver=public_host,
    )

    with pytest.raises(WebsiteFetchError, match="outside"):
        client.fetch("https://example.com/")

    assert requests == ["https://example.com/"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "http://localhost/",
        "http://127.0.0.1/",
        "https://facebook.com/example",
    ],
)
def test_nonpublic_or_third_party_urls_are_rejected(url: str) -> None:
    client = FirstPartyWebsiteClient(host_resolver=public_host)

    with pytest.raises(WebsiteFetchError):
        client.fetch(url)


def test_response_size_is_bounded() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"x" * 101,
            headers={"content-type": "text/html"},
            request=request,
        )
    )
    client = FirstPartyWebsiteClient(
        http_client=httpx.Client(transport=transport),
        host_resolver=public_host,
        max_response_bytes=100,
    )

    with pytest.raises(WebsiteFetchError, match="response-size"):
        client.fetch("https://example.com/")
