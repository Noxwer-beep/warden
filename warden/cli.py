"""Command-line interface.

    warden check              run every check, record it, print the result
    warden status             latest recorded state
    warden drift              what changed since the previous run
    warden ui                 local dashboard

Exit codes: 0 all ok · 1 error · 2 inventory problem · 3 missing dependency
            · 4 something failed (or --fail-on threshold met)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, config, digest as digestmod, drift as driftmod, inventory as invmod, report, store
from .config import STATUS_ORDER
from .prober import CheckResult, run_checks

# Piped output must be stable: a table that reflows at the default 80 columns
# would wrap mid-row and make every screenshot and diff unreadable.
_WIDTH = None if sys.stdout.isatty() else int(os.environ.get("COLUMNS", "100"))
console = Console(width=_WIDTH)
err = Console(stderr=True)

STATUS_STYLE = {
    "ok": "green",
    "warn": "yellow",
    "fail": "bold red",
    "unknown": "dim",
}
STATUS_MARK = {"ok": "✓", "warn": "!", "fail": "✗", "unknown": "?"}


def header(subtitle: str = "") -> None:
    t = Text()
    t.append("WARDEN", style="bold")
    t.append("  ·  homelab inventory, health and drift", style="dim")
    if subtitle:
        t.append(f"\n{subtitle}", style="dim italic")
    console.print(Panel(t, border_style="bright_black", padding=(0, 2), expand=False))
    console.print()


def status_cell(status: str) -> Text:
    return Text(f"{STATUS_MARK.get(status, '?')} {status}",
                style=STATUS_STYLE.get(status, "dim"))


def load_inventory(args) -> invmod.Inventory | None:
    try:
        path = Path(args.inventory).expanduser() if getattr(args, "inventory", None) else invmod.find()
        return invmod.load(path)
    except invmod.InventoryError as exc:
        err.print(f"[red]inventory:[/] {exc}")
        return None


# ---------------------------------------------------------------- check

class CheckView:
    """Live table that fills in as probes complete."""

    def __init__(self, inv: invmod.Inventory):
        self.inv = inv
        self.results: list[CheckResult] = []
        self.started = time.monotonic()

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    def render(self):
        elapsed = time.monotonic() - self.started
        total = len(self.inv.services)
        done = len(self.results)
        counts = report.summary(self.results)

        head = Text()
        head.append("WARDEN", style="bold")
        head.append(f"  ·  {self.inv.path.name}  ·  ", style="dim")
        head.append(f"{done}/{total}", style="bold")
        head.append(f"   {elapsed:.0f}s", style="dim")

        bar_width = max(10, console.width - 14)
        filled = int(bar_width * done / total) if total else bar_width
        bar = Text()
        bar.append("━" * filled, style="#57e0c9")
        bar.append("━" * (bar_width - filled), style="#2a343c")
        bar.append(f" {int(100 * done / max(total, 1)):3d}%", style="dim")

        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
        table.add_column("SERVICE", no_wrap=True, overflow="ellipsis", width=26)
        table.add_column("STATUS", width=9, no_wrap=True)
        table.add_column("LATENCY", width=9, no_wrap=True, justify="right")
        table.add_column("DETAIL", no_wrap=True, overflow="ellipsis", ratio=1)
        for r in sorted(self.results, key=lambda x: x.service):
            table.add_row(
                _service_cell(r),
                status_cell(r.status),
                f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "—",
                r.detail or r.error or "",
            )

        counters = Text()
        for key in STATUS_ORDER:
            if counts.get(key):
                counters.append(f"{counts[key]} {key}  ", style=STATUS_STYLE[key])
        if not self.results:
            counters.append("starting…", style="dim")

        return Group(head, bar, table, counters)


def cmd_check(args: argparse.Namespace) -> int:
    inv = load_inventory(args)
    if inv is None:
        return 2

    services = inv.services
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        services = [s for s in services if wanted & set(s.tags)]
    if args.service:
        services = [s for s in services if s.name == args.service]
    if not services:
        err.print("[red]no services matched[/] — check --only / --service")
        return 2

    json_mode = bool(args.json)
    live_ok = console.is_terminal and not json_mode and not args.plain and not args.quiet
    view = CheckView(inv)
    started = time.time()
    run_id = store.new_run_id()

    def on_result(result: CheckResult) -> None:
        view.add(result)

    if live_ok:
        with Live(view.render(), console=console, refresh_per_second=10, get_renderable=view.render):
            results = run_checks(inv, services, on_result=on_result)
            time.sleep(0.15)
    else:
        if not args.quiet and not json_mode:
            console.print(f"checking {len(services)} service(s)…")
        results = run_checks(inv, services, on_result=on_result)

    finished = time.time()
    if not args.no_save:
        store.save_run(run_id, _iso(started), _iso(finished), str(inv.path), results)

    base = store.previous_run(run_id) if not args.no_save else None
    base_results = store.results_for(base["id"]) if base else []
    drift = driftmod.diff_runs(base, base_results, [r.to_dict() for r in results])
    drift.compare_run = run_id
    drift.compare_started = _iso(started)

    if json_mode:
        payload = report.to_dict(inv, results, run_id, _iso(started), _iso(finished),
                                 drift=driftmod.to_dict(drift))
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return _exit_code(results, args.fail_on)

    console.print()
    if not args.quiet:
        console.print(f"[bold]{report.headline(results)}[/]  [dim]{finished - started:.1f}s[/]")
        console.print()

    # The live table has already shown every result; plain output has not.
    if args.plain or not console.is_terminal:
        _print_results(results)

    if not args.plain:
        _print_drift(drift, console)

    console.print(f"[dim]run[/] [bold]{run_id}[/]")
    return _exit_code(results, args.fail_on)


def _service_cell(result: CheckResult) -> Text:
    """Service name with its port — one line, so rows stay aligned."""
    cell = Text(result.service)
    cell.append(f"  :{result.port}", style="dim")
    return cell


def _result_table(results: list[CheckResult]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
    table.add_column("SERVICE", no_wrap=True, overflow="ellipsis", width=26)
    table.add_column("STATUS", width=9, no_wrap=True)
    table.add_column("LATENCY", width=9, no_wrap=True, justify="right")
    table.add_column("DETAIL", no_wrap=True, overflow="ellipsis", ratio=1)
    for r in sorted(results, key=lambda x: x.service):
        table.add_row(
            _service_cell(r),
            status_cell(r.status),
            f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "—",
            r.detail or r.error or "",
        )
    return table


def _print_results(results: list[CheckResult]) -> None:
    console.print(_result_table(results))
    console.print()


def _print_drift(drift, out=console) -> None:
    if drift.noisy:
        for change in drift.changes:
            arrow = {"worsened": "↓", "improved": "↑", "appeared": "+",
                     "vanished": "−", "cert": "⟳"}.get(change.kind, "•")
            style = {"worsened": "red", "improved": "green", "cert": "yellow"}.get(change.kind, "dim")
            out.print(
                f"  [{style}]{arrow}[/] {change.service}: {change.before} → {change.after} "
                f"[dim]{change.detail}[/]"
            )
        out.print()


# ---------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> int:
    inv = load_inventory(args) if args.inventory or _has_inventory() else None
    latest = store.latest_run()
    if latest is None:
        console.print("[dim]no runs recorded yet — run `warden check`[/]")
        return 0

    results = store.results_for(latest["id"])
    if args.json:
        payload = {
            "run": latest,
            "services": results,
            "uptime": {r["service"]: store.uptime(r["service"]) for r in results},
        }
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0 if all(r["status"] in ("ok", "unknown") for r in results) else 4

    header(f"latest run · {latest['started']}")
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
    table.add_column("SERVICE", no_wrap=True, overflow="ellipsis", width=26)
    table.add_column("STATUS", width=9, no_wrap=True)
    table.add_column("UP", width=7, no_wrap=True, justify="right")
    table.add_column("LATENCY", width=9, no_wrap=True, justify="right")
    table.add_column("DETAIL", no_wrap=True, overflow="ellipsis", ratio=1)
    for r in results:
        up = store.uptime(r["service"])
        cell = Text(r["service"])
        cell.append(f"  :{r['port']}", style="dim")
        table.add_row(
            cell,
            status_cell(r["status"]),
            f"{up['rate']}%" if up.get("rate") is not None else "—",
            f"{r['latency_ms']:.0f} ms" if r["latency_ms"] is not None else "—",
            r["detail"] or r["error"] or "",
        )
    console.print(table)
    console.print()
    counts = {k: latest[f"n_{k}"] for k in ("ok", "warn", "fail", "unknown") if latest.get(f"n_{k}")}
    console.print("  " + "  ".join(f"[{STATUS_STYLE[k]}]{v} {k}[/]" for k, v in counts.items()))
    console.print()
    return 0 if not latest.get("n_fail") else 4


CAT = [
    r"        /\                 /\ ",
    r"       /  \_______________/  \ ",
    r"      |    ||         ||    | ",
    r"      |          v           | ",
    r"       \___________________/  ",
]

CAT_LINES = [
    "homelab inventory, health and drift",
    "one file says what should be running",
    "warden checks, remembers, and reports the difference",
]


def print_cat(extra: str = "") -> None:
    """The mark, in ASCII. Same shape as the logo: ears, slit eyes, one nose."""
    text = Text()
    for index, line in enumerate(CAT):
        if index:
            text.append("\n")
        if "v" in line and "|" in line:
            head, _, tail = line.partition("v")
            text.append(head, style="default")
            text.append("v", style="#c9f24d")
            text.append(tail, style="default")
        else:
            text.append(line)

    side = Text()
    side.append("\n\n")
    side.append("WARDEN", style="bold")
    side.append(f"  {__version__}\n", style="dim")
    for line in CAT_LINES:
        side.append(line + "\n", style="dim")

    table = Table.grid(padding=(0, 3))
    table.add_row(text, side)
    console.print()
    console.print(table)
    if extra:
        console.print(f"[dim]  {extra}[/]")
    console.print()


def cmd_cat(args: argparse.Namespace) -> int:
    print_cat("press c on the portfolio site for the other one")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """A window of history, summarised. The point is the shape of the week."""
    digest = digestmod.build(args.days)

    if args.json:
        json.dump(digest.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    md = report.digest_markdown(digest)
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        console.print(f"[dim]wrote[/] {path}")
        return 0

    header(f"last {digest.window_days} day(s)")
    console.print(f"  [bold]{digest.headline()}[/]")
    console.print()

    if not digest.runs:
        console.print("[dim]  No runs in this window — check that the cron job is running.[/]")
        console.print()
        return 0

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=False)
    table.add_column("SERVICE", no_wrap=True, overflow="ellipsis", width=22)
    table.add_column("CHECKS", width=7, no_wrap=True, justify="right")
    table.add_column("UPTIME", width=8, no_wrap=True, justify="right")
    table.add_column("CURRENT", width=9, no_wrap=True)
    table.add_column("AVG LAT", width=9, no_wrap=True, justify="right")
    table.add_column("CERT", width=6, no_wrap=True, justify="right")
    for s in digest.services:
        table.add_row(
            s.service,
            str(s.samples),
            f"{s.rate}%" if s.rate is not None else "—",
            status_cell(s.current),
            f"{s.avg_latency_ms:.0f} ms" if s.avg_latency_ms is not None else "—",
            f"{s.cert_days} d" if s.cert_days is not None else "—",
        )
    console.print(table)
    console.print()

    if digest.changed:
        console.print("  [dim]changed during the window[/]")
        for change in digest.changed:
            arrow = "↑" if change["improved"] else "↓"
            style = "green" if change["improved"] else "bold red"
            console.print(
                f"  [{style}]{arrow}[/] [bold]{change['service']}[/] "
                f"{change['before']} → {change['after']}  [dim]{change['detail']}[/]"
            )
        console.print()

    if digest.flapping:
        console.print("  [yellow]flapping[/] [dim]— answered some of the time[/]")
        for s in digest.flapping:
            console.print(f"    {s.service}: {s.ok} ok, {s.fail} fail of {s.samples}")
        console.print()

    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    latest = store.latest_run()
    if latest is None:
        console.print("[dim]no runs recorded yet — run `warden check`[/]")
        return 0
    base = store.previous_run(latest["id"])
    current = store.results_for(latest["id"])
    base_results = store.results_for(base["id"]) if base else []
    drift = driftmod.diff_runs(base, base_results, current)
    drift.compare_run = latest["id"]
    drift.compare_started = latest["started"]

    if args.json:
        json.dump(driftmod.to_dict(drift), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 4 if drift.worsened else 0

    header("drift since previous run")
    if drift.first_run:
        console.print("  [dim]First recorded run — nothing to compare.[/]")
        console.print()
        return 0

    console.print(
        f"  [dim]{drift.base_started} → {drift.compare_started}[/]"
        f"   {drift.unchanged} unchanged"
    )
    console.print()
    if not drift.noisy:
        console.print("  [green]Nothing changed.[/]")
        console.print()
        return 0
    _print_drift(drift, console)
    return 4 if drift.worsened else 0


def cmd_history(args: argparse.Namespace) -> int:
    rows = store.history(args.service, limit=args.limit)
    if not rows:
        console.print(f"[dim]no history for {args.service!r}[/]")
        return 0
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
    table.add_column("WHEN", width=19, no_wrap=True)
    table.add_column("STATUS", width=9, no_wrap=True)
    table.add_column("LATENCY", width=9, no_wrap=True, justify="right")
    table.add_column("DETAIL", no_wrap=True, overflow="ellipsis", ratio=1)
    for r in rows:
        table.add_row(r["started"], status_cell(r["status"]),
                      f"{r['latency_ms']:.0f} ms" if r["latency_ms"] is not None else "—",
                      r["detail"] or r["error"] or "")
    console.print(table)
    up = store.uptime(args.service, limit=args.limit)
    console.print(f"  [dim]uptime {up['rate']}% over {up['samples']} checks · streak {up['streak']}[/]")
    console.print()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    inv = load_inventory(args)
    if inv is None:
        return 2
    latest = store.latest_run()
    if latest is None:
        err.print("[red]nothing recorded yet[/] — run `warden check` first")
        return 1
    results_rows = store.results_for(latest["id"])
    results = [_row_to_result(r) for r in results_rows]
    base = store.previous_run(latest["id"])
    base_results = store.results_for(base["id"]) if base else []
    drift = driftmod.diff_runs(base, base_results, results_rows)
    uptimes = {r.service: store.uptime(r.service) for r in results}

    body = report.markdown(inv, results, uptimes, drift, title=args.title)
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        console.print(f"[green]wrote[/] {path}")
    else:
        sys.stdout.write(body)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .web.app import serve
    header(f"dashboard at http://{args.host}:{args.port}")
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    serve(host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = invmod.write_template(args.path, force=args.force)
    except invmod.InventoryError as exc:
        err.print(f"[red]{exc}[/]")
        return 2
    console.print(f"[green]wrote[/] {path}")
    console.print("  [dim]edit the hosts and services, then run `warden check`[/]")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    header("environment check")
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=False)
    table.add_column("ITEM", width=16)
    table.add_column("STATUS", width=10)
    table.add_column("DETAIL")
    try:
        path = invmod.find()
        inv = invmod.load(path)
        table.add_row("inventory", "[green]ok[/]",
                      f"{path} · {len(inv.hosts)} host(s) · {len(inv.services)} service(s)")
    except invmod.InventoryError as exc:
        table.add_row("inventory", "[red]missing[/]", str(exc))
    table.add_row("database", "[green]ok[/]", str(config.DB_PATH()))
    stats = store.stats()
    table.add_row("history", "[dim]info[/]", f"{stats['runs']} run(s), {stats['services']} service(s)")
    table.add_row("python", "[dim]info[/]", ".".join(map(str, sys.version_info[:3])))
    table.add_row("warden", "[dim]info[/]", __version__)
    console.print(table)
    console.print()
    console.print("  [dim]Warden checks availability: connect, request, handshake. "
                  "No credentials, no writes, no trust decisions.[/]")
    return 0


# ---------------------------------------------------------------- helpers

def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _row_to_result(row: dict) -> CheckResult:
    return CheckResult(
        service=row["service"], check=row["check"] or "", address=row["address"] or "",
        port=row["port"] or 0, status=row["status"] or "unknown",
        latency_ms=row["latency_ms"], detail=row["detail"] or "",
        http_status=row["http_status"], tls_days_left=row["tls_days_left"],
        tls_fingerprint=row["tls_fingerprint"] or "", error=row["error"] or "",
        ts=row["ts"] or "",
    )


def _has_inventory() -> bool:
    try:
        invmod.find()
        return True
    except invmod.InventoryError:
        return False


def _exit_code(results: list[CheckResult], fail_on: str) -> int:
    failures = [r for r in results if r.status == "fail"]
    if fail_on == "any" and failures:
        return 4
    if any(r.status == "fail" for r in results):
        return 4
    if fail_on == "warn" and any(r.status == "warn" for r in results):
        return 4
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="warden",
        description="Homelab inventory, health checks and drift.",
        epilog="Read-only checks: TCP connect, HTTP request, TLS handshake. "
               "Warden reports availability, not trust.",
    )
    p.add_argument("--version", action="version", version=f"warden {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("-i", "--inventory", help="path to warden.toml")

    c = sub.add_parser("check", help="run checks and record the result")
    common(c)
    c.add_argument("--only", metavar="TAGS", help="comma separated tags, e.g. web,db")
    c.add_argument("--service", help="a single service by name")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.add_argument("--plain", action="store_true", help="no live table")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("--no-save", action="store_true", help="do not record this run")
    c.add_argument("--fail-on", default="fail", choices=["fail", "warn", "any"],
                   help="exit 4 when this is present (default: fail)")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="latest recorded state")
    common(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("drift", help="what changed since the previous run")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_drift)

    dg = sub.add_parser("digest", help="a week of history in one page")
    common(dg)
    dg.add_argument("--days", type=int, default=7, help="window in days (default: 7)")
    dg.add_argument("--json", action="store_true")
    dg.add_argument("-o", "--out", metavar="PATH", help="write markdown here instead of stdout")
    dg.set_defaults(func=cmd_digest)

    h = sub.add_parser("history", help="history for one service")
    h.add_argument("service")
    h.add_argument("-n", "--limit", type=int, default=30)
    h.set_defaults(func=cmd_history)

    e = sub.add_parser("export", help="write a markdown status page")
    common(e)
    e.add_argument("-o", "--out", metavar="PATH", help="write here instead of stdout")
    e.add_argument("--title", default="Homelab status")
    e.set_defaults(func=cmd_export)

    u = sub.add_parser("ui", help="local dashboard")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8770)
    u.add_argument("--open", action="store_true")
    u.add_argument("--debug", action="store_true")
    u.set_defaults(func=cmd_ui)

    i = sub.add_parser("init", help="write a starter warden.toml")
    i.add_argument("path", nargs="?", default="warden.toml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    doc = sub.add_parser("doctor", help="check the environment")
    doc.set_defaults(func=cmd_doctor)

    cat = sub.add_parser("cat", help="the cat")
    cat.set_defaults(func=cmd_cat)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print_cat("bare `warden` is the greeting; `warden check` is the work")
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
