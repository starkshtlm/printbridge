# Set up printbridge — from nothing to a printed page

This guide takes you from "I have a printer" to "I can tell Claude or ChatGPT to print something
and paper comes out at home". It assumes you have **never opened a terminal**. Every command is in
its own box with a line saying what it does, and every station ends with a checkpoint that tells
you exactly what success looks like.

**Total time: 60–90 minutes.** You can stop after any station and come back later.

> **About the pictures.** Screenshots are being added. Where one is missing you will see a broken
> image and, under it, an italic line describing exactly what that screen shows — enough to find it
> yourself.

You will need:

* a printer made in roughly the last ten years that supports AirPrint or Mopria (almost all do —
  if it can print from an iPhone, it qualifies);
* your home router's admin app;
* a computer;
* about €4 a month for a small server, **or** a Raspberry Pi / NAS / old laptop you leave on at home.

> **Terminal.** A window where you type commands instead of clicking. On a Mac press ⌘+Space, type
> "Terminal", press Enter. On Windows press the Start button and type "PowerShell". You will paste
> commands into it and press Enter. That is all.

---

## Station 0 — Two questions before you start

**Time: 5 minutes.** Answer these two questions and the rest of the guide has one path, not four.

### Question 1: where should printbridge run?

| | **A small rented server** | **A box at home** |
|---|---|---|
| What it is | A "VPS" — a computer in a data centre, ~€4/month | A Raspberry Pi, a NAS, an old laptop — anything that stays on |
| Extra hardware | none | €60 for a Pi, or something you own |
| Extra setup | you must connect your home network to it (Station 3) | **none** — it is already on the same network as the printer |
| Best if | you already rent a server, or you want the server off your home network | you want the simplest possible setup |

**If you are unsure, choose the box at home.** It is fewer moving parts: you can skip Station 3
entirely, and there is no network route that can break.

### Question 2 (only if you chose a rented server): what is your router?

* **A UniFi Dream Machine** (UDM, UDM Pro, UDR) → you can use the router itself in Station 3.
  Honest warning: this uses an add-on Ubiquiti does not support, and a major firmware update can
  remove it. The fix is one command, but you have to notice and run it.
* **Anything else, or you would rather not think about firmware** → use a Raspberry Pi in
  Station 3. It costs about €60 and then never breaks.

### Write this down

Copy this into a note. You will fill it in as you go.

```
Printer's address (IP):         ____________________  (Station 1, everybody)
Printer's name for printbridge: home                  (you can change this)
Tailscale account e-mail:       ____________________  (Station 2, rented server only)
Server address:                 ____________________  (Station 4)
Connector link:                 ____________________  (Station 5, everybody)
```

**If you chose "a box at home", your route through this guide is: Station 1 → Station 4 →
Station 5 → Station 6.** Stations 2 and 3 say so at the top; skip them without worrying.

**Checkpoint:** you know whether you are renting a server or using a box at home, and you have the
note above. ✅

---

## Station 1 — Give the printer a fixed address

**Time: 5 minutes.** Skip this only if you already know your printer never changes address.

Every device on your network has an address like `192.168.1.50`. Your router hands these out and
can change them — and if the printer's address changes, printing stops. So we pin it.

1. Open your router's app or web page. (UniFi: the UniFi Network app. Others: usually
   `http://192.168.1.1` in a browser — the address and password are often printed on the router.)
2. Find the list of connected devices. In UniFi it is **Client Devices**. Search for the printer by
   brand name.
3. Open the printer's entry, find **Fixed IP Address** (sometimes "DHCP reservation" or "Static
   lease"), turn it on, and keep the address it already has.
4. Save.

![Station 1 — the printer's fixed address in UniFi](setup/01-fixed-ip.png)
*Screenshot to take: the UniFi Client Devices page with the printer selected, the Settings tab
open, and the "Fixed IP Address" toggle switched on with the address visible.*

**Checkpoint:** type that address into a browser (for example `http://192.168.1.50`). The printer's
own web page opens. Write the address in your note. ✅

*If nothing opens:* some printers have no web page. Instead, check that the address still appears
in the router's device list after you switch the printer off and on again.

---

## Station 2 — Create a Tailscale account

**Time: 5 minutes.** Skip this station if you chose "a box at home" in Station 0 — you do not need
it.

Tailscale connects your server to your home network privately, without opening anything on your
router. It is free for personal use.

1. Go to [tailscale.com](https://tailscale.com) and sign up (it uses a Google, Microsoft or GitHub
   login — no new password).
2. Install the Tailscale app on your phone or laptop and sign in with the same account.

![Station 2 — the Tailscale admin console with one machine](setup/02-first-machine.png)
*Screenshot to take: the Machines page at login.tailscale.com/admin/machines showing your phone or
laptop as the only entry.*

**Checkpoint:** the admin console at
[login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines) lists at least one
machine. ✅

---

## Station 3 — Connect your home network

**Time: 15 minutes.** Skip this station if you chose "a box at home" — the box is already on the
printer's network, and there is nothing to connect.

This station gives your rented server a private path to the printer. Pick the path you chose in
Station 0.

> **SSH.** A way to type commands on another machine — your router or your Pi — from your own
> computer. You will use it twice and then never again.

### Path A — UniFi Dream Machine

1. In the UniFi console: **Settings → Control Plane → Console → Advanced → SSH**. Turn it on and
   set a password.

   ![Station 3 — enabling SSH on the UDM](setup/03a-udm-ssh.png)
   *Screenshot to take: the Control Plane → Console → Advanced page with the SSH toggle on and the
   password field filled (blur the password).*

2. Open a terminal on your computer and connect to the router. Replace `192.168.1.1` with your
   router's address.

   ```bash
   ssh root@192.168.1.1
   ```
   *What it does: opens a command line on the router. It will ask for the password you just set.*

3. Install Tailscale on the router.

   ```bash
   curl -sSLq https://raw.githubusercontent.com/SierraSoftworks/tailscale-unifi/main/install.sh | sh
   ```
   *What it does: downloads and runs the community installer that puts Tailscale on UniFi OS.*

   **If it says it cannot detect your UniFi OS version, stop.** Use Path B instead — the Pi. Your
   router is fine; the add-on simply does not support it yet.

4. Start Tailscale and tell it to share the printer. Replace `192.168.1.50` with your printer's
   address from Station 1.

   ```bash
   tailscale up --advertise-routes=192.168.1.50/32 --accept-dns=false
   ```
   *What it does: connects the router to your Tailscale account and offers a private path to the
   printer — only the printer, nothing else on your network.*

   It prints a web link. Open it in a browser and sign in with the same account as Station 2.

### Path B — Raspberry Pi

1. Install Raspberry Pi OS with the [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
   choose "Raspberry Pi OS Lite", and in the settings gear turn on SSH and set a username and
   password. Write it to the SD card, put the card in the Pi, plug the Pi into power and into your
   network with a cable.

   ![Station 3 — Raspberry Pi Imager settings](setup/03b-pi-imager.png)
   *Screenshot to take: the Imager's settings dialog with "Enable SSH" ticked and a username set.*

2. From your computer, connect to it (replace `pi` with the username you chose):

   ```bash
   ssh pi@raspberrypi.local
   ```
   *What it does: opens a command line on the Pi.*

3. Install Tailscale and share the printer, replacing `192.168.1.50` with your printer's address:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --advertise-routes=192.168.1.50/32 --accept-dns=false
   ```
   *What it does: connects the Pi to your Tailscale account and offers a private path to the
   printer.*

   Open the link it prints and sign in.

### Both paths: approve the route

This is the step everyone forgets, and nothing works without it.

1. Go to [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines).
2. Find the new machine (your router or Pi). Click **⋯ → Edit route settings** and switch the
   route on.

   ![Station 3 — approving the route](setup/03c-approve-route.png)
   *Screenshot to take: the "Edit route settings" panel with the printer's /32 route toggled on.*

3. On the same **⋯** menu choose **Disable key expiry**. Do this for every machine you add,
   including the server in Station 4.

   ![Station 3 — disabling key expiry](setup/03d-key-expiry.png)
   *Screenshot to take: the machine's ⋯ menu with "Disable key expiry" visible, and the "Expiry
   disabled" label on the machine row afterwards.*

   *Why: without this, everything stops working a few months from now, silently, and you will have
   forgotten this guide existed.*

**Checkpoint:** the admin console shows your router or Pi with a **Subnet router** badge and the
route enabled, and the machine says "Expiry disabled". ✅

---

## Station 4 — Get a server

**Time: 15 minutes.**

### If you chose "a box at home"

Use a Raspberry Pi, a NAS that runs Docker, or an old laptop. It must be on the same network as the
printer and stay switched on. There is no route to set up: the box can already reach the printer.

**A new Raspberry Pi:** install Raspberry Pi OS with the
[Raspberry Pi Imager](https://www.raspberrypi.com/software/) — choose "Raspberry Pi OS Lite", and
in the settings gear turn on SSH and set a username and password. Put the card in the Pi, plug it
into power and into your network with a cable.

![Station 4 — Raspberry Pi Imager settings](setup/03b-pi-imager.png)
*Screenshot to take: the Imager's settings dialog with "Enable SSH" ticked and a username set.*

**Then, from your own computer** (replace `pi` with the username you chose):

```bash
ssh pi@raspberrypi.local
```
*What it does: opens a command line on the box. If the name does not work, use its address from
your router's device list, e.g. `ssh pi@192.168.1.23`.*

**Checkpoint:** on the box, this succeeds (replace with your printer's address):

```bash
ping -c 3 192.168.1.50
```
*What it does: asks the printer "are you there?" three times.*

Three lines saying `64 bytes from …` means yes. Then go straight to Station 5. ✅

### If you chose a rented server

1. Create a small server at any provider — [Hetzner](https://www.hetzner.com/cloud) and
   [DigitalOcean](https://www.digitalocean.com) both work. Choose the smallest Ubuntu option
   (1 shared CPU, 2 GB RAM is plenty; ~€4/month). Save the password or SSH key it gives you.

   ![Station 4 — creating the server](setup/04a-create-server.png)
   *Screenshot to take: the provider's create-server page with Ubuntu selected and the smallest
   size highlighted.*

2. Connect to it from your computer (replace with your server's address):

   ```bash
   ssh root@203.0.113.10
   ```
   *What it does: opens a command line on your new server.*

3. Put the server on your Tailscale network and let it use the route from Station 3:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   sudo tailscale set --accept-routes
   ```
   *What it does: joins the server to your private network and lets it use the path to your printer.
   The first command prints a link — open it and sign in.*

4. Turn off key expiry for this machine too, in the admin console (Station 3, step 3).

**Checkpoint:** on the server, this must succeed (replace with your printer's address):

```bash
ping -c 3 192.168.1.50
```
*What it does: asks the printer "are you there?" three times.*

Success looks like three lines saying `64 bytes from 192.168.1.50 …`. Failure looks like
`Destination Host Unreachable` or nothing at all for ten seconds.

*If it fails:* the route is not approved (Station 3, "approve the route"), or the router/Pi is
offline, or you typed a different address than the one you fixed in Station 1. ✅

---

## Station 5 — Install printbridge

**Time: 10 minutes, most of it waiting.**

Everything here happens on the server (or the box at home) — the machine you connected to at the
end of Station 4.

### First: decide how assistants will reach it

The installer will ask. Answer it now so you are not deciding mid-wizard.

* **You have no domain name** (most people): use **Tailscale Funnel**, which gives you a web
  address for free. Set it up *before* running the installer:

  ```bash
  sudo tailscale funnel --bg 8000
  tailscale funnel status
  ```
  *What it does: gives this machine a public web address ending in `.ts.net` and points it at
  printbridge. Copy the `https://…` address it prints — the wizard asks for it.*

  (Funnel must be allowed in your tailnet. If the command complains, open
  [login.tailscale.com/admin/acls](https://login.tailscale.com/admin/acls) and follow
  [the Funnel notes](../deploy/tailscale/funnel.md).)

* **You do have a domain name** and want `print.yourname.com`: point an A record at the server's
  public IP, then answer `caddy` (or `caddy-host` if a web server already runs there). See
  [topologies.md](topologies.md).

### Then: one command

```bash
curl -fsSL https://raw.githubusercontent.com/starkshtlm/printbridge/main/install.sh | bash
```
*What it does: installs Docker if it is missing, downloads printbridge into a folder called
`printbridge` in your home directory, builds it, and starts the setup wizard.*

The wizard asks three questions:

1. **Printer address** — from Station 1. Just `192.168.1.50` is enough.
2. **A name for it** — press Enter to accept `home`.
3. **How assistants should reach it** — `funnel` if you followed the Funnel path above, then paste
   the `https://…ts.net` address when it asks for the public address.

> **If a command says `permission denied … docker.sock`:** the installer just added you to the
> `docker` group, which only takes effect in a new session. Type `exit`, connect again with `ssh`,
> `cd printbridge`, and run `docker compose up -d`.

At the end it prints a table of checks and **your connector link**:

```
https://your-box.tail1234.ts.net/mcp/pb_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**Copy that link into your note now** — it is shown once. Scrolled past it? You are not locked out:

```bash
cd ~/printbridge
docker compose exec printbridge printbridge keys rotate <your-name>
```
*What it does: prints a new link and keeps the old one working for an hour.*

> **Treat the link like a password.** Anyone who has it can print on your printer — that is all
> they can do, but it is your paper. Do not put it in a public document or a screenshot.

### Finally: print a test page

```bash
cd ~/printbridge
docker compose exec printbridge printbridge test-page
```
*What it does: renders a one-page test document and sends it to your printer — the same path an
assistant uses.*

**Checkpoint:** a page comes out of the printer, headed "printbridge test page". ✅

*If nothing comes out,* run:

```bash
docker compose exec printbridge printbridge doctor
```
*What it does: checks every part and prints a table where each ✗ carries the next step.*

The most common cause is a sleeping printer — printbridge holds the job and prints it when the
printer wakes, so wake it and wait a minute. Everything else has an entry in
[troubleshooting.md](troubleshooting.md).

### Coming back later

```bash
ssh <user>@<your server or box>
cd ~/printbridge
docker compose exec printbridge printbridge status
```
*What it does: reconnects to the machine, moves into the printbridge folder, and shows what has
been printed and who is connected. Everything in this guide that starts with `docker compose` must
be run from that folder.*

## Station 6 — Connect your assistant

**Time: 5 minutes.**

> **Custom connectors need a paid plan** — Claude Pro/Max/Team, or ChatGPT Plus/Pro. If your
> Settings has no "Connectors" section at all, that is why. You can still print from Claude Code,
> Claude Desktop or your own scripts — see [clients.md](clients.md).

### Claude (claude.ai)

1. Open **Settings → Connectors → Add custom connector**.
2. Paste your connector link from Station 5.
3. Leave authentication empty — the link *is* the credential.
4. Save. The print tools appear in your chats.

![Station 6 — adding the connector in Claude](setup/06a-claude-connector.png)
*Screenshot to take: the "Add custom connector" dialog with the URL field filled (blur the key
part after `/mcp/`) and no authentication selected.*

### ChatGPT

1. **Settings → Connectors → Advanced → Developer mode**, turn it on.
2. **Create**, paste the same link, choose **No authentication**.
3. Save.

![Station 6 — the ChatGPT developer-mode connector](setup/06b-chatgpt-connector.png)
*Screenshot to take: the connector creation form with the URL filled (blur the key) and "No
authentication" selected.*

### Then, in any chat

> print a test page on my printer

**Checkpoint:** the assistant says it printed, and a page comes out. You are done. ✅

---

## Everyday use

Nothing to maintain. A few things worth knowing:

* **Ask for what you want:** *"print this shopping list"*, *"two copies"*, *"single-sided"*,
  *"in colour"*. The assistant passes it through.
* **Long documents:** above ten pages the assistant asks you to confirm first. Above a hundred it
  refuses. That is on purpose.
* **Someone else in the household:** give them their own link, never a copy of yours —
  `docker compose exec printbridge printbridge keys add fanny`. You can then see who printed what,
  and you can cut off one person without disturbing anyone else.
* **What did I print?** Ask your assistant *"what did I print today?"*, or run
  `docker compose exec printbridge printbridge status` from `~/printbridge`.
* **Is it still working?** `docker compose exec printbridge printbridge test-page` prints one page.
* **Get told when it breaks:** [operations.md](operations.md) sets up a 15-minute check that
  messages you — on Telegram, say — the first time something changes for the worse.

---

## If you get stuck

Run this and copy the output:

```bash
docker compose exec printbridge printbridge doctor
```
*What it does: checks every part and prints a table. It is the single most useful thing to include
when you ask for help.*

Then open an issue at
[github.com/starkshtlm/printbridge/issues](https://github.com/starkshtlm/printbridge/issues) with:

```
What I expected:
What happened instead:
Which station I was on:
Output of `printbridge doctor`:   (paste it — remove any line containing your connector link)
My printer make and model:
My router:
```

The [troubleshooting guide](troubleshooting.md) has one entry for every ✗ that `doctor` can print.

---

## What this guide deliberately does not cover

* **Your own domain name** (`print.yourname.com`) instead of the free Tailscale address. It is
  nicer but needs DNS. See [topologies.md](topologies.md) and
  [../deploy/caddy/printbridge.caddy](../deploy/caddy/printbridge.caddy).
* **Printers that only connect over USB.** Plug them into the box at home and share them with CUPS;
  see [../deploy/tailscale/subnet-router.md](../deploy/tailscale/subnet-router.md).
* **Whether your particular assistant supports custom connectors.** Claude and ChatGPT do, on paid
  plans. See [clients.md](clients.md) for the current state, including which ones are untested.
