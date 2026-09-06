# printbridge — MCP print server. Renders to PDF (WeasyPrint) and spools through CUPS.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRINTBRIDGE_DATA_DIR=/data \
    PRINTBRIDGE_IN_CONTAINER=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

# WeasyPrint needs pango/cairo/gdk-pixbuf; cups-client gives us lp/lpstat/cancel; cups-ipp-utils
# gives ipptool, which is how `doctor` answers "will this printer work?" honestly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
      libffi8 shared-mime-info fonts-dejavu-core fonts-liberation2 \
      cups-client cups-ipp-utils curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY printbridge ./printbridge
RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin printbridge \
    && mkdir -p /data && chown -R printbridge:printbridge /data

# The process that serves requests is never root — see docker-entrypoint.sh.
EXPOSE 8000 8001
HEALTHCHECK --interval=60s --timeout=15s --start-period=25s --retries=3 \
  CMD ["/usr/local/bin/docker-entrypoint.sh", "doctor", "--quiet", "--self"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
