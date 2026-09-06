"""Connector keys — one key per *person*, never one per AI service.

A key gives its owner a single link, `PUBLIC_URL/mcp/<key>`, that works identically in Claude,
ChatGPT, Grok and any other MCP client, and doubles as `Authorization: Bearer <key>` for agents.
Which assistant was used is detected from the MCP handshake, not from the key — see `sessions.py`.

Storage is `data/keys.json`, mode 0600, hashed. The plaintext is shown exactly once, at creation
(and again only by `rotate`), and never touches a log, a commit or `.env`.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

KEY_PREFIX = "pb_"
KEY_BYTES = 32  # 256 bits
_ROLES = ("user", "admin")


class KeyError_(ValueError):
    """A key operation that cannot be carried out; the message says what to do instead."""


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def hash_key(plaintext: str) -> str:
    return "sha256:" + sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass
class KeyRecord:
    """One key. `user` is the person; `label` distinguishes several keys for the same person."""

    id: str
    user: str
    label: str = ""
    role: str = "user"
    hash: str = ""
    printers: list[str] = field(default_factory=list)  # empty = every printer
    max_pages: int | None = None
    rate_per_hour: int | None = None
    created: str = ""
    revoked: str | None = None
    expires: str | None = None  # set by rotate(): the old key dies after the grace period
    last_used: str | None = None

    @property
    def name(self) -> str:
        return f"{self.user}/{self.label}" if self.label else self.user

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self.revoked:
            return False
        if self.expires and _parse(self.expires) <= now:
            return False
        return True

    def describe(self) -> str:
        bits = [self.role]
        if self.printers:
            bits.append("printers=" + "+".join(self.printers))
        if self.max_pages:
            bits.append(f"{self.max_pages}p")
        if self.rate_per_hour:
            bits.append(f"{self.rate_per_hour}/h")
        state = "active"
        if self.revoked:
            state = "revoked " + self.revoked[:10]
        elif self.expires:
            state = "expires " + self.expires[:16].replace("T", " ")
        return f"{self.name} ({', '.join(bits)}) — {state}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class KeyStore:
    """Reads and writes `data/keys.json`. Every mutation rewrites the file atomically at 0600."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._records: list[KeyRecord] | None = None
        self._mtime: float | None = None

    # --- persistence ---

    def _load(self, force: bool = False) -> list[KeyRecord]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._records, self._mtime = [], None
            return self._records
        if force or self._records is None or mtime != self._mtime:
            raw = json.loads(self.path.read_text("utf-8") or "{}")
            self._records = [KeyRecord(**r) for r in raw.get("keys", [])]
            self._mtime = mtime
        return self._records

    def _save(self, records: list[KeyRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"version": 1, "keys": [asdict(r) for r in records]}
        tmp.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)
        self._records = records
        self._mtime = self.path.stat().st_mtime

    # --- queries ---

    def all(self) -> list[KeyRecord]:
        return list(self._load())

    def active(self) -> list[KeyRecord]:
        return [r for r in self._load() if r.is_active()]

    def count_active(self) -> int:
        return len(self.active())

    def find(self, selector: str) -> KeyRecord:
        """`fanny`, or `daniel/agent` when a person has more than one key."""
        matches = [r for r in self._load() if r.name == selector and r.is_active()]
        if not matches:
            matches = [r for r in self._load() if r.user == selector and r.is_active()]
        if not matches:
            known = ", ".join(sorted({r.name for r in self._load() if r.is_active()})) or "(none)"
            raise KeyError_(f"No active key named {selector!r}. Active keys: {known}.")
        if len(matches) > 1:
            names = ", ".join(sorted(r.name for r in matches))
            raise KeyError_(
                f"{selector!r} matches several keys ({names}). Use the full name, e.g. "
                f"`{matches[0].name}`."
            )
        return matches[0]

    def resolve(self, plaintext: str, grace_minutes: int = 60) -> KeyRecord | None:
        """Plaintext key → record, or None. Constant-time compare; revoked keys die instantly."""
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            return None
        digest = hash_key(plaintext)
        now = datetime.now(UTC)
        for record in self._load():
            if not hmac.compare_digest(record.hash, digest):
                continue
            if not record.is_active(now):
                return None
            return record
        return None

    def touch(self, record: KeyRecord) -> None:
        """Record last use. Best-effort: never let bookkeeping fail a print job."""
        try:
            records = self._load(force=True)
            for r in records:
                if r.id == record.id:
                    r.last_used = _now()
            self._save(records)
        except Exception:  # noqa: BLE001 - a failed timestamp must not break printing
            pass

    # --- mutations ---

    def add(
        self,
        user: str,
        *,
        role: str = "user",
        printers: list[str] | None = None,
        max_pages: int | None = None,
        rate_per_hour: int | None = None,
        label: str = "",
    ) -> tuple[str, KeyRecord]:
        user = user.strip()
        label = (label or "").strip()
        if not user:
            raise KeyError_("A key needs a person's name: `printbridge keys add <person>`.")
        if role not in _ROLES:
            raise KeyError_(f"Role must be one of {', '.join(_ROLES)} — got {role!r}.")
        records = self._load(force=True)
        name = f"{user}/{label}" if label else user
        if any(r.name == name and r.is_active() for r in records):
            raise KeyError_(
                f"An active key for {name!r} already exists. Use `keys rotate {name}` to replace "
                f"it, or add a second one with a label: `keys add {user} --label agent`."
            )
        plaintext = generate_key()
        record = KeyRecord(
            id=f"k_{secrets.token_hex(6)}",
            user=user,
            label=label,
            role=role,
            hash=hash_key(plaintext),
            printers=list(printers or []),
            max_pages=max_pages,
            rate_per_hour=rate_per_hour,
            created=_now(),
        )
        records.append(record)
        self._save(records)
        return plaintext, record

    def revoke(self, selector: str) -> KeyRecord:
        records = self._load(force=True)
        target = self.find(selector)
        for r in records:
            if r.id == target.id:
                r.revoked = _now()
                r.expires = None
                self._save(records)
                return r
        raise KeyError_(f"No active key named {selector!r}.")

    def rotate(self, selector: str, grace_minutes: int = 60) -> tuple[str, KeyRecord]:
        """New key for the same person; the old one keeps working for `grace_minutes`."""
        records = self._load(force=True)
        old = self.find(selector)
        deadline = (datetime.now(UTC) + timedelta(minutes=grace_minutes)).isoformat(timespec="seconds")
        for r in records:
            if r.id == old.id:
                r.expires = deadline
        plaintext = generate_key()
        new = KeyRecord(
            id=f"k_{secrets.token_hex(6)}",
            user=old.user,
            label=old.label,
            role=old.role,
            hash=hash_key(plaintext),
            printers=list(old.printers),
            max_pages=old.max_pages,
            rate_per_hour=old.rate_per_hour,
            created=_now(),
        )
        records.append(new)
        self._save(records)
        return plaintext, new

    def prune(self) -> int:
        """Drop revoked/expired records older than 90 days. Returns how many went."""
        cutoff = datetime.now(UTC) - timedelta(days=90)
        records = self._load(force=True)
        keep = [
            r
            for r in records
            if r.is_active()
            or _parse(r.revoked or r.expires or _now()) > cutoff  # noqa: SIM222
        ]
        removed = len(records) - len(keep)
        if removed:
            self._save(keep)
        return removed

    def permissions_ok(self) -> bool:
        """True when keys.json is not readable by anyone but its owner."""
        if not self.path.exists():
            return True
        return (self.path.stat().st_mode & 0o077) == 0

    def readable(self) -> bool:
        """True when *we* can read it. A 0600 file owned by another uid passes the mode check and
        still breaks every request — so check what actually matters."""
        if not self.path.exists():
            return True
        try:
            self.path.read_text("utf-8")
        except OSError:
            return False
        return True


def redact(plaintext: str) -> str:
    """What may appear in a log line: enough to recognise, useless to replay."""
    if not plaintext:
        return "(none)"
    return f"{plaintext[:6]}…{plaintext[-4:]}" if len(plaintext) > 14 else "…"


_ = time  # kept for callers that import time from here
