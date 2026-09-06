"""The objects a running printbridge needs, assembled once and passed around explicitly.

`build_server()` must have no import-time side effects (it is mountable into someone else's hub),
so nothing here is a module-level singleton: a runtime is created, used, and thrown away. Tests
build one per test with a temporary data directory.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from printbridge.auth import AuthBackend, KeyAuth, NoAuth, ProbeCounter
from printbridge.config import Settings
from printbridge.keys import KeyStore
from printbridge.spool import Spooler
from printbridge.store import Store


class RateLimiter:
    """Jobs per key per hour, in memory. Restarting the server forgives — that is acceptable for
    a printer, and the page limits are the real defence against an injected 'print 500 pages'."""

    def __init__(self, window_seconds: int = 3600):
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        events = self._events[key]
        cutoff = time.monotonic() - self.window
        while events and events[0] < cutoff:
            events.popleft()
        return events

    def count(self, key: str) -> int:
        return len(self._prune(key))

    def allow(self, key: str, limit: int) -> bool:
        return self.count(key) < limit

    def record(self, key: str) -> int:
        events = self._prune(key)
        events.append(time.monotonic())
        return len(events)

    def retry_after_seconds(self, key: str) -> int:
        events = self._prune(key)
        if not events:
            return 0
        return max(1, int(self.window - (time.monotonic() - events[0])))


@dataclass
class Runtime:
    """Everything a server instance needs. One per process (or per test)."""

    settings: Settings
    keys: KeyStore
    spooler: Spooler
    store: Store
    auth: AuthBackend
    rate: RateLimiter = field(default_factory=RateLimiter)
    probes: ProbeCounter = field(default_factory=ProbeCounter)
    notifier: object | None = None  # printbridge.notify.Notifier — set by build_runtime

    @property
    def mock_printing(self) -> bool:
        return self.settings.is_mock_printing


def build_runtime(settings: Settings) -> Runtime:
    """Assemble a runtime from settings, creating the data directory if it is missing."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    keystore = KeyStore(settings.keys_path)
    auth: AuthBackend = (
        KeyAuth(store=keystore, grace_minutes=settings.key_grace_minutes)
        if settings.auth_mode == "keys"
        else NoAuth()
    )
    runtime = Runtime(
        settings=settings,
        keys=keystore,
        spooler=Spooler(cups_server=settings.cups_server),
        store=Store(settings.db_path),
        auth=auth,
    )
    from printbridge.notify import Notifier  # local import: keeps module import side-effect free

    runtime.notifier = Notifier(settings, runtime.store)
    return runtime
