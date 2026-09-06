# Running printbridge

What you do in steady state: **nothing**, until a message tells you exactly what to fix.

This page is what those messages mean, and the handful of commands behind them.

## The five commands

```bash
docker compose exec printbridge printbridge doctor     # what is wrong, and the next step for each ✗
docker compose exec printbridge printbridge status     # printers, who is connected, 24 h totals
docker compose exec printbridge printbridge jobs       # what has been printed
docker compose exec printbridge printbridge keys list  # who has a link
docker compose exec printbridge printbridge backup     # .env + data/, one tar
```

Add `alias pb='docker compose exec printbridge printbridge'` to your shell and they become
`pb doctor`, `pb status`, and so on.

## `status` — who is connected

```
  printers
    home: idle

  connected now (15 min)
    fanny via chatgpt — 4 calls, 1 jobs, last seen 21:07:40 from 203.0.113.9
    daniel via claude-ai — 12 calls, 3 jobs, last seen 21:09:02 from 203.0.113.9

  last 24 hours
    daniel via claude-ai: 3 jobs, 11 pages
    fanny via chatgpt: 1 jobs, 2 pages

  alerts: telegram webhook
  keys:   daniel (admin) — active, fanny (user, home, 20p) — active
```

Nobody configured "via chatgpt" — the assistant identifies itself in the MCP handshake, and
printbridge records it against the person whose key was used.

## `jobs` — what was printed

```bash
pb jobs --since 7d
pb jobs --since 24h --principal fanny
pb jobs --since 30d --csv > printing.csv
```

Columns: when, person, which assistant, pages, printer, status, title. **Titles, never content** —
see [security.md](security.md#what-is-stored-and-what-is-not).

## Keys

```bash
pb keys add fanny                       # one link, printed once
pb keys add fanny --printers home --max-pages 20 --rate 10/h
pb keys add daniel --label agent        # a second key for the same person
pb keys rotate fanny                    # new link; the old one works for KEY_GRACE_MINUTES
pb keys revoke fanny                    # that link stops working immediately
pb keys list --all                      # including revoked and expired
```

One key is one **person**, never one per assistant. A person pastes the same link into everything
they use.

## Alerts

Set `NOTIFY_WEBHOOK` in `.env` to a Telegram bot URL
(`https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>`) or any endpoint that accepts JSON.
Without one, alerts are written to `data/notify-outbox.jsonl` in the same shape — a record, not a
queue: they are never replayed later.

| Event | Level | When |
|---|---|---|
| `printer_offline` | warning | a job failed because the printer or CUPS did not answer |
| `printer_online` | info | it came back |
| `new_client` | info | a person used a new assistant for the first time (off by default) |
| `unknown_origin` | warning | a new assistant **and** a new network for that person — the "someone else has my link" signal |
| `rate_limit` | warning | a key hit its hourly limit |
| `cert_expiring` | warning | under 14 days |
| `probe_404` | warning | 20+ requests an hour to connector links that do not exist |

**One message per state change, and no repeats while the state holds.** An alarm that repeats
teaches you to ignore it. Pick which ones you want with `NOTIFY_EVENTS`.

A **weekly digest** goes out Monday at 08:00: jobs and pages per person, broken down by assistant,
plus failures and sessions seen.

## The watchdog

`printbridge doctor` answers when asked; the watchdog asks every 15 minutes and messages you when
the answer *changes*. This is what turns "a firmware update killed the route three weeks ago" into
a notification the same afternoon.

Install it from [../deploy/systemd/README.md](../deploy/systemd/README.md) — as a root unit, as a
user unit if you cannot become root, or as a cron line. It also runs the weekly digest, deletes
finished spool files and applies `JOB_LOG_RETENTION_DAYS`.

## Updating

```bash
pb update      # git pull + compose pull + up -d --build + doctor
```

Images are pinned by tag and Dependabot keeps them current in the repository. There is no
auto-update by default; `AUTO_UPDATE=weekly` is opt-in.

## Backup and restore

```bash
pb backup -o /data/printbridge-backup.tar.gz
```

`.env` plus `data/` (keys and job log) is **all** the state. Not the containers, not `/etc/cups`,
not the spool. Restore is: untar into the repository directory and `docker compose up -d`.

The tar contains your keys — treat it like the key file itself (it is written 0600).

## When a printer is asleep

Jobs are held, not lost: `printer-error-policy=retry-job`, retried every 30 seconds for two hours,
then marked failed with an alert. Wake the printer and the page comes out.

## Adding a second printer

```bash
PRINTERS=home=ipp://192.168.1.50/ipp/print,office=ipp://192.168.1.60/ipp/print
```

```bash
docker compose restart cups
pb doctor
```

The first entry is the default. Assistants can pass `printer="office"`, and a key can be restricted
to one of them with `--printers`.

## Log locations

| What | Where |
|---|---|
| printbridge | `docker compose logs printbridge` (JSON lines, rotated at 10 MB × 5) |
| CUPS | `docker compose logs cups` |
| Caddy on the host | `journalctl -u caddy` — the connector key is redacted from the path |
| Alerts without a webhook | `data/notify-outbox.jsonl` |
| Jobs and sessions | `data/printbridge.db` (SQLite), or `pb jobs` / `pb status` |
