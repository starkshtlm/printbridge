"""The tools, through a real FastMCP client, with CUPS faked.

These are the tests that would have caught every guard being wrong: page ceilings, the confirm
threshold, dry runs that must not spool, per-key printer restrictions and rate limits.
"""

from __future__ import annotations

import base64

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Implementation

from printbridge.auth import Principal
from printbridge.server import build_server
from tests.conftest import make_caller

LONG = "\n\n".join(f"## Section {i}\n\n" + ("filler " * 80) for i in range(80))


@pytest.fixture
def server(runtime):
    return build_server(runtime.settings, runtime)


async def call(server, tool, **kwargs):
    async with Client(server) as client:
        result = await client.call_tool(tool, kwargs)
        return result.data


async def test_print_text_renders_counts_and_spools(server, runtime):
    data = await call(server, "print_text", content="# Hello\n\nworld", title="Hello")
    assert data["pages"] == 1
    assert data["printer"] == "home"
    assert data["job_id"] == "home-1"
    submitted = runtime.spooler.submitted[0]
    assert "-d" in submitted and submitted[submitted.index("-d") + 1] == "home"
    assert "sides=two-sided-long-edge" in submitted
    assert "print-color-mode=monochrome" in submitted


async def test_defaults_can_be_overridden_per_job(server, runtime):
    await call(
        server, "print_text", content="x", copies=3, duplex=False, color=True, paper="Letter"
    )
    args = runtime.spooler.submitted[0]
    assert args[args.index("-n") + 1] == "3"
    assert "sides=one-sided" in args
    assert "print-color-mode=color" in args
    assert "media=na_letter_8.5x11in" in args


async def test_dry_run_never_reaches_the_printer(server, runtime):
    data = await call(server, "print_text", content="# Hi", dry_run=True)
    assert data["dry_run"] is True and data["pages"] == 1
    assert runtime.spooler.submitted == []


async def test_a_long_document_needs_confirmation(server, runtime):
    with pytest.raises(ToolError, match="confirmed=true"):
        await call(server, "print_text", content=LONG, title="Long")
    assert runtime.spooler.submitted == []


async def test_a_long_document_prints_once_confirmed(server, runtime):
    data = await call(server, "print_text", content=LONG, title="Long", confirmed=True)
    assert data["pages"] > 10
    assert len(runtime.spooler.submitted) == 1


async def test_max_pages_is_a_hard_refusal(runtime):
    runtime.settings.max_pages = 3
    runtime.settings.confirm_above_pages = 2
    server = build_server(runtime.settings, runtime)
    with pytest.raises(ToolError, match="above MAX_PAGES"):
        await call(server, "print_text", content=LONG, confirmed=True)
    assert runtime.spooler.submitted == []


async def test_a_key_limited_to_one_printer_is_refused_another(runtime, monkeypatch):
    import printbridge.server as server_module

    limited = Principal(user="fanny", role="user", key_id="k_f", printers=("home",))
    monkeypatch.setattr(
        server_module, "describe_caller", lambda ctx: make_caller(principal=limited)
    )
    server = build_server(runtime.settings, runtime)
    with pytest.raises(ToolError, match="may only print to"):
        await call(server, "print_text", content="x", printer="office")
    assert await call(server, "print_text", content="x", printer="home")


async def test_a_per_key_page_limit_beats_the_global_one(runtime, monkeypatch):
    import printbridge.server as server_module

    limited = Principal(user="fanny", role="user", key_id="k_f", max_pages=2)
    monkeypatch.setattr(
        server_module, "describe_caller", lambda ctx: make_caller(principal=limited)
    )
    server = build_server(runtime.settings, runtime)
    with pytest.raises(ToolError, match="your key's limit"):
        await call(server, "print_text", content=LONG, confirmed=True)


async def test_rate_limit_stops_the_twenty_first_job(runtime, monkeypatch):
    import printbridge.server as server_module

    principal = Principal(user="fanny", role="user", key_id="k_f", rate_per_hour=3)
    monkeypatch.setattr(
        server_module, "describe_caller", lambda ctx: make_caller(principal=principal)
    )
    server = build_server(runtime.settings, runtime)
    for _ in range(3):
        await call(server, "print_text", content="x")
    with pytest.raises(ToolError, match="Rate limit"):
        await call(server, "print_text", content="x")
    assert len(runtime.spooler.submitted) == 3
    assert any(event == "rate_limit" for event, _ in runtime.notifier.sent)


async def test_unknown_printer_is_named_with_the_alternatives(server):
    with pytest.raises(ToolError, match="Configured printers: home, office"):
        await call(server, "print_text", content="x", printer="garage")


async def test_more_than_twenty_copies_is_refused(server):
    with pytest.raises(ToolError, match="20 copies"):
        await call(server, "print_text", content="x", copies=99)


async def test_print_file_prints_a_pdf_unchanged(server, runtime, tmp_path):
    from printbridge.render import render_to_pdf

    pdf = render_to_pdf("# From a file", tmp_path / "f.pdf", title="f")
    encoded = base64.b64encode(pdf.path.read_bytes()).decode()
    data = await call(server, "print_file", filename="f.pdf", content_base64=encoded)
    assert data["pages"] == 1 and data["job_id"].startswith("home-")


async def test_print_file_renders_markdown_and_text(server):
    encoded = base64.b64encode(b"# Title\n\nbody").decode()
    data = await call(server, "print_file", filename="note.md", content_base64=encoded)
    assert data["pages"] == 1


async def test_print_file_rejects_rubbish_base64(server):
    with pytest.raises(ToolError, match="not valid base64"):
        await call(server, "print_file", filename="x.pdf", content_base64="!!!not base64!!!")


async def test_list_printers_reports_state_and_default(server):
    data = await call(server, "list_printers")
    names = {p["name"]: p for p in data["printers"]}
    assert set(names) == {"home", "office"}
    assert names["home"]["default"] is True
    assert names["home"]["state"] == "idle"


async def test_printer_status_answers_when_a_print_did_not_appear(server):
    data = await call(server, "printer_status")
    assert data["printer"] == "home" and data["online"] is True
    assert "Test Printer 9000" in data["model"]


async def test_get_job_and_cancel_job(server):
    printed = await call(server, "print_text", content="x")
    job = await call(server, "get_job", job_id=printed["job_id"])
    assert job["job_id"] == printed["job_id"]
    cancelled = await call(server, "cancel_job", job_id=printed["job_id"])
    assert "cancelled" in cancelled["message"].lower() or cancelled["cancelled"] is False


async def test_a_failing_printer_is_reported_not_swallowed(server, runtime):
    runtime.spooler.fail_with = "lp: Destination home is not accepting jobs"
    with pytest.raises(ToolError, match="not accepting jobs"):
        await call(server, "print_text", content="x")
    rows = runtime.store.recent_jobs(limit=5)
    assert rows[0]["status"] == "failed"
    assert any(event == "printer_offline" for event, _ in runtime.notifier.sent)


async def test_the_spool_file_is_deleted_after_the_job(server, runtime):
    await call(server, "print_text", content="x")
    leftovers = list(runtime.settings.spool_path.glob("pb-*"))
    assert leftovers == [], "KEEP_SPOOL_HOURS=0 means the document does not linger"


async def test_the_job_log_records_metadata_and_no_content(server, runtime):
    await call(server, "print_text", content="secret sauce recipe", title="Dinner")
    row = runtime.store.recent_jobs(limit=1)[0]
    assert row["title"] == "Dinner" and row["pages"] == 1 and row["printer"] == "home"
    assert "secret sauce" not in str(row)


async def test_the_assistant_is_detected_from_the_handshake(runtime):
    """One key, two assistants: same person, different `client` — nobody configures anything."""
    server = build_server(runtime.settings, runtime)
    for name in ("claude-ai", "chatgpt"):
        async with Client(server, client_info=Implementation(name=name, version="1")) as client:
            await client.call_tool("print_text", {"content": "hi"})
    clients = {row["client"] for row in runtime.store.recent_jobs(limit=10)}
    principals = {row["principal"] for row in runtime.store.recent_jobs(limit=10)}
    assert clients == {"claude-ai", "chatgpt"}
    assert principals == {"local"}, "stdio/in-memory callers are the local admin principal"


async def test_recent_jobs_is_scoped_to_the_caller(runtime, monkeypatch):
    import printbridge.server as server_module

    server = build_server(runtime.settings, runtime)
    for person in ("daniel", "fanny"):
        principal = Principal(user=person, role="user", key_id=f"k_{person}")
        monkeypatch.setattr(
            server_module, "describe_caller", lambda ctx, p=principal: make_caller(principal=p)
        )
        await call(server, "print_text", content=f"{person} note", title=f"{person} note")

    fanny = Principal(user="fanny", role="user", key_id="k_fanny")
    monkeypatch.setattr(
        server_module, "describe_caller", lambda ctx: make_caller(principal=fanny)
    )
    mine = await call(server, "recent_jobs")
    assert [j["title"] for j in mine["jobs"]] == ["fanny note"]

    admin = Principal(user="daniel", role="admin", key_id="k_admin")
    monkeypatch.setattr(
        server_module, "describe_caller", lambda ctx: make_caller(principal=admin)
    )
    everything = await call(server, "recent_jobs")
    assert {j["title"] for j in everything["jobs"]} == {"fanny note", "daniel note"}


async def test_tools_are_all_present_and_described_for_an_assistant(server):
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "print_text",
        "print_url",
        "print_file",
        "list_printers",
        "get_job",
        "cancel_job",
        "printer_status",
        "recent_jobs",
    }
    assert "user asks" in tools["print_text"].description
