# mail2print — the e-mail side door

**Optional, and not part of printbridge.** It is a separate 200-line program that watches a mailbox
and prints PDF attachments. It exists because some things never reach an AI assistant: a boarding
pass on a phone, a PDF a school sent, a form somebody e-mailed you. Forward it, and it prints.

If you only ever print through an assistant, ignore this directory.

## How it decides to print

Both must be true:

1. the sender is on `M2P_ALLOWED`, **and**
2. the subject contains `M2P_TOKEN`.

`From:` headers are forgeable, so the allowlist alone proves nothing — **the token is the actual
credential**. Treat it like a password: choose something unguessable, and rotate it if it leaks
(it is visible to anyone who can see the subject line of a print e-mail).

Only `application/pdf` attachments are printed, and only after the bytes are checked to actually
start with `%PDF-`. Anything else is logged and ignored.

## Setup

1. **A dedicated mailbox.** Never a personal one — everything unread in it becomes a print
   candidate. With Gmail: enable 2-step verification, then create an *app password*.

2. **Install:**

   ```bash
   sudo mkdir -p /opt/mail2print
   sudo cp mail2print.py /opt/mail2print/
   sudo cp .env.example /etc/mail2print.env
   sudo chmod 600 /etc/mail2print.env
   sudo $EDITOR /etc/mail2print.env        # fill in the mailbox, the token and the allowlist
   ```

3. **Point it at a printer.** Either a CUPS queue on the host (`CUPS_SERVER=127.0.0.1:631`) or
   printbridge's CUPS container. For the latter, publish nothing — run mail2print on the same host
   and set `CUPS_SERVER` to the container's address on the compose network, or add mail2print as a
   third compose service.

4. **Run it:**

   ```bash
   sudo cp mail2print.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mail2print
   journalctl -u mail2print -f
   ```

## Test it — including the failures

```
To:      print@example.com
Subject: YOUR-TOKEN test
Attach:  any small PDF
```

It should print within a minute. Then check that the guards hold, because an untested guard is not
a guard:

* the same mail **without** the token → the journal says `rejected: no token in the subject`;
* a mail from an address not on the allowlist → `rejected: sender … is not on the allowlist`;
* a mail with a `.docx` attached → `no printable attachment`.

## Subject-line options

| Write this | Effect |
|---|---|
| `copies=2` | two copies (capped at 5) |
| `simplex` | single-sided |

## What it does not do

No OCR, no format conversion, no reply, no delivery report. If the print fails, the failure is in
the journal and the message is still marked read — it will not retry, and it will not loop.

## Known limitation

If the process dies between printing and marking a message read, that message prints again on the
next poll. Rare enough to accept; if it ever happens to you, add a Message-ID ledger.
