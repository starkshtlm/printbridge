# Security model

## What is at stake

printbridge has a public endpoint on the internet and a private path to a printer inside somebody's
home. The interesting question is not "can someone print without permission" — the answer to that
is "they would need your link, and the damage is paper". The interesting question is **whether the
endpoint can be used to reach anything else**: the LAN behind it, the documents that pass through
it, or the machine it runs on.

**Threat model.** The public endpoint is reachable by anyone. Tool arguments are written by a
language model that may have been prompt-injected by the document it is summarising. The printer,
the LAN and the job log must stay out of reach in both cases.

## What protects what

| Control | Protects against | Does not protect against |
|---|---|---|
| Per-person connector keys | someone who finds the hostname; one person's leak | someone who has your link |
| 404 for a wrong key | probing that maps which endpoints exist | a valid key |
| SSRF guard on `print_url` | an injected *"print http://192.168.1.1/admin"* | a public URL that is merely embarrassing |
| Page limits + confirmation | an injected *"print 500 pages"*; a runaway loop | a person who wants to waste their own paper |
| Rate limit per key | one leaked key burning a ream overnight | a slow, patient abuser |
| Loopback binding | the internet reaching the app directly | someone who is already on the host |
| Admin port never proxied | usage data leaking to the internet | someone on the host or the tailnet |
| No content in the job log | a stolen backup revealing what you printed | someone reading the spool during the seconds a job takes |
| Spool deleted after the job | documents accumulating on disk | a backup taken mid-job |

## Why the key is in the URL

Claude's web connectors cannot send an `Authorization` header — they accept an authless endpoint or
full OAuth with dynamic client registration. So for a chat client the choice is: put the secret in
the path, or build OAuth.

For a printer, the path secret is proportionate, and it is the same model as a Slack or Zapier
webhook: the URL *is* the credential, protected by TLS. The worst case if it leaks is wasted paper
until you run `printbridge keys revoke <person>`. There is no data to exfiltrate through it — the
tools print, they do not read.

Consequences we accept, and the mitigations:

* **The key appears in browser history and in the connector settings page.** Per-person keys mean
  one screenshot leaks one person. `keys rotate` gives a new link and keeps the old one alive for
  an hour so nobody is locked out mid-paste.
* **The key would appear in an access log.** The shipped Caddy snippet redacts the path, and
  `LOG_REQUEST_PATH=false` keeps it out of the application log. `doctor` complains if you turn that
  on.
* **A key used from a new assistant *and* a new network** raises an `unknown_origin` alert — the
  "someone else has my link" signal. A new assistant from a network you already use is just you,
  pasting your link somewhere new, and is only informational.

**When to switch to OAuth** (designed in as `AUTH_MODE=github|google`, shipping in v1.1): when the
same endpoint exposes anything more sensitive than printing, when you need per-person audit that
survives a shared link, or when more people use it than you can name.

## Identity

Everything downstream keys off a `Principal`, never off a key or a header:

```python
Principal(user, role, method, key_label, printers, max_pages, rate_per_hour)
```

`auth.resolve_principal(request)` is the only place identity is decided. Path and bearer produce
the same principal. The job log, the sessions, the rate limits and `recent_jobs` scoping all read
`Principal`, which is why adding OAuth later is a new backend rather than a rewrite.

Roles: `user` prints and sees their own jobs; `admin` also sees everyone's. Per key: which printers,
a lower page ceiling, a lower rate limit.

## The SSRF guard, in detail

`print_url` takes a URL from a language model. The guard:

1. accepts only `http` and `https`;
2. rejects a literal private address immediately;
3. resolves the hostname and rejects the request if **any** answer is loopback, RFC 1918,
   link-local (including `169.254.169.254`, the cloud metadata address), CGNAT `100.64/10` — which
   is the tailnet range, so a Tailscale IP is refused like any other private address — IPv6 ULA,
   multicast or reserved;
4. re-checks every redirect hop, at most three;
5. counts downloaded bytes rather than trusting `Content-Length`.

The renderer is guarded separately: WeasyPrint is given a URL fetcher that allows `data:` URIs and
nothing else, so a document containing `<img src="http://192.168.1.1/…">` cannot turn rendering
into a request. HTML input passes through an allowlist first — no scripts, no event handlers, no
remote stylesheets.

**Residual risk, stated rather than hidden:** a hostname whose DNS answer changes between our check
and the HTTP client's own connection (DNS rebinding) is not fully closed. Closing it means pinning
the connection to the checked address, which breaks virtual hosting and TLS SNI in ways that are
worse than the risk here — the reachable result is one page of paper, or one HTTP GET to an
internal address whose response is rendered but never returned to the caller.

`ALLOW_PRIVATE_URLS=true` disables all of this deliberately, for people who genuinely want to print
their router's status page. `doctor` warns whenever it is on.

## What is stored, and what is not

**Stored** (`data/printbridge.db`, SQLite): job id, time, person, which of their keys, which
assistant, tool name, title, page count, copies, printer, options, status, error, duration, and
whether the source was text, a URL or a file.

**Never stored:** the document, the URL, the file bytes. `print_url` logs the page's `<title>`, not
its address.

Titles are logged because they are the only way to answer "what did I print?". An assistant can
pass `title="(untitled)"` when even that is too much.

The rendered PDF lives in the spool only until the job leaves the queue (`KEEP_SPOOL_HOURS=0`).

## The network shape

* printbridge binds `127.0.0.1:8000`. The reverse proxy is the only public face. Config validation
  refuses `0.0.0.0` outright.
* Inside a container that would mean the container's own loopback, which no proxy could reach, so
  there it binds the container namespace and Docker's `127.0.0.1:8000:8000` publish rule enforces
  loopback on the host. A test asserts the compose file never publishes anything else.
* The admin app (`/status`, `/metrics`) binds `127.0.0.1:8001` and is never proxied. It knows who
  printed what and when; that belongs on the host, not on the internet.
* CUPS publishes no port at all and is reachable only on the compose network.
* The printer is reached over the private route. No port forward exists anywhere in this design.

## Secrets on disk

* `data/keys.json` — 0600, hashed (SHA-256 over a 256-bit random token; the token has full entropy,
  so a slow KDF would buy nothing). Plaintext is shown once and never logged.
* `.env` — settings only, mode 0600. `doctor` complains if it is world-readable.
* Backups (`printbridge backup`) contain both. Treat the tar like the key file.

## Reporting a vulnerability

Open a GitHub issue for anything that is already public, and for anything that is not, use GitHub's
private vulnerability reporting on the repository. Please include what you tried and what you got —
`printbridge doctor` output is useful, with the connector link removed.
