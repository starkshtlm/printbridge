"""ISS-0122: the job log must report what CUPS did, not what we hoped at submission time.

Before this, a page that came out perfectly stayed "queued" for ever, and a job only turned
"completed" if a client happened to call `get_job` on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client

from printbridge.reconcile import reconcile_jobs
from printbridge.server import build_server
from printbridge.spool import parse_lpstat_jobs, parse_lpstat_submitted


@pytest.fixture
def server(runtime):
    return build_server(runtime.settings, runtime)


async def test_a_finished_job_reads_completed_without_anyone_calling_get_job(runtime, server):
    """The regression this whole module exists for."""
    async with Client(server) as client:
        printed = (await client.call_tool("print_text", {"content": "x", "title": "Note"})).data
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "queued"

    runtime.spooler.finish(printed["job_id"], "completed")

    async with Client(server) as client:
        listed = (await client.call_tool("recent_jobs", {})).data["jobs"]
    assert listed[0]["status"] == "completed"
    assert not any(call[0] == "cancel" for call in runtime.spooler.calls)
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "completed"


async def test_a_job_still_printing_says_printing(runtime, server):
    async with Client(server) as client:
        printed = (await client.call_tool("print_text", {"content": "x"})).data
    runtime.spooler.finish(printed["job_id"], "printing")
    assert reconcile_jobs(runtime) == 1
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "printing"


@pytest.mark.parametrize("cups_state,expected", [("cancelled", "cancelled"), ("failed", "failed")])
async def test_cancelled_and_aborted_jobs_are_not_reported_as_printed(
    runtime, server, cups_state, expected
):
    async with Client(server) as client:
        printed = (await client.call_tool("print_text", {"content": "x"})).data
    runtime.spooler.finish(printed["job_id"], cups_state)
    reconcile_jobs(runtime)
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == expected


def test_a_job_cups_has_forgotten_counts_as_printed_after_the_grace_period(runtime):
    """CUPS keeps finished jobs only for a while. Absent and old means it left the queue."""
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    runtime.store.record_job(
        principal="daniel", tool="print_text", status="queued", job_id="home-99", ts=old
    )
    assert reconcile_jobs(runtime) == 1
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "completed"


def test_a_job_submitted_seconds_ago_is_left_alone(runtime):
    """Absent from CUPS *and new* means the scheduler has not caught up — not that it printed."""
    runtime.store.record_job(
        principal="daniel", tool="print_text", status="queued", job_id="home-100"
    )
    assert reconcile_jobs(runtime, grace_seconds=600) == 0
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "queued"


def test_a_reused_job_id_from_a_restarted_scheduler_is_not_adopted(runtime):
    """After a cupsd restart, `home-1` refers to somebody else's job. Do not copy its state."""
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    runtime.store.record_job(
        principal="daniel", tool="print_text", status="queued", job_id="home-1", ts=long_ago
    )
    runtime.spooler.finish("home-1", "printing")  # a *new* home-1, printing right now
    reconcile_jobs(runtime)
    status = runtime.store.recent_jobs(limit=1)[0]["status"]
    assert status == "completed", "the old row must be closed by inference, not by the new job"


def test_terminal_rows_are_never_touched_again(runtime):
    runtime.store.record_job(
        principal="daniel", tool="print_text", status="failed", job_id="home-7", error="paper jam"
    )
    assert reconcile_jobs(runtime) == 0
    row = runtime.store.recent_jobs(limit=1)[0]
    assert row["status"] == "failed" and row["error"] == "paper jam"


def test_reconciliation_survives_cups_being_down(runtime):
    from printbridge.spool import SpoolError

    runtime.store.record_job(
        principal="daniel", tool="print_text", status="queued", job_id="home-5"
    )

    def boom(*args, **kwargs):
        raise SpoolError("no CUPS here")

    runtime.spooler.job_states = boom
    assert reconcile_jobs(runtime) == 0
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "queued"


def test_status_and_jobs_reconcile_before_they_print(runtime, server, capsys):
    import argparse

    from printbridge.__main__ import cmd_jobs

    runtime.store.record_job(
        principal="daniel",
        tool="print_text",
        status="queued",
        job_id="home-42",
        ts=(datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds"),
    )
    args = argparse.Namespace(
        since="7d", principal=None, printer=None, limit=10, csv=False, env_file=None
    )
    import printbridge.__main__ as main_module

    main_module._runtime = lambda _args: runtime  # noqa: SLF001
    cmd_jobs(args)
    assert "completed" in capsys.readouterr().out


def test_doctor_reports_the_job_log(runtime):
    from printbridge.doctor import run_doctor

    runtime.store.record_job(
        principal="daniel",
        tool="print_text",
        status="queued",
        job_id="home-8",
        ts=(datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds"),
    )
    report = run_doctor(runtime)
    entry = next(c for c in report.checks if c.name == "job log")
    assert "reconciled" in entry.detail
    assert runtime.store.recent_jobs(limit=1)[0]["status"] == "completed"


# --- the parser, against output captured from a real CUPS 2.4 ---


def test_parsing_a_real_completed_listing():
    listing = (
        "home-1                  unknown          14336   Mon Sep  7 09:53:06 2026\n"
        "\tStatus: \n"
        "\tAlerts: processing-to-stop-point\n"
        "\tqueued for home\n"
    )
    jobs = parse_lpstat_jobs(listing, "completed")
    assert jobs["home-1"].state == "completed"
    assert jobs["home-1"].printer == "home"


def test_parsing_a_real_pending_listing():
    listing = (
        "home-2                  daniel           9000   Mon Sep  7 09:59:00 2026\n"
        "\tStatus: Connected to printer.\n"
        "\tAlerts: job-printing\n"
        "\tqueued for home\n"
        "home-3                  daniel           9000   Mon Sep  7 09:59:10 2026\n"
        "\tqueued for home\n"
    )
    jobs = parse_lpstat_jobs(listing, "not-completed")
    assert jobs["home-2"].state == "printing"
    assert jobs["home-3"].state == "queued"


def test_the_cups_timestamp_format_parses():
    when = parse_lpstat_submitted("Mon Sep  7 09:53:06 2026")
    assert when is not None and when.year == 2026 and when.hour == 9
    assert parse_lpstat_submitted("nonsense") is None
