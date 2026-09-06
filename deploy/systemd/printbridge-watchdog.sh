#!/usr/bin/env bash
# What the watchdog timer runs every 15 minutes.
#
# `printbridge watchdog` = doctor + alert on state change + weekly digest + retention cleanup.
# It runs inside the printbridge container, which is where the data directory and CUPS live.
#
# Three outcomes, three different messages — a watchdog that reports the wrong cause is worse than
# one that stays quiet:
#   * the container is running        → check inside it
#   * the container is not running    → check from a throwaway container, so the alert still goes out
#   * Docker itself cannot be reached → say exactly that, and do not pretend to know anything else
set -uo pipefail

cd "${PRINTBRIDGE_DIR:-$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")}" || exit 1

if ! docker info >/dev/null 2>&1; then
    echo "watchdog: cannot reach the Docker daemon as $(id -un)." >&2
    echo "  Is Docker running, and is this user in the 'docker' group?" >&2
    echo "  A group added after login only takes effect for new sessions (log out and in, or reboot)." >&2
    exit 2
fi

running="$(docker compose ps --status running --format '{{.Service}}' 2>/dev/null)"
if printf '%s\n' "$running" | grep -qx printbridge; then
    exec docker compose exec -T printbridge printbridge watchdog
fi

echo "watchdog: the printbridge container is not running — checking from a throwaway container" >&2
exec docker compose run --rm --no-deps -T printbridge watchdog
