"""The weekly digest: what a week of checks actually says.

A status table answers "is it up now". A digest answers "was it up, how often does
it wobble, and did anything degrade quietly" — the questions that need history
rather than a snapshot.

The digest reads the same rows as everything else; nothing is recomputed at check
time, so the numbers are only ever as good as the recorded history.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from . import store


@dataclass
class ServiceDigest:
    service: str
    samples: int = 0
    ok: int = 0
    warn: int = 0
    fail: int = 0
    unknown: int = 0
    rate: float | None = None
    avg_latency_ms: float | None = None
    worst_latency_ms: float | None = None
    current: str = "unknown"
    first: str = ""
    last: str = ""
    cert_days: int | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "service": self.service, "samples": self.samples, "ok": self.ok,
            "warn": self.warn, "fail": self.fail, "unknown": self.unknown,
            "rate": self.rate, "avg_latency_ms": self.avg_latency_ms,
            "worst_latency_ms": self.worst_latency_ms,
            "current": self.current, "cert_days": self.cert_days, "note": self.note,
        }


@dataclass
class Digest:
    window_days: int
    start: str
    end: str
    runs: int = 0
    services: list[ServiceDigest] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)

    # -- derived ----------------------------------------------------------

    @property
    def declared(self) -> int:
        return len(self.services)

    @property
    def at_risk(self) -> list[ServiceDigest]:
        """Anything that failed at some point in the window."""
        return [s for s in self.services if s.fail > 0]

    @property
    def always_down(self) -> list[ServiceDigest]:
        return [s for s in self.services if s.samples and s.ok == 0]

    @property
    def flapping(self) -> list[ServiceDigest]:
        """Both ok and fail in the window — the interesting kind of broken."""
        return [s for s in self.services if s.ok > 0 and s.fail > 0]

    def headline(self) -> str:
        if not self.runs:
            return f"No runs recorded in the last {self.window_days} day(s)."

        if not self.services:
            return f"{self.runs} run(s), but no service history in the window."

        healthy = sum(1 for s in self.services if s.fail == 0 and s.warn == 0)
        worst = min(self.services, key=lambda s: (s.rate if s.rate is not None else 101))
        parts = [f"{self.declared} service(s) over {self.runs} run(s)"]
        parts.append(f"{healthy} clean")
        if self.fail_total:
            parts.append(f"{self.fail_total} failed check(s)")
        if worst.rate is not None and worst.rate < 100:
            parts.append(f"worst: {worst.service} at {worst.rate}%")
        return " · ".join(parts)

    @property
    def fail_total(self) -> int:
        return sum(s.fail for s in self.services)

    def to_dict(self) -> dict:
        return {
            "window_days": self.window_days,
            "start": self.start,
            "end": self.end,
            "runs": self.runs,
            "headline": self.headline(),
            "services": [s.to_dict() for s in self.services],
            "changed": self.changed,
        }


def build(window_days: int = 7, now: dt.datetime | None = None) -> Digest:
    """Aggregate the last `window_days` of history into a digest."""
    now = now or dt.datetime.now()
    start = (now - dt.timedelta(days=window_days)).replace(microsecond=0).isoformat()
    end = now.replace(microsecond=0).isoformat()

    digest = Digest(window_days=window_days, start=start, end=end)
    digest.runs = store.runs_since(start)
    if not digest.runs:
        return digest

    rows = store.checks_since(start)
    by_service: dict[str, list[dict]] = {}
    for row in rows:
        by_service.setdefault(row["service"], []).append(row)

    for name in sorted(by_service):
        history = by_service[name]           # oldest first
        entry = ServiceDigest(service=name)
        entry.samples = len(history)
        for row in history:
            status = row["status"] or "unknown"
            if status in ("ok", "warn", "fail", "unknown"):
                setattr(entry, status, getattr(entry, status) + 1)

        latencies = [r["latency_ms"] for r in history if r["latency_ms"] is not None]
        if latencies:
            entry.avg_latency_ms = round(sum(latencies) / len(latencies), 1)
            entry.worst_latency_ms = round(max(latencies), 1)

        if entry.samples:
            entry.rate = round(100.0 * entry.ok / entry.samples, 1)
            entry.current = history[-1]["status"] or "unknown"
            entry.first = history[0]["status"] or "unknown"
            entry.last = history[-1]["ts"] or ""
            cert = history[-1].get("tls_days_left")
            entry.cert_days = cert if cert is not None else None
            if entry.current != "ok":
                entry.note = history[-1].get("detail") or ""

        digest.services.append(entry)

    digest.changed = _changes(by_service)
    return digest


def _changes(by_service: dict[str, list[dict]]) -> list[dict[str, Any]]:
    """First status vs last status inside the window."""
    out = []
    rank = {"ok": 0, "warn": 1, "unknown": 2, "fail": 3}
    for name, history in by_service.items():
        if len(history) < 2:
            continue
        before = history[0]["status"] or "unknown"
        after = history[-1]["status"] or "unknown"
        if before == after:
            continue
        out.append({
            "service": name,
            "before": before,
            "after": after,
            "improved": rank.get(after, 2) < rank.get(before, 2),
            "detail": history[-1].get("detail") or "",
        })
    out.sort(key=lambda c: (c["improved"], c["service"]))
    return out
