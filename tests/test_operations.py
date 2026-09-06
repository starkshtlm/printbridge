"""B4c: the wizard, the backup, the watchdog and the retry policy that holds jobs."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

import pytest

from printbridge.config import load_settings
from printbridge.init_wizard import _normalise_printer, probe_printer, render_env, run_init

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRINTBRIDGE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("PRINTBRIDGE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _init_args(**kwargs) -> argparse.Namespace:
    base = {
        "printer": None,
        "name": "home",
        "expose": None,
        "public_url": None,
        "key": None,
        "mock": False,
        "non_interactive": True,
        "force": False,
        "no_watchdog": True,
        "env_file": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    "given,expected",
    [
        ("192.168.1.50", "ipp://192.168.1.50/ipp/print"),
        ("ipp://192.168.1.50/ipp/print", "ipp://192.168.1.50/ipp/print"),
        ("socket://192.168.1.70:9100", "socket://192.168.1.70:9100"),
        ("", ""),
    ],
)
def test_a_bare_ip_becomes_a_driverless_uri(given, expected):
    assert _normalise_printer(given) == expected


def test_init_writes_a_working_env_and_the_first_key(workdir):
    exit_code = run_init(_init_args(mock=True, expose="none", key="daniel"))
    env = (workdir / ".env").read_text("utf-8")
    assert "PRINTERS=home=cups-pdf:/" in env
    assert "EXPOSE=none" in env
    assert "AUTH_MODE=keys" in env
    assert oct((workdir / ".env").stat().st_mode & 0o777) == "0o600"
    keys = workdir / "data" / "keys.json"
    assert keys.exists() and "daniel" in keys.read_text("utf-8")
    assert exit_code in (0, 1)  # 1 only because there is no CUPS in the test environment


def test_init_keeps_the_comments_from_env_example(workdir):
    run_init(_init_args(mock=True, expose="none", key="me"))
    env = (workdir / ".env").read_text("utf-8")
    assert "# printbridge — settings only" in env
    assert "MAX_FETCH_MB" in env, "settings the wizard did not ask about must survive"


def test_init_refuses_to_overwrite_an_existing_env(workdir):
    (workdir / ".env").write_text("PRINTERS=home=cups-pdf:/\n")
    assert run_init(_init_args(mock=True, expose="none")) == 1
    assert (workdir / ".env").read_text() == "PRINTERS=home=cups-pdf:/\n"


def test_init_will_not_publish_without_a_public_url(workdir):
    """A wizard that produces an invalid configuration is worse than no wizard."""
    from printbridge.config import ConfigError

    with pytest.raises(ConfigError, match="public-url is required"):
        run_init(_init_args(mock=True, expose="caddy-host", public_url="", key="me"))


def test_render_env_sets_only_what_it_was_given():
    out = render_env({"PRINTERS": "home=cups-pdf:/", "EXPOSE": "none"})
    assert "PRINTERS=home=cups-pdf:/" in out
    assert "EXPOSE=none" in out
    assert "CONFIRM_ABOVE_PAGES=10" in out


def test_probe_of_a_mock_queue_needs_no_network():
    ok, note = probe_printer("cups-pdf:/")
    assert ok and "mock" in note


def test_probe_of_an_unreachable_printer_says_so():
    ok, note = probe_printer("ipp://192.0.2.1/ipp/print", timeout=0.4)
    assert not ok and "192.0.2.1" in note


def test_backup_round_trip(workdir, monkeypatch):
    from printbridge.__main__ import cmd_backup

    (workdir / ".env").write_text("PRINTERS=home=cups-pdf:/\nEXPOSE=none\n")
    data = workdir / "data"
    (data / "spool").mkdir(parents=True)
    (data / "keys.json").write_text('{"version":1,"keys":[]}')
    (data / "printbridge.db").write_bytes(b"sqlite-ish")
    (data / "spool" / "in-flight.pdf").write_bytes(b"%PDF-1.4 transient")

    out = workdir / "backup.tar.gz"
    assert cmd_backup(argparse.Namespace(output=str(out), env_file=None)) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert ".env" in names
    assert "data/keys.json" in names and "data/printbridge.db" in names
    assert not any("spool" in n for n in names), "the spool is transient, not state"

    restored = workdir / "restored"
    restored.mkdir()
    with tarfile.open(out) as tar:
        tar.extractall(restored, filter="data")
    assert (restored / "data" / "keys.json").read_text() == '{"version":1,"keys":[]}'
    assert (restored / ".env").read_text().startswith("PRINTERS=")


def test_spool_cleanup_removes_finished_documents(tmp_path):
    from printbridge.server import cleanup_spool

    settings = load_settings(
        PRINTERS="home=cups-pdf:/",
        EXPOSE="none",
        KEEP_SPOOL_HOURS="0",
        PRINTBRIDGE_DATA_DIR=str(tmp_path / "data"),
    )
    settings.spool_path.mkdir(parents=True)
    (settings.spool_path / "pb-old.pdf").write_bytes(b"x")
    (settings.spool_path / "not-ours.txt").write_bytes(b"x")
    assert cleanup_spool(settings) == 1
    assert not (settings.spool_path / "pb-old.pdf").exists()
    assert (settings.spool_path / "not-ours.txt").exists()


def test_the_watchdog_alerts_on_a_state_change_and_then_stays_quiet(runtime, monkeypatch):
    from printbridge.doctor import Report, watchdog_tick

    bad = Report()
    bad.add("queue home", "fail", "does not exist in CUPS", "docker compose restart cups")
    monkeypatch.setattr("printbridge.doctor.run_doctor", lambda rt, **kw: bad)

    _, first = watchdog_tick(runtime)
    _, second = watchdog_tick(runtime)
    assert first is True and second is False
    assert len(runtime.notifier.sent) == 1
    assert "docker compose restart cups" in runtime.notifier.sent[0][1]


def test_cups_holds_jobs_for_a_sleeping_printer():
    """`retry-job` is what turns 'the printer was off' into 'it printed later'."""
    conf = (ROOT / "cups" / "cupsd.conf").read_text("utf-8")
    assert "ErrorPolicy retry-job" in conf
    entrypoint = (ROOT / "cups" / "entrypoint.sh").read_text("utf-8")
    assert "printer-error-policy=retry-job" in entrypoint
    # 30 s × 240 attempts = two hours, then the job fails loudly (spec §19)
    assert "JobRetryInterval 30" in conf and "JobRetryLimit 240" in conf


def test_the_installer_is_syntactically_valid_and_never_touches_the_system_blindly():
    import subprocess

    script = ROOT / "install.sh"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    body = script.read_text("utf-8")
    assert "get.docker.com" in body
    assert "rm -rf" not in body


def test_pinned_images_and_dependabot():
    compose = (ROOT / "docker-compose.yml").read_text("utf-8")
    assert "caddy:2.11-alpine" in compose, "images are pinned by tag, not :latest"
    assert ":latest" not in compose
    assert (ROOT / ".github" / "dependabot.yml").is_file()
