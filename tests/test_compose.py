"""The deployment files carry hard rules too — a wrong port here undoes every guard in the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text("utf-8")


def _published_ports(text: str) -> list[str]:
    body = text.split("caddy:", 1)[0]  # the optional caddy profile is the documented exception
    return re.findall(r'^\s*-\s*"([^"]+)"\s*(?:#.*)?$', body, re.MULTILINE)


def test_printbridge_publishes_only_to_loopback():
    for mapping in _published_ports(COMPOSE):
        if ":" not in mapping:
            continue
        assert mapping.startswith("127.0.0.1:"), (
            f"{mapping!r} would expose printbridge beyond loopback; the reverse proxy is the only "
            "public face."
        )


def test_cups_publishes_nothing():
    cups_block = COMPOSE.split("cups:", 1)[1].split("printbridge:", 1)[0]
    assert "ports:" not in cups_block, "the printer must be reachable only over the compose network"


def test_no_host_networking():
    assert "network_mode" not in COMPOSE


def test_resource_limits_are_set():
    assert COMPOSE.count("mem_limit:") >= 2
    assert "mem_limit: 256m" in COMPOSE and "mem_limit: 512m" in COMPOSE


def test_log_rotation_is_configured():
    assert COMPOSE.count('max-size: "10m"') >= 2


def test_restart_policy_is_unless_stopped():
    assert COMPOSE.count("restart: unless-stopped") >= 2


def test_the_app_waits_for_cups():
    assert "condition: service_healthy" in COMPOSE


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "deploy/caddy/printbridge.caddy",
        "deploy/systemd/printbridge-doctor.timer",
        ".env.example",
    ],
)
def test_shipped_files_exist(path):
    assert (ROOT / path).is_file()


def test_secrets_and_state_are_gitignored():
    ignored = (ROOT / ".gitignore").read_text("utf-8")
    for entry in (".env", "data/", "BUILD_LOG.md", "DANIEL_TODO.md", "MOCKS.md"):
        assert entry in ignored


def test_the_caddy_snippet_never_logs_the_connector_key():
    snippet = (ROOT / "deploy" / "caddy" / "printbridge.caddy").read_text("utf-8")
    assert "replace /mcp/REDACTED" in snippet
    assert "Authorization" in snippet and "delete" in snippet


def test_the_caddy_snippet_only_proxies_mcp():
    snippet = (ROOT / "deploy" / "caddy" / "printbridge.caddy").read_text("utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in snippet
    directives = [
        line.split("#", 1)[0]
        for line in snippet.splitlines()
        if not line.strip().startswith("#")
    ]
    proxied = [line for line in directives if "reverse_proxy" in line]
    assert proxied and all("8000" in line for line in proxied), (
        "the admin port must never be reachable from the internet"
    )
    assert not any("8001" in line for line in directives)
