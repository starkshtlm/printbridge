"""Keep the job log honest about what CUPS actually did (ISS-0122).

The job log used to record `queued` at submission and never look again, so a page that came out
perfectly stayed "queued" forever — and a job only turned `completed` if a client happened to call
`get_job` on it. A print log that reports the moment of submission rather than the outcome answers
the wrong question.

So: before anything *reads* the log — `recent_jobs`, `printbridge status`, `printbridge jobs`,
`doctor`, and every watchdog tick — reconcile the open rows against CUPS. Two `lpstat` calls cover
every job, not one call per row.

Three cases, and the third is the one that needs stating honestly:

1. CUPS still lists the job as pending or printing → `queued` / `printing`.
2. CUPS lists it under completed → `completed`, or `cancelled` / `failed` if the job's own state
   reasons say so.
3. **CUPS has never heard of it.** CUPS keeps finished jobs only for a while, and forgets
   everything when the scheduler restarts. After a grace period we record `completed`, because a
   job that left the queue and was never reported failed did print. It is an inference, and
   `docs/operations.md` says so rather than pretending otherwise.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from printbridge.runtime import Runtime
from printbridge.spool import SpoolError, parse_lpstat_submitted

log = logging.getLogger("printbridge.reconcile")

OPEN_STATES = ("queued", "printing")
DEFAULT_GRACE_SECONDS = 90
# Beyond this, a job id may well have been reused by a restarted scheduler; do not adopt a CUPS
# state for a row that old, only infer that it finished.
MAX_ADOPT_AGE = timedelta(hours=24)


def reconcile_jobs(runtime: Runtime, grace_seconds: int = DEFAULT_GRACE_SECONDS) -> int:
    """Update every open job row from CUPS. Returns how many rows changed. Never raises."""
    store = runtime.store
    try:
        open_rows = store.open_jobs()
    except Exception:  # noqa: BLE001 - reading the log must not break the caller
        return 0
    if not open_rows:
        return 0

    try:
        states = runtime.spooler.job_states()
    except (SpoolError, Exception) as exc:  # noqa: BLE001 - CUPS down is not our caller's problem
        log.debug("could not reach CUPS to reconcile the job log: %s", exc)
        return 0

    now = datetime.now(UTC)
    changed = 0
    for row in open_rows:
        job_id = row.get("job_id")
        if not job_id:
            continue
        submitted = _row_time(row)
        age = (now - submitted) if submitted else timedelta(seconds=grace_seconds + 1)
        live = states.get(job_id)

        if live is not None and age <= MAX_ADOPT_AGE and _same_job(live, submitted):
            new_status = live.state
            error = live.reason if new_status in {"failed", "cancelled"} else ""
        elif age.total_seconds() >= grace_seconds:
            # It is not in the queue and it is not new: it left the queue (case 3).
            new_status = "completed"
            error = ""
        else:
            continue

        if new_status != row["status"]:
            store.update_job(row["id"], status=new_status, error=error or row.get("error", ""))
            changed += 1
    return changed


def _row_time(row: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(row["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _same_job(live, submitted: datetime | None) -> bool:
    """Guard against a restarted scheduler handing out `home-1` a second time.

    If CUPS gives a submission time and it is more than an hour from the row's own timestamp, this
    is a different job that happens to share an id — do not adopt its state.
    """
    if submitted is None or not live.submitted:
        return True
    cups_time = parse_lpstat_submitted(live.submitted)
    if cups_time is None:
        return True
    return abs((cups_time - submitted).total_seconds()) < 3600


def reconcile_quietly(runtime: Runtime) -> int:
    """For call sites that only want the side effect."""
    try:
        return reconcile_jobs(runtime)
    except Exception:  # noqa: BLE001
        return 0
