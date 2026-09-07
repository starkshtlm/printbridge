"""Shared fixtures. Nothing here touches a real printer, a real network or the real `.env`."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Never let a developer's own .env leak into a test run.
os.environ.setdefault("PRINTBRIDGE_ENV_FILE", "/nonexistent-printbridge-env")

from printbridge.auth import KeyAuth, Principal  # noqa: E402
from printbridge.config import Settings, load_settings  # noqa: E402
from printbridge.keys import KeyStore  # noqa: E402
from printbridge.runtime import RateLimiter, Runtime  # noqa: E402
from printbridge.server import CallerInfo  # noqa: E402
from printbridge.spool import Spooler  # noqa: E402
from printbridge.store import Store  # noqa: E402


class FakeSpooler(Spooler):
    """A CUPS that always answers, records what it was asked, and prints nothing."""

    def __init__(self, **kwargs):
        super().__init__(cups_server="fake:631")
        self.calls: list[list[str]] = []
        self.next_job = 1
        self.finished: dict[str, str] = {}
        self.attrs: dict[str, str] | None = None
        self.fail_with: str | None = None
        self.queues_text = (
            "printer home is idle.  enabled since Sat 06 Sep 2026 10:00:00 PM CEST\n"
            "\tDevice URI: ipp://192.0.2.50/ipp/print\n"
            "\tReasons: none\n"
            "printer office is idle.  enabled since Sat 06 Sep 2026 10:00:00 PM CEST\n"
            "\tDevice URI: ipp://192.0.2.60/ipp/print\n"
            "\tReasons: none\n"
            "system default destination: home\n"
        )

    def finish(self, job_id: str, state: str = "completed") -> None:
        """Pretend CUPS printed (or cancelled, or aborted) a job we submitted."""
        self.finished[job_id] = state

    def _run(self, args: list[str], *, check: bool = True):  # type: ignore[override]
        self.calls.append(list(args))
        stdout, code = "", 0
        if self.fail_with and args[0] == "lp":
            from printbridge.spool import SpoolError

            raise SpoolError(self.fail_with)
        if args[0] == "lp":
            printer = args[args.index("-d") + 1]
            stdout = f"request id is {printer}-{self.next_job} (1 file(s))\n"
            self.next_job += 1
        elif args[0] == "lpstat":
            if "-r" in args:
                stdout = "scheduler is running\n"
            elif "-p" in args:
                stdout = self.queues_text
            elif "-o" in args:
                window = args[args.index("-W") + 1] if "-W" in args else "not-completed"
                wanted = {
                    "completed": lambda st: st in {"completed", "cancelled", "failed"},
                    "not-completed": lambda st: st in {"queued", "printing"},
                    "all": lambda st: True,
                }[window]
                rows = []
                for job_id, state in self.finished.items():
                    if not wanted(state):
                        continue
                    stamp = datetime.now(UTC).strftime("%a %b %e %H:%M:%S %Y")
                    rows.append(f"{job_id}   daniel   9000   {stamp}")
                    if state == "cancelled":
                        rows.append("\tAlerts: job-canceled-by-user")
                    elif state == "failed":
                        rows.append("\tAlerts: job-aborted-by-system")
                    elif state == "printing":
                        rows.append("\tAlerts: job-printing")
                stdout = "\n".join(rows) + ("\n" if rows else "")
        elif args[0] == "cancel":
            stdout = ""
        return subprocess.CompletedProcess(args, code, stdout, "")

    def printer_attributes(self, uri: str) -> dict[str, str]:  # type: ignore[override]
        if uri.startswith("cups-pdf"):
            return {}
        if self.attrs is not None:
            return self.attrs
        return {
            "printer-make-and-model": "Test Printer 9000",
            "document-format-supported": "application/pdf,image/urf",
            "sides-supported": "one-sided,two-sided-long-edge",
            "print-color-mode-supported": "color,monochrome",
        }

    @property
    def submitted(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "lp"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        PRINTERS="home=ipp://192.0.2.50/ipp/print,office=ipp://192.0.2.60/ipp/print",
        EXPOSE="none",
        AUTH_MODE="keys",
        PUBLIC_URL="",
        PRINTBRIDGE_DATA_DIR=str(tmp_path / "data"),
        CONFIRM_ABOVE_PAGES="10",
        MAX_PAGES="100",
        RATE_LIMIT_JOBS_PER_HOUR="30",
        KEEP_SPOOL_HOURS="0",
    )


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    keystore = KeyStore(settings.keys_path)
    return Runtime(
        settings=settings,
        keys=keystore,
        spooler=FakeSpooler(),
        store=Store(settings.db_path),
        auth=KeyAuth(store=keystore, grace_minutes=settings.key_grace_minutes),
        rate=RateLimiter(),
        notifier=_SilentNotifier(),
    )


class _SilentNotifier:
    """Records alerts instead of sending them, so tests can assert on them."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.states: dict[str, str] = {}

    def send(self, event: str, text: str, **extra) -> bool:
        self.sent.append((event, text))
        return True

    def on_state(self, event: str, state: str, text: str, *, key: str | None = None, **extra) -> bool:
        memory_key = key or event
        if self.states.get(memory_key) == state:
            return False
        self.states[memory_key] = state
        self.sent.append((event, text))
        return True

    def describe(self) -> str:
        return "test notifier"

    @property
    def configured(self) -> bool:
        return True


@pytest.fixture
def caller() -> CallerInfo:
    return CallerInfo(
        principal=Principal(user="daniel", role="admin", method="key-path", key_id="k_test"),
        client="claude-ai",
        client_version="1.0",
        session_id="sess-test",
        remote_ip="203.0.113.5",
    )


def make_caller(**kwargs) -> CallerInfo:
    base = {
        "principal": Principal(user="daniel", role="user", method="key-path", key_id="k_test"),
        "client": "claude-ai",
        "client_version": "1.0",
        "session_id": "sess-test",
        "remote_ip": "203.0.113.5",
    }
    base.update(kwargs)
    return CallerInfo(**base)
