"""The job log and the session log — spec §18. Metadata always, content never.

What is stored: who printed, from which assistant, what it was called, how many pages, which
printer, whether it worked. What is never stored: the document, the URL, the file bytes. A title
is the only thing that describes the content, and an assistant can pass `title="(untitled)"` when
that is too much.

SQLite in WAL mode at `data/printbridge.db`. One connection, one lock — the write volume of a
household printer does not need more.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT,
    ts           TEXT NOT NULL,
    principal    TEXT NOT NULL,
    key_label    TEXT DEFAULT '',
    client       TEXT DEFAULT 'unknown',
    client_version TEXT DEFAULT '',
    tool         TEXT NOT NULL,
    title        TEXT DEFAULT '',
    pages        INTEGER DEFAULT 0,
    copies       INTEGER DEFAULT 1,
    printer      TEXT DEFAULT '',
    options      TEXT DEFAULT '{}',
    status       TEXT NOT NULL,
    error        TEXT DEFAULT '',
    duration_ms  INTEGER DEFAULT 0,
    source_kind  TEXT DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS jobs_ts        ON jobs (ts);
CREATE INDEX IF NOT EXISTS jobs_principal ON jobs (principal, ts);
CREATE INDEX IF NOT EXISTS jobs_job_id    ON jobs (job_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    principal    TEXT NOT NULL,
    client       TEXT DEFAULT 'unknown',
    client_version TEXT DEFAULT '',
    remote_ip    TEXT DEFAULT '',
    remote_net   TEXT DEFAULT '',
    user_agent   TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    calls        INTEGER DEFAULT 0,
    jobs         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_last_seen ON sessions (last_seen);
CREATE INDEX IF NOT EXISTS sessions_principal ON sessions (principal, last_seen);

CREATE TABLE IF NOT EXISTS alert_state (
    event   TEXT PRIMARY KEY,
    state   TEXT NOT NULL,
    since   TEXT NOT NULL,
    detail  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def network_of(address: str) -> str:
    """The network an address belongs to, for "have we seen this person from here before?".

    Comparing whole addresses sounds stricter and is in fact worse: a cloud-hosted assistant leaves
    from a different egress address on almost every request, so an exact match would raise the
    "someone else has my link" warning several times an hour and teach the operator to ignore it.
    A /24 (or /48 for IPv6) still distinguishes a different provider or a different continent.
    """
    address = (address or "").strip()
    if not address or address == "-":
        return ""
    if ":" in address:  # IPv6 → first three hextets, i.e. the /48
        return ":".join(address.split(":")[:3])
    parts = address.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else address


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SessionSignal:
    """What the alert engine needs to know about a session that just spoke."""

    new_session: bool
    new_client_for_principal: bool
    new_origin: bool  # a new client *and* a network we have not seen for this person


class Store:
    """Job log + sessions. Every method is safe to call from any thread."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns that later versions introduced. SQLite has no IF NOT EXISTS for columns."""
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "remote_net" not in have:
            self._db.execute("ALTER TABLE sessions ADD COLUMN remote_net TEXT DEFAULT ''")
            self._db.execute(
                "UPDATE sessions SET remote_net = "
                "substr(remote_ip, 1, length(remote_ip) - length(replace(remote_ip, '.', '')) "
                "+ instr(remote_ip, '.')) WHERE remote_ip LIKE '%.%.%.%'"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- jobs ---

    def record_job(
        self,
        *,
        principal: str,
        tool: str,
        status: str,
        job_id: str | None = None,
        key_label: str = "",
        client: str = "unknown",
        client_version: str = "",
        title: str = "",
        pages: int = 0,
        copies: int = 1,
        printer: str = "",
        options: dict[str, Any] | None = None,
        error: str = "",
        duration_ms: int = 0,
        source_kind: str = "text",
        ts: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO jobs (job_id, ts, principal, key_label, client, client_version,
                                     tool, title, pages, copies, printer, options, status, error,
                                     duration_ms, source_kind)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    ts or utcnow(),
                    principal,
                    key_label,
                    client,
                    client_version,
                    tool,
                    title,
                    int(pages),
                    int(copies),
                    printer,
                    json.dumps(options or {}, sort_keys=True),
                    status,
                    error[:500],
                    int(duration_ms),
                    source_kind,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def update_job(self, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"job_id", "status", "error", "duration_ms", "pages"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        clause = ", ".join(f"{k} = ?" for k in sets)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {clause} WHERE id = ?", (*sets.values(), row_id))
            self._db.commit()

    def set_status_by_job_id(self, job_id: str, status: str, error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, error = ? WHERE job_id = ?", (status, error[:500], job_id)
            )
            self._db.commit()

    def recent_jobs(
        self,
        *,
        principal: str | None = None,
        printer: str | None = None,
        limit: int = 10,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        where, params = [], []
        if principal:
            where.append("principal = ?")
            params.append(principal)
        if printer:
            where.append("printer = ?")
            params.append(printer)
        if since:
            where.append("ts >= ?")
            params.append(since.astimezone(UTC).isoformat(timespec="seconds"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def open_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        """Rows that claim to be still in the queue — what reconciliation has to check."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','printing') AND job_id IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_jobs_since(self, principal: str, since: datetime) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE principal = ? AND ts >= ? "
                "AND status NOT IN ('rejected','dry-run')",
                (principal, since.astimezone(UTC).isoformat(timespec="seconds")),
            ).fetchone()
        return int(row["n"] if row else 0)

    def totals(self, since: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT principal, client, COUNT(*) AS jobs, COALESCE(SUM(pages*copies),0) AS pages,
                          SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failures
                   FROM jobs WHERE ts >= ? AND status != 'dry-run'
                   GROUP BY principal, client ORDER BY jobs DESC""",
                (since.astimezone(UTC).isoformat(timespec="seconds"),),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_errors(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, principal, client, printer, title, status, error FROM jobs "
                "WHERE status IN ('failed','error','rejected') ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- sessions ---

    def touch_session(
        self,
        *,
        session_id: str,
        principal: str,
        client: str = "unknown",
        client_version: str = "",
        remote_ip: str = "",
        user_agent: str = "",
        counts_call: bool = True,
    ) -> SessionSignal:
        """Upsert a session row and report what is new about it."""
        now = utcnow()
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            seen_client = self._db.execute(
                "SELECT 1 FROM sessions WHERE principal = ? AND client = ? LIMIT 1",
                (principal, client),
            ).fetchone()
            network = network_of(remote_ip)
            seen_origin = self._db.execute(
                "SELECT 1 FROM sessions WHERE principal = ? AND remote_net = ? LIMIT 1",
                (principal, network),
            ).fetchone()
            if existing:
                self._db.execute(
                    "UPDATE sessions SET last_seen = ?, calls = calls + ?, client = ?, "
                    "client_version = ?, remote_ip = ?, remote_net = ?, user_agent = ? "
                    "WHERE session_id = ?",
                    (
                        now,
                        1 if counts_call else 0,
                        client or existing["client"],
                        client_version or existing["client_version"],
                        remote_ip or existing["remote_ip"],
                        network or existing["remote_net"],
                        user_agent or existing["user_agent"],
                        session_id,
                    ),
                )
            else:
                self._db.execute(
                    """INSERT INTO sessions (session_id, principal, client, client_version,
                                             remote_ip, remote_net, user_agent, first_seen,
                                             last_seen, calls, jobs)
                       VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        session_id,
                        principal,
                        client,
                        client_version,
                        remote_ip,
                        network,
                        user_agent,
                        now,
                        now,
                        1 if counts_call else 0,
                    ),
                )
            self._db.commit()
        new_client = not seen_client
        return SessionSignal(
            new_session=existing is None,
            new_client_for_principal=new_client,
            new_origin=new_client and not seen_origin,
        )

    def count_session_job(self, session_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET jobs = jobs + 1 WHERE session_id = ?", (session_id,)
            )
            self._db.commit()

    def active_sessions(self, minutes: int = 15) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions ORDER BY last_seen DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- alert state (one message per state change, never a repeat) ---

    def alert_state(self, event: str) -> tuple[str, str]:
        with self._lock:
            row = self._db.execute(
                "SELECT state, since FROM alert_state WHERE event = ?", (event,)
            ).fetchone()
        return (row["state"], row["since"]) if row else ("", "")

    def set_alert_state(self, event: str, state: str, detail: str = "") -> bool:
        """Returns True when the state actually changed — i.e. when a message should go out."""
        previous, _ = self.alert_state(event)
        if previous == state:
            return False
        with self._lock:
            self._db.execute(
                "INSERT INTO alert_state (event, state, since, detail) VALUES (?,?,?,?) "
                "ON CONFLICT(event) DO UPDATE SET state=excluded.state, since=excluded.since, "
                "detail=excluded.detail",
                (event, state, utcnow(), detail[:400]),
            )
            self._db.commit()
        return True

    # --- meta + retention ---

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._db.commit()

    def purge(self, retention_days: int, session_days: int = 90) -> tuple[int, int]:
        job_cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        sess_cutoff = (datetime.now(UTC) - timedelta(days=session_days)).isoformat(timespec="seconds")
        with self._lock:
            jobs = self._db.execute("DELETE FROM jobs WHERE ts < ?", (job_cutoff,)).rowcount
            sessions = self._db.execute(
                "DELETE FROM sessions WHERE last_seen < ?", (sess_cutoff,)
            ).rowcount
            self._db.commit()
        return jobs, sessions
