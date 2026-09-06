# Where to run printbridge

printbridge needs exactly two things from its environment:

1. **an IP route to the printer**, and
2. **a public HTTPS address** — unless only local agents use it, in which case it needs neither a
   public address nor a route.

Everything below is a way to provide those two. The container image and the configuration are
identical in all four; only `.env` differs.

| | **A. VPS + Tailscale** | **B. Box at home** | **C. VPS + another overlay** | **D. Local only** |
|---|---|---|---|---|
| Route to printer | subnet route from a home node | same LAN — none needed | WireGuard / Netbird / Headscale | same LAN or tailnet |
| Public HTTPS | Caddy on your domain, or Funnel | Funnel or Cloudflare Tunnel | Caddy | none |
| Best for | you already rent a server | simplest; no VPS to pay for | you avoid Tailscale's control plane | agents on the same host |
| Maintenance | the home node must survive firmware updates | lowest — one box, no route | same as A | none |
| `EXPOSE=` | `caddy-host` / `caddy` / `funnel` | `funnel` / `cloudflared` | `caddy-host` / `caddy` | `none` |

The non-technical guide leads with **B** because it has the fewest parts. **A** is what this
repository is developed on.

---

## A. VPS + Tailscale subnet router

```
assistant ──HTTPS──> VPS (printbridge) ──tailnet──> home node ──LAN──> printer
```

```bash
PRINTERS=home=ipp://192.168.1.50/ipp/print
PUBLIC_URL=https://print.example.com
EXPOSE=caddy-host          # a proxy already owns 80/443 here
```

Set up the route with [../deploy/tailscale/subnet-router.md](../deploy/tailscale/subnet-router.md),
then `sudo tailscale set --accept-routes` on the VPS. `ping <PRINTER_IP>` must succeed before
printbridge can print.

`EXPOSE=caddy-host` ships a site snippet for the proxy you already run;
`EXPOSE=caddy` starts printbridge's own Caddy container (`docker compose --profile caddy up -d`)
for a host with nothing on 80/443.

**The one thing that breaks:** the home node. A UniFi firmware update can remove the Tailscale
add-on; the watchdog turns that into a message instead of a mystery.

## B. Box at home

```
assistant ──HTTPS(Funnel/Cloudflare)──> Pi or NAS (printbridge) ──LAN──> printer
```

No VPN, no route, nothing to approve. A Raspberry Pi 4 or 5, a NAS that runs Docker, or an old
laptop that stays on.

```bash
PRINTERS=home=ipp://192.168.1.50/ipp/print
PUBLIC_URL=https://your-box.tail1234.ts.net
EXPOSE=funnel
```

```bash
sudo tailscale funnel --bg 8000     # public HTTPS, no domain to buy
```

See [../deploy/tailscale/funnel.md](../deploy/tailscale/funnel.md). Cloudflare Tunnel is the
alternative:

```bash
EXPOSE=cloudflared
```

with a tunnel pointed at `http://127.0.0.1:8000`. Both terminate TLS for you, so there is no
certificate to renew.

**USB printers** live here naturally: plug the printer into the box, let its CUPS own it, and point
`PRINTERS` at the local queue.

## C. VPS + an overlay that is not Tailscale

printbridge does not know which overlay you use — it only needs `ping <PRINTER_IP>` to work.

* **WireGuard site-to-site:** a peer at home advertising the printer's subnet with `AllowedIPs`.
* **Netbird / ZeroTier:** their own routing feature, same shape as a Tailscale subnet route.
* **[Headscale](https://headscale.net):** the Tailscale client with your own control plane; the
  home-node recipes work unchanged.

Configuration is identical to A. Only `doctor`'s Tailscale key-expiry check goes quiet, because
there is no Tailscale to ask.

## D. Local only

No public surface at all: agents on the same host, or clients on your tailnet.

```bash
PRINTERS=home=ipp://192.168.1.50/ipp/print
EXPOSE=none
AUTH_MODE=keys          # or none — allowed only because EXPOSE=none
```

Agents connect to `http://localhost:8000/mcp` with a bearer key, or launch
`python -m printbridge serve --transport stdio`.

`AUTH_MODE=none` is accepted *only* with `EXPOSE=none`; the configuration validator refuses the
combination that would put an unauthenticated endpoint on the internet.

---

## The same acceptance test everywhere

Whichever you pick, the finish line is identical:

```bash
docker compose exec printbridge printbridge doctor      # green, and prints the connector link
```

then, from an assistant or an agent, *"print a test page on my printer"* — and a page comes out.
