"""The CUPS side: submit a PDF, ask about a job, cancel a job, list queues.

We shell out to `lp`, `lpstat` and `cancel` rather than binding libcups. Two reasons: the CUPS
command line is the most stable interface CUPS has, and it lets the printbridge container stay a
plain Python image that talks to a separate CUPS container over `CUPS_SERVER`.

Every argument is passed as a list element — never through a shell — because titles and printer
names arrive from an LLM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

JOB_ID_RE = re.compile(r"request id is (\S+)")
_STATE_ORDER = ("pending", "held", "processing", "printing", "stopped", "canceled", "aborted", "completed")


class SpoolError(RuntimeError):
    """CUPS refused, or is not reachable. The message tells the operator what to do."""


@dataclass(frozen=True)
class PrintOptions:
    copies: int = 1
    duplex: bool = True
    color: bool = False
    paper: str = "A4"
    title: str = ""

    def to_lp_args(self) -> list[str]:
        args: list[str] = []
        if self.copies and self.copies != 1:
            args += ["-n", str(int(self.copies))]
        args += ["-o", "sides=two-sided-long-edge" if self.duplex else "sides=one-sided"]
        args += ["-o", "print-color-mode=color" if self.color else "print-color-mode=monochrome"]
        media = "na_letter_8.5x11in" if self.paper == "Letter" else "iso_a4_210x297mm"
        args += ["-o", f"media={media}"]
        if self.title:
            args += ["-t", self.title[:120]]
        return args


@dataclass
class QueueInfo:
    name: str
    state: str = "unknown"  # idle | processing | stopped | unknown
    reason: str = ""
    is_default: bool = False
    accepting: bool = True
    device_uri: str = ""
    jobs_queued: int = 0

    @property
    def is_mock(self) -> bool:
        return self.device_uri.startswith("cups-pdf")


@dataclass
class JobInfo:
    job_id: str
    state: str = "unknown"  # queued | printing | completed | failed | cancelled | unknown
    printer: str = ""
    size: str = ""
    submitted: str = ""
    reason: str = ""


@dataclass
class Spooler:
    """Thin, testable wrapper. Tests replace `_run`; nothing else knows about subprocesses."""

    cups_server: str = "cups:631"
    timeout: int = 45
    env_extra: dict[str, str] = field(default_factory=dict)

    # --- plumbing ---

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.cups_server:
            env["CUPS_SERVER"] = self.cups_server
        env.update(self.env_extra)
        return env

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        binary = shutil.which(args[0])
        if binary is None:
            raise SpoolError(
                f"`{args[0]}` is not installed in this container. The printbridge image ships "
                "cups-client; if you run printbridge outside Docker, install it "
                "(`apt install cups-client`)."
            )
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, list args, no shell
                [binary, *args[1:]],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpoolError(
                f"CUPS did not answer within {self.timeout}s (CUPS_SERVER={self.cups_server}). "
                "Is the cups container running? `docker compose ps`"
            ) from exc
        if check and proc.returncode != 0:
            raise SpoolError(_friendly_cups_error(args, proc))
        return proc

    # --- operations ---

    def submit(self, pdf_path: Path, printer: str, options: PrintOptions) -> str:
        """Hand a rendered PDF to a queue. Returns the CUPS job id, e.g. `home-42`."""
        if not Path(pdf_path).exists():
            raise SpoolError(f"Nothing to spool: {pdf_path} does not exist.")
        proc = self._run(["lp", "-d", printer, *options.to_lp_args(), str(pdf_path)])
        match = JOB_ID_RE.search(proc.stdout or "")
        if not match:
            raise SpoolError(
                f"CUPS accepted the file but returned no job id: {(proc.stdout or '').strip()!r}"
            )
        return match.group(1)

    def queues(self) -> list[QueueInfo]:
        """Every configured queue with its state, default flag and device URI."""
        proc = self._run(["lpstat", "-l", "-p", "-d"], check=False)
        if proc.returncode != 0 and not (proc.stdout or "").strip():
            raise SpoolError(_friendly_cups_error(["lpstat"], proc))
        return parse_lpstat_printers(proc.stdout or "")

    def job(self, job_id: str) -> JobInfo:
        """State of one job. CUPS forgets completed jobs eventually; then state is `completed`."""
        pending = self._run(["lpstat", "-W", "not-completed", "-l", "-o"], check=False)
        info = _find_job(pending.stdout or "", job_id)
        if info:
            return info
        done = self._run(["lpstat", "-W", "completed", "-l", "-o"], check=False)
        info = _find_job(done.stdout or "", job_id)
        if info:
            info.state = info.state if info.state in {"cancelled", "failed"} else "completed"
            return info
        return JobInfo(
            job_id=job_id,
            state="completed",
            reason="not in the queue any more — CUPS keeps finished jobs only for a while",
        )

    def cancel(self, job_id: str) -> bool:
        proc = self._run(["cancel", job_id], check=False)
        if proc.returncode == 0:
            return True
        text = (proc.stderr or proc.stdout or "").lower()
        if "already" in text or "not found" in text or "no such job" in text:
            return False
        raise SpoolError(_friendly_cups_error(["cancel", job_id], proc))

    def queue_depth(self, printer: str | None = None) -> int:
        proc = self._run(["lpstat", "-W", "not-completed", "-o"], check=False)
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if printer:
            lines = [ln for ln in lines if ln.split("-")[0] == printer or ln.startswith(printer + "-")]
        return len(lines)

    def reachable(self) -> bool:
        try:
            proc = self._run(["lpstat", "-r"], check=False)
        except SpoolError:
            return False
        return "is running" in (proc.stdout or "")

    def printer_attributes(self, uri: str) -> dict[str, str]:
        """IPP attributes straight from the printer — the honest answer to 'will this work?'.

        Empty dict when `ipptool` is missing or the printer does not answer; the caller reports
        that as "could not query", never as "unsupported".
        """
        if shutil.which("ipptool") is None or not uri.startswith(("ipp://", "ipps://")):
            return {}
        try:
            proc = self._run(["ipptool", "-tv", uri, "get-printer-attributes.test"], check=False)
        except SpoolError:
            return {}
        return parse_ipptool(proc.stdout or "")


def _find_job(listing: str, job_id: str) -> JobInfo | None:
    for line in listing.splitlines():
        line = line.rstrip()
        if not line or not line.startswith(job_id):
            continue
        parts = line.split()
        if parts[0] != job_id:
            continue
        state = "printing" if "processing" in line.lower() else "queued"
        low = line.lower()
        if "cancel" in low:
            state = "cancelled"
        elif "abort" in low or "held" in low:
            state = "failed"
        return JobInfo(
            job_id=job_id,
            state=state,
            printer=job_id.rsplit("-", 1)[0],
            size=parts[2] if len(parts) > 2 else "",
            submitted=" ".join(parts[3:7]) if len(parts) > 6 else "",
        )
    return None


def parse_lpstat_printers(text: str) -> list[QueueInfo]:
    """Parse `lpstat -l -p -d`. Written against real CUPS output, tolerant of missing lines."""
    queues: dict[str, QueueInfo] = {}
    current: QueueInfo | None = None
    default_name = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        low = line.strip().lower()
        if low.startswith("system default destination:"):
            default_name = line.split(":", 1)[1].strip()
            continue
        m = re.match(r"printer (\S+) (?:is|now) ?(.*)$", line.strip())
        if m:
            name, rest = m.group(1), m.group(2).lower()
            state = "idle" if "idle" in rest else "processing" if "printing" in rest else "unknown"
            if "disabled" in rest or "stopped" in rest:
                state = "stopped"
            current = QueueInfo(name=name, state=state)
            queues[name] = current
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.lower().startswith("device uri:"):
            current.device_uri = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("reasons:"):
            reason = stripped.split(":", 1)[1].strip()
            current.reason = "" if reason in {"none", "-"} else reason
        elif stripped.lower().startswith("rejecting jobs"):
            current.accepting = False
    for name, q in queues.items():
        q.is_default = name == default_name
    return list(queues.values())


def parse_ipptool(text: str) -> dict[str, str]:
    """Pull the attributes `doctor` reports on out of ipptool's verbose output."""
    wanted = {
        "printer-make-and-model",
        "printer-state",
        "document-format-supported",
        "sides-supported",
        "print-color-mode-supported",
        "media-default",
        "printer-is-accepting-jobs",
        "marker-levels",
        "marker-names",
        "printer-state-reasons",
    }
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        for key in wanted:
            if stripped.startswith(key):
                value = stripped[len(key) :].strip()
                value = re.sub(r"^\([^)]*\)\s*=?\s*", "", value).strip()
                if value.startswith("="):
                    value = value[1:].strip()
                if value:
                    out.setdefault(key, value)
    return out


def _friendly_cups_error(args: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    text = ((proc.stderr or "") + (proc.stdout or "")).strip()
    low = text.lower()
    hint = ""
    if "connection refused" in low or "unable to connect" in low or "bad file descriptor" in low:
        hint = " — CUPS is not answering. Check `docker compose ps cups` and CUPS_SERVER."
    elif "unknown destination" in low or "does not exist" in low:
        hint = " — that queue does not exist. `printbridge doctor` lists the queues that do."
    elif "not accepting" in low:
        hint = " — the queue is rejecting jobs. Check the printer, then `cupsenable <queue>`."
    return f"`{' '.join(args[:3])}` failed: {text or f'exit {proc.returncode}'}{hint}"


def state_rank(state: str) -> int:
    return _STATE_ORDER.index(state) if state in _STATE_ORDER else -1
