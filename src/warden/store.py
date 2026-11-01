"""SQLite history.

Every run is recorded, so "it was fine yesterday" is a claim that can be checked
instead of remembered.
"""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from . import config
from .prober import CheckResult

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    started     TEXT NOT NULL,
    finished    TEXT,
    inventory   TEXT,
    n_services  INTEGER DEFAULT 0,
    n_ok        INTEGER DEFAULT 0,
    n_warn      INTEGER DEFAULT 0,
    n_fail      INTEGER DEFAULT 0,
    n_unknown   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS checks (
    run_id          TEXT NOT NULL,
    service         TEXT NOT NULL,
    "check"         TEXT,
    address         TEXT,
    port            INTEGER,
    status          TEXT,
    latency_ms      REAL,
    detail          TEXT,
    http_status     INTEGER,
    tls_days_left   INTEGER,
    tls_fingerprint TEXT,
    error           TEXT,
    ts              TEXT,
    PRIMARY KEY (run_id, service),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checks_service ON checks(service, ts);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started DESC);
"""


def new_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


@contextmanager
def connect():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def save_run(
    run_id: str,
    started: str,
    finished: str,
    inventory_path: str,
    results: list[CheckResult],
) -> None:
    init_db()
    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (id, started, finished, inventory, n_services, n_ok, n_warn, n_fail, n_unknown)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, started, finished, inventory_path, len(results),
             counts["ok"], counts["warn"], counts["fail"], counts["unknown"]),
        )
        conn.executemany(
            """INSERT OR REPLACE INTO checks
               (run_id, service, "check", address, port, status, latency_ms, detail,
                http_status, tls_days_left, tls_fingerprint, error, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, r.service, r.check, r.address, r.port, r.status, r.latency_ms,
                 r.detail, r.http_status, r.tls_days_left, r.tls_fingerprint, r.error, r.ts)
                for r in results
            ],
        )


def latest_run() -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def previous_run(before_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE started < (SELECT started FROM runs WHERE id=?) "
            "ORDER BY started DESC LIMIT 1",
            (before_id,),
        ).fetchone()
    return dict(row) if row else None


def results_for(run_id: str) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM checks WHERE run_id=? ORDER BY service", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def history(service: str, limit: int = 30) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, r.started FROM checks c JOIN runs r ON r.id = c.run_id
               WHERE c.service=? ORDER BY r.started DESC LIMIT ?""",
            (service, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def uptime(service: str, limit: int = 100) -> dict[str, Any]:
    """Uptime over the last `limit` recorded checks of one service."""
    rows = history(service, limit)
    if not rows:
        return {"service": service, "samples": 0, "ok": 0, "rate": None,
                "avg_latency_ms": None, "streak": 0}
    ok = sum(1 for r in rows if r["status"] == "ok")
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    streak = 0
    for r in rows:  # newest first
        if r["status"] == "ok":
            streak += 1
        else:
            break
    return {
        "service": service,
        "samples": len(rows),
        "ok": ok,
        "rate": round(100.0 * ok / len(rows), 1),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "streak": streak,
    }


def checks_since(since: str) -> list[dict[str, Any]]:
    """Every recorded check at or after `since` (an ISO timestamp), oldest first.

    The digest needs the whole window in one pass — per-service queries would mean
    one round trip per service and a race between them.
    """
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, r.started FROM checks c JOIN runs r ON r.id = c.run_id
               WHERE r.started >= ? ORDER BY r.started ASC, c.service ASC""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def runs_since(since: str) -> int:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM runs WHERE started >= ?", (since,)).fetchone()
    return row["c"] if row else 0


def prune(keep_runs: int = 200) -> int:
    """Delete the oldest runs beyond `keep_runs`. Returns the number removed."""
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM runs ORDER BY started DESC LIMIT -1 OFFSET ?", (keep_runs,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        for run_id in ids:
            conn.execute("DELETE FROM checks WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    return len(ids)


def stats() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        runs = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        services = conn.execute("SELECT COUNT(DISTINCT service) c FROM checks").fetchone()["c"]
        last = conn.execute("SELECT MAX(started) s FROM runs").fetchone()["s"]
    return {"runs": runs, "services": services, "last_run": last}
