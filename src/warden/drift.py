"""Drift: what changed between two runs.

The useful question is never "is everything green" — it is "what is different
since the last time I looked". Silent changes are how a lab rots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITY_OF = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}


@dataclass
class Change:
    service: str
    kind: str                 # worsened | improved | appeared | vanished | cert
    before: str = ""
    after: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"service": self.service, "kind": self.kind, "before": self.before,
                "after": self.after, "detail": self.detail}


@dataclass
class Drift:
    base_run: str = ""
    compare_run: str = ""
    base_started: str = ""
    compare_started: str = ""
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0
    declared: int = 0

    @property
    def worsened(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "worsened"]

    @property
    def improved(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "improved"]

    @property
    def appeared(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "appeared"]

    @property
    def vanished(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "vanished"]

    @property
    def certs(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "cert"]

    @property
    def noisy(self) -> bool:
        return bool(self.changes)

    @property
    def first_run(self) -> bool:
        return not self.base_run

    def counts(self) -> dict[str, int]:
        out = {"worsened": 0, "improved": 0, "appeared": 0, "vanished": 0, "cert": 0}
        for change in self.changes:
            out[change.kind] = out.get(change.kind, 0) + 1
        out["unchanged"] = self.unchanged
        return out


def diff_runs(
    base: dict[str, Any] | None,
    base_results: list[dict[str, Any]],
    current_results: list[dict[str, Any]],
) -> Drift:
    """Compare the latest results against the previous run's results."""
    drift = Drift(declared=len(current_results))
    if base:
        drift.base_run = base.get("id", "")
        drift.base_started = base.get("started", "")

    before = {r["service"]: r for r in base_results}
    after = {r["service"]: r for r in current_results}

    for name, now in after.items():
        old = before.get(name)
        if old is None:
            if base:
                drift.changes.append(
                    Change(name, "appeared", before="—", after=now["status"],
                           detail="new in this run (added to the inventory)")
                )
            continue

        if old["status"] != now["status"]:
            sev_before = SEVERITY_OF.get(old["status"], 1)
            sev_after = SEVERITY_OF.get(now["status"], 1)
            kind = "worsened" if sev_after > sev_before else "improved"
            detail = now.get("detail") or now.get("error") or ""
            drift.changes.append(
                Change(name, kind, before=old["status"], after=now["status"], detail=detail)
            )
        else:
            drift.unchanged += 1

        if (
            now.get("tls_fingerprint")
            and old.get("tls_fingerprint")
            and now["tls_fingerprint"] != old["tls_fingerprint"]
        ):
            drift.changes.append(
                Change(
                    name, "cert",
                    before=old["tls_fingerprint"], after=now["tls_fingerprint"],
                    detail="certificate changed since the previous run",
                )
            )

    for name, old in before.items():
        if name not in after:
            drift.changes.append(
                Change(name, "vanished", before=old["status"], after="—",
                       detail="was checked last run, no longer in the inventory")
            )

    order = {"worsened": 0, "vanished": 1, "cert": 2, "appeared": 3, "improved": 4}
    drift.changes.sort(key=lambda c: (order.get(c.kind, 9), c.service))
    return drift


def to_dict(drift: Drift) -> dict[str, Any]:
    return {
        "base_run": drift.base_run,
        "compare_run": drift.compare_run,
        "base_started": drift.base_started,
        "compare_started": drift.compare_started,
        "first_run": drift.first_run,
        "counts": drift.counts(),
        "changes": [c.to_dict() for c in drift.changes],
    }
