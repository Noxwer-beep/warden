"""Paths and defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "warden"

#: Status vocabulary, ordered by how much attention it deserves.
STATUS_ORDER = ("ok", "warn", "fail", "unknown")

STATUS_HELP = {
    "ok": "reachable and behaving as declared",
    "warn": "reachable, but something is off — slow, expiring, unexpected status",
    "fail": "declared and not reachable (or answering wrong)",
    "unknown": "could not be determined",
}

CHECK_TYPES = ("tcp", "http", "https", "tls")


def data_dir() -> Path:
    env = os.environ.get("WARDEN_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def ensure_dirs() -> dict[str, Path]:
    root = data_dir()
    dirs = {"root": root, "exports": root / "exports", "logs": root / "logs"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


DB_PATH = lambda: data_dir() / "warden.db"  # noqa: E731


@dataclass(frozen=True)
class Defaults:
    """Values used when the inventory does not say otherwise."""

    timeout_s: float = 5.0
    workers: int = 8
    warn_latency_ms: int = 1200
    cert_warn_days: int = 21
    user_agent: str = "Warden/1.0 (homelab health check)"
    follow_redirects: bool = True
