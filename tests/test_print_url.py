"""`print_url` end to end: the guard refuses the LAN, a good page reaches the queue."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from printbridge.server import build_server


@pytest.fixture
def server(runtime):
    return build_server(runtime.settings, runtime)


async def call(server, tool, **kwargs):
    async with Client(server) as client:
        return (await client.call_tool(tool, kwargs)).data


def _mock_httpx(monkeypatch, handler):
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("printbridge.fetch.httpx.Client", factory)


async def test_a_tailnet_address_is_refused(server, runtime):
    with pytest.raises(ToolError, match="cgnat/tailnet"):
        await call(server, "print_url", url="http://100.92.80.36/printer")
    assert runtime.spooler.submitted == []


async def test_a_lan_address_is_refused(server, runtime):
    with pytest.raises(ToolError, match="private address"):
        await call(server, "print_url", url="http://192.168.1.50/status")
    assert runtime.spooler.submitted == []


async def test_the_cloud_metadata_endpoint_is_refused(server):
    with pytest.raises(ToolError, match="link-local"):
        await call(server, "print_url", url="http://169.254.169.254/latest/meta-data/")


async def test_a_public_html_page_prints(server, runtime, monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>Recipe</title></head><body><h1>Pancakes</h1></body></html>",
        ),
    )
    data = await call(server, "print_url", url="https://example.com/recipe")
    assert data["pages"] == 1
    assert data["title"] == "Recipe", "the page title beats the URL, which we never log"
    assert len(runtime.spooler.submitted) == 1


async def test_a_public_pdf_passes_through_with_its_own_page_count(server, runtime, monkeypatch, tmp_path):
    from printbridge.render import render_to_pdf

    pdf = render_to_pdf("# One\n\n<div style='page-break-after:always'></div>\n\n# Two",
                        tmp_path / "two.pdf", title="two")
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=pdf.path.read_bytes()
        ),
    )
    data = await call(server, "print_url", url="https://example.com/doc.pdf")
    assert data["pages"] == pdf.pages


async def test_an_unprintable_content_type_is_refused(server, monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\x00"),
    )
    with pytest.raises(ToolError, match="prints PDF, HTML and plain text"):
        await call(server, "print_url", url="https://example.com/clip.mp4")


async def test_the_url_is_never_written_to_the_job_log(server, runtime, monkeypatch):
    monkeypatch.setattr("printbridge.fetch.resolve_host", lambda h: ["93.184.216.34"])
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<title>Page</title>hi"
        ),
    )
    await call(server, "print_url", url="https://example.com/private/token-in-url?secret=1")
    row = runtime.store.recent_jobs(limit=1)[0]
    assert "example.com" not in str(row) and "secret" not in str(row)
    assert row["source_kind"] == "url"


async def test_allowing_private_urls_is_an_explicit_choice(runtime, monkeypatch):
    runtime.settings.allow_private_urls = True
    server = build_server(runtime.settings, runtime)
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<title>Router</title>hi"
        ),
    )
    data = await call(server, "print_url", url="http://192.168.1.1/status")
    assert data["pages"] == 1
