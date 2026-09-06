# Getting a route to the printer

printbridge needs exactly one thing from your network: **an IP route from the server to the
printer**. Everything below is a way to provide it. Pick one; you never need two.

| Recipe | Hardware | Effort | Breaks when |
|---|---|---|---|
| [UniFi Dream Machine](#a-unifi-dream-machine-udm-udm-pro-udr) | none (you own it) | ~15 min | a major UniFi OS upgrade removes the add-on |
| [Raspberry Pi](#b-raspberry-pi-or-any-always-on-linux-box) | ~€60 | ~20 min | practically never |
| [Any Linux box you already have](#b-raspberry-pi-or-any-always-on-linux-box) | none | ~10 min | the box is off |
| [No route at all](#d-no-subnet-router-server-on-the-same-network) | none | 0 min | — (server is on the same LAN) |

If you are not comfortable in a terminal, take the Raspberry Pi path or run printbridge on a box
at home (topology B in [../../docs/topologies.md](../../docs/topologies.md)) — that needs no
subnet router at all.

---

## A. UniFi Dream Machine (UDM, UDM Pro, UDR)

There is **no official Ubiquiti Tailscale app**. The community package
[`tailscale-unifi`](https://github.com/SierraSoftworks/tailscale-unifi) installs Tailscale's own
Debian package on UniFi OS and adds a timer that reinstalls it after firmware updates.

**The honest caveat:** a *major* UniFi OS upgrade can still remove it. You then SSH in and run one
command. If that sentence makes you uneasy, use the Pi.

1. Give the printer a fixed address in the UniFi app: **Client Devices → the printer → Settings →
   Fixed IP Address**. Write the address down; it is your `PRINTER_IP`.
2. Enable SSH: **Settings → Control Plane → Console → Advanced → SSH**, set a password.
3. From your own computer:
   ```bash
   ssh root@<your-udm-ip>
   curl -sSLq https://raw.githubusercontent.com/SierraSoftworks/tailscale-unifi/main/install.sh | sh
   tailscale status      # "Logged out" or a login prompt — anything but "command not found"
   ```
   If the installer says it cannot detect the OS version, stop and use the Pi path.
4. Advertise only the printer, not the whole network:
   ```bash
   tailscale up --advertise-routes=<PRINTER_IP>/32 --accept-dns=false
   ```
   Open the login link it prints and sign in.
5. In the [Tailscale admin console](https://login.tailscale.com/admin/machines):
   * the UDM node → **⋯ → Edit route settings → approve** the route. *Advertising does nothing
     until it is approved* — this is the step everyone forgets.
   * **⋯ → Disable key expiry** for the UDM node **and** the server node. Otherwise printing dies
     silently in a few months.

**After every UniFi firmware update:** `ssh root@<udm>`, `tailscale status`. If Tailscale is gone:

```bash
/data/tailscale/manage.sh install!     # the "!" matters — it forces a reinstall
tailscale up --advertise-routes=<PRINTER_IP>/32 --accept-dns=false
```

## B. Raspberry Pi (or any always-on Linux box)

Any Linux machine on the printer's network works — a Pi, a NAS with Docker, an old laptop.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-routes=<PRINTER_IP>/32 --accept-dns=false
```

Then approve the route and disable key expiry in the admin console, exactly as in step 5 above.

To advertise the whole home network instead of one printer, use your subnet:
`--advertise-routes=192.168.1.0/24`. Advertising only the printer is tighter and enough.

## C. The server side (do this once, wherever the printer route comes from)

Linux does not accept subnet routes unless you say so:

```bash
sudo tailscale set --accept-routes
ping -c 3 <PRINTER_IP>          # must succeed before printbridge can print
```

If the ping fails, the route is not approved, the home node is offline, or a firewall on the
printer's network blocks it. `printbridge doctor` reports the same thing from the application side.

## D. No subnet router: server on the same network

If printbridge runs on a box on the same network as the printer, there is nothing to route: set
`PRINTERS=home=ipp://<PRINTER_IP>/ipp/print` and you are done. You still need a public HTTPS
address for chat assistants — see [../../docs/topologies.md](../../docs/topologies.md).

## E. Not Tailscale

printbridge does not know or care which overlay you use. WireGuard site-to-site, Netbird, ZeroTier
and self-hosted [Headscale](https://headscale.net) all work: the only requirement is that
`ping <PRINTER_IP>` succeeds from the server. Tailscale is the default in the docs because its
home-side step is "paste two commands and click Approve".

## A tighter tailnet (optional)

A Tailscale ACL that lets the server reach only the printer's IPP port limits what a compromised
server could touch:

```jsonc
{
  "acls": [
    { "action": "accept", "src": ["tag:printserver"], "dst": ["<PRINTER_IP>:631"] }
  ]
}
```
