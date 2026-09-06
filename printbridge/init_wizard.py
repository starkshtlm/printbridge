"""`printbridge init` — three questions, then a printed test page.

The wizard is the whole difference between "self-hosted" and "self-hosted if you already know
CUPS". It asks only what it cannot find out by itself:

1. Where is the printer?      → probes it over IPP and says what it found
2. How should it be reached?  → detects a proxy already owning 80/443 and proposes `caddy-host`
3. Who is the first person?   → creates their key and prints the link once

Everything else is derived, written to `.env`, and verified by `doctor`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
from pathlib import Path

from printbridge.config import ConfigError, load_settings
from printbridge.runtime import build_runtime

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _ask(prompt: str, default: str = "", non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _normalise_printer(value: str) -> str:
    """`192.168.1.50` → `ipp://192.168.1.50/ipp/print`; a full URI is left alone."""
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"ipp://{value}/ipp/print"


def probe_printer(uri: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Can we reach the printer at all, and what does it say it is?"""
    if uri.startswith("cups-pdf"):
        return True, "mock queue (cups-pdf) — PDFs are written to data/mock-out"
    host = uri.split("://", 1)[-1].split("/")[0].split(":")[0]
    port = 631
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return False, f"nothing answers on {host}:{port} ({exc})"
    if shutil.which("ipptool"):
        result = subprocess.run(  # noqa: S603
            [shutil.which("ipptool") or "ipptool", "-tv", uri, "get-printer-attributes.test"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            if "printer-make-and-model" in line:
                model = line.split("=", 1)[-1].strip().strip('"')
                return True, f"answers IPP: {model}"
    return True, f"{host}:631 is open (IPP port), model not queried"


def detect_host_proxy() -> str:
    """Something already listening on 80/443 means printbridge must not start its own Caddy."""
    for port in (443, 80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6):
                return "caddy-host"
        except OSError:
            continue
    for port in (443, 80):
        try:
            sock = socket.socket()
            sock.settimeout(0.6)
            sock.bind(("0.0.0.0", port))  # noqa: S104 - probing whether the port is free
            sock.close()
        except OSError:
            return "caddy-host"
    return ""


def render_env(values: dict[str, str]) -> str:
    """Start from `.env.example` so every comment survives, then set the answers."""
    template = ENV_EXAMPLE.read_text("utf-8") if ENV_EXAMPLE.exists() else ""
    lines: list[str] = []
    seen: set[str] = set()
    for line in template.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                comment = ""
                if "#" in line:
                    comment = "  " + line.split("#", 1)[1].strip().join(("# ", ""))[2:]
                lines.append(f"{key}={values[key]}{comment}")
                seen.add(key)
                continue
        lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def run_init(args: argparse.Namespace) -> int:
    env_path = Path(os.environ.get("PRINTBRIDGE_ENV_FILE", ".env"))
    non_interactive = bool(args.non_interactive)

    print("printbridge init — three questions, then a test page.\n")

    if env_path.exists() and not args.force:
        print(f"  {env_path} already exists. Re-run with --force to overwrite it, or edit it by hand.")
        print("  Nothing was changed.")
        return 1

    # 1 — the printer
    if args.mock:
        printer_uri = "cups-pdf:/"
    else:
        raw = args.printer or _ask(
            "Printer address (IP or full ipp:// URI; empty = mock queue)", "", non_interactive
        )
        printer_uri = _normalise_printer(raw) or "cups-pdf:/"
    reachable, note = probe_printer(printer_uri)
    print(f"  printer: {printer_uri}\n    {'✓' if reachable else '✗'} {note}")
    if not reachable:
        print("    The queue will still be created and will retry; `printbridge doctor` will keep")
        print("    telling you it is unreachable until the route works.")
    name = args.name or _ask("A short name for it", "home", non_interactive) or "home"

    # 2 — how it is reached
    detected = detect_host_proxy()
    default_expose = args.expose or detected or "none"
    if detected and not args.expose:
        print("\n  Something already serves 80/443 on this host, so `caddy-host` is proposed:")
        print("    printbridge stays on 127.0.0.1:8000 and you install one site snippet.")
    expose = args.expose or _ask(
        "How should assistants reach this server? caddy-host | caddy | funnel | cloudflared | none",
        default_expose,
        non_interactive,
    )
    public_url = args.public_url or ""
    if expose != "none" and not public_url:
        if non_interactive:
            raise ConfigError(
                f"--expose {expose} publishes this server, so --public-url is required "
                "(e.g. --public-url https://print.example.com). Inventing a hostname here would "
                "produce a configuration that cannot work."
            )
        public_url = _ask("Public address assistants will use (https://…)", "", False)
        if not public_url:
            raise ConfigError(
                "A published server needs a public address. Re-run and give one, or choose "
                "exposure 'none' for a local-only server."
            )

    values = {
        "PRINTERS": f"{name}={printer_uri}",
        "PRINTER_URI": "",
        "PRINTER_NAME": name,
        "EXPOSE": expose,
        "PUBLIC_URL": public_url,
        "AUTH_MODE": "keys",
    }
    env_path.write_text(render_env(values), "utf-8")
    os.chmod(env_path, 0o600)
    print(f"\n  wrote {env_path} (0600)")

    # 3 — the first person. Validate before going further: a wizard that leaves behind a broken
    # .env is worse than one that refuses, because the error surfaces later and elsewhere.
    try:
        settings = load_settings()
    except ConfigError:
        env_path.unlink(missing_ok=True)
        raise
    runtime = build_runtime(settings)
    person = args.key or _ask("Who is the first person to use it?", "me", non_interactive) or "me"
    plaintext = ""
    if runtime.keys.count_active() == 0:
        plaintext, record = runtime.keys.add(person, role="admin")
        print(f"\n  created an admin key for {record.name}. It is shown once:\n")
        print(f"    {plaintext}\n")
        if settings.public_url:
            print("  Paste this link into Claude, ChatGPT or any other assistant:\n")
            print(f"    {settings.connector_url(plaintext)}\n")
    else:
        print(f"\n  {runtime.keys.count_active()} key(s) already exist — none created.")

    # 4 — verify
    from printbridge.doctor import run_doctor

    report = run_doctor(runtime)
    print("\nprintbridge doctor")
    print(report.to_text())

    print("\n  Try it now:")
    print("    docker compose exec printbridge printbridge doctor")
    print(f"    docker compose exec cups sh -c 'echo printbridge works | lp -d {name}'")
    if settings.public_url:
        print("    …then say to your assistant: \u201cprint a test page on my printer\u201d")

    if not args.no_watchdog:
        _watchdog_hint()
    return 0 if report.ok else 1


def _watchdog_hint() -> None:
    unit_dir = Path("/etc/systemd/system")
    source = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
    if not source.exists():
        return
    can_install = os.geteuid() == 0 and unit_dir.is_dir() and shutil.which("systemctl")
    if can_install:
        for unit in ("printbridge-doctor.service", "printbridge-doctor.timer"):
            shutil.copy(source / unit, unit_dir / unit)
        subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["systemctl", "enable", "--now", "printbridge-doctor.timer"], check=False
        )
        print("\n  watchdog: printbridge-doctor.timer installed and started (every 15 minutes).")
        return
    print("\n  watchdog (optional, 2 commands as root on the host):")
    print(f"    sudo install -m 644 {source}/printbridge-doctor.* /etc/systemd/system/")
    print("    sudo systemctl daemon-reload && sudo systemctl enable --now printbridge-doctor.timer")
