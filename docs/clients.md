# Connecting an assistant or an agent

One key is one **person**, not one AI service. You paste the same link into every assistant you
use; printbridge reads which assistant it is from the MCP handshake and records it, so
`printbridge status` can say *"fanny via chatgpt, 2 jobs today"* without anyone configuring
anything.

```bash
printbridge keys add fanny            # prints her link, once
printbridge keys add daniel --role admin
printbridge keys add daniel --label agent   # a second key for his agents; still "daniel"
```

| Client | How | Verified |
|---|---|---|
| [Claude (web/desktop connector)](#claude-web-and-desktop) | paste `PUBLIC_URL/mcp/<key>`, no authentication | documented by Anthropic; **live check pending** |
| [ChatGPT](#chatgpt-developer-mode) | developer mode → same URL, "No authentication" | documented; **live check pending** |
| [Grok](#grok) | connectors, same URL | **untested — no claim made** |
| [Claude Code / Claude Desktop (config)](#claude-code-and-claude-desktop) | HTTP + `Authorization: Bearer <key>`, or stdio | ✅ verified |
| [MCP Inspector](#mcp-inspector) | `npx @modelcontextprotocol/inspector --cli <url>` | ✅ verified 2026-09-06 |
| [Agents on the same host](#agents-on-the-same-host) | `http://localhost:8000/mcp` + bearer | ✅ verified |
| [Anything, over stdio](#stdio) | `python -m printbridge serve --transport stdio` | ✅ verified |

> **Why "pending" and not "yes":** the endpoint has been proven with two independent MCP client
> implementations over real HTTPS, but nobody has yet clicked through Claude.ai's and ChatGPT's own
> connector UIs against this server. Those two rows get a date when somebody has.

---

## Claude (web and desktop)

**Settings → Connectors → Add custom connector → paste the link → Save.** No login, no client ID:
the link is the credential.

Claude's custom connectors accept either an authless endpoint or OAuth with dynamic client
registration; they cannot send a custom header. That is exactly why printbridge puts the key in the
path — see [security.md](security.md#why-the-key-is-in-the-url).

*Known wrinkle:* one report describes an organisation-admin connector UI without a "no
authentication" option. On a personal account the option is there. If yours is missing it, use
Claude Desktop's config file instead (below), which can send a header.

## ChatGPT (developer mode)

**Settings → Connectors → Advanced → Developer mode**, then **Create** → paste the link → **No
authentication**. Plus or Pro plan required.

## Grok

Grok's support for remote MCP connectors is not confirmed in documentation we can point at. Try the
same URL; if it works, please open an issue and this table gets a ✅ and a date. Until then this
project does not claim Grok support.

## Claude Code and Claude Desktop

These read a config file, so they can send a header — no key in the URL:

```bash
claude mcp add --transport http printbridge https://print.example.com/mcp \
  --header "Authorization: Bearer pb_your_key_here"
```

Over a tailnet you do not need the public address at all:

```bash
claude mcp add --transport http printbridge http://100.92.80.36:8000/mcp \
  --header "Authorization: Bearer pb_your_key_here"
```

…provided printbridge binds a tailnet address (`BIND=100.92.80.36:8000`) rather than loopback. The
default is loopback-only, which is the safer choice when a public proxy is in front.

## MCP Inspector

The quickest way to see what an assistant sees:

```bash
npx @modelcontextprotocol/inspector --cli https://print.example.com/mcp/pb_your_key \
  --transport http --method tools/list

npx @modelcontextprotocol/inspector --cli https://print.example.com/mcp/pb_your_key \
  --transport http --method tools/call --tool-name print_text \
  --tool-arg 'content=# Hello' --tool-arg 'dry_run=true'
```

`dry_run=true` renders and counts pages without printing — a safe first call.

## Agents on the same host

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    "http://localhost:8000/mcp",
    headers={"Authorization": "Bearer pb_your_key_here"},
)
async with Client(transport) as client:
    await client.call_tool("print_text", {"content": "# Nightly report\n\n…"})
```

Give agents their own labelled key so their jobs are distinguishable from yours in `printbridge
jobs`, while still belonging to you:

```bash
printbridge keys add daniel --label agent
```

## stdio

For a client that launches the server itself:

```bash
python -m printbridge serve --transport stdio
```

stdio has no network identity, so the caller is the local admin principal. Only expose it to
processes you already trust with the machine.

## Mounting printbridge inside a larger MCP server

`build_server()` returns a plain FastMCP instance with no import-time side effects, and the tool
names tolerate a prefix:

```python
from fastmcp import FastMCP
from printbridge.config import load_settings
from printbridge.server import build_server

hub = FastMCP("my-hub")
hub.mount(build_server(load_settings()), prefix="print")
```

Auth then belongs to the hub: `printbridge.auth.resolve_principal()` reads the principal off the
request scope, so whatever the hub puts there is what the job log records.

## What the assistant sees

| Tool | When it is used |
|---|---|
| `print_text` | 90 % of everything — the assistant writes markdown and prints it |
| `print_url` | a web page or an online PDF |
| `print_file` | bytes the client already has (agents, coding clients) |
| `list_printers` | "which printers do I have?" |
| `printer_status` | "why didn't it print?" |
| `get_job` / `cancel_job` | following up on a specific job |
| `recent_jobs` | "what did I print today?" — your own jobs only, unless your key is an admin key |

Every tool takes `dry_run=true` (render and count, print nothing) and, above
`CONFIRM_ABOVE_PAGES`, requires `confirmed=true`.
