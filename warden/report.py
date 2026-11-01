"""Markdown and JSON output.

The markdown export is meant to be dropped into the documentation vault, so it
states when it ran and what was not covered — a status page without a timestamp
is a rumour.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .inventory import Inventory
from .prober import CheckResult

STATUS_MARK = {"ok": "ok", "warn": "warn", "fail": "FAIL", "unknown": "?"}


def summary(results: list[CheckResult]) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    counts["total"] = len(results)
    return counts


def headline(results: list[CheckResult]) -> str:
    counts = summary(results)
    parts = [f"{counts['total']} service{'s' if counts['total'] != 1 else ''}"]
    for key in ("ok", "warn", "fail", "unknown"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key}")
    return " · ".join(parts)


def to_dict(
    inventory: Inventory,
    results: list[CheckResult],
    run_id: str = "",
    started: str = "",
    finished: str = "",
    drift: Any = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started": started,
        "finished": finished,
        "inventory": str(inventory.path),
        "summary": summary(results),
        "services": [r.to_dict() for r in results],
        "drift": drift,
    }


def markdown(
    inventory: Inventory,
    results: list[CheckResult],
    uptimes: dict[str, dict[str, Any]] | None = None,
    drift: Any = None,
    title: str = "Homelab status",
) -> str:
    uptimes = uptimes or {}
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = summary(results)
    lines: list[str] = []

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"_Checked {now} · inventory `{inventory.path.name}`_")
    lines.append("")
    lines.append(f"**{headline(results)}**")
    lines.append("")

    if drift is not None and getattr(drift, "first_run", False):
        lines.append("> First recorded run — there is nothing to compare against yet.")
        lines.append("")
    elif drift is not None and drift.noisy:
        lines.append("## Changes since the previous run")
        lines.append("")
        for change in drift.changes:
            arrow = {"worsened": "↓", "improved": "↑", "appeared": "+",
                     "vanished": "−", "cert": "⟳"}.get(change.kind, "•")
            lines.append(
                f"- {arrow} **{change.service}** — {change.kind}: "
                f"{change.before} → {change.after}. {change.detail}".rstrip()
            )
        lines.append("")
    elif drift is not None:
        lines.append("Nothing changed since the previous run.")
        lines.append("")

    lines.append("## Services")
    lines.append("")
    lines.append("| Service | Host | Check | Status | Latency | Detail |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        service = next((s for s in inventory.services if s.name == r.service), None)
        address = inventory.address_of(service) if service else r.address
        latency = f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "—"
        detail = (r.detail or r.error or "").replace("|", "\\|")
        lines.append(
            f"| `{r.service}` | `{address}:{r.port}` | {r.check} | "
            f"**{STATUS_MARK.get(r.status, r.status)}** | {latency} | {detail} |"
        )
    lines.append("")

    if uptimes:
        lines.append("## Uptime")
        lines.append("")
        lines.append("| Service | Checks | Uptime | Streak | Avg latency |")
        lines.append("|---|---|---|---|---|")
        for name in sorted(uptimes):
            u = uptimes[name]
            rate = f"{u['rate']}%" if u.get("rate") is not None else "—"
            avg = f"{u['avg_latency_ms']} ms" if u.get("avg_latency_ms") else "—"
            lines.append(f"| `{name}` | {u['samples']} | {rate} | {u['streak']} | {avg} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Availability only: Warden connects, requests and handshakes. It does not "
        "authenticate, change anything, or judge certificate trust — use Vantage "
        "for trust and policy._"
    )
    lines.append("")
    return "\n".join(lines)


def digest_markdown(digest: Any, uptimes: dict[str, dict[str, Any]] | None = None) -> str:
    """A week in one page, for the vault."""
    lines: list[str] = []
    lines.append(f"# Homelab digest — last {digest.window_days} day(s)")
    lines.append("")
    lines.append(f"_{digest.start.replace('T', ' ')} → {digest.end.replace('T', ' ')}_")
    lines.append("")
    lines.append(f"**{digest.headline()}**")
    lines.append("")

    if not digest.runs:
        lines.append("> No runs recorded in this window — nothing to report. Check the cron job.")
        lines.append("")
        return "\n".join(lines)

    if digest.changed:
        lines.append("## What changed")
        lines.append("")
        for change in digest.changed:
            arrow = "↑" if change["improved"] else "↓"
            word = "improved" if change["improved"] else "degraded"
            detail = (change.get("detail") or "").replace("|", "\\|")
            lines.append(
                f"- {arrow} **{change['service']}** {word}: "
                f"{change['before']} → {change['after']}. {detail}".rstrip()
            )
        lines.append("")
    else:
        lines.append("No service changed state during the window.")
        lines.append("")

    lines.append("## Services")
    lines.append("")
    lines.append("| Service | Checks | Uptime | Current | Avg latency | Cert |")
    lines.append("|---|---|---|---|---|---|")
    for s in digest.services:
        rate = f"{s.rate}%" if s.rate is not None else "—"
        avg = f"{s.avg_latency_ms} ms" if s.avg_latency_ms is not None else "—"
        cert = f"{s.cert_days} d" if s.cert_days is not None else "—"
        lines.append(
            f"| `{s.service}` | {s.samples} | {rate} | **{STATUS_MARK.get(s.current, s.current)}** "
            f"| {avg} | {cert} |"
        )
    lines.append("")

    if digest.flapping:
        lines.append("## Flapping")
        lines.append("")
        lines.append("These answered some of the time — usually worse than being plainly down.")
        lines.append("")
        for s in digest.flapping:
            lines.append(f"- **{s.service}** — {s.ok} ok, {s.fail} fail of {s.samples} checks")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Availability only, over recorded history. A service that failed between two "
        "runs is invisible here — check more often to see more._"
    )
    lines.append("")
    return "\n".join(lines)


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)
