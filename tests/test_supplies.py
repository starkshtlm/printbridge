"""Toner and ink: report the level, warn once when it goes low, and never nag."""

from __future__ import annotations

import pytest
from fastmcp import Client

from printbridge.doctor import _alert_on_supplies, collect_supplies, run_doctor
from printbridge.notify import Notifier
from printbridge.server import build_server
from printbridge.spool import parse_supplies

# Captured from a Canon LBP673C over IPP: black at 5 %, the colours at 58 %.
CANON = {
    "printer-make-and-model": "Canon LBP673C",
    "document-format-supported": "application/pdf,image/urf",
    "sides-supported": "one-sided,two-sided-long-edge",
    "print-color-mode-supported": "color,monochrome",
    "printer-state-reasons": "toner-low-warning",
    "marker-names": (
        "Canon Cartridge 069 Black Toner,Canon Cartridge 069 Cyan Toner,"
        "Canon Cartridge 069 Magenta To,Canon Cartridge 069 Yellow Ton"
    ),
    "marker-levels": "5,58,58,58",
    "marker-high-levels": "100,100,100,100",
    "marker-low-levels": "7,7,7,7",
    "marker-types": "toner,toner,toner,toner",
}


def test_real_printer_attributes_parse_into_supplies():
    supplies = parse_supplies(CANON)
    assert [s.level for s in supplies] == [5, 58, 58, 58]
    low = [s for s in supplies if s.is_low]
    assert len(low) == 1 and "Black" in low[0].name


def test_levels_are_normalised_against_the_printers_own_high_level():
    supplies = parse_supplies(
        {"marker-names": "Ink", "marker-levels": "20", "marker-high-levels": "200"}
    )
    assert supplies[0].level == 10


@pytest.mark.parametrize(
    "attrs",
    [
        {"marker-names": "Waste Toner Box", "marker-levels": "95", "marker-types": "waste-toner"},
        {"marker-names": "Unknown", "marker-levels": "-1"},
        {"marker-names": "Broken", "marker-levels": "not-a-number"},
        {},
    ],
)
def test_things_that_cannot_be_reported_are_skipped_not_guessed(attrs):
    assert parse_supplies(attrs) == []


def test_doctor_warns_once_the_toner_is_low(runtime):
    runtime.spooler.attrs = CANON
    report = run_doctor(runtime)
    entry = next(c for c in report.checks if c.name.startswith("supplies"))
    assert entry.status == "warn"
    assert "5%" in entry.detail
    assert "order a replacement" in entry.fix.lower()


def test_doctor_is_quiet_when_everything_is_full(runtime):
    runtime.spooler.attrs = {**CANON, "marker-levels": "80,58,58,58"}
    report = run_doctor(runtime)
    entry = next(c for c in report.checks if c.name.startswith("supplies"))
    assert entry.status == "ok"


def test_a_mock_queue_reports_no_supplies(runtime):
    runtime.settings.printers = "home=cups-pdf:/"
    assert collect_supplies(runtime) == {}


def test_one_alert_when_it_goes_low_and_one_when_it_is_replaced(runtime):
    runtime.settings.printers = "home=ipp://192.0.2.50/ipp/print"
    runtime.spooler.attrs = CANON
    _alert_on_supplies(runtime)
    _alert_on_supplies(runtime)  # still low: must stay silent
    assert [e for e, _ in runtime.notifier.sent] == ["supply_low"]
    assert "5%" in runtime.notifier.sent[0][1]

    runtime.spooler.attrs = {**CANON, "marker-levels": "90,58,58,58"}
    _alert_on_supplies(runtime)
    assert len(runtime.notifier.sent) == 2
    assert "back above the threshold" in runtime.notifier.sent[1][1]


def test_each_printer_gets_its_own_alert_memory(runtime):
    """Two printers are two subjects; keyed by event alone they would alert on every check."""
    runtime.spooler.attrs = CANON  # both `home` and `office` report low black toner
    _alert_on_supplies(runtime)
    first_round = len(runtime.notifier.sent)
    _alert_on_supplies(runtime)
    assert first_round == 2, "one message per printer"
    assert len(runtime.notifier.sent) == 2, "and then silence while nothing changes"


def test_supply_low_is_on_by_default(runtime):
    assert "supply_low" in runtime.settings.notify_event_set
    from printbridge.notify import EVENT_LEVELS

    assert EVENT_LEVELS["supply_low"] == "warning"


def test_an_operator_can_turn_it_off(runtime):
    runtime.settings.notify_events = "printer_offline"
    notifier = Notifier(runtime.settings, runtime.store)
    assert not notifier.on_state("supply_low", "home:black", "black toner is at 5%")


async def test_printer_status_tells_the_assistant_about_low_toner(runtime):
    runtime.spooler.attrs = CANON
    server = build_server(runtime.settings, runtime)
    async with Client(server) as client:
        data = (await client.call_tool("printer_status", {})).data
    assert data["supplies"]["Canon Cartridge 069 Black Toner"] == "5%"
    assert data["supplies_low"] == ["Canon Cartridge 069 Black Toner"]
    assert "Running low" in data["message"]


def test_a_placeholder_webhook_is_not_a_configured_webhook(runtime):
    """"webhook OK" used to mean "the string is not empty", which is not the same thing."""
    runtime.settings.notify_webhook = "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>"
    notifier = Notifier(runtime.settings, runtime.store)
    assert notifier.is_placeholder and not notifier.configured
    runtime.notifier = notifier
    report = run_doctor(runtime)
    entry = next(c for c in report.checks if c.name == "notifications")
    assert entry.status == "fail" and "placeholder" in entry.detail


def test_a_real_webhook_url_is_accepted(runtime):
    runtime.settings.notify_webhook = "https://api.telegram.org/bot123:abc/sendMessage?chat_id=42"
    notifier = Notifier(runtime.settings, runtime.store)
    assert notifier.configured and not notifier.is_placeholder
    assert notifier.is_telegram
