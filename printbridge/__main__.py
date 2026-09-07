"""`printbridge` — the command line: serve, doctor, keys, init, status, jobs, backup, update.

Everything a user is ever asked to type lives here, and every command prints what it did in words
rather than leaving them to read a log.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from printbridge import __version__
from printbridge.config import ConfigError, Settings, bind_parts, load_settings
from printbridge.keys import KeyError_
from printbridge.runtime import Runtime, build_runtime

DURATION_RE = re.compile(r"^(\d+)([hdw])$")


# --------------------------------------------------------------------------- helpers


def _drop_privileges(settings: Settings) -> None:
    """If we are root and `data/` belongs to someone else, become them before touching anything.

    `docker compose exec printbridge printbridge keys add …` bypasses the image entrypoint and so
    runs as root. Without this, `keys.json` is rewritten root-owned and 0600, and the server — which
    is not root — starts answering 500 to every request. Found the hard way in B2.
    """
    if os.geteuid() != 0:
        return
    try:
        stat = settings.data_dir.stat()
    except OSError:
        return
    if stat.st_uid == 0:
        return
    try:
        os.setgroups([])
        os.setgid(stat.st_gid)
        os.setuid(stat.st_uid)
    except OSError:
        pass


def _settings(args: argparse.Namespace) -> Settings:
    if getattr(args, "env_file", None):
        os.environ["PRINTBRIDGE_ENV_FILE"] = str(args.env_file)
    return load_settings()


def _runtime(args: argparse.Namespace) -> Runtime:
    settings = _settings(args)
    _drop_privileges(settings)
    return build_runtime(settings)


def _parse_since(value: str) -> datetime:
    match = DURATION_RE.match(value.strip().lower())
    if not match:
        raise SystemExit(f"--since wants a duration like 24h, 7d or 2w — got {value!r}")
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
    return datetime.now(UTC) - delta


def _print_key(plaintext: str, record, settings: Settings) -> None:
    print()
    print(f"  Key for {record.name} ({record.role}) — shown once, it is stored hashed:")
    print()
    print(f"    {plaintext}")
    print()
    if settings.public_url:
        print("  Paste this link into Claude, ChatGPT or any other assistant:")
        print()
        print(f"    {settings.connector_url(plaintext)}")
        print()
        print("  Agents and Claude Code can use the same key as a header instead:")
        print(f'    Authorization: Bearer {plaintext[:8]}…   →  {settings.public_url}/mcp')
    else:
        print("  PUBLIC_URL is not set, so there is no link to paste yet. Local agents can use")
        print(f"    Authorization: Bearer <key>  →  http://{settings.bind}/mcp")
    print()


# --------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _drop_privileges(settings)
    runtime = build_runtime(settings)
    from printbridge.config import validate_runtime

    try:
        for warning in validate_runtime(settings, runtime.keys.count_active()):
            print(f"  ! {warning}", file=sys.stderr)
    except ConfigError as exc:
        print(f"printbridge refuses to start: {exc}", file=sys.stderr)
        return 2

    if args.transport == "stdio":
        from printbridge.server import build_server

        build_server(settings, runtime).run(transport="stdio")
        return 0

    import asyncio

    import uvicorn

    from printbridge.admin import create_admin_app
    from printbridge.app import create_app

    host, port = settings.listen_bind
    admin_host, admin_port = settings.admin_listen_bind
    app = create_app(runtime)
    admin = create_admin_app(runtime)

    print(
        f"printbridge {__version__} — MCP on http://{host}:{port}/mcp, admin on "
        f"http://{admin_host}:{admin_port}/status"
    )
    if (host, port) != bind_parts(settings.bind):
        print(
            f"  (inside a container: listening on {host}:{port} of the container namespace; "
            f"docker publishes it as {settings.bind} on the host)"
        )
    if settings.is_mock_printing:
        print("  ! every configured queue is a mock (cups-pdf): nothing will reach paper")

    async def run_both() -> None:
        servers = [
            uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=args.log_level,
                                          access_log=settings.log_request_path)),
            uvicorn.Server(uvicorn.Config(admin, host=admin_host, port=admin_port,
                                          log_level="warning", access_log=False)),
        ]
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(run_both())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from printbridge.doctor import run_doctor, watchdog_tick

    try:
        runtime = _runtime(args)
    except ConfigError as exc:
        print(f"  ✗ config  {exc}")
        return 2
    if args.watchdog:
        report, alerted = watchdog_tick(runtime)
    else:
        report = run_doctor(runtime, self_only=args.self)
    if args.json:
        print(report.to_json())
    else:
        text = report.to_text(quiet=args.quiet)
        if text.strip() or not args.quiet:
            print(f"printbridge doctor — {datetime.now():%Y-%m-%d %H:%M}")
            print(text)
    return 0 if report.ok else 1


def cmd_keys(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _drop_privileges(settings)
    runtime = build_runtime(settings)
    store = runtime.keys
    try:
        if args.keys_command == "add":
            printers = [p.strip() for p in (args.printers or "").split(",") if p.strip()]
            rate = int(args.rate.rstrip("/h")) if args.rate else None
            plaintext, record = store.add(
                args.person,
                role=args.role,
                printers=printers,
                max_pages=args.max_pages,
                rate_per_hour=rate,
                label=args.label or "",
            )
            _print_key(plaintext, record, settings)
        elif args.keys_command == "list":
            records = store.all() if args.all else store.active()
            if not records:
                print("  no keys yet — `printbridge keys add <person>`")
            for record in records:
                last = f" · last used {record.last_used[:16]}" if record.last_used else ""
                print(f"  {record.describe()}{last}")
        elif args.keys_command == "revoke":
            record = store.revoke(args.person)
            print(f"  revoked {record.name} — that link stops working immediately.")
        elif args.keys_command == "rotate":
            plaintext, record = store.rotate(args.person, settings.key_grace_minutes)
            print(
                f"  rotated {record.name}; the old link keeps working for "
                f"{settings.key_grace_minutes} minutes."
            )
            _print_key(plaintext, record, settings)
    except (KeyError_, ConfigError) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1
    return 0


TEST_PAGE = """# printbridge test page

If you are holding this on paper, everything works: the assistant's request reached this server,
the document was rendered here, and your printer received it over your own private network.

| Step | What it proves |
|---|---|
| Rendered on the server | markdown, tables and page numbers come out right |
| Queued in CUPS | the printer is reachable and accepting jobs |
| Printed | the whole path works end to end |

Nothing about this page left your own infrastructure.

Next: paste your connector link into Claude or ChatGPT and say
*"print a test page on my printer"*.
"""


def cmd_test_page(args: argparse.Namespace) -> int:
    """Print one page through the real path — render, count, spool — and say what happened."""
    from printbridge.render import RenderError, render_to_pdf
    from printbridge.server_helpers import spool_target
    from printbridge.spool import PrintOptions, SpoolError

    settings = _settings(args)
    _drop_privileges(settings)
    runtime = build_runtime(settings)
    try:
        spec = settings.printer_or_default(args.printer)
    except ConfigError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1

    target = spool_target(settings)
    try:
        result = render_to_pdf(
            TEST_PAGE, target, title="printbridge test page",
            paper=settings.default_paper, locale=settings.locale,
        )
    except RenderError as exc:
        print(f"  ✗ could not render the test page: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        target.unlink(missing_ok=True)
        print(f"  {result.pages} page(s) would go to {spec.name}. Nothing was printed.")
        return 0

    try:
        job_id = runtime.spooler.submit(
            target, spec.name,
            PrintOptions(copies=1, duplex=False, color=False,
                         paper=settings.default_paper, title="printbridge test page"),
        )
    except SpoolError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        print("    Run `printbridge doctor` — it says which part is broken and what to do.")
        return 1
    finally:
        if settings.keep_spool_hours == 0:
            target.unlink(missing_ok=True)

    runtime.store.record_job(
        principal="local", tool="test_page", status="queued", job_id=job_id,
        title="printbridge test page", pages=result.pages, printer=spec.name, source_kind="text",
    )
    if spec.is_mock:
        print(f"  queued as {job_id} on the mock queue {spec.name} —")
        print(f"    no paper moves; the PDF appears in {settings.mock_out_path} instead.")
    else:
        print(f"  queued as {job_id} on {spec.name}. A page should come out within a few seconds.")
        print("    Nothing? The printer may be asleep — CUPS holds the job and prints it on wake.")
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Send one message so you find out here, not during an outage, whether alerts work."""
    runtime = _runtime(args)
    notifier = runtime.notifier
    if notifier is None:
        print("  ✗ notifications are not configured at all.", file=sys.stderr)
        return 1
    print(f"  target: {notifier.describe()}")
    delivered = notifier.send(
        "test", "test message — if you can read this, printbridge can reach you."
    )
    if delivered:
        print("  ✓ delivered.")
        return 0
    failure = notifier.last_delivery_error()
    if notifier.configured:
        print(f"  ✗ not delivered; written to the outbox instead. {failure}", file=sys.stderr)
        return 1
    print(f"  written to {runtime.settings.outbox_path} (no webhook configured).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from printbridge.admin import collect
    from printbridge.reconcile import reconcile_quietly

    runtime = _runtime(args)
    reconcile_quietly(runtime)
    data = collect(runtime)
    print(f"printbridge {data['version']} — {datetime.now():%Y-%m-%d %H:%M}")
    print()
    print("  printers")
    if not data["queues"]:
        print(f"    (none — CUPS says: {data['queues_error'] or 'no queues configured'})")
    for q in data["queues"]:
        mock = " [mock]" if q["mock"] else ""
        waiting = f", {q['depth']} waiting" if q["depth"] else ""
        print(f"    {q['name']}: {q['state']}{mock}{waiting} {q['reason']}")
    print()
    print("  connected now (15 min)")
    if not data["sessions"]:
        print("    nobody")
    for s in data["sessions"]:
        print(
            f"    {s['principal']} via {s['client']} — {s['calls']} calls, {s['jobs']} jobs, "
            f"last seen {s['last_seen'][11:19]} from {s['remote_ip'] or '-'}"
        )
    print()
    print("  last 24 hours")
    if not data["totals_24h"]:
        print("    nothing printed")
    for t in data["totals_24h"]:
        fail = f", {t['failures']} failed" if t["failures"] else ""
        print(f"    {t['principal']} via {t['client']}: {t['jobs']} jobs, {t['pages']} pages{fail}")
    if data["errors"]:
        print()
        print("  last errors")
        for e in data["errors"]:
            print(f"    {e['ts'][:16]} {e['principal']}: {(e['error'] or e['status'])[:100]}")
    print()
    print(f"  alerts: {data['notify']}")
    print(f"  keys:   {', '.join(data['keys']) or 'none'}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    from printbridge.reconcile import reconcile_quietly

    runtime = _runtime(args)
    reconcile_quietly(runtime)
    rows = runtime.store.recent_jobs(
        principal=args.principal, printer=args.printer, limit=args.limit, since=_parse_since(args.since)
    )
    if args.csv:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            ["ts", "principal", "client", "tool", "title", "pages", "copies", "printer", "status"]
        )
        for r in rows:
            writer.writerow(
                [r["ts"], r["principal"], r["client"], r["tool"], r["title"], r["pages"],
                 r["copies"], r["printer"], r["status"]]
            )
        print(out.getvalue().rstrip())
        return 0
    if not rows:
        print(f"  no jobs in the last {args.since}")
        return 0
    print(f"  {'when':16}  {'person':12} {'via':12} {'pages':>5}  {'printer':10} {'status':10} title")
    for r in rows:
        print(
            f"  {r['ts'][:16]}  {r['principal'][:12]:12} {r['client'][:12]:12} "
            f"{r['pages']:>5}  {r['printer'][:10]:10} {r['status'][:10]:10} {r['title'][:40]}"
        )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from printbridge.init_wizard import run_init

    return run_init(args)


def cmd_backup(args: argparse.Namespace) -> int:
    settings = _settings(args)
    target = Path(args.output or f"printbridge-backup-{datetime.now():%Y%m%d-%H%M%S}.tar.gz")
    env_path = Path(os.environ.get("PRINTBRIDGE_ENV_FILE", ".env"))
    with tarfile.open(target, "w:gz") as tar:
        if env_path.exists():
            tar.add(env_path, arcname=".env")
        if settings.data_dir.exists():
            for path in sorted(settings.data_dir.rglob("*")):
                if path.is_file() and "spool" not in path.parts and "mock-out" not in path.parts:
                    tar.add(path, arcname=str(Path("data") / path.relative_to(settings.data_dir)))
    os.chmod(target, 0o600)
    size = target.stat().st_size
    print(f"  wrote {target} ({size / 1024:.0f} kB) — .env and data/ (keys, job log).")
    print("  restore: untar into the repo directory, then `docker compose up -d`.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    steps = [
        ["git", "pull", "--ff-only"],
        ["docker", "compose", "pull"],
        ["docker", "compose", "up", "-d", "--build"],
    ]
    for step in steps:
        binary = shutil.which(step[0])
        if binary is None:
            print(f"  ✗ {step[0]} is not installed — run this on the host, not inside the container.")
            return 1
        print(f"  $ {' '.join(step)}")
        result = subprocess.run([binary, *step[1:]], check=False)  # noqa: S603
        if result.returncode != 0:
            print(f"  ✗ {step[0]} failed; nothing further was run.")
            return result.returncode
    print("  now checking the result:")
    return cmd_doctor(argparse.Namespace(**{**vars(args), "quiet": False, "self": False,
                                            "json": False, "watchdog": False}))


def cmd_watchdog(args: argparse.Namespace) -> int:
    """What the systemd timer runs: doctor, alert on state change, weekly digest, retention."""
    from printbridge.doctor import watchdog_tick
    from printbridge.server import cleanup_spool, digest_if_due

    runtime = _runtime(args)
    report, alerted = watchdog_tick(runtime)
    digest_if_due(runtime)
    removed = cleanup_spool(runtime.settings)
    jobs, sessions = runtime.store.purge(runtime.settings.job_log_retention_days)
    if not args.quiet or not report.ok:
        print(report.to_text(quiet=args.quiet))
        if removed or jobs or sessions:
            print(f"  housekeeping: {removed} spool files, {jobs} job rows, {sessions} sessions removed")
    return 0 if report.ok else 1


def cmd_version(args: argparse.Namespace) -> int:
    print(f"printbridge {__version__}")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="printbridge",
        description="Self-hosted MCP print server — assistants print on your own printer.",
    )
    parser.add_argument("--env-file", help="path to the .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the server (default)")
    serve.add_argument("--transport", choices=["http", "stdio"], default="http")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="check everything and say what to fix")
    doctor.add_argument("--quiet", action="store_true", help="only print problems")
    doctor.add_argument("--self", action="store_true", help="only local checks (container health)")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--watchdog", action="store_true", help="alert on state change")
    doctor.set_defaults(func=cmd_doctor)

    keys = sub.add_parser("keys", help="connector keys — one per person")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    add = keys_sub.add_parser("add", help="create a key and print the link once")
    add.add_argument("person")
    add.add_argument("--role", choices=["user", "admin"], default="user")
    add.add_argument("--printers", help="comma separated; default: all")
    add.add_argument("--max-pages", type=int, help="lower the page ceiling for this key")
    add.add_argument("--rate", help="jobs per hour, e.g. 10/h")
    add.add_argument("--label", help="a second key for the same person, e.g. agent")
    listing = keys_sub.add_parser("list")
    listing.add_argument("--all", action="store_true", help="include revoked and expired keys")
    revoke = keys_sub.add_parser("revoke")
    revoke.add_argument("person")
    rotate = keys_sub.add_parser("rotate")
    rotate.add_argument("person")
    keys.set_defaults(func=cmd_keys)

    test_page = sub.add_parser("test-page", help="print one page to prove the whole path works")
    test_page.add_argument("--printer")
    test_page.add_argument("--dry-run", action="store_true", help="render and count, print nothing")
    test_page.set_defaults(func=cmd_test_page)

    notify = sub.add_parser("notify", help="send a test alert and report whether it arrived")
    notify.add_argument("--test", action="store_true", default=True)
    notify.set_defaults(func=cmd_notify)

    status = sub.add_parser("status", help="printers, who is connected, 24 h totals")
    status.set_defaults(func=cmd_status)

    jobs = sub.add_parser("jobs", help="what has been printed")
    jobs.add_argument("--since", default="7d")
    jobs.add_argument("--principal")
    jobs.add_argument("--printer")
    jobs.add_argument("--limit", type=int, default=50)
    jobs.add_argument("--csv", action="store_true")
    jobs.set_defaults(func=cmd_jobs)

    init = sub.add_parser("init", help="first-run wizard: printer, exposure, first key")
    init.add_argument("--printer", help="printer URI or IP (non-interactive)")
    init.add_argument("--name", default="home", help="queue name")
    init.add_argument("--expose", choices=["caddy-host", "caddy", "funnel", "cloudflared", "none"])
    init.add_argument("--public-url")
    init.add_argument("--key", help="create the first key for this person")
    init.add_argument("--mock", action="store_true", help="use the cups-pdf mock queue")
    init.add_argument("--non-interactive", action="store_true")
    init.add_argument("--force", action="store_true", help="overwrite an existing .env")
    init.add_argument("--no-watchdog", action="store_true", help="skip installing the systemd timer")
    init.set_defaults(func=cmd_init)

    backup = sub.add_parser("backup", help="tar of .env + data/")
    backup.add_argument("-o", "--output")
    backup.set_defaults(func=cmd_backup)

    update = sub.add_parser("update", help="pull, rebuild, doctor")
    update.set_defaults(func=cmd_update)

    watchdog = sub.add_parser("watchdog", help="what the timer runs every 15 minutes")
    watchdog.add_argument("--quiet", action="store_true", default=True)
    watchdog.add_argument("--verbose", dest="quiet", action="store_false")
    watchdog.set_defaults(func=cmd_watchdog)

    version = sub.add_parser("version")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args([*(argv or []), "serve"])
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"printbridge: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
