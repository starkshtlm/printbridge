# CUPS spooler for printbridge — driverless first, PPD fallback, cups-pdf for mock mode.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      cups cups-filters cups-ipp-utils cups-client \
      printer-driver-gutenprint hplip printer-driver-cups-pdf \
      ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY cupsd.conf /etc/cups/cupsd.conf
COPY cups-pdf.conf /etc/cups/cups-pdf.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /out && chmod 1777 /out

EXPOSE 631
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD lpstat -r 2>/dev/null | grep -q "is running" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
