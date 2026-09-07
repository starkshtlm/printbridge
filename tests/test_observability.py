"""Job log, sessions, alerts and the digest — spec §18. Metadata always, content never."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from mcp.types import Implementation

from printbridge.admin import collect, render_metrics, render_status_html
from printbridge.notify import Notifier
from printbridge.server import build_server
from printbridge.store import Store


@pytest.fixture
def server(runtime):
    return build_server(runtime.settings, runtime)


async def test_a_successful_job_is_logged_with_person_and_service(runtime, server):
    async with Client(server, client_info=Implementation(name="chatgpt", version="2")) as client:
        await client.call_tool("print_text", {"content": "# Note", "title": "Note"})
    row = runtime.store.recent_jobs(limit=1)[0]
    assert row["client"] == "chatgpt"
    assert row["principal"] == "local"
    assert row["status"] == "queued" and row["pages"] == 1
    assert row["tool"] == "print_text" and row["source_kind"] == "text"


async def test_a_failed_job_is_logged_as_failed(runtime, server):
    runtime.spooler.fail_with = "lp: The printer is not responding"
    with pytest.raises(Exception, match="not responding"):
        async with Client(server) as client:
            await client.call_tool("print_text", {"content": "x"})
    row = runtime.store.recent_jobs(limit=1)[0]
    assert row["status"] == "failed" and "not responding" in row["error"]


async def test_a_cancelled_job_is_logged_as_cancelled(runtime, server):
    async with Client(server) as client:
        printed = (await client.call_tool("print_text", {"content": "x"})).data
        await client.call_tool("cancel_job", {"job_id": printed["job_id"]})
    row = runtime.store.recent_jobs(limit=1)[0]
    assert row["status"] == "cancelled"


def test_two_people_in_the_same_account_never_see_each_others_jobs(runtime):
    """Acceptance (i): two keys pasted into the same Claude account stay separate."""
    store: Store = runtime.store
    store.record_job(principal="daniel", tool="print_text", status="completed", title="Daniel note")
    store.record_job(principal="fanny", tool="print_text", status="completed", title="Fanny note")
    assert [r["title"] for r in store.recent_jobs(principal="fanny")] == ["Fanny note"]
    assert [r["title"] for r in store.recent_jobs(principal="daniel")] == ["Daniel note"]
    assert len(store.recent_jobs()) == 2, "an admin view still sees both"


def test_a_new_service_for_a_person_is_information_not_alarm(runtime):
    first = runtime.store.touch_session(
        session_id="s1", principal="fanny", client="claude-ai", remote_ip="203.0.113.9"
    )
    same = runtime.store.touch_session(
        session_id="s2", principal="fanny", client="chatgpt", remote_ip="203.0.113.9"
    )
    assert first.new_client_for_principal and first.new_origin
    assert same.new_client_for_principal and not same.new_origin, (
        "a new assistant from the same network is expected — she pasted her link somewhere new"
    )


def test_a_rotating_egress_address_is_not_a_new_origin(runtime):
    """Claude Code leaves from a different address almost every request. That is not an intruder."""
    runtime.store.touch_session(
        session_id="s1", principal="daniel", client="claude-code", remote_ip="160.79.106.161"
    )
    same_provider = runtime.store.touch_session(
        session_id="s2", principal="daniel", client="claude-code", remote_ip="160.79.106.175"
    )
    assert not same_provider.new_origin
    assert not same_provider.new_client_for_principal


def test_one_conversation_is_one_session_row_even_when_the_address_moves(runtime):
    from printbridge.server import _stable_session_id

    first = _stable_session_id("daniel", "claude-code", "160.79.106.161", "ua")
    second = _stable_session_id("daniel", "claude-code", "160.79.106.175", "ua")
    assert first == second


def test_a_different_provider_is_still_a_new_origin(runtime):
    runtime.store.touch_session(
        session_id="s1", principal="daniel", client="claude-code", remote_ip="160.79.106.161"
    )
    elsewhere = runtime.store.touch_session(
        session_id="s2", principal="daniel", client="grok", remote_ip="203.0.113.9"
    )
    assert elsewhere.new_origin


def test_network_prefixes():
    from printbridge.store import network_of

    assert network_of("160.79.106.161") == network_of("160.79.106.175") == "160.79.106"
    assert network_of("2001:db8:1234:5678::1") == "2001:db8:1234"
    assert network_of("") == "" and network_of("-") == ""


def test_a_new_service_from_a_new_network_is_the_someone_has_my_link_signal(runtime):
    runtime.store.touch_session(
        session_id="s1", principal="fanny", client="claude-ai", remote_ip="203.0.113.9"
    )
    suspicious = runtime.store.touch_session(
        session_id="s2", principal="fanny", client="grok", remote_ip="198.51.100.4"
    )
    assert suspicious.new_origin


def test_sessions_show_who_is_connected_right_now(runtime, server):
    runtime.store.touch_session(
        session_id="s-now", principal="fanny", client="chatgpt", remote_ip="203.0.113.9"
    )
    active = runtime.store.active_sessions(minutes=15)
    assert active[0]["principal"] == "fanny" and active[0]["client"] == "chatgpt"


def test_retention_removes_old_rows_only(runtime):
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    runtime.store.record_job(principal="daniel", tool="print_text", status="completed", ts=old)
    runtime.store.record_job(principal="daniel", tool="print_text", status="completed")
    removed_jobs, _ = runtime.store.purge(retention_days=365)
    assert removed_jobs == 1
    assert len(runtime.store.recent_jobs()) == 1


def test_the_digest_summarises_per_person_then_per_service(runtime, tmp_path):
    for client, count in (("claude-ai", 3), ("chatgpt", 2)):
        for _ in range(count):
            runtime.store.record_job(
                principal="daniel", client=client, tool="print_text", status="completed", pages=2
            )
    runtime.store.record_job(
        principal="fanny", client="chatgpt", tool="print_text", status="failed", pages=1
    )
    notifier = Notifier(runtime.settings, runtime.store)
    text = notifier.build_digest(datetime.now(UTC) - timedelta(days=7))
    assert "daniel: 5 jobs, 10 pages" in text
    assert "claude-ai: 3" in text and "chatgpt: 2" in text
    assert "fanny" in text


def test_alerts_are_written_to_the_outbox_when_no_webhook_is_configured(runtime):
    notifier = Notifier(runtime.settings, runtime.store)
    assert not notifier.configured
    notifier.send("printer_offline", "the printer is not answering")
    lines = runtime.settings.outbox_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1 and "printer_offline" in lines[0]
    assert "outbox" in notifier.describe()


def test_one_message_per_state_change_and_no_repeats(runtime):
    notifier = Notifier(runtime.settings, runtime.store)
    assert notifier.on_state("printer_offline", "down", "printer is offline")
    assert not notifier.on_state("printer_offline", "down", "printer is offline")
    assert notifier.on_state("printer_offline", "up", "printer is back")
    lines = runtime.settings.outbox_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2


def test_events_not_in_notify_events_stay_silent(runtime):
    runtime.settings.notify_events = "printer_offline"
    notifier = Notifier(runtime.settings, runtime.store)
    assert not notifier.on_state("new_client", "x", "a new assistant appeared")
    assert not runtime.settings.outbox_path.exists()


def test_the_admin_page_renders_without_a_printer(runtime):
    html = render_status_html(runtime)
    assert "printbridge" in html and "Connected now" in html


def test_metrics_are_prometheus_shaped(runtime):
    runtime.store.record_job(
        principal="daniel", client="claude-ai", tool="print_text", status="completed", pages=3
    )
    text = render_metrics(runtime)
    assert "printbridge_up 1" in text
    assert 'printbridge_jobs_total{principal="daniel",client="claude-ai"} 1' in text
    assert "printbridge_sessions_active" in text


def test_the_status_snapshot_holds_together_when_cups_is_down(runtime):
    from printbridge.spool import SpoolError

    def boom(*args, **kwargs):
        raise SpoolError("no CUPS here")

    runtime.spooler.queues = boom
    data = collect(runtime)
    assert data["cups_up"] is False and "no CUPS" in data["queues_error"]
