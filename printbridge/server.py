"""The MCP server: tools, guards, and the one print flow they all share.

`build_server(settings) -> FastMCP` has no import-time side effects and its tool names tolerate a
prefix, so a private "personal MCP hub" can `hub.mount(build_server(settings), prefix="print")`
without changing anything here (spec §14).

The order inside a print is always the same, and the order *is* the security model:

    resolve principal → resolve printer → render → count pages → apply limits → spool → log

Nothing is spooled before the page count is known, so `MAX_PAGES` and `CONFIRM_ABOVE_PAGES` are
hard limits rather than advice.
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from printbridge import __version__
from printbridge.auth import (
    LOCAL_PRINCIPAL,
    AuthError,
    Principal,
    client_ip,
    resolve_principal,
)
from printbridge.config import ConfigError, Settings
from printbridge.render import RenderError, pdf_page_count, render_to_pdf
from printbridge.runtime import Runtime, build_runtime
from printbridge.server_helpers import spool_target as _spool_target
from printbridge.spool import PrintOptions, SpoolError

SERVER_INSTRUCTIONS = """\
printbridge prints on the user's own physical printer at home or in their office.

Use `print_text` when the user asks you to print something you have written or can write — a note,
a list, a recipe, a summary. Write it as markdown; printbridge applies a proper print stylesheet
with a header and page numbers. Use `print_url` for a web page and `print_file` when you already
have the bytes.

Defaults are double-sided A4 in black and white. The user can say "two copies", "single-sided" or
"in colour" and you pass that through. Long documents need `confirmed=true`; use `dry_run=true`
first if you want to tell the user the page count before spending paper.
"""

MAX_UPLOAD_MB = 25
_TEXT_SUFFIXES = {".txt", ".text", ".log"}
_MD_SUFFIXES = {".md", ".markdown"}
_HTML_SUFFIXES = {".html", ".htm"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass(frozen=True)
class CallerInfo:
    """Who called, from which assistant, over which session — assembled per tool call."""

    principal: Principal
    client: str = "unknown"
    client_version: str = ""
    session_id: str = ""
    remote_ip: str = "-"
    user_agent: str = ""

    @property
    def where(self) -> str:
        return f"{self.principal.name} via {self.client}"


# --------------------------------------------------------------------------------------
# session bookkeeping
# --------------------------------------------------------------------------------------


class SessionMiddleware(Middleware):
    """Records every MCP message as session activity, so `status` can answer 'who is connected'.

    The AI service is read from the `initialize` handshake (`clientInfo`), which is why nobody has
    to configure which assistant they are using.
    """

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def on_message(self, context: MiddlewareContext, call_next):  # type: ignore[no-untyped-def]
        try:
            caller = describe_caller(context.fastmcp_context)
            signal = self.runtime.store.touch_session(
                session_id=caller.session_id,
                principal=caller.principal.name,
                client=caller.client,
                client_version=caller.client_version,
                remote_ip=caller.remote_ip,
                user_agent=caller.user_agent,
            )
            notifier = self.runtime.notifier
            if notifier is not None and caller.client != "unknown":
                if signal.new_origin:
                    notifier.send(
                        "unknown_origin",
                        f"{caller.principal.name} used a new assistant ({caller.client}) from a "
                        f"network we have not seen before ({caller.remote_ip}). If that was not "
                        f"them: `printbridge keys revoke {caller.principal.name}`.",
                    )
                elif signal.new_client_for_principal:
                    notifier.on_state(
                        "new_client",
                        f"{caller.principal.name}:{caller.client}",
                        f"{caller.principal.name} connected a new assistant: {caller.client}.",
                    )
        except Exception:  # noqa: BLE001 - bookkeeping must never break a call
            pass
        return await call_next(context)


def describe_caller(ctx: Context | None) -> CallerInfo:
    """Principal (from auth) + assistant (from the MCP handshake) + session id, best effort."""
    principal = LOCAL_PRINCIPAL
    remote_ip, user_agent = "-", ""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        principal = resolve_principal(request)
        remote_ip = client_ip(request)
        user_agent = request.headers.get("user-agent", "")[:200]
    except AuthError:
        raise
    except Exception:  # noqa: BLE001 - stdio and in-memory transports have no HTTP request
        pass

    # The MCP session id is what makes "who is connected" a list of people rather than a list of
    # requests. Streamable HTTP carries it in `mcp-session-id`; before a session is negotiated (and
    # for stdio) we derive a stable id from who is calling, so one client is one row.
    http_session = ""
    try:
        from fastmcp.server.dependencies import get_http_request

        http_session = get_http_request().headers.get("mcp-session-id", "")
    except Exception:  # noqa: BLE001
        pass

    client, version, session_id = "unknown", "", ""
    if ctx is not None:
        try:
            params = ctx.session.client_params
            # Wire name is `clientInfo`; the Python SDK exposes it as `client_info`. Read both so
            # an SDK rename cannot silently turn every assistant into "unknown".
            info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
            if info is not None:
                client = (getattr(info, "name", "") or "unknown").strip() or "unknown"
                version = str(getattr(info, "version", "") or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            session_id = ctx.session_id
        except Exception:  # noqa: BLE001
            pass
    if http_session:
        session_id = http_session
    if not session_id or _looks_generated(session_id):
        session_id = _stable_session_id(principal.name, client, remote_ip, user_agent)
    return CallerInfo(
        principal=principal,
        client=client,
        client_version=version,
        session_id=session_id,
        remote_ip=remote_ip,
        user_agent=user_agent,
    )


# --------------------------------------------------------------------------------------
# the print flow
# --------------------------------------------------------------------------------------




def cleanup_spool(settings: Settings) -> int:
    """Delete rendered documents once their job is out of the queue. Returns how many went."""
    keep_hours = settings.keep_spool_hours
    removed = 0
    if not settings.spool_path.exists():
        return 0
    cutoff = time.time() - keep_hours * 3600
    for path in settings.spool_path.glob("pb-*"):
        try:
            if keep_hours == 0 or path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _guard_pages(runtime: Runtime, principal: Principal, pages: int, confirmed: bool) -> None:
    settings = runtime.settings
    ceiling = principal.page_ceiling(settings.max_pages)
    if pages > ceiling:
        which = "your key's limit" if principal.max_pages and ceiling == principal.max_pages else "MAX_PAGES"
        raise ToolError(
            f"Refused: {pages} pages is above {which} ({ceiling}). Split the document, or ask the "
            f"operator to raise the limit."
        )
    if pages > settings.confirm_above_pages and not confirmed:
        raise ToolError(
            f"This is {pages} pages. Tell the user the page count and call the tool again with "
            f"confirmed=true if they want it printed."
        )


def _guard_rate(runtime: Runtime, caller: CallerInfo) -> None:
    principal = caller.principal
    limit = principal.rate_ceiling(runtime.settings.rate_limit_jobs_per_hour)
    bucket = principal.key_id or principal.name
    if not runtime.rate.allow(bucket, limit):
        wait = runtime.rate.retry_after_seconds(bucket)
        if runtime.notifier is not None:
            runtime.notifier.on_state(
                "rate_limit",
                f"{principal.name}:hit",
                f"{principal.name} hit the rate limit ({limit} jobs/hour) from {caller.client}. "
                f"Raise it with `printbridge keys rotate {principal.name} --rate N/h` if that is "
                f"normal for them.",
            )
        raise ToolError(
            f"Rate limit reached: {limit} print jobs per hour for this key. Try again in about "
            f"{max(1, wait // 60)} minutes."
        )


def _resolve_printer(runtime: Runtime, principal: Principal, requested: str | None) -> str:
    try:
        spec = runtime.settings.printer_or_default(requested)
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    try:
        principal.require_printer(spec.name)
    except AuthError as exc:
        raise ToolError(str(exc)) from exc
    return spec.name


def _submit(
    runtime: Runtime,
    caller: CallerInfo,
    *,
    pdf_path: Path,
    printer: str,
    pages: int,
    title: str,
    tool: str,
    source_kind: str,
    copies: int,
    duplex: bool,
    color: bool,
    paper: str,
    started: float,
) -> dict[str, Any]:
    """Spool a rendered PDF, write the job log row, clean up. Shared by every print tool."""
    options = PrintOptions(copies=copies, duplex=duplex, color=color, paper=paper, title=title)
    row = runtime.store.record_job(
        principal=caller.principal.name,
        key_label=caller.principal.key_label,
        client=caller.client,
        client_version=caller.client_version,
        tool=tool,
        title=title,
        pages=pages,
        copies=copies,
        printer=printer,
        options={"duplex": duplex, "color": color, "paper": paper},
        status="queued",
        source_kind=source_kind,
    )
    try:
        job_id = runtime.spooler.submit(pdf_path, printer, options)
    except SpoolError as exc:
        runtime.store.update_job(
            row,
            status="failed",
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if runtime.notifier is not None:
            runtime.notifier.on_state(
                "printer_offline",
                f"{printer}:down",
                f"Printing to {printer} failed: {exc} "
                f"See docs/troubleshooting.md — start with `printbridge doctor`.",
            )
        raise ToolError(str(exc)) from exc

    runtime.rate.record(caller.principal.key_id or caller.principal.name)
    runtime.store.update_job(
        row, job_id=job_id, duration_ms=int((time.monotonic() - started) * 1000)
    )
    runtime.store.count_session_job(caller.session_id)
    if runtime.notifier is not None:
        runtime.notifier.on_state("printer_offline", f"{printer}:up", f"{printer} is printing again.")
    if runtime.settings.keep_spool_hours == 0:
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            pass
    mock = runtime.settings.printer_or_default(printer).is_mock
    return {
        "job_id": job_id,
        "pages": pages,
        "copies": copies,
        "printer": printer,
        "title": title,
        "status": "queued",
        "duplex": duplex,
        "color": color,
        "paper": paper,
        "mock": mock,
        "message": (
            f"Queued {pages} page(s){f' ×{copies}' if copies > 1 else ''} on {printer} as {job_id}."
            + (" This queue is a mock (cups-pdf) — no paper moves." if mock else "")
        ),
    }


def _decode_upload(content_base64: str, filename: str) -> bytes:
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError(f"`content_base64` is not valid base64 ({exc}).") from exc
    if not data:
        raise ToolError(f"{filename or 'The file'} is empty.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ToolError(
            f"{filename or 'The file'} is {len(data) / 1e6:.1f} MB; the limit is {MAX_UPLOAD_MB} MB."
        )
    return data


# --------------------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------------------


def build_server(settings: Settings, runtime: Runtime | None = None) -> FastMCP:
    """Build the MCP server. Pure: no files opened, no ports bound, no globals touched."""
    rt = runtime or build_runtime(settings)
    mcp: FastMCP = FastMCP(
        name="printbridge",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )
    mcp.add_middleware(SessionMiddleware(rt))

    def caller_of(ctx: Context | None) -> CallerInfo:
        try:
            return describe_caller(ctx)
        except AuthError as exc:
            raise ToolError(str(exc)) from exc

    # ---------------------------------------------------------------- print_text

    @mcp.tool(
        description=(
            "Print text on the user's own printer. Use this when the user asks you to print "
            "something you have written or can write — a note, a checklist, a recipe, a summary. "
            "Prefer markdown: headings, tables and lists are rendered with a proper print "
            "stylesheet, a header line and page numbers. Default is double-sided A4 in black and "
            "white; pass copies/duplex/color/paper when the user asks for something else. Use "
            "dry_run=true to get the page count without printing."
        )
    )
    async def print_text(
        content: str,
        title: str = "",
        format: str = "markdown",
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
        queue = _resolve_printer(rt, caller.principal, printer)
        duplex = settings.default_duplex if duplex is None else duplex
        color = settings.default_color if color is None else color
        paper = paper or settings.default_paper
        copies = _clamp_copies(copies)
        title = (title or _title_from(content)).strip()[:120]

        target = _spool_target(settings)
        try:
            result = render_to_pdf(
                content, target, fmt=format, title=title, paper=paper, locale=settings.locale
            )
        except RenderError as exc:
            raise ToolError(str(exc)) from exc

        _guard_pages(rt, caller.principal, result.pages, confirmed)
        if dry_run:
            target.unlink(missing_ok=True)
            rt.store.record_job(
                principal=caller.principal.name,
                key_label=caller.principal.key_label,
                client=caller.client,
                client_version=caller.client_version,
                tool="print_text",
                title=title,
                pages=result.pages,
                copies=copies,
                printer=queue,
                status="dry-run",
                source_kind="text",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {
                "dry_run": True,
                "pages": result.pages,
                "printer": queue,
                "title": title,
                "message": f"{result.pages} page(s) on {queue}. Nothing has been printed.",
            }
        _guard_rate(rt, caller)
        return _submit(
            rt,
            caller,
            pdf_path=target,
            printer=queue,
            pages=result.pages,
            title=title,
            tool="print_text",
            source_kind="text",
            copies=copies,
            duplex=duplex,
            color=color,
            paper=paper,
            started=started,
        )

    # ---------------------------------------------------------------- print_file

    @mcp.tool(
        description=(
            "Print a file the caller already has as bytes (base64). PDFs are printed as they are; "
            "TXT, MD and HTML are rendered with the print stylesheet. Use this from agents and "
            "coding clients that can read files; chat assistants normally want print_text. Same "
            "options as print_text, including dry_run."
        )
    )
    async def print_file(
        filename: str,
        content_base64: str,
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
        queue = _resolve_printer(rt, caller.principal, printer)
        duplex = settings.default_duplex if duplex is None else duplex
        color = settings.default_color if color is None else color
        paper = paper or settings.default_paper
        copies = _clamp_copies(copies)
        title = (title or Path(filename or "document").name).strip()[:120]

        data = _decode_upload(content_base64, filename)
        suffix = Path(filename or "").suffix.lower()
        target = _spool_target(settings)
        try:
            if data[:5] == b"%PDF-" or suffix == ".pdf":
                pages = pdf_page_count(data, target)
            elif suffix in _IMAGE_SUFFIXES:
                mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"
                encoded = base64.b64encode(data).decode("ascii")
                body = f'<img src="data:{mime};base64,{encoded}" alt="{title}">'
                pages = render_to_pdf(
                    body, target, fmt="html", title=title, paper=paper, locale=settings.locale
                ).pages
            else:
                fmt = (
                    "markdown"
                    if suffix in _MD_SUFFIXES
                    else "html"
                    if suffix in _HTML_SUFFIXES
                    else "plain"
                    if suffix in _TEXT_SUFFIXES
                    else "plain"
                )
                text = data.decode("utf-8", errors="replace")
                pages = render_to_pdf(
                    text, target, fmt=fmt, title=title, paper=paper, locale=settings.locale
                ).pages
        except RenderError as exc:
            target.unlink(missing_ok=True)
            raise ToolError(str(exc)) from exc

        _guard_pages(rt, caller.principal, pages, confirmed)
        if dry_run:
            target.unlink(missing_ok=True)
            return {
                "dry_run": True,
                "pages": pages,
                "printer": queue,
                "title": title,
                "message": f"{pages} page(s) on {queue}. Nothing has been printed.",
            }
        _guard_rate(rt, caller)
        return _submit(
            rt,
            caller,
            pdf_path=target,
            printer=queue,
            pages=pages,
            title=title,
            tool="print_file",
            source_kind="file",
            copies=copies,
            duplex=duplex,
            color=color,
            paper=paper,
            started=started,
        )

    # ---------------------------------------------------------------- queue tools

    @mcp.tool(
        description=(
            "List the printers this server can reach: name, state, whether it is the default, and "
            "what it can do. Use it when the user has more than one printer or asks 'which "
            "printers do I have?'."
        )
    )
    async def list_printers(ctx: Context | None = None) -> dict[str, Any]:
        caller = caller_of(ctx)
        configured = {s.name: s for s in rt.settings.printer_specs}
        try:
            live = {q.name: q for q in rt.spooler.queues()}
        except SpoolError:
            live = {}
        printers = []
        default = rt.settings.default_printer
        for name, spec in configured.items():
            if not caller.principal.may_print_to(name):
                continue
            q = live.get(name)
            printers.append(
                {
                    "name": name,
                    "state": q.state if q else "unknown",
                    "reason": q.reason if q else "queue not created yet — run printbridge doctor",
                    "default": name == default,
                    "duplex": rt.settings.default_duplex,
                    "color": rt.settings.default_color,
                    "paper": rt.settings.default_paper,
                    "mock": spec.is_mock,
                }
            )
        return {
            "printers": printers,
            "default": default,
            "message": (
                ", ".join(f"{p['name']} ({p['state']})" for p in printers) or "no printers configured"
            ),
        }

    @mcp.tool(
        description=(
            "Check what happened to a print job you submitted. Returns pending, printing, "
            "completed, failed or cancelled."
        )
    )
    async def get_job(job_id: str, ctx: Context | None = None) -> dict[str, Any]:
        caller_of(ctx)
        try:
            info = rt.spooler.job(job_id)
        except SpoolError as exc:
            raise ToolError(str(exc)) from exc
        if info.state in {"completed", "cancelled", "failed"}:
            rt.store.set_status_by_job_id(job_id, info.state, info.reason)
        return {
            "job_id": info.job_id,
            "status": info.state,
            "printer": info.printer,
            "submitted": info.submitted,
            "message": f"Job {info.job_id} is {info.state}."
            + (f" ({info.reason})" if info.reason else ""),
        }

    @mcp.tool(
        description="Cancel a print job that has not finished yet. Paper already printed stays printed."
    )
    async def cancel_job(job_id: str, ctx: Context | None = None) -> dict[str, Any]:
        caller_of(ctx)
        try:
            cancelled = rt.spooler.cancel(job_id)
        except SpoolError as exc:
            raise ToolError(str(exc)) from exc
        if cancelled:
            rt.store.set_status_by_job_id(job_id, "cancelled", "cancelled by the user")
        return {
            "job_id": job_id,
            "cancelled": cancelled,
            "message": (
                f"Job {job_id} cancelled."
                if cancelled
                else f"Job {job_id} was already finished or unknown — nothing to cancel."
            ),
        }

    @mcp.tool(
        description=(
            "Is the printer online, how many jobs are waiting, what went wrong last, and — if the "
            "printer reports it over IPP — how much toner or ink is left. Use this when a print "
            "did not appear."
        )
    )
    async def printer_status(printer: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        caller = caller_of(ctx)
        queue = _resolve_printer(rt, caller.principal, printer)
        spec = rt.settings.printer_or_default(queue)
        try:
            queues = {q.name: q for q in rt.spooler.queues()}
        except SpoolError as exc:
            return {
                "printer": queue,
                "online": False,
                "mock": spec.is_mock,
                "message": f"CUPS is not reachable: {exc}",
            }
        info = queues.get(queue)
        depth = rt.spooler.queue_depth(queue)
        attrs = rt.spooler.printer_attributes(spec.uri) if not spec.is_mock else {}
        errors = [e for e in rt.store.last_errors(3) if e["printer"] == queue]
        online = bool(info and info.state in {"idle", "processing"})
        supplies = {}
        if attrs.get("marker-names") and attrs.get("marker-levels"):
            names = [n.strip().strip('"') for n in attrs["marker-names"].split(",")]
            levels = [v.strip() for v in attrs["marker-levels"].split(",")]
            supplies = dict(zip(names, levels, strict=False))
        return {
            "printer": queue,
            "online": online,
            "state": info.state if info else "unknown",
            "reason": info.reason if info else "queue not created — run printbridge doctor",
            "queue_depth": depth,
            "mock": spec.is_mock,
            "model": attrs.get("printer-make-and-model", ""),
            "supplies": supplies,
            "last_error": errors[0]["error"] if errors else "",
            "message": (
                f"{queue} is {'online' if online else 'not reachable'}"
                f"{f', {depth} job(s) waiting' if depth else ''}."
                + (" Mock queue (cups-pdf) — no paper moves." if spec.is_mock else "")
            ),
        }

    @mcp.tool(
        description=(
            "What has been printed recently — title, pages, printer, status and time. Answers "
            "'what did I print today?' and gives you the job ids to re-check or cancel. A person "
            "sees only their own jobs; an admin key sees everyone's."
        )
    )
    async def recent_jobs(
        limit: int = 10,
        printer: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        caller = caller_of(ctx)
        limit = max(1, min(int(limit or 10), 100))
        rows = rt.store.recent_jobs(
            principal=None if caller.principal.is_admin else caller.principal.name,
            printer=printer,
            limit=limit,
        )
        jobs = [
            {
                "job_id": r["job_id"],
                "when": r["ts"],
                "title": r["title"],
                "pages": r["pages"],
                "copies": r["copies"],
                "printer": r["printer"],
                "status": r["status"],
                "client": r["client"],
                **({"principal": r["principal"]} if caller.principal.is_admin else {}),
            }
            for r in rows
        ]
        return {
            "jobs": jobs,
            "scope": "everyone" if caller.principal.is_admin else caller.principal.name,
            "message": f"{len(jobs)} job(s)." if jobs else "Nothing printed yet.",
        }

    # print_url lives in fetch.py's neighbourhood but is registered here (B3).
    from printbridge.fetch import register_print_url

    register_print_url(mcp, rt, caller_of, _submit, _guard_pages, _guard_rate, _resolve_printer)

    return mcp


def _looks_generated(session_id: str) -> bool:
    """A bare uuid4 means the transport had no session to give us — a fresh one every request."""
    try:
        return uuid.UUID(session_id).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def _stable_session_id(principal: str, client: str, remote_ip: str, user_agent: str) -> str:
    digest = sha256(f"{principal}|{client}|{remote_ip}|{user_agent}".encode()).hexdigest()
    return f"pb-{digest[:20]}"


def _clamp_copies(copies: int) -> int:
    try:
        value = int(copies)
    except (TypeError, ValueError):
        return 1
    if value < 1:
        return 1
    if value > 20:
        raise ToolError("Refused: more than 20 copies. Print fewer, or ask the operator.")
    return value


def _title_from(content: str) -> str:
    """First markdown heading, else first non-empty line, else a date — never empty on the page."""
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120]
        if stripped:
            return stripped[:120]
    return f"printbridge {datetime.now():%Y-%m-%d %H:%M}"


def digest_if_due(runtime: Runtime) -> bool:
    """Called by the watchdog: sends the weekly digest at most once a week."""
    notifier = runtime.notifier
    if notifier is None or not notifier.due_for_digest():
        return False
    notifier.send_digest(datetime.now(UTC) - timedelta(days=7))
    notifier.mark_digest_sent()
    return True
