"""`print_url` and the SSRF guard that makes it safe to expose.

The tool arguments are written by a language model that may have been prompt-injected, so a URL is
untrusted input in the strongest sense. The guard therefore:

* accepts only http/https;
* resolves the hostname **before** connecting and refuses loopback, RFC 1918, link-local, CGNAT
  (100.64/10 — the tailnet range), IPv6 ULA, multicast and reserved space unless
  `ALLOW_PRIVATE_URLS=true`;
* re-checks every redirect hop (max 3), because hop 1 being public says nothing about hop 2;
* stops the download at `MAX_FETCH_MB` instead of trusting Content-Length;
* prints PDFs as they are and renders HTML through the same sanitising path as `print_text`.

Residual risk, stated rather than hidden: a hostname whose DNS answer changes between our check and
httpx's own connection (DNS rebinding) is not fully closed by this design. The blast radius is a
page of paper — see docs/security.md.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError

from printbridge.render import RenderError, pdf_page_count, render_to_pdf
from printbridge.server_helpers import spool_target

MAX_REDIRECTS = 3
FETCH_TIMEOUT = 20.0
ALLOWED_SCHEMES = {"http", "https"}


class FetchError(ValueError):
    """The URL cannot or must not be fetched. The message is what the assistant tells the user."""


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    content_type: str
    final_url: str


def classify_address(host: str) -> str:
    """'public', or the name of the private class this address belongs to."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "not-an-ip"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat/tailnet"
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return "ipv6-ula"
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("fc00::/7"):
        return "ipv6-ula"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return classify_address(str(ip.ipv4_mapped))
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    return "public"


def is_public_address(host: str) -> bool:
    return classify_address(host) == "public"


def resolve_host(hostname: str) -> list[str]:
    """Every address the hostname resolves to. All of them must pass, not just the first."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve {hostname!r} ({exc.strerror or exc}).") from exc
    return sorted({info[4][0] for info in infos})


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Validate one URL (one hop). Returns the hostname; raises FetchError with the reason."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError(
            f"Only http and https URLs can be printed — got {parsed.scheme or 'no scheme'!r}."
        )
    if not parsed.hostname:
        raise FetchError(f"{url!r} has no hostname.")
    host = parsed.hostname
    if allow_private:
        return host
    literal = classify_address(host)
    if literal not in {"public", "not-an-ip"}:
        raise FetchError(
            f"Refused: {host} is a {literal} address. printbridge does not fetch from private "
            "networks. Set ALLOW_PRIVATE_URLS=true if you really want that."
        )
    for address in resolve_host(host):
        kind = classify_address(address)
        if kind != "public":
            raise FetchError(
                f"Refused: {host} resolves to {address}, which is a {kind} address. printbridge "
                "does not fetch from private networks (loopback, LAN, tailnet). Set "
                "ALLOW_PRIVATE_URLS=true if you really want that."
            )
    return host


def fetch_url(
    url: str,
    *,
    max_mb: int = 25,
    allow_private: bool = False,
    timeout: float = FETCH_TIMEOUT,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch a document, checking every redirect hop and stopping at the size limit."""
    limit = max_mb * 1024 * 1024
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=False, timeout=timeout)
    current = url
    try:
        for hop in range(MAX_REDIRECTS + 1):
            check_url(current, allow_private=allow_private)
            with http.stream("GET", current, headers={"User-Agent": "printbridge/1.0"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"{current} redirected without a destination.")
                    current = str(response.url.join(location))
                    if hop == MAX_REDIRECTS:
                        raise FetchError(
                            f"Too many redirects (more than {MAX_REDIRECTS}) starting at {url}."
                        )
                    continue
                if response.status_code >= 400:
                    raise FetchError(
                        f"{current} returned HTTP {response.status_code}. Nothing was printed."
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise FetchError(
                        f"That page is {int(declared) / 1e6:.1f} MB; the limit is {max_mb} MB."
                    )
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise FetchError(
                            f"Download stopped at {max_mb} MB — the document is larger than the "
                            "limit."
                        )
                    chunks.append(chunk)
                return FetchResult(
                    content=b"".join(chunks),
                    content_type=response.headers.get("content-type", "").split(";")[0].strip(),
                    final_url=str(response.url),
                )
        raise FetchError(f"Too many redirects starting at {url}.")
    except httpx.HTTPError as exc:
        raise FetchError(f"Could not fetch {url}: {exc}") from exc
    finally:
        if owns_client:
            http.close()


def register_print_url(mcp, rt, caller_of, submit, guard_pages, guard_rate, resolve_printer) -> None:  # noqa: ANN001
    """Register `print_url` on the server. Kept here so the guard and the tool stay together."""

    @mcp.tool(
        description=(
            "Print a web page or an online PDF on the user's printer. Give the full URL. The page "
            "is downloaded on the server and rendered for paper — the user's browser is not "
            "involved, so pages behind a login will not work. Same print options as print_text, "
            "including dry_run for the page count."
        )
    )
    async def print_url(
        url: str,
        title: str = "",
        copies: int = 1,
        duplex: bool | None = None,
        color: bool | None = None,
        paper: str | None = None,
        printer: str | None = None,
        dry_run: bool = False,
        confirmed: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        caller = caller_of(ctx)
        settings = rt.settings
        queue = resolve_printer(rt, caller.principal, printer)
        duplex = settings.default_duplex if duplex is None else duplex
        color = settings.default_color if color is None else color
        paper = paper or settings.default_paper

        try:
            fetched = fetch_url(
                url,
                max_mb=settings.max_fetch_mb,
                allow_private=settings.allow_private_urls,
            )
        except FetchError as exc:
            raise ToolError(str(exc)) from exc

        page_title = (title or _title_for(fetched)).strip()[:120]
        target = spool_target(settings)
        try:
            if fetched.content[:5] == b"%PDF-" or fetched.content_type == "application/pdf":
                pages = pdf_page_count(fetched.content, target)
            elif fetched.content_type in {"text/html", "application/xhtml+xml", ""}:
                pages = render_to_pdf(
                    fetched.content.decode("utf-8", errors="replace"),
                    target,
                    fmt="html",
                    title=page_title,
                    paper=paper,
                    locale=settings.locale,
                ).pages
            elif fetched.content_type.startswith("text/"):
                pages = render_to_pdf(
                    fetched.content.decode("utf-8", errors="replace"),
                    target,
                    fmt="plain",
                    title=page_title,
                    paper=paper,
                    locale=settings.locale,
                ).pages
            else:
                raise ToolError(
                    f"{url} is {fetched.content_type or 'an unknown type'}; printbridge prints PDF, "
                    "HTML and plain text from a URL."
                )
        except RenderError as exc:
            target.unlink(missing_ok=True)
            raise ToolError(str(exc)) from exc

        guard_pages(rt, caller.principal, pages, confirmed)
        if dry_run:
            target.unlink(missing_ok=True)
            return {
                "dry_run": True,
                "pages": pages,
                "printer": queue,
                "title": page_title,
                "message": f"{pages} page(s) on {queue}. Nothing has been printed.",
            }
        guard_rate(rt, caller)
        return submit(
            rt,
            caller,
            pdf_path=target,
            printer=queue,
            pages=pages,
            title=page_title,
            tool="print_url",
            source_kind="url",
            copies=copies if copies and copies > 0 else 1,
            duplex=duplex,
            color=color,
            paper=paper,
            started=started,
        )


def _title_for(fetched: FetchResult) -> str:
    """A title from <title> when there is one — never the URL, which we do not log."""
    if fetched.content_type in {"text/html", "application/xhtml+xml", ""}:
        text = fetched.content[:8000].decode("utf-8", errors="replace")
        lower = text.lower()
        start = lower.find("<title>")
        if start != -1:
            end = lower.find("</title>", start)
            if end != -1:
                title = text[start + 7 : end].strip()
                if title:
                    return " ".join(title.split())[:120]
    host = urlparse(fetched.final_url).hostname or "web page"
    return host
