# printbridge

**Let an AI assistant print on your own printer.** You paste one link into Claude, ChatGPT or any
other MCP client, then type *"print this on my printer"* — and the page comes out at home.
No print cloud, no third party ever sees the document, no ports opened on your home router.

> **New here and not a developer?** Start with **[docs/SETUP.md](docs/SETUP.md)** — a
> seven-station guide, from a printer without a fixed address to a printed test page.
> Everything below is the technical layer underneath it.

MIT licensed · self-hosted · Python + [FastMCP](https://gofastmcp.com) + CUPS.

## What it is

printbridge is an MCP server. Assistants call its tools (`print_text`, `print_url`, `print_file`,
`list_printers`, `get_job`, `cancel_job`, `printer_status`, `recent_jobs`); the server renders the
document to PDF and hands it to a CUPS queue that reaches your printer over a private route —
Tailscale by default, but any route works.

```
┌───────────────────────── Internet ─────────────────────────┐
│  Claude.ai   ChatGPT   Grok   Claude Desktop   Claude Code │
│      └──────────┴────────┴──────────┴──────────────┘       │
│        remote MCP (Streamable HTTP on a per-person key)    │
└───────────────────────────────┬────────────────────────────┘
                                ▼  HTTPS (Caddy, Funnel or Cloudflare Tunnel)
┌──────────────────── Server (tailnet node) ──────────────────┐
│  printbridge            127.0.0.1:8000 — never public       │
│  ├─ MCP server (FastMCP)          ← local agents: bearer    │
│  ├─ Renderer   markdown/html/text → PDF (WeasyPrint)        │
│  ├─ Auth       per-person connector keys → Principal        │
│  ├─ Job log    SQLite: metadata only, never content         │
│  └─ Spooler    CUPS (driverless IPP Everywhere queue)       │
│                     │ ipp://PRINTER_IP:631                  │
└─────────────────────┼───────────────────────────────────────┘
                      │ private route (Tailscale subnet router, WireGuard, or same LAN)
┌─────────────────────▼──── Home network ─────────────────────┐
│  Subnet router: UDM Pro | Raspberry Pi | any Linux box      │
│                      │                                      │
│  Printer (any IPP Everywhere / AirPrint printer)            │
└─────────────────────────────────────────────────────────────┘
```

The server is the only thing with a public surface, and that surface is one authenticated MCP
endpoint. The printer only ever sees traffic from your own private network.

## Quickstart (10 minutes, for people who know a terminal)

```bash
git clone https://github.com/starkshtlm/printbridge && cd printbridge
cp .env.example .env && $EDITOR .env     # PRINTERS, PUBLIC_URL, EXPOSE
docker compose up -d
docker compose exec printbridge printbridge init      # wizard: printer → exposure → first key
docker compose exec printbridge printbridge doctor    # green table + your connector URL
```

Or let the installer do all of it:

```bash
curl -fsSL https://raw.githubusercontent.com/starkshtlm/printbridge/main/install.sh | bash
```

Then paste the connector URL from `doctor` into your assistant
(see **[docs/clients.md](docs/clients.md)**) and say *"print a test page on my printer"*.

## One key per person, not per assistant

```bash
printbridge keys add daniel --role admin
printbridge keys add fanny  --printers home --max-pages 20
printbridge keys list
```

Each person gets **one** link, `https://print.example.com/mcp/<key>`, and pastes the same link into
every assistant they use. Which assistant they used is detected automatically from the MCP
handshake, so `printbridge status` can say *"fanny via chatgpt, 2 jobs today"* without anybody
configuring anything. Revoking one person leaves everyone else alone.

## What it will not do

* It will not print more than `MAX_PAGES` (default 100), and above `CONFIRM_ABOVE_PAGES`
  (default 10) it refuses until the assistant passes `confirmed=true`. An injected
  *"print 500 pages"* fails safely.
* `print_url` will not fetch from your LAN, your tailnet or localhost unless you set
  `ALLOW_PRIVATE_URLS=true`. Every redirect hop is re-checked.
* It never stores what you printed — only *that* you printed: title, pages, printer, person,
  assistant, status. The spool file is deleted when the job leaves the queue.
* It never binds a public interface. `127.0.0.1:8000` for MCP, `127.0.0.1:8001` for admin and
  metrics; a reverse proxy is the only public face.

## Documentation

| Document | For |
|---|---|
| **[docs/SETUP.md](docs/SETUP.md)** | Non-technical, start to finish, seven stations |
| [docs/clients.md](docs/clients.md) | Claude.ai, ChatGPT, Grok, Claude Desktop/Code, agents, stdio |
| [docs/security.md](docs/security.md) | Threat model; what a connector key does and does not protect |
| [docs/operations.md](docs/operations.md) | update, backup, watchdog, alerts, `status` and `jobs` |
| [docs/topologies.md](docs/topologies.md) | VPS+Tailscale, home box, WireGuard/Headscale, local only |
| [docs/troubleshooting.md](docs/troubleshooting.md) | One entry per `doctor` finding |
| [deploy/tailscale/subnet-router.md](deploy/tailscale/subnet-router.md) | UDM Pro, Raspberry Pi, generic Linux |
| [extras/mail2print](extras/mail2print/) | Optional: print PDFs sent to a mailbox |

## Status

v1.0.0 — verified end to end on 2026-09-07: a page out of a real printer from Claude.ai, from
ChatGPT in developer mode, from Claude Code over a bearer token, and from an agent on the server
itself. Grok is untested and this README will not claim it until somebody runs it.

## Licence

MIT — see [LICENSE](LICENSE).
