"""`printbridge doctor` — the one command a user is asked to run when something is wrong.

Design rule: **every ✗ carries the next step**, and every entry in docs/troubleshooting.md maps to
a finding here. A diagnostic that only says "printer unreachable" moves the problem; one that says
"the tailnet route is down — SSH to the UDM and run …" ends it.

Modes:
    doctor            full table, human readable
    doctor --quiet    print only failures; exit code 1 when something is wrong (the watchdog)
    doctor --self     only checks that need neither the printer nor the internet (healthcheck)
    doctor --json     machine readable
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from printbridge import __version__
from printbridge.config import ConfigError, Settings, validate_runtime
from printbridge.runtime import Runtime
from printbridge.store import utcnow

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "–"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def mark(self) -> str:
        return _MARK[self.status]


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    connector_urls: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> Check:
        check = Check(name, status, detail, fix)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failed

    def state_signature(self) -> str:
        """What the watchdog compares between runs — a change is what triggers an alert."""
        return ";".join(f"{c.name}={c.status}" for c in self.checks if c.status in {FAIL, WARN})

    def to_text(self, quiet: bool = False) -> str:
        lines: list[str] = []
        width = max((len(c.name) for c in self.checks), default=10)
        for check in self.checks:
            if quiet and check.status in {OK, SKIP}:
                continue
            lines.append(f"  {check.mark} {check.name.ljust(width)}  {check.detail}")
            if check.fix and check.status in {FAIL, WARN}:
                lines.append(f"      → {check.fix}")
        if not quiet:
            for url in self.connector_urls:
                lines.append(f"\n  connector URL: {url}")
            summary = (
                f"\n  {len(self.checks)} checks · {len(self.failed)} failed · {len(self.warned)} warnings"
            )
            lines.append(summary)
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": __version__,
                "ok": self.ok,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                    for c in self.checks
                ],
                "connector_urls": self.connector_urls,
            },
            indent=2,
        )


def run_doctor(
    runtime: Runtime,
    *,
    self_only: bool = False,
    show_keys: bool = False,
) -> Report:
    settings = runtime.settings
    report = Report()

    _check_config(report, settings, runtime)
    _check_files(report, settings, runtime)
    _check_keys(report, settings, runtime, show_keys)
    if self_only:
        _check_self_http(report, settings)
        return report

    _check_cups(report, runtime)
    _check_queues(report, runtime)
    _check_job_log(report, runtime)
    _check_supplies(report, runtime)
    _check_public_url(report, settings)
    _check_tls(report, settings)
    _check_tailscale(report)
    _check_notify(report, runtime)
    _check_watchdog(report, runtime)
    _check_version(report)
    return report


# --------------------------------------------------------------------------- checks


def _check_config(report: Report, settings: Settings, runtime: Runtime) -> None:
    try:
        warnings = validate_runtime(settings, runtime.keys.count_active())
        detail = (
            f"EXPOSE={settings.expose} · AUTH_MODE={settings.auth_mode} · BIND={settings.bind}"
        )
        report.add("config", OK, detail)
        for warning in warnings:
            report.add("config warning", WARN, warning, "Edit .env and restart printbridge.")
    except ConfigError as exc:
        report.add("config", FAIL, str(exc), "Fix .env, then `docker compose up -d`.")

    specs = settings.printer_specs
    if not specs:
        report.add(
            "printers configured",
            FAIL,
            "PRINTERS is empty",
            "Set PRINTERS in .env, e.g. PRINTERS=home=ipp://192.168.1.50/ipp/print — or run "
            "`printbridge init`.",
        )
    else:
        names = ", ".join(f"{s.name}{' (mock)' if s.is_mock else ''}" for s in specs)
        report.add("printers configured", OK, f"{len(specs)}: {names}")


def _check_files(report: Report, settings: Settings, runtime: Runtime) -> None:
    env_path = Path(os.environ.get("PRINTBRIDGE_ENV_FILE", ".env"))
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        if mode & 0o077:
            report.add(
                ".env permissions",
                WARN,
                f"{env_path} is mode {mode:o} — other users on this machine can read it",
                f"chmod 600 {env_path}",
            )
        else:
            report.add(".env permissions", OK, f"{env_path} is {mode:o}")
    else:
        report.add(".env permissions", SKIP, f"{env_path} not present (settings come from the environment)")

    if not runtime.keys.permissions_ok():
        report.add(
            "keys.json permissions",
            FAIL,
            f"{settings.keys_path} is readable by others",
            f"chmod 600 {settings.keys_path}",
        )
    elif not runtime.keys.readable():
        report.add(
            "keys.json permissions",
            FAIL,
            f"{settings.keys_path} exists but this process cannot read it (owned by another user)",
            "In Docker this fixes itself on restart (`docker compose up -d printbridge`); "
            "bare metal: chown the file to the user printbridge runs as.",
        )
    else:
        report.add("keys.json permissions", OK, "0600 and readable by this process")

    try:
        settings.spool_path.mkdir(parents=True, exist_ok=True)
        probe = settings.spool_path / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        report.add("spool writable", OK, str(settings.spool_path))
    except OSError as exc:
        report.add(
            "spool writable",
            FAIL,
            f"{settings.spool_path}: {exc}",
            "Check the volume mount and ownership of ./data.",
        )


def _check_keys(report: Report, settings: Settings, runtime: Runtime, show_keys: bool) -> None:
    active = runtime.keys.active()
    if settings.auth_mode == "none":
        report.add(
            "auth",
            WARN if settings.expose == "none" else FAIL,
            "AUTH_MODE=none — anyone who reaches this server can print",
            "Only acceptable with EXPOSE=none. Otherwise: AUTH_MODE=keys + `printbridge keys add "
            "<person>`.",
        )
        return
    if not active:
        report.add(
            "connector keys",
            FAIL if settings.expose != "none" else WARN,
            "no keys exist",
            "`printbridge keys add <person>` — the link it prints is what you paste into Claude or "
            "ChatGPT.",
        )
        return
    report.add("connector keys", OK, f"{len(active)}: " + ", ".join(k.name for k in active))
    if settings.public_url:
        report.connector_urls.append(
            f"{settings.public_url}/mcp/<key>  (the key is shown once, by `keys add` / `keys rotate`)"
        )


def _check_self_http(report: Report, settings: Settings) -> None:
    host, port = settings.listen_bind
    host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((host, port), timeout=3):
            report.add("server listening", OK, f"{host}:{port}")
    except OSError as exc:
        report.add(
            "server listening",
            FAIL,
            f"nothing accepts connections on {host}:{port} ({exc})",
            "`docker compose ps` / `docker compose logs printbridge`.",
        )


def _check_cups(report: Report, runtime: Runtime) -> None:
    if shutil.which("lpstat") is None:
        report.add(
            "CUPS client",
            FAIL,
            "lpstat is not installed here",
            "Run doctor inside the container (`docker compose exec printbridge printbridge doctor`) "
            "or install cups-client.",
        )
        return
    if runtime.spooler.reachable():
        report.add("CUPS reachable", OK, f"CUPS_SERVER={runtime.settings.cups_server}")
    else:
        report.add(
            "CUPS reachable",
            FAIL,
            f"no answer from {runtime.settings.cups_server}",
            "`docker compose ps cups` and `docker compose logs cups`. The app waits for CUPS to be "
            "healthy, so a failure here usually means the cups container is restarting.",
        )


def _check_queues(report: Report, runtime: Runtime) -> None:
    settings = runtime.settings
    try:
        live = {q.name: q for q in runtime.spooler.queues()}
    except Exception as exc:  # noqa: BLE001
        report.add("queues", FAIL, f"could not list queues: {exc}", "See the CUPS check above.")
        return
    for spec in settings.printer_specs:
        queue = live.get(spec.name)
        if queue is None:
            report.add(
                f"queue {spec.name}",
                FAIL,
                "does not exist in CUPS",
                "`docker compose up -d cups` recreates every queue in PRINTERS; the cups log says "
                "why it could not.",
            )
            continue
        if spec.is_mock:
            report.add(
                f"queue {spec.name}",
                WARN,
                f"MOCK (cups-pdf) — PDFs land in {settings.mock_out_path}, no paper moves",
                "Set a real printer in PRINTERS and `docker compose up -d cups` when the printer "
                "is reachable.",
            )
            continue
        if queue.state == "stopped":
            report.add(
                f"queue {spec.name}",
                FAIL,
                f"stopped ({queue.reason or 'no reason given'})",
                f"`cupsenable {spec.name}` after fixing the printer; jobs are held, not lost.",
            )
            continue
        attrs = runtime.spooler.printer_attributes(spec.uri)
        if not attrs:
            report.add(
                f"queue {spec.name}",
                WARN,
                f"{queue.state}, but the printer did not answer an IPP query",
                "The queue exists; the printer may be asleep or the route may be down. "
                f"`ping {spec.host or 'PRINTER_IP'}` from the host.",
            )
            continue
        formats = attrs.get("document-format-supported", "")
        driverless = "image/urf" in formats or "application/pdf" in formats
        detail = ", ".join(
            filter(
                None,
                [
                    attrs.get("printer-make-and-model", "").strip('"'),
                    "duplex" if "two-sided" in attrs.get("sides-supported", "") else "",
                    "colour" if "color" in attrs.get("print-color-mode-supported", "") else "",
                ],
            )
        )
        if driverless:
            report.add(f"queue {spec.name}", OK, f"driverless OK — {detail or queue.state}")
        else:
            report.add(
                f"queue {spec.name}",
                WARN,
                f"answers IPP but not driverless — {detail}",
                "Add a PPD: PRINTERS=…;driver=<model>. `lpinfo -m | grep -i <make>` inside the cups "
                "container lists candidates (Gutenprint and HPLIP are installed).",
            )


def _check_job_log(report: Report, runtime: Runtime) -> None:
    """Reconcile the log against CUPS and say so — ISS-0122 was invisible until it was reported."""
    from printbridge.reconcile import reconcile_jobs

    updated = reconcile_jobs(runtime)
    still_open = len(runtime.store.open_jobs())
    detail = f"{updated} row(s) reconciled with CUPS" if updated else "in step with CUPS"
    if still_open:
        detail += f" · {still_open} job(s) still in the queue"
    report.add("job log", OK, detail)


def collect_supplies(runtime: Runtime) -> dict[str, list]:
    """{queue: [Supply, …]} for every real printer that reports marker levels over IPP."""
    from printbridge.spool import parse_supplies

    out: dict[str, list] = {}
    for spec in runtime.settings.printer_specs:
        if spec.is_mock:
            continue
        try:
            attrs = runtime.spooler.printer_attributes(spec.uri)
        except Exception:  # noqa: BLE001 - a sleeping printer is not a doctor crash
            continue
        supplies = parse_supplies(attrs)
        if supplies:
            out[spec.name] = supplies
    return out


def _check_supplies(report: Report, runtime: Runtime) -> None:
    supplies = collect_supplies(runtime)
    if not supplies:
        report.add("supplies", SKIP, "no printer reports toner or ink levels over IPP")
        return
    for queue, items in supplies.items():
        low = [s for s in items if s.is_low]
        summary = ", ".join(s.describe() for s in items)
        if low:
            report.add(
                f"supplies {queue}",
                WARN,
                summary,
                f"{' and '.join(s.name for s in low)} will run out soon — order a replacement. "
                "printbridge keeps printing until the printer itself refuses.",
            )
        else:
            report.add(f"supplies {queue}", OK, summary)


def _check_public_url(report: Report, settings: Settings) -> None:
    if settings.expose == "none":
        report.add("public URL", SKIP, "EXPOSE=none — this server is local only")
        return
    if not settings.public_url:
        report.add("public URL", FAIL, "PUBLIC_URL is empty", "Set PUBLIC_URL in .env.")
        return
    host = urlparse(settings.public_url).hostname or ""
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as exc:
        report.add(
            "public URL",
            FAIL,
            f"{host} does not resolve ({exc.strerror or exc})",
            "Add an A record for this hostname pointing at this server's public IP, then wait for "
            "the TTL.",
        )
        return
    mine = _my_public_addresses()
    if mine and not (set(addresses) & mine):
        report.add(
            "public URL",
            WARN,
            f"{host} → {', '.join(addresses)}; this server answers on {', '.join(sorted(mine))}",
            "If a proxy or CDN sits in front, this is expected. Otherwise fix the DNS record.",
        )
    else:
        report.add("public URL", OK, f"{host} → {', '.join(addresses)}")


def _check_tls(report: Report, settings: Settings) -> None:
    if settings.expose == "none" or not settings.public_url.startswith("https://"):
        report.add("TLS certificate", SKIP, "no public HTTPS endpoint configured")
        return
    host = urlparse(settings.public_url).hostname or ""
    port = urlparse(settings.public_url).port or 443
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=6) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except Exception as exc:  # noqa: BLE001
        report.add(
            "TLS certificate",
            FAIL,
            f"could not complete a TLS handshake with {host}:{port} ({exc})",
            "Is the reverse proxy running and does the site block exist? For a host Caddy: "
            "`sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.",
        )
        return
    not_after = cert.get("notAfter", "") if cert else ""
    try:
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except ValueError:
        report.add("TLS certificate", OK, "valid (expiry not parsed)")
        return
    days = (expires - datetime.now(UTC)).days
    if days < 14:
        report.add(
            "TLS certificate",
            WARN,
            f"expires in {days} days ({expires:%Y-%m-%d})",
            "Caddy renews automatically; if it has not, check `journalctl -u caddy`.",
        )
    else:
        report.add("TLS certificate", OK, f"valid for {days} more days")


def _check_tailscale(report: Report) -> None:
    if shutil.which("tailscale") is None:
        report.add("tailscale", SKIP, "tailscale CLI not available here (normal inside the container)")
        return
    try:
        raw = subprocess.run(  # noqa: S603
            [shutil.which("tailscale") or "tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        data = json.loads(raw.stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        report.add("tailscale", WARN, f"could not read tailscale status ({exc})")
        return
    self_node = data.get("Self") or {}
    expiry = self_node.get("KeyExpiry")
    if not expiry:
        report.add("tailscale key expiry", OK, "disabled on this node")
        return
    try:
        when = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        report.add("tailscale key expiry", WARN, f"unparsable expiry {expiry!r}")
        return
    days = (when - datetime.now(UTC)).days
    status = OK if days > 30 else WARN
    report.add(
        "tailscale key expiry",
        status,
        f"{days} days left ({when:%Y-%m-%d})",
        "Disable key expiry for this node and the subnet router in the Tailscale admin console — "
        "otherwise printing dies silently when the key expires."
        if status == WARN
        else "",
    )


def _check_notify(report: Report, runtime: Runtime) -> None:
    notifier = runtime.notifier
    if notifier is None:
        report.add("notifications", SKIP, "not configured")
        return
    if getattr(notifier, "is_placeholder", False):
        report.add(
            "notifications",
            FAIL,
            "NOTIFY_WEBHOOK is still a placeholder — every alert goes to the outbox instead",
            "Put the real bot URL in .env (no angle brackets left in it) and "
            "`docker compose up -d printbridge`.",
        )
    elif notifier.configured:
        failure = notifier.last_delivery_error() if hasattr(notifier, "last_delivery_error") else ""
        if failure:
            report.add(
                "notifications",
                WARN,
                f"webhook configured, but the last alert did not get through — {failure}",
                "Check the bot token and chat id, then `printbridge notify --test`.",
            )
        else:
            report.add("notifications", OK, f"webhook OK ({notifier.describe()})")
    else:
        report.add(
            "notifications",
            WARN,
            f"outbox only — alerts are written to {runtime.settings.outbox_path}",
            "Set NOTIFY_WEBHOOK in .env (a Telegram bot URL works) to get alerts on your phone.",
        )


WATCHDOG_STALE_MINUTES = 45


def _check_watchdog(report: Report, runtime: Runtime) -> None:
    """Is anything actually checking on this server, or does it only get checked when you ask?

    The watchdog writes a heartbeat on every tick, so this works from inside the container and
    catches every way a timer can be broken — wrong unit, disabled timer, no Docker access. The
    first version of the shipped unit failed with status 203/EXEC every fifteen minutes and nothing
    said so; that is what this check exists to prevent.
    """
    last = runtime.store.get_meta("last_watchdog", "")
    install = (
        "Install it: deploy/systemd/README.md (one sed, two installs). Without it, nothing tells "
        "you when the printer disappears."
    )
    if not last:
        report.add("watchdog", WARN, "has never run", install)
        return
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        report.add("watchdog", WARN, f"unreadable heartbeat {last!r}", install)
        return
    minutes = (datetime.now(UTC) - when).total_seconds() / 60
    if minutes > WATCHDOG_STALE_MINUTES:
        report.add(
            "watchdog",
            WARN,
            f"last ran {int(minutes)} minutes ago ({last[:16].replace('T', ' ')})",
            "The timer is not firing, or it fires and fails. Check "
            "`systemctl status printbridge-doctor.service` — a system unit needs `User=` or it runs "
            "as root, looks for the checkout in /root and fails 203/EXEC every tick.",
        )
    else:
        report.add("watchdog", OK, f"last ran {int(minutes)} minute(s) ago")


def _check_version(report: Report) -> None:
    latest = _latest_release()
    if latest is None:
        report.add("version", OK, f"{__version__} (latest release not checked)")
        return
    if latest.lstrip("v") != __version__:
        report.add(
            "version",
            WARN,
            f"{__version__} installed, {latest} available",
            "`printbridge update` pulls, rebuilds and re-runs doctor.",
        )
    else:
        report.add("version", OK, f"{__version__} is current")


def _latest_release() -> str | None:
    try:
        import httpx

        response = httpx.get(
            "https://api.github.com/repos/starkshtlm/printbridge/releases/latest", timeout=4
        )
        if response.status_code != 200:
            return None
        return str(response.json().get("tag_name") or "") or None
    except Exception:  # noqa: BLE001 - no network is not a failure
        return None


def _my_public_addresses() -> set[str]:
    found: set[str] = set()
    try:
        import httpx

        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                response = httpx.get(url, timeout=4)
                if response.status_code == 200 and response.text.strip():
                    found.add(response.text.strip())
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return found


def watchdog_tick(runtime: Runtime) -> tuple[Report, bool]:
    """One watchdog run: doctor, alert on a *state change*, return (report, alerted)."""
    runtime.store.set_meta("last_watchdog", utcnow())
    report = run_doctor(runtime)
    _alert_on_supplies(runtime)
    signature = report.state_signature()
    previous = runtime.store.get_meta("doctor_signature", "")
    alerted = False
    if signature != previous:
        runtime.store.set_meta("doctor_signature", signature)
        if runtime.notifier is not None and (report.failed or report.warned):
            problems = report.failed or report.warned
            first = problems[0]
            runtime.notifier.send(
                "printer_offline" if "queue" in first.name or "CUPS" in first.name else "cert_expiring",
                f"doctor: {first.name} — {first.detail}. {first.fix}",
            )
            alerted = True
        elif runtime.notifier is not None and previous:
            runtime.notifier.send("printer_online", "doctor: everything is green again.")
            alerted = True
    return report, alerted


def _alert_on_supplies(runtime: Runtime) -> None:
    """One message when a consumable goes low, one when it is replaced — never in between.

    The state is bucketed (`low`/`ok` plus the name), so a cartridge sliding from 5 % to 4 % does
    not produce a second message. Crossing back above the threshold does.
    """
    notifier = runtime.notifier
    if notifier is None:
        return
    for queue, items in collect_supplies(runtime).items():
        low = [s for s in items if s.is_low]
        if low:
            notifier.on_state(
                "supply_low",
                "low:" + ",".join(sorted(s.name for s in low)),
                f"{queue}: "
                + ", ".join(f"{s.name} is at {s.level}%" for s in low)
                + ". Order a replacement — printing continues until the printer itself refuses.",
                key=f"supply_low:{queue}",
            )
        else:
            notifier.on_state(
                "supply_low",
                "ok",
                f"{queue}: toner and ink are back above the threshold.",
                key=f"supply_low:{queue}",
            )


def collect_for_status(runtime: Runtime) -> dict[str, Any]:
    from printbridge.admin import collect

    return collect(runtime)
