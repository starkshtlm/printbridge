# Tailscale Funnel — a public HTTPS address without buying a domain

Funnel gives your server a public `https://<machine>.<tailnet>.ts.net` address with a valid
certificate and no DNS to configure. It is the path the non-technical guide uses, because there is
nothing to buy and nothing to renew.

**Trade-off, stated:** the hostname contains your tailnet name, and the address is public (anyone
who knows it can reach it — which is why connector keys exist). If you would rather use your own
domain, see [../caddy/printbridge.caddy](../caddy/printbridge.caddy).

## Setup

1. Enable Funnel for your tailnet once, in the admin console under **Access controls**, by adding
   the `funnel` node attribute:

   ```jsonc
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

2. On the server:

   ```bash
   sudo tailscale funnel --bg 8000
   tailscale funnel status        # prints the public URL
   ```

3. Put that URL in `.env`:

   ```bash
   PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   EXPOSE=funnel
   ```

   then `docker compose up -d` and `printbridge doctor`. The connector URL doctor prints is what
   you paste into your assistant.

## Turning it off

```bash
sudo tailscale funnel --bg off
```

## Notes

* Funnel forwards to a **local port**; printbridge listens on `127.0.0.1:8000`, which is exactly
  what Funnel expects. No port is opened on your router.
* Funnel terminates TLS itself, so there is no certificate to renew and `doctor`'s
  `cert_expiring` alert simply never fires.
* Funnel supports ports 443, 8443 and 10000 on the public side; the local port is your choice.
* Not verified by this project against every tailnet plan — if `tailscale funnel` refuses, the
  admin console will say which attribute is missing.
