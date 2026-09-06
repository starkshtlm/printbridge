#!/usr/bin/env bash
# Creates every queue in PRINTERS (name=uri[;driver=ppd], comma separated; first = default).
# The legacy single form PRINTER_NAME + PRINTER_URI is still accepted.
# Mock: name=cups-pdf:/ writes PDFs to /out (bind-mounted to ./data/mock-out).
#
# Queue creation is idempotent and retried: a printer that is asleep, or a tailnet route that is
# not up yet, must not make the container fail — doctor is what reports it, and CUPS holds the job.
set -euo pipefail

log() { echo "[cups] $*"; }

# This container IS the CUPS server. `.env` sets CUPS_SERVER for the *app* container; inheriting it
# here would make lpstat/lpadmin talk to a name that is this container, over the network, before
# the network alias exists — which is exactly how the first build of this file failed to start.
unset CUPS_SERVER

cupsd -f &
CUPSD_PID=$!
for _ in $(seq 1 40); do
  lpstat -r 2>/dev/null | grep -q "is running" && break
  sleep 0.5
done
lpstat -r 2>/dev/null | grep -q "is running" || { log "cupsd did not start"; exit 1; }
log "cupsd is running"

PRINTERS="${PRINTERS:-}"
if [ -z "$PRINTERS" ] && [ -n "${PRINTER_URI:-}" ]; then
  PRINTERS="${PRINTER_NAME:-home}=${PRINTER_URI}"
fi
if [ -z "$PRINTERS" ]; then
  log "PRINTERS is empty — no queues to create (printbridge doctor will say so)"
fi

paper="${DEFAULT_PAPER:-A4}"
media="iso_a4_210x297mm"
[ "$paper" = "Letter" ] && media="na_letter_8.5x11in"
sides="two-sided-long-edge"
[ "${DEFAULT_DUPLEX:-true}" = "false" ] && sides="one-sided"

# The exact cups-pdf model name differs between distributions; ask CUPS rather than guess.
pdf_model() {
  lpinfo -m 2>/dev/null | awk 'tolower($0) ~ /cups-pdf/ {print $1; exit}'
}

first=""
IFS=',' read -ra entries <<< "$PRINTERS"
for entry in "${entries[@]}"; do
  entry="$(echo "$entry" | xargs)"
  [ -n "$entry" ] || continue
  name="${entry%%=*}"
  rest="${entry#*=}"
  uri="${rest%%;*}"
  driver="everywhere"
  [[ "$rest" == *";driver="* ]] && driver="${rest#*;driver=}"
  [ -z "$first" ] && first="$name"

  if lpstat -p "$name" >/dev/null 2>&1; then
    log "queue $name exists"
    continue
  fi

  if [ "$uri" = "cups-pdf:/" ] || [[ "$uri" == cups-pdf:* ]]; then
    model="$(pdf_model)"
    if [ -z "$model" ]; then
      log "queue $name: cups-pdf driver not found in this image"
      continue
    fi
    if lpadmin -p "$name" -E -v cups-pdf:/ -m "$model" -o media="$media" 2>/tmp/lpadmin.err; then
      cupsaccept "$name" 2>/dev/null || true
      cupsenable "$name" 2>/dev/null || true
      log "queue $name ready (MOCK cups-pdf -> /out, model=$model)"
    else
      log "queue $name: FAILED to create mock queue: $(cat /tmp/lpadmin.err)"
    fi
    continue
  fi

  ok=0
  for attempt in 1 2 3 4 5; do
    if lpadmin -p "$name" -E -v "$uri" -m "$driver" \
        -o media="$media" -o sides="$sides" \
        -o printer-error-policy=retry-job 2>/tmp/lpadmin.err; then
      cupsaccept "$name" 2>/dev/null || true
      cupsenable "$name" 2>/dev/null || true
      log "queue $name ready ($uri, driver=$driver)"
      ok=1
      break
    fi
    log "queue $name: printer unreachable, retrying ($attempt/5): $(cat /tmp/lpadmin.err)"
    sleep 10
  done
  [ "$ok" = 1 ] || log "queue $name: NOT created after 5 attempts — doctor will report it"
done

if [ -n "$first" ] && lpstat -p "$first" >/dev/null 2>&1; then
  lpadmin -d "$first" && log "default printer: $first"
fi
cupsctl --no-remote-admin --no-share-printers 2>/dev/null || true

log "ready"
wait "$CUPSD_PID"
