"""Small helpers shared by `server.py` and `fetch.py` without importing one from the other."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from printbridge.config import Settings


def spool_target(settings: Settings, suffix: str = ".pdf") -> Path:
    """A fresh path in the spool directory. The file is deleted again by `cleanup_spool`."""
    settings.spool_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return settings.spool_path / f"pb-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"
