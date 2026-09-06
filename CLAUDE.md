# printbridge — rules for every Claude Code session in this repo

printbridge is a self-hosted MCP print server: assistants and agents call `print_text`/`print_url`/`print_file`; the server renders to PDF and hands it to a CUPS queue that reaches a printer over Tailscale (or any route). Open source (MIT). Governing documents live in `~/build/printbridge_docs/` — the build brief is the work order, the architecture spec wins on conflict.

## Session discipline
1. Read `BUILD_LOG.md` (all of it), `DANIEL_TODO.md`, `MOCKS.md` before doing anything. Append to `BUILD_LOG.md` at the end of every session and after every increment.
2. Re-run the brief's §0 probes and `printbridge doctor` at session start and after every increment. If a dependency that was MOCK is now real (printer reachable, DNS resolves, `gh` authenticated, sudoers line present), switch to real mode per `MOCKS.md` and run the real acceptance — without asking.
3. **Never ask a human, never wait.** Anything that needs Daniel becomes a numbered row in `DANIEL_TODO.md` (what · where · why · how you notice it is done) and you continue in mock mode.
4. After every increment: `pytest`, `printbridge doctor`, `git commit` (`feat(b2): …`, `docs(setup): …`, `fix(cups): …`).
5. English in the repo (code, docs, commits). Swedish only in `BUILD_LOG.md`, `DANIEL_TODO.md`, `MOCKS.md`.

## Hard rules — a violation is a build defect
- No secrets in git: `.env`, `data/`, `BUILD_LOG.md`, `DANIEL_TODO.md`, `MOCKS.md` are gitignored from the first commit. Keys are created by `printbridge keys add <person>`, stored hashed in `data/keys.json` (0600), shown once.
- One connector key = one person (never one per AI service). The AI service is detected from MCP `clientInfo`.
- A public endpoint without keys refuses to start (config validation + test).
- The app binds `127.0.0.1:8000`, admin `127.0.0.1:8001` — never `0.0.0.0`. The host Caddy is the only public face (`EXPOSE=caddy-host` on this VPS).
- The printer is reached only through the tailnet route. No port forwards, no `network_mode: host`.
- `print_url` never reaches private networks unless `ALLOW_PRIVATE_URLS=true` (resolve first, check every redirect).
- Page limits are hard (`MAX_PAGES`, `CONFIRM_ABOVE_PAGES` → `confirmed=true`). Always render → count → spool.
- Metadata always, content never: job log and sessions store titles/pages/principal/client, never document content, URLs or bytes. Spool deleted after completion.
- Generic: nothing Canon-, houseofstark- or Daniel-specific in code or docs except as examples.
- `build_server(settings) -> FastMCP` has no import-time side effects; tool names tolerate a prefix; auth goes through `resolve_principal()`.

## Production on this VPS — DO NOT TOUCH
This server runs Daniel's production agents and website. The following are off-limits, read-only, always:
- `~/.hermes/**` config, cron, `.env` values (you may ADD one disabled MCP server entry for printbridge and nothing else; never modify or remove existing keys).
- The `hunterbot` OS user, its home, cron and Notion surfaces.
- `/etc/caddy/Caddyfile` and every existing site block (houseofstark.io, bridge.houseofstark.io). The only permitted Caddy change is installing `printbridge.caddy` into `/etc/caddy/sites/` via the sudoers-allowed commands (`install`, `caddy validate`, `systemctl reload caddy`) — never `restart`, never edit the main file.
- `/etc/cron*`, systemd units other than `printbridge-*`, Tailscale settings (exception: `tailscale set --accept-routes`, reversible, logged), the firewall, apt packages beyond what the brief names (Docker CE if missing).
- Never run `docker system prune`, `docker volume rm` on volumes you did not create, or anything that stops containers you did not start.
- Resource courtesy: run builds with `nice -n 10`; set `mem_limit`/`cpus` in compose (cups 256m, printbridge 512m); check ≥6 GB free before `docker build`.
- If any of the above would be needed to proceed: write it to `DANIEL_TODO.md` and continue with mocks.

## Definition of done for a session
`BUILD_LOG.md` ends with: built · mocked · waiting for Daniel · time per increment · deviations from the spec (explicit) · open uncertainties. Then print that block in the chat.
