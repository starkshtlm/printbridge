"""The SSRF guard. `print_url` takes a URL written by a language model — treat it as hostile."""

from __future__ import annotations

import httpx
import pytest

from printbridge.fetch import FetchError, check_url, classify_address, fetch_url

PRIVATE_CASES = [
    ("http://127.0.0.1/x", "loopback"),
    ("http://127.1.2.3/x", "loopback"),
    ("http://localhost/x", "loopback"),
    ("http://10.0.0.5/x", "private"),
    ("http://172.16.4.4/x", "private"),
    ("http://192.168.1.50/x", "private"),
    ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("http://100.92.80.36/x", "cgnat/tailnet"),
    ("http://[::1]/x", "loopback"),
    ("http://[fd7a:115c:a1e0::1]/x", "ipv6-ula"),
    ("http://[fe80::1]/x", "link-local"),
    ("http://0.0.0.0/x", "unspecified"),
]


@pytest.mark.parametrize("url,expected", PRIVATE_CASES)
def test_every_private_address_class_is_refused(url, expected, monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["127.0.0.1"])
    with pytest.raises(FetchError) as exc:
        check_url(url)
    assert expected in str(exc.value) or "private" in str(exc.value)


@pytest.mark.parametrize("url,_expected", PRIVATE_CASES)
def test_the_same_addresses_are_allowed_when_the_operator_opts_in(url, _expected):
    assert check_url(url, allow_private=True)


def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    assert check_url("https://example.com/page") == "example.com"


def test_a_hostname_that_resolves_into_the_lan_is_refused(monkeypatch):
    """The classic bypass: a public-looking name with a private A record."""
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["192.168.1.50"])
    with pytest.raises(FetchError, match="resolves to 192.168.1.50"):
        check_url("https://printer.example.com/")


def test_every_answer_must_pass_not_just_the_first(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(FetchError, match="10.1.2.3"):
        check_url("https://dual.example.com/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/y", "data:text/html,x"])
def test_only_http_and_https_are_fetched(url):
    with pytest.raises(FetchError, match="Only http and https"):
        check_url(url)


def test_a_url_without_a_host_is_refused():
    with pytest.raises(FetchError, match="no hostname"):
        check_url("http:///nohost")


@pytest.mark.parametrize(
    "address,kind",
    [
        ("8.8.8.8", "public"),
        ("127.0.0.1", "loopback"),
        ("100.64.0.1", "cgnat/tailnet"),
        ("169.254.1.1", "link-local"),
        ("::ffff:10.0.0.1", "private"),
        ("2001:4860:4860::8888", "public"),
    ],
)
def test_address_classification(address, kind):
    assert classify_address(address) == kind


# --- the fetch itself, against a mock transport ---


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_a_redirect_into_the_lan_is_caught_at_the_second_hop(monkeypatch):
    monkeypatch.setattr(
        "printbridge.fetch.resolve_host",
        lambda h: ["192.168.1.50"] if h == "inside.example.com" else ["93.184.216.34"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "start.example.com":
            return httpx.Response(302, headers={"location": "http://inside.example.com/secret"})
        return httpx.Response(200, content=b"should never be reached")

    with pytest.raises(FetchError, match="resolves to 192.168.1.50"):
        fetch_url("https://start.example.com/", client=_client(handler))


def test_a_redirect_loop_stops(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    with pytest.raises(FetchError, match="[Tt]oo many redirects"):
        fetch_url("https://example.com/", client=_client(handler))


def test_a_declared_size_over_the_limit_is_refused_before_download(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(50 * 1024 * 1024)}, content=b"x")

    with pytest.raises(FetchError, match="the limit is 25 MB"):
        fetch_url("https://example.com/big.pdf", max_mb=25, client=_client(handler))


def test_a_lying_content_length_is_still_stopped_mid_stream(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        # A server that understates its size: the guard must count bytes, not believe headers.
        return httpx.Response(
            200,
            headers={"content-length": "10"},
            content=iter([b"x" * 512 * 1024] * 6),
        )

    with pytest.raises(FetchError, match="Download stopped"):
        fetch_url("https://example.com/big", max_mb=1, client=_client(handler))


def test_an_http_error_is_reported_not_printed(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    handler = lambda request: httpx.Response(404, content=b"nope")  # noqa: E731
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_url("https://example.com/missing", client=_client(handler))


def test_a_good_page_comes_back_with_its_content_type(monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html><title>Hello</title><body>hi</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    result = fetch_url("https://example.com/", client=_client(handler))
    assert result.content_type == "text/html"
    assert b"Hello" in result.content
