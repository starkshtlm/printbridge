"""Alerts and the weekly digest — spec §18.

Rules that make alerts worth reading:

* one message per *state change*, never a repeat while the state holds (`store.set_alert_state`);
* every message names the next step, not just the symptom;
* with no `NOTIFY_WEBHOOK` configured, messages go to `data/notify-outbox.jsonl` in the same shape.
  The outbox is a record, not a queue: it is never replayed when a webhook appears later.

A URL that looks like `https://api.telegram.org/bot<token>/sendMessage?chat_id=…` gets Telegram's
payload shape; anything else gets generic JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from printbridge.config import Settings
from printbridge.store import Store, utcnow

# event -> (level, one-line next step template)
EVENT_LEVELS: dict[str, str] = {
    "printer_offline": "warning",
    "printer_online": "info",
    "unknown_origin": "warning",
    "rate_limit": "warning",
    "cert_expiring": "warning",
    "supply_low": "warning",
    "probe_404": "warning",
    "new_client": "info",
    "job_failed": "warning",
    "digest": "info",
    "test": "info",
}


@dataclass
class Notifier:
    settings: Settings
    store: Store

    # --- transport ---

    PLACEHOLDER_MARKERS = ("<", ">", "YOUR_TOKEN", "example.com/webhook")

    @property
    def configured(self) -> bool:
        return bool(self.settings.notify_webhook.strip()) and not self.is_placeholder

    @property
    def is_placeholder(self) -> bool:
        """`https://api.telegram.org/bot<TOKEN>/…` is not a webhook, it is a note to self.

        Worth its own check: a non-empty string used to be enough to report "webhook OK", so an
        unfinished copy-paste looked configured while every alert quietly went to the outbox.
        """
        url = self.settings.notify_webhook.strip()
        return bool(url) and any(marker in url for marker in self.PLACEHOLDER_MARKERS)

    @property
    def is_telegram(self) -> bool:
        return "api.telegram.org/bot" in self.settings.notify_webhook

    def describe(self) -> str:
        if self.is_placeholder:
            return "NOTIFY_WEBHOOK still holds a placeholder — alerts go to the outbox"
        if not self.configured:
            return f"outbox ({self.settings.outbox_path}) — no webhook configured"
        return "telegram webhook" if self.is_telegram else "generic JSON webhook"

    def last_delivery_error(self) -> str:
        """The most recent alert that could not be delivered, if any is still in the outbox."""
        path = self.settings.outbox_path
        if not path.exists():
            return ""
        try:
            tail = path.read_text("utf-8").splitlines()[-20:]
        except OSError:
            return ""
        for line in reversed(tail):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("delivery_error"):
                return f"{entry.get('ts', '')}: {entry['delivery_error']}"
        return ""

    def _to_outbox(self, payload: dict[str, Any]) -> None:
        path = self.settings.outbox_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _post(self, payload: dict[str, Any]) -> bool:
        url = self.settings.notify_webhook.strip()
        try:
            if self.is_telegram:
                body: dict[str, Any] = {"text": payload["text"], "disable_web_page_preview": True}
                if "chat_id=" not in url:
                    body["chat_id"] = payload.get("chat_id", "")
                response = httpx.post(url, json=body, timeout=10)
            else:
                response = httpx.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - a failed alert must never break printing
            self._to_outbox({**payload, "delivery_error": str(exc)[:200]})
            return False

    # --- API ---

    def send(self, event: str, text: str, **extra: Any) -> bool:
        """Send one message now, regardless of state. Returns True when it left the machine."""
        payload = {
            "ts": utcnow(),
            "event": event,
            "level": EVENT_LEVELS.get(event, "info"),
            "text": f"printbridge · {text}",
            "host": self.settings.public_url or "local",
            **extra,
        }
        if not self.configured:
            self._to_outbox(payload)
            return False
        return self._post(payload)

    def on_state(self, event: str, state: str, text: str, *, key: str | None = None, **extra: Any) -> bool:
        """Send only when `state` differs from the last state recorded for this subject.

        `event` is the *kind* of alert (it decides the level and whether NOTIFY_EVENTS wants it);
        `key` is the *subject* it is about. They differ whenever one event can apply to more than
        one thing — two printers, two people — and keying the memory by event alone would make
        them overwrite each other and alert on every single check.
        """
        memory_key = key or event
        if event not in self.settings.notify_event_set and event not in {"digest", "test"}:
            self.store.set_alert_state(memory_key, state, text)
            return False
        if not self.store.set_alert_state(memory_key, state, text):
            return False
        self.send(event, text, state=state, **extra)
        return True  # a message was emitted; `send` reports whether it left the machine

    # --- the weekly digest ---

    def build_digest(self, since: datetime | None = None) -> str:
        since = since or (datetime.now(UTC) - timedelta(days=7))
        totals = self.store.totals(since)
        by_person: dict[str, dict[str, Any]] = {}
        for row in totals:
            person = by_person.setdefault(
                row["principal"], {"jobs": 0, "pages": 0, "failures": 0, "clients": {}}
            )
            person["jobs"] += row["jobs"]
            person["pages"] += row["pages"]
            person["failures"] += row["failures"] or 0
            person["clients"][row["client"]] = row["jobs"]
        lines = [f"weekly digest — {since.date()} → {datetime.now(UTC).date()}"]
        if not by_person:
            lines.append("no print jobs this week")
        for person, data in sorted(by_person.items(), key=lambda kv: -kv[1]["jobs"]):
            services = ", ".join(f"{c}: {n}" for c, n in sorted(data["clients"].items()))
            lines.append(
                f"· {person}: {data['jobs']} jobs, {data['pages']} pages"
                f"{f', {data['failures']} failed' if data['failures'] else ''} ({services})"
            )
        sessions = self.store.all_sessions(limit=200)
        recent = [s for s in sessions if s["last_seen"] >= since.isoformat(timespec="seconds")]
        lines.append(f"· sessions seen: {len(recent)}")
        errors = self.store.last_errors(limit=3)
        for err in errors:
            lines.append(f"! {err['ts'][:16]} {err['principal']}: {err['error'][:80]}")
        return "\n".join(lines)

    def send_digest(self, since: datetime | None = None) -> bool:
        return self.send("digest", self.build_digest(since))

    def due_for_digest(self, now: datetime | None = None) -> bool:
        """Monday 08:00 local, at most once a week."""
        now = now or datetime.now()
        last = self.store.get_meta("last_digest", "")
        if now.weekday() != 0 or now.hour < 8:
            return False
        if last:
            try:
                if (now.astimezone(UTC) - datetime.fromisoformat(last)) < timedelta(days=6):
                    return False
            except ValueError:
                pass
        return True

    def mark_digest_sent(self) -> None:
        self.store.set_meta("last_digest", utcnow())
