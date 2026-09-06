# Troubleshooting

Start here, always:

```bash
docker compose exec printbridge printbridge doctor
```

Every ✗ and ! it can print has an entry below, in the order doctor prints them. If you are opening
an issue, paste the whole table — with the line containing your connector link removed.

---

## `✗ config`

The message is the fix; it names the setting and what it should be. Common ones:

* **"EXPOSE=… publishes this server, but AUTH_MODE=none."** A public endpoint with no
  authentication would let anyone who finds the address print. Either `AUTH_MODE=keys` plus
  `printbridge keys add <person>`, or `EXPOSE=none` for a local-only server.
* **"BIND=0.0.0.0:8000 would listen on every interface."** printbridge binds loopback; the proxy is
  the public face. Use `127.0.0.1:8000`.
* **"CONFIRM_ABOVE_PAGES (200) is above MAX_PAGES (100)."** The confirmation would never be
  reachable. Lower the first or raise the second.

## `✗ printers configured` — PRINTERS is empty

Set it in `.env`, one entry per printer, first one is the default:

```bash
PRINTERS=home=ipp://192.168.1.50/ipp/print
```

To try printbridge without a printer at all, use the mock queue — PDFs land in `data/mock-out`:

```bash
PRINTERS=home=cups-pdf:/
```

## `! .env permissions` — mode 644

```bash
chmod 600 .env
```

Anyone with an account on the machine could otherwise read your settings.

## `✗ keys.json permissions` — cannot read it

The file belongs to a different user than the process. In Docker this fixes itself:

```bash
docker compose restart printbridge
```

The entrypoint adopts the owner of `data/` and repairs stragglers. On bare metal, `chown` the file
to the user printbridge runs as.

## `✗ spool writable`

`data/` is not writable. Check the volume mount and the ownership of `./data`. In Docker,
`docker compose restart printbridge` normally repairs it.

## `✗ connector keys` — no keys exist

```bash
docker compose exec printbridge printbridge keys add daniel --role admin
```

It prints a link, once. Paste it into your assistant. Lost it? `keys rotate daniel` prints a new
one and keeps the old alive for an hour.

## `✗ CUPS client` — lpstat is not installed here

You are running `doctor` outside the container. Run it inside:

```bash
docker compose exec printbridge printbridge doctor
```

## `✗ CUPS reachable` — no answer

```bash
docker compose ps cups
docker compose logs cups --tail 30
```

If the container restarts in a loop, the log's last line says why. Two causes seen in the wild:

* **`cupsd did not start`** — usually `CUPS_SERVER` leaking into the CUPS container itself, making
  it look for a server elsewhere. The shipped entrypoint unsets it; a hand-edited one may not.
* **`Error - add '/version=1.1' to server name`** — this is CUPS rejecting the HTTP `Host:` header,
  not a version problem at all. The shipped `cupsd.conf` has `ServerAlias *` for exactly this. If
  you replaced that file, put it back.

## `✗ queues` — could not list queues

doctor could not ask CUPS what exists. This is the CUPS check above wearing a different hat: fix
"CUPS reachable" first, and this clears with it.

## `✗ server listening` — nothing accepts connections

Only `doctor --self` prints this (it is what the container healthcheck runs). The web server is not
up:

```bash
docker compose ps
docker compose logs printbridge --tail 30
```

The last line says why. The commonest cause is a configuration the server refuses to start with —
see `✗ config` above; printbridge would rather not start than publish an unprotected endpoint.

## `✗ queue <name>` — does not exist in CUPS

```bash
docker compose restart cups
docker compose logs cups --tail 20
```

The entrypoint recreates every queue in `PRINTERS` at each start and logs one line per queue. If it
says "printer unreachable, retrying", the printer is not reachable yet — see the next entry.

## `✗ queue <name>` — stopped

CUPS disabled the queue after repeated failures.

```bash
docker compose exec cups cupsenable <name>
docker compose exec cups lpstat -l -p <name>     # the reason
```

Jobs are held, not lost: the error policy is `retry-job` with a two-hour window.

## `! queue <name>` — answers IPP but not driverless

The printer works but does not advertise IPP Everywhere, so it needs a driver. Find one:

```bash
docker compose exec cups lpinfo -m | grep -i <make>
```

Then name it in `.env` and restart CUPS:

```bash
PRINTERS=old=socket://192.168.1.70:9100;driver=gutenprint.5.3://brother-hl-2170w/expert
```

Gutenprint and HPLIP are both installed in the CUPS image.

## `! queue <name>` — the printer did not answer an IPP query

The queue exists but the printer is asleep or the route is down. From the **host**:

```bash
ping -c 3 192.168.1.50
```

If that fails, the private route is the problem, not printbridge:

1. `tailscale status` on the server — is it connected?
2. Is the route still approved in the Tailscale admin console?
3. Is the home node (UDM or Pi) online?
4. On the server: `sudo tailscale set --accept-routes` — Linux does not accept routes by default.

### After a UniFi firmware update

A major UniFi OS upgrade can remove the Tailscale add-on despite its reinstall timer. Symptom: ping
to the printer times out from the server, everything else is fine.

```bash
ssh root@<your-udm>
tailscale status                          # "command not found" confirms it
/data/tailscale/manage.sh install!        # the "!" matters — it forces a reinstall
tailscale up --advertise-routes=192.168.1.50/32 --accept-dns=false
```

Your settings in `/data/tailscale/tailscale-env` survive. The Raspberry Pi path does not have this
failure mode at all.

## `! queue <name>` — MOCK (cups-pdf)

Working as configured: printbridge is in mock mode and PDFs go to `data/mock-out`. Point `PRINTERS`
at a real printer and `docker compose restart cups` when you have one.

## `✗ public URL` — does not resolve

The hostname in `PUBLIC_URL` has no DNS record, or it has not propagated yet. Add an A record
pointing at the server's public IP and wait for the TTL. Check with `dig +short print.example.com`.

## `! public URL` — resolves elsewhere

The hostname points at a different address than the one this server answers on. Expected behind a
CDN; otherwise the DNS record is wrong.

## `✗ TLS certificate` — handshake failed

The reverse proxy is not serving that hostname yet.

**Host Caddy** (`EXPOSE=caddy-host`):

```bash
sudo install -m 644 deploy/caddy/printbridge.caddy /etc/caddy/sites/printbridge.caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Two things that bite here:

* The main `Caddyfile` must actually import the directory: `import /etc/caddy/sites/*.caddy`.
* **Never point the snippet's log at a file you have not created.** `sudo caddy validate` runs as
  root and creates it as `root:root 0600`; Caddy then runs as the `caddy` user and fails every
  reload with "permission denied" on a file you never made. The shipped snippet logs to the journal
  precisely to avoid this. Read it with `journalctl -u caddy -f`.

**Caddy container** (`EXPOSE=caddy`): `docker compose --profile caddy up -d` and check
`docker compose logs caddy`. Ports 80 and 443 must be free and reachable — Let's Encrypt validates
over port 80.

**Funnel:** `tailscale funnel status`. If it is off, `sudo tailscale funnel --bg 8000`, and check
that the tailnet policy grants the `funnel` node attribute.

## `! tailscale key expiry` — under 30 days

Printing will stop, silently, when the key expires. In the Tailscale admin console: the machine →
**⋯ → Disable key expiry**. Do it for the server *and* the home node.

## `! notifications` — outbox only

Alerts are being written to `data/notify-outbox.jsonl` instead of reaching you. Set
`NOTIFY_WEBHOOK` in `.env` (a Telegram bot URL works) and restart. Old outbox entries are never
replayed.

## `! version` — a newer release exists

```bash
docker compose exec printbridge printbridge update
```

---

## Problems doctor cannot see

### The assistant does not show the print tools

* The connector link must be complete, including the `/mcp/pb_…` part.
* `curl -sI https://print.example.com/` should return `200`. If it returns nothing, the proxy is
  the problem, not printbridge.
* A revoked or rotated key returns 404. `printbridge keys list` shows what is active.
* Claude and ChatGPT need paid plans for custom connectors.

### "It says it printed but nothing came out"

```bash
docker compose exec printbridge printbridge jobs --since 1d
docker compose exec cups lpstat -W not-completed -o
```

If the job is still queued, the printer is asleep or unreachable and CUPS is retrying — that is by
design, and it will print when the printer comes back. If the job is `completed` and no paper
moved, the queue is a mock queue (`printbridge doctor` says so) or the printer's own display has an
error on it.

### Margins are wrong, or the page is cut off

The queue is driverless but the printer's real margins differ from the IPP defaults. Add a PPD as
described under "answers IPP but not driverless".

### Pages come out double-sided (or single) against your wishes

Defaults live in `.env` (`DEFAULT_DUPLEX`, `DEFAULT_COLOR`, `DEFAULT_PAPER`). Per job, tell the
assistant: *"print this single-sided"*.

### A 20-page document is refused

By design: above `CONFIRM_ABOVE_PAGES` (10) the assistant must confirm, above `MAX_PAGES` (100)
printbridge refuses outright. Raise them in `.env` if you mean it.

### `Rate limit reached`

`RATE_LIMIT_JOBS_PER_HOUR` (30 by default), or a lower per-key limit. Raise it with
`printbridge keys rotate <person> --rate 60/h`, or wait.

### Everything worked yesterday and nothing works today

In order of likelihood: the Tailscale key expired (see above); a UniFi update removed the add-on
(see above); the printer's address changed because its DHCP reservation was lost (Station 1 of
[SETUP.md](SETUP.md)); the server rebooted and Docker did not start
(`sudo systemctl enable --now docker`).
