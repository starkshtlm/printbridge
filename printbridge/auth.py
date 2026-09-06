"""Identity. Everything downstream keys off `Principal`, never off a key or a header.

`resolve_principal(request) -> Principal` is the whole interface. v1 ships two backends behind it
(`keys` and `none`); v1.1 adds `oauth-github`/`oauth-google`, which fill the same `Principal`. That
is why the job log, sessions, rate limits and `recent_jobs` scoping never mention keys.

Two request shapes give the same principal:

    GET/POST  PUBLIC_URL/mcp/<key>          the link a person pastes into an assistant
    POST      PUBLIC_URL/mcp                with `Authorization: Bearer <key>` (agents, Claude Code)

An unknown, revoked or expired key gets **404**, not 401: a probe cannot tell an endpoint that
exists from one that does not.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from printbridge.keys import KeyRecord, KeyStore

SCOPE_PRINCIPAL = "printbridge.principal"
SCOPE_CLIENT_IP = "printbridge.client_ip"
SCOPE_KEY_ID = "printbridge.key_id"


class AuthError(PermissionError):
    """The caller may not do this. The message is shown to the assistant."""


@dataclass(frozen=True)
class Principal:
    """Who is printing. One per person — the AI service is a property of the *session*."""

    user: str
    role: str = "user"
    method: str = "key-path"  # key-path | key-bearer | stdio | none
    key_label: str = ""
    key_id: str = ""
    printers: tuple[str, ...] = ()  # empty = every configured printer
    max_pages: int | None = None
    rate_per_hour: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def name(self) -> str:
        return f"{self.user}/{self.key_label}" if self.key_label else self.user

    def may_print_to(self, printer: str) -> bool:
        return not self.printers or printer in self.printers

    def require_printer(self, printer: str) -> None:
        if not self.may_print_to(printer):
            allowed = ", ".join(self.printers)
            raise AuthError(
                f"Your key may only print to: {allowed}. Asked for {printer!r}. "
                "The operator can widen this with `printbridge keys rotate`."
            )

    def page_ceiling(self, global_max: int) -> int:
        return min(global_max, self.max_pages) if self.max_pages else global_max

    def rate_ceiling(self, global_rate: int) -> int:
        return min(global_rate, self.rate_per_hour) if self.rate_per_hour else global_rate

    @classmethod
    def from_key(cls, record: KeyRecord, method: str) -> Principal:
        return cls(
            user=record.user,
            role=record.role,
            method=method,
            key_label=record.label,
            key_id=record.id,
            printers=tuple(record.printers),
            max_pages=record.max_pages,
            rate_per_hour=record.rate_per_hour,
        )


LOCAL_PRINCIPAL = Principal(user="local", role="admin", method="stdio")


class AuthBackend(Protocol):
    """v1.1 adds an OAuth implementation here; nothing downstream changes."""

    def resolve(self, *, path_key: str | None, bearer: str | None) -> Principal | None: ...


@dataclass
class KeyAuth:
    """`AUTH_MODE=keys` — per-person connector keys, by path or bearer."""

    store: KeyStore
    grace_minutes: int = 60

    def resolve(self, *, path_key: str | None, bearer: str | None) -> Principal | None:
        for candidate, method in ((path_key, "key-path"), (bearer, "key-bearer")):
            if not candidate:
                continue
            record = self.store.resolve(candidate, self.grace_minutes)
            if record:
                return Principal.from_key(record, method)
        return None


@dataclass
class NoAuth:
    """`AUTH_MODE=none` — only legal together with `EXPOSE=none` (config.py enforces that)."""

    def resolve(self, *, path_key: str | None, bearer: str | None) -> Principal | None:
        return Principal(user="local", role="admin", method="none")


@dataclass
class ProbeCounter:
    """Counts 404s on `/mcp/*` so the alert engine can say 'someone is guessing keys'."""

    window_seconds: int = 3600
    events: deque[float] = field(default_factory=deque)

    def record(self) -> int:
        now = time.monotonic()
        self.events.append(now)
        cutoff = now - self.window_seconds
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return len(self.events)

    def count(self) -> int:
        cutoff = time.monotonic() - self.window_seconds
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return len(self.events)


def resolve_principal(request: Any) -> Principal:
    """The principal the middleware attached to this request."""
    scope = getattr(request, "scope", request)
    principal = scope.get(SCOPE_PRINCIPAL) if isinstance(scope, dict) else None
    if principal is None:
        raise AuthError("No identity on this request. The connector link is wrong or revoked.")
    return principal


def client_ip(request: Any) -> str:
    scope = getattr(request, "scope", request)
    if isinstance(scope, dict) and scope.get(SCOPE_CLIENT_IP):
        return scope[SCOPE_CLIENT_IP]
    return "-"


def extract_bearer(headers: dict[str, str] | Any) -> str | None:
    get = headers.get if hasattr(headers, "get") else None
    if get is None:
        return None
    value = get("authorization") or get("Authorization")
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def forwarded_ip(headers: Any, fallback: str = "-") -> str:
    """Caddy sets X-Forwarded-For; without a proxy we use the socket address."""
    get = headers.get if hasattr(headers, "get") else None
    if get:
        xff = get("x-forwarded-for") or get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        real = get("x-real-ip") or get("X-Real-IP")
        if real:
            return real.strip()
    return fallback
