"""Configuration validation — the guard that keeps an unprotected endpoint off the internet."""

from __future__ import annotations

import pytest

from printbridge.config import ConfigError, load_settings, parse_printers, validate_runtime


def test_public_endpoint_without_auth_refuses_to_start():
    with pytest.raises(ConfigError, match="AUTH_MODE=none"):
        load_settings(EXPOSE="caddy-host", AUTH_MODE="none", PUBLIC_URL="https://print.example.com")


def test_public_endpoint_without_any_key_refuses_to_start(tmp_path):
    settings = load_settings(
        EXPOSE="caddy-host",
        AUTH_MODE="keys",
        PUBLIC_URL="https://print.example.com",
        PRINTERS="home=ipp://192.0.2.50/ipp/print",
        PRINTBRIDGE_DATA_DIR=str(tmp_path),
    )
    with pytest.raises(ConfigError, match="no connector keys exist"):
        validate_runtime(settings, key_count=0)
    assert validate_runtime(settings, key_count=1) == []


def test_local_only_server_may_run_without_auth():
    settings = load_settings(EXPOSE="none", AUTH_MODE="none", PRINTERS="home=cups-pdf:/")
    assert settings.auth_mode == "none"


@pytest.mark.parametrize("bind", ["0.0.0.0:8000", "::", "0.0.0.0:8080"])
def test_never_binds_a_public_interface(bind):
    with pytest.raises(ConfigError, match="every interface"):
        load_settings(BIND=bind, EXPOSE="none", AUTH_MODE="keys")


def test_admin_bind_is_guarded_too():
    with pytest.raises(ConfigError, match="every interface"):
        load_settings(ADMIN_BIND="0.0.0.0:8001", EXPOSE="none", AUTH_MODE="keys")


def test_public_endpoint_needs_a_public_url():
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        load_settings(EXPOSE="funnel", AUTH_MODE="keys", PUBLIC_URL="")


def test_confirm_threshold_above_max_pages_is_unreachable():
    with pytest.raises(ConfigError, match="never be reachable"):
        load_settings(CONFIRM_ABOVE_PAGES="200", MAX_PAGES="100", EXPOSE="none")


def test_oauth_modes_are_not_pretended_to_work():
    with pytest.raises(ConfigError, match="v1.1"):
        load_settings(AUTH_MODE="github", EXPOSE="none")


def test_printer_parsing_forms():
    specs = parse_printers(
        "home=ipp://192.0.2.50/ipp/print,old=socket://192.0.2.70:9100;driver=gutenprint.5.3://x"
    )
    assert [s.name for s in specs] == ["home", "old"]
    assert specs[0].driver == "everywhere"
    assert specs[1].driver == "gutenprint.5.3://x"
    assert specs[0].host == "192.0.2.50"


def test_legacy_single_printer_form_is_still_accepted():
    specs = parse_printers("", legacy_uri="ipp://192.0.2.50/ipp/print", legacy_name="downstairs")
    assert specs[0].name == "downstairs"


def test_mock_queue_is_recognised():
    settings = load_settings(PRINTERS="home=cups-pdf:/", EXPOSE="none")
    assert settings.is_mock_printing


@pytest.mark.parametrize(
    "raw,message",
    [
        ("noequalssign", "has no '='"),
        ("bad name=ipp://x/", "not a valid CUPS queue name"),
        ("home=ipp://a/,home=ipp://b/", "listed twice"),
    ],
)
def test_bad_printer_config_says_what_is_wrong(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_printers(raw)


def test_runtime_warnings_name_the_risk(tmp_path):
    settings = load_settings(
        EXPOSE="none",
        AUTH_MODE="keys",
        PRINTERS="home=cups-pdf:/",
        LOG_REQUEST_PATH="true",
        ALLOW_PRIVATE_URLS="true",
        PRINTBRIDGE_DATA_DIR=str(tmp_path),
    )
    warnings = validate_runtime(settings, key_count=1)
    assert any("access log" in w for w in warnings)
    assert any("tailnet" in w for w in warnings)
