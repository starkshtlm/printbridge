"""Loopback-only status page and Prometheus metrics — spec §18.

This is deliberately *not* reachable through the public proxy: usage data says who prints what and
when, and none of that belongs on the internet. It binds `ADMIN_BIND` (127.0.0.1:8001 by default,
or a tailnet address) and the shipped Caddy snippet proxies only `/mcp*`.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

from printbridge import __version__
from printbridge.runtime import Runtime


def create_admin_app(runtime: Runtime) -> Starlette:
    async def status_page(request: Request) -> HTMLResponse:
        return HTMLResponse(render_status_html(runtime))

    async def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(render_metrics(runtime), media_type="text/plain; version=0.0.4")

    return Starlette(routes=[Route("/status", status_page), Route("/metrics", metrics)])


def collect(runtime: Runtime) -> dict[str, Any]:
    """One snapshot, shared by the HTML page, the metrics endpoint and `printbridge status`."""
    store = runtime.store
    since_24h = datetime.now(UTC) - timedelta(hours=24)
    try:
        queues = [
            {
                "name": q.name,
                "state": q.state,
                "reason": q.reason,
                "mock": q.is_mock,
                "depth": runtime.spooler.queue_depth(q.name),
            }
            for q in runtime.spooler.queues()
        ]
        cups_up = True
    except Exception as exc:  # noqa: BLE001 - the status page must render when CUPS is down
        queues, cups_up = [], False
        queues_error = str(exc)
    else:
        queues_error = ""
    return {
        "version": __version__,
        "cups_up": cups_up,
        "queues": queues,
        "queues_error": queues_error,
        "sessions": store.active_sessions(minutes=15),
        "totals_24h": store.totals(since_24h),
        "errors": store.last_errors(5),
        "keys": [k.describe() for k in runtime.keys.active()],
        "notify": runtime.notifier.describe() if runtime.notifier else "disabled",
        "mock_printing": runtime.mock_printing,
    }


def render_status_html(runtime: Runtime) -> str:
    data = collect(runtime)
    rows = []
    for q in data["queues"]:
        badge = "mock" if q["mock"] else q["state"]
        rows.append(
            f"<tr><td>{html.escape(q['name'])}</td><td>{html.escape(badge)}</td>"
            f"<td>{q['depth']}</td><td>{html.escape(q['reason'] or '—')}</td></tr>"
        )
    sessions = "".join(
        f"<tr><td>{html.escape(s['principal'])}</td><td>{html.escape(s['client'])}</td>"
        f"<td>{html.escape(s['remote_ip'] or '—')}</td><td>{html.escape(s['last_seen'][11:19])}</td>"
        f"<td>{s['calls']}</td><td>{s['jobs']}</td></tr>"
        for s in data["sessions"]
    ) or '<tr><td colspan="6">nobody connected in the last 15 minutes</td></tr>'
    totals = "".join(
        f"<tr><td>{html.escape(t['principal'])}</td><td>{html.escape(t['client'])}</td>"
        f"<td>{t['jobs']}</td><td>{t['pages']}</td><td>{t['failures'] or 0}</td></tr>"
        for t in data["totals_24h"]
    ) or '<tr><td colspan="5">nothing printed in the last 24 hours</td></tr>'
    errors = "".join(
        f"<li>{html.escape(e['ts'][:16])} — {html.escape(e['principal'])}: "
        f"{html.escape((e['error'] or e['status'])[:160])}</li>"
        for e in data["errors"]
    ) or "<li>no errors recorded</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>printbridge status</title><meta http-equiv="refresh" content="30">
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:56rem;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}}
th,td{{border:1px solid #ccc;padding:.35rem .5rem;text-align:left}}th{{background:#f3f3f3}}
h2{{margin-top:1.6rem;font-size:1.1rem}} .warn{{color:#a33}}</style></head><body>
<h1>printbridge {html.escape(data['version'])}</h1>
<p>CUPS: {'reachable' if data['cups_up'] else '<span class="warn">unreachable</span>'}
{f"— {html.escape(data['queues_error'])}" if data["queues_error"] else ""} ·
alerts: {html.escape(data['notify'])}
{' · <strong>mock printing</strong>' if data["mock_printing"] else ""}</p>
<h2>Printers</h2><table><tr><th>Queue</th><th>State</th><th>Waiting</th><th>Reason</th></tr>
{"".join(rows) or '<tr><td colspan="4">no queues</td></tr>'}</table>
<h2>Connected now (15 min)</h2><table>
<tr><th>Person</th><th>Assistant</th><th>From</th><th>Last seen</th><th>Calls</th><th>Jobs</th></tr>
{sessions}</table>
<h2>Last 24 hours</h2><table>
<tr><th>Person</th><th>Assistant</th><th>Jobs</th><th>Pages</th><th>Failed</th></tr>{totals}</table>
<h2>Last errors</h2><ul>{errors}</ul>
<h2>Keys</h2><ul>{"".join(f"<li>{html.escape(k)}</li>" for k in data["keys"]) or "<li>none</li>"}</ul>
</body></html>"""


def render_metrics(runtime: Runtime) -> str:
    data = collect(runtime)
    lines = [
        "# HELP printbridge_up printbridge is running",
        "# TYPE printbridge_up gauge",
        "printbridge_up 1",
        "# HELP printbridge_printer_up Printer queue is idle or processing",
        "# TYPE printbridge_printer_up gauge",
    ]
    for q in data["queues"]:
        up = 1 if q["state"] in {"idle", "processing"} else 0
        lines.append(f'printbridge_printer_up{{printer="{q["name"]}"}} {up}')
        lines.append(f'printbridge_queue_depth{{printer="{q["name"]}"}} {q["depth"]}')
    lines += [
        "# HELP printbridge_jobs_total Jobs in the last 24 hours",
        "# TYPE printbridge_jobs_total gauge",
    ]
    for t in data["totals_24h"]:
        label = f'principal="{t["principal"]}",client="{t["client"]}"'
        lines.append(f"printbridge_jobs_total{{{label}}} {t['jobs']}")
        lines.append(f"printbridge_pages_total{{{label}}} {t['pages']}")
    lines += [
        "# HELP printbridge_sessions_active Sessions seen in the last 15 minutes",
        "# TYPE printbridge_sessions_active gauge",
        f"printbridge_sessions_active {len(data['sessions'])}",
        "# HELP printbridge_cups_up CUPS answers",
        "# TYPE printbridge_cups_up gauge",
        f"printbridge_cups_up {1 if data['cups_up'] else 0}",
    ]
    return "\n".join(lines) + "\n"
