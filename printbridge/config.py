"""Settings (from `.env`) and the validation that refuses an unsafe configuration.

Two rules matter more than the rest and both are enforced here:

* `.env` holds *settings*; `data/` holds *state* (keys, job log, spool). That split is what makes
  backup and upgrade trivial.
* A public endpoint without authentication refuses to start. `EXPOSE != none` requires
  `AUTH_MODE=keys` (and, at runtime, at least one key).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ExposeMode = Literal["caddy-host", "caddy", "funnel", "cloudflared", "none"]
AuthMode = Literal["keys", "none", "github", "google"]

MOCK_URI = "cups-pdf:/"
_QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or unusable. The message is the fix."""


@dataclass(frozen=True)
class PrinterSpec:
    """One CUPS queue: a name, a device URI and the driver used to create it."""

    name: str
    uri: str
    driver: str = "everywhere"

    @property
    def is_mock(self) -> bool:
        return self.uri.startswith("cups-pdf")

    @property
    def host(self) -> str | None:
        """Best-effort host part of the device URI, for reachability checks."""
        m = re.match(r"^[a-z0-9+.-]+://(?:[^@/]*@)?(\[[^\]]+\]|[^/:]+)", self.uri, re.I)
        if not m:
            return None
        return m.group(1).strip("[]")


def parse_printers(raw: str, legacy_uri: str = "", legacy_name: str = "home") -> list[PrinterSpec]:
    """`name=uri[;driver=ppd],name2=uri2` → specs. The first entry is the default queue.

    The legacy single-printer form (`PRINTER_URI` + `PRINTER_NAME`) is still accepted.
    """
    raw = (raw or "").strip()
    if not raw and legacy_uri.strip():
        raw = f"{(legacy_name or 'home').strip()}={legacy_uri.strip()}"
    specs: list[PrinterSpec] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigError(
                f"PRINTERS entry {entry!r} has no '='. Expected name=uri, e.g. "
                "home=ipp://192.168.1.50/ipp/print"
            )
        name, rest = entry.split("=", 1)
        name = name.strip()
        rest = rest.strip()
        driver = "everywhere"
        if ";driver=" in rest:
            rest, driver = rest.split(";driver=", 1)
            rest, driver = rest.strip(), driver.strip() or "everywhere"
        if not _QUEUE_NAME_RE.match(name):
            raise ConfigError(
                f"Printer name {name!r} is not a valid CUPS queue name "
                "(letters, digits, '_', '.', '-'; 64 chars max)."
            )
        if not rest:
            raise ConfigError(f"Printer {name!r} has an empty URI.")
        if name in seen:
            raise ConfigError(f"Printer {name!r} is listed twice in PRINTERS.")
        seen.add(name)
        specs.append(PrinterSpec(name=name, uri=rest, driver=driver))
    return specs


class Settings(BaseSettings):
    """Everything from `.env`. Nothing here is a secret except what the operator puts in it."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("PRINTBRIDGE_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- printers ---
    printers: str = Field("", alias="PRINTERS")
    printer_uri: str = Field("", alias="PRINTER_URI")
    printer_name: str = Field("home", alias="PRINTER_NAME")
    cups_server: str = Field("cups:631", alias="CUPS_SERVER")

    # --- defaults ---
    default_paper: Literal["A4", "Letter"] = Field("A4", alias="DEFAULT_PAPER")
    default_duplex: bool = Field(True, alias="DEFAULT_DUPLEX")
    default_color: bool = Field(False, alias="DEFAULT_COLOR")
    locale: str = Field("en_GB", alias="LOCALE")

    # --- network ---
    public_url: str = Field("", alias="PUBLIC_URL")
    expose: ExposeMode = Field("none", alias="EXPOSE")
    bind: str = Field("127.0.0.1:8000", alias="BIND")
    admin_bind: str = Field("127.0.0.1:8001", alias="ADMIN_BIND")

    # --- auth ---
    auth_mode: AuthMode = Field("keys", alias="AUTH_MODE")
    key_grace_minutes: int = Field(60, alias="KEY_GRACE_MINUTES", ge=0, le=1440)

    # --- limits ---
    confirm_above_pages: int = Field(10, alias="CONFIRM_ABOVE_PAGES", ge=1)
    max_pages: int = Field(100, alias="MAX_PAGES", ge=1)
    max_fetch_mb: int = Field(25, alias="MAX_FETCH_MB", ge=1, le=500)
    allow_private_urls: bool = Field(False, alias="ALLOW_PRIVATE_URLS")
    rate_limit_jobs_per_hour: int = Field(30, alias="RATE_LIMIT_JOBS_PER_HOUR", ge=1)
    keep_spool_hours: int = Field(0, alias="KEEP_SPOOL_HOURS", ge=0)

    # --- observability ---
    job_log_retention_days: int = Field(365, alias="JOB_LOG_RETENTION_DAYS", ge=1)
    log_request_path: bool = Field(False, alias="LOG_REQUEST_PATH")
    notify_webhook: str = Field("", alias="NOTIFY_WEBHOOK")
    notify_events: str = Field(
        "printer_offline,unknown_origin,rate_limit,cert_expiring,probe_404",
        alias="NOTIFY_EVENTS",
    )

    # --- operations ---
    auto_update: Literal["off", "weekly"] = Field("off", alias="AUTO_UPDATE")

    # --- paths (not usually set by hand) ---
    data_dir: Path = Field(Path("data"), alias="PRINTBRIDGE_DATA_DIR")
    spool_dir: Path | None = Field(None, alias="PRINTBRIDGE_SPOOL_DIR")

    @field_validator("expose", mode="before")
    @classmethod
    def _normalise_expose(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            if v in {"", "off", "false"}:
                return "none"
        return v

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _normalise_auth(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("public_url", mode="before")
    @classmethod
    def _strip_url(cls, v: object) -> object:
        return v.rstrip("/") if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_safety(self) -> Settings:
        # Hard rule 2: a public endpoint without authentication must not start.
        if self.expose != "none" and self.auth_mode == "none":
            raise ConfigError(
                f"EXPOSE={self.expose} publishes this server, but AUTH_MODE=none. "
                "Set AUTH_MODE=keys and create a key (`printbridge keys add <person>`), "
                "or set EXPOSE=none for a local-only server."
            )
        # Hard rule 3: never bind a public interface. The proxy is the only public face.
        for label, value in (("BIND", self.bind), ("ADMIN_BIND", self.admin_bind)):
            host = _host_of(value)
            if host in {"0.0.0.0", "::", "*", ""}:  # noqa: S104 - this is the check, not a bind
                raise ConfigError(
                    f"{label}={value} would listen on every interface. printbridge binds loopback "
                    f"(or a tailnet IP) only; put a reverse proxy in front. Use 127.0.0.1:"
                    f"{_port_of(value)}."
                )
        if self.expose != "none" and not self.public_url:
            raise ConfigError(
                f"EXPOSE={self.expose} needs PUBLIC_URL so the connector link can be printed, "
                "e.g. PUBLIC_URL=https://print.example.com"
            )
        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            raise ConfigError(f"PUBLIC_URL must start with https:// — got {self.public_url!r}")
        if self.confirm_above_pages > self.max_pages:
            raise ConfigError(
                f"CONFIRM_ABOVE_PAGES ({self.confirm_above_pages}) is above MAX_PAGES "
                f"({self.max_pages}); the confirmation would never be reachable."
            )
        if self.auth_mode in {"github", "google"}:
            raise ConfigError(
                f"AUTH_MODE={self.auth_mode} is planned for v1.1 and not implemented yet. "
                "Use AUTH_MODE=keys."
            )
        return self

    # --- derived ---

    @property
    def printer_specs(self) -> list[PrinterSpec]:
        return parse_printers(self.printers, self.printer_uri, self.printer_name)

    @property
    def default_printer(self) -> str | None:
        specs = self.printer_specs
        return specs[0].name if specs else None

    @property
    def notify_event_set(self) -> set[str]:
        return {e.strip() for e in self.notify_events.split(",") if e.strip()}

    @property
    def listen_bind(self) -> tuple[str, int]:
        """Where uvicorn actually binds.

        `BIND` is the *host* contract and is validated against 0.0.0.0 above. Inside a container
        that address means the container's own loopback, which no reverse proxy could ever reach,
        so there we listen on every interface **of the container namespace** and let Docker's
        `127.0.0.1:8000:8000` publish rule keep it off the host's public interfaces
        (docker-compose.yml, and a test asserts it).
        """
        host, port = bind_parts(self.bind)
        if in_container() and host in {"127.0.0.1", "::1", "localhost"}:
            return "0.0.0.0", port  # noqa: S104 - namespace-private; see the docstring
        return host, port

    @property
    def admin_listen_bind(self) -> tuple[str, int]:
        host, port = bind_parts(self.admin_bind)
        if in_container() and host in {"127.0.0.1", "::1", "localhost"}:
            return "0.0.0.0", port  # noqa: S104 - namespace-private; see listen_bind
        return host, port

    @property
    def spool_path(self) -> Path:
        return self.spool_dir or (self.data_dir / "spool")

    @property
    def keys_path(self) -> Path:
        return self.data_dir / "keys.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "printbridge.db"

    @property
    def outbox_path(self) -> Path:
        return self.data_dir / "notify-outbox.jsonl"

    @property
    def mock_out_path(self) -> Path:
        return self.data_dir / "mock-out"

    @property
    def is_mock_printing(self) -> bool:
        specs = self.printer_specs
        return bool(specs) and all(s.is_mock for s in specs)

    def connector_url(self, key: str) -> str:
        base = self.public_url or f"http://{self.bind}"
        return f"{base}/mcp/{key}"

    def printer_or_default(self, name: str | None) -> PrinterSpec:
        specs = self.printer_specs
        if not specs:
            raise ConfigError(
                "No printers configured. Set PRINTERS in .env, e.g. "
                "PRINTERS=home=ipp://192.168.1.50/ipp/print (or home=cups-pdf:/ to try it without "
                "a printer)."
            )
        if name is None:
            return specs[0]
        for spec in specs:
            if spec.name == name:
                return spec
        known = ", ".join(s.name for s in specs)
        raise ConfigError(f"Unknown printer {name!r}. Configured printers: {known}.")


def _split_bind(bind: str) -> tuple[str, str]:
    """`host:port` in every shape we might meet: v4, `[::1]:8000`, `:::8000`, a bare `::`."""
    bind = (bind or "").strip()
    if bind.startswith("["):  # [::1]:8000
        host = bind[1 : bind.index("]")]
        rest = bind[bind.index("]") + 1 :]
        return host, rest[1:] if rest.startswith(":") else "8000"
    if bind.count(":") >= 2:  # unbracketed IPv6, with or without a port
        head, _, tail = bind.rpartition(":")
        if head and tail.isdigit():
            return head, tail
        return bind, "8000"
    if ":" in bind:
        host, _, port = bind.partition(":")
        return host, port or "8000"
    return bind, "8000"


def _host_of(bind: str) -> str:
    return _split_bind(bind)[0]


def _port_of(bind: str) -> str:
    return _split_bind(bind)[1]


def bind_parts(bind: str) -> tuple[str, int]:
    host, port = _split_bind(bind)
    return host, int(port)


def in_container() -> bool:
    """True inside Docker/Podman. Used only to decide *where* to listen, never *whether* to guard."""
    if os.environ.get("PRINTBRIDGE_IN_CONTAINER", "").lower() in {"1", "true", "yes"}:
        return True
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text() or "containerd" in Path(
            "/proc/1/cgroup"
        ).read_text()
    except OSError:
        return False


def load_settings(**overrides: object) -> Settings:
    """Load settings, turning pydantic's validation errors into one readable sentence."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError
        inner = getattr(exc, "errors", None)
        if callable(inner):
            for err in exc.errors():  # type: ignore[attr-defined]
                cause = err.get("ctx", {}).get("error")
                if isinstance(cause, ConfigError):
                    raise cause from exc
        raise ConfigError(str(exc)) from exc


def validate_runtime(settings: Settings, key_count: int) -> list[str]:
    """Checks that need state as well as settings. Returns warnings; raises on a hard stop."""
    warnings: list[str] = []
    if settings.expose != "none" and settings.auth_mode == "keys" and key_count == 0:
        raise ConfigError(
            f"EXPOSE={settings.expose} publishes this server but no connector keys exist. "
            "Create one first: `printbridge keys add <person>` — the endpoint would otherwise be "
            "open to anyone who finds it."
        )
    if not settings.printer_specs:
        warnings.append(
            "No printers configured (PRINTERS is empty) — printing will fail until you set one."
        )
    if settings.log_request_path:
        warnings.append(
            "LOG_REQUEST_PATH=true writes connector keys into the access log. Set it to false."
        )
    if settings.allow_private_urls:
        warnings.append(
            "ALLOW_PRIVATE_URLS=true lets print_url reach your LAN and tailnet. Only keep this on "
            "if you meant it."
        )
    return warnings
