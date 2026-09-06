#!/usr/bin/env python3
"""Print PDFs that arrive in a mailbox.

An optional side door for the things an MCP connector cannot reach: a PDF on a phone, a ticket
from an airline, a form somebody e-mailed you. Forward it to a dedicated address with a secret
word in the subject, and it prints.

**This is not part of printbridge's security model.** It is a separate program with a separate
threat model, kept here because it is genuinely useful and because writing it badly is easy:

* `From:` headers are trivially forged, so the sender allowlist alone proves nothing. The subject
  token is what actually authenticates the request — treat it like a password.
* Only `application/pdf` attachments are printed. Nothing else is opened, rendered or executed.
* Every message is marked read exactly once, whatever happens, so one malformed mail cannot make
  the poller loop on it forever.

Configuration is environment variables only — see .env.example in this directory.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("mail2print")

IMAP_HOST = os.environ.get("M2P_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("M2P_IMAP_PORT", "993"))
IMAP_USER = os.environ.get("M2P_USER", "")
IMAP_PASS = os.environ.get("M2P_PASS", "")
TOKEN = os.environ.get("M2P_TOKEN", "")
PRINTER = os.environ.get("M2P_PRINTER", "home")
CUPS_SERVER = os.environ.get("CUPS_SERVER", "")
SPOOL = Path(os.environ.get("M2P_SPOOL", "/var/spool/mail2print"))
POLL_SECONDS = int(os.environ.get("M2P_POLL_SECONDS", "60"))
MAX_BYTES = int(os.environ.get("M2P_MAX_MB", "25")) * 1024 * 1024
MAX_COPIES = 5
ALLOWED = {
    address.strip().lower()
    for address in os.environ.get("M2P_ALLOWED", "").split(",")
    if address.strip()
}


class ConfigError(SystemExit):
    pass


def check_config() -> None:
    missing = [
        name
        for name, value in (
            ("M2P_USER", IMAP_USER),
            ("M2P_PASS", IMAP_PASS),
            ("M2P_TOKEN", TOKEN),
            ("M2P_ALLOWED", ",".join(ALLOWED)),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"mail2print: missing required settings: {', '.join(missing)}")
    if len(TOKEN) < 8:
        raise ConfigError("mail2print: M2P_TOKEN is shorter than 8 characters — it is a password.")


def sender_of(message: email.message.Message) -> str:
    _, address = email.utils.parseaddr(message.get("From", ""))
    return address.lower()


def subject_of(message: email.message.Message) -> str:
    raw = message.get("Subject", "")
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:  # noqa: BLE001 - a broken header must not stop the loop
        return raw


def options_from_subject(subject: str) -> list[str]:
    """`copies=2` and `simplex` in the subject line; anything else is ignored."""
    options: list[str] = []
    match = re.search(r"copies=(\d{1,2})", subject)
    if match:
        options += ["-n", str(min(int(match.group(1)), MAX_COPIES))]
    if "simplex" in subject.lower():
        options += ["-o", "sides=one-sided"]
    return options


def print_pdf(path: Path, options: list[str]) -> None:
    env = dict(os.environ)
    if CUPS_SERVER:
        env["CUPS_SERVER"] = CUPS_SERVER
    subprocess.run(  # noqa: S603 - fixed binary, list arguments, no shell
        ["lp", "-d", PRINTER, *options, str(path)],
        check=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    LOG.info("printed %s", path.name)


def handle_message(raw: bytes) -> None:
    message = email.message_from_bytes(raw)
    sender = sender_of(message)
    subject = subject_of(message)

    if sender not in ALLOWED:
        LOG.warning("rejected: sender %s is not on the allowlist", sender)
        return
    if TOKEN not in subject:
        LOG.warning("rejected: no token in the subject from %s", sender)
        return

    options = options_from_subject(subject)
    printed = 0
    SPOOL.mkdir(parents=True, exist_ok=True)
    for part in message.walk():
        if part.get_content_type() != "application/pdf":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > MAX_BYTES:
            LOG.warning("skipped an attachment from %s: %.1f MB is over the limit",
                        sender, len(payload) / 1e6)
            continue
        if payload[:5] != b"%PDF-":
            LOG.warning("skipped an attachment from %s: not a PDF despite its content type", sender)
            continue
        path = SPOOL / f"{int(time.time())}_{printed}.pdf"
        path.write_bytes(payload)
        try:
            print_pdf(path, options)
            printed += 1
        except subprocess.CalledProcessError as exc:
            LOG.error("lp failed: %s", (exc.stderr or b"").decode("utf-8", "replace").strip())
        finally:
            path.unlink(missing_ok=True)
    if printed == 0:
        LOG.info("no printable attachment from %s (subject %r)", sender, subject)


def poll_once() -> None:
    connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        connection.login(IMAP_USER, IMAP_PASS)
        connection.select("INBOX")
        _, data = connection.search(None, "UNSEEN")
        for number in data[0].split():
            try:
                _, fetched = connection.fetch(number, "(RFC822)")
                handle_message(fetched[0][1])
            except Exception as exc:  # noqa: BLE001 - one bad mail must not kill the loop
                LOG.error("error handling a message: %s", exc)
            finally:
                connection.store(number, "+FLAGS", "\\Seen")
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    check_config()
    LOG.info("mail2print started: %s -> printer %r every %ss", IMAP_USER, PRINTER, POLL_SECONDS)
    while True:
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001 - a mail server outage is not fatal
            LOG.error("poll failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
