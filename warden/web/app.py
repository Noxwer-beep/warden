"""Local dashboard.

Same engine as the CLI. One background thread per run, a JSON endpoint the page
polls, and no authentication — bound to loopback because it is an operator
console, not a service.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import Any

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .. import __version__, config, drift as driftmod, inventory as invmod, report, store
from ..prober import run_checks

LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "running": False,
    "started": "",
    "run_id": "",
    "total": 0,
    "done": 0,
    "results": [],
    "error": "",
    "path": "",
}


def create_app(inventory_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["INVENTORY"] = inventory_path

    @app.context_processor
    def inject_globals() -> dict:
        return {"version": __version__}

    def load_inventory():
        try:
            path = app.config["INVENTORY"] or invmod.find()
            return invmod.load(path)
        except invmod.InventoryError:
            return None

    # ------------------------------------------------------------ views

    @app.get("/")
    def index():
        inv = load_inventory()
        with LOCK:
            running = STATE["running"]
            progress = {"done": STATE["done"], "total": STATE["total"]}
        latest = store.latest_run()
        rows = store.results_for(latest["id"]) if latest else []
        base = store.previous_run(latest["id"]) if latest else None
        base_rows = store.results_for(base["id"]) if base else []
        drift = driftmod.diff_runs(base, base_rows, rows) if latest else None

        services = {s.name: s for s in inv.services} if inv else {}
        uptimes = {r["service"]: store.uptime(r["service"]) for r in rows}
        counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1

        return render_template(
            "index.html",
            inv=inv,
            latest=latest,
            rows=rows,
            services=services,
            uptimes=uptimes,
            counts=counts,
            drift=drift,
            running=running,
            progress=progress,
        )

    @app.post("/run")
    def run_now():
        inv = load_inventory()
        if inv is None:
            return render_template("error.html", message="No usable warden.toml.", title="Inventory"), 400
        started = start_run(inv)
        if not started:
            return redirect(url_for("index"))
        return redirect(url_for("index"))

    @app.get("/history/<service>")
    def history_page(service: str):
        rows = store.history(service, limit=40)
        return render_template(
            "history.html", service=service, rows=rows, uptime=store.uptime(service, limit=40)
        )

    # ------------------------------------------------------------ api

    @app.get("/api/status")
    def api_status():
        with LOCK:
            running = STATE["running"]
            progress = {"done": STATE["done"], "total": STATE["total"], "started": STATE["started"]}
            live = {"error": STATE["error"]} if STATE["error"] else {}
        latest = store.latest_run()
        rows = store.results_for(latest["id"]) if latest else []
        return jsonify({
            "running": running,
            "progress": progress,
            "run": latest,
            "services": rows,
            "drift": driftmod.to_dict(
                driftmod.diff_runs(
                    store.previous_run(latest["id"]) if latest else None,
                    store.results_for(store.previous_run(latest["id"])["id"])
                    if latest and store.previous_run(latest["id"]) else [],
                    rows,
                )
            ) if latest else None,
            **live,
        })

    @app.post("/api/run")
    def api_run():
        inv = load_inventory()
        if inv is None:
            return jsonify({"error": "no inventory"}), 400
        started = start_run(inv)
        return jsonify({"started": started}), (200 if started else 409)

    @app.get("/api/drift")
    def api_drift():
        latest = store.latest_run()
        if latest is None:
            return jsonify({"error": "no runs yet"}), 404
        base = store.previous_run(latest["id"])
        drift = driftmod.diff_runs(
            base,
            store.results_for(base["id"]) if base else [],
            store.results_for(latest["id"]),
        )
        return jsonify(driftmod.to_dict(drift))

    @app.get("/api/history/<service>")
    def api_history(service: str):
        return jsonify({
            "service": service,
            "history": store.history(service, limit=100),
            "uptime": store.uptime(service),
        })

    @app.get("/api/inventory")
    def api_inventory():
        inv = load_inventory()
        if inv is None:
            return jsonify({"error": "no inventory"}), 404
        return jsonify(inv.to_dict())

    @app.get("/export.md")
    def export_md():
        inv = load_inventory()
        latest = store.latest_run()
        if inv is None or latest is None:
            abort(404)
        rows = store.results_for(latest["id"])
        from ..cli import _row_to_result

        results = [_row_to_result(r) for r in rows]
        base = store.previous_run(latest["id"])
        drift = driftmod.diff_runs(base, store.results_for(base["id"]) if base else [], rows)
        uptimes = {r.service: store.uptime(r.service) for r in results}
        body = report.markdown(inv, results, uptimes, drift)
        return app.response_class(body, mimetype="text/markdown")

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", message="No such page.", title="Not found"), 404

    return app


def start_run(inv) -> bool:
    """Start a check run in a background thread. False if one is already going."""
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update({
            "running": True,
            "started": dt.datetime.now().isoformat(timespec="seconds"),
            "run_id": store.new_run_id(),
            "total": len(inv.services),
            "done": 0,
            "results": [],
            "error": "",
            "path": str(inv.path),
        })

    def worker() -> None:
        started_ts = time.time()
        results = []

        def on_result(result):
            with LOCK:
                STATE["done"] += 1
                STATE["results"].append(result.to_dict())

        try:
            results = run_checks(inv, on_result=on_result)
            store.save_run(
                STATE["run_id"],
                dt.datetime.fromtimestamp(started_ts).isoformat(timespec="seconds"),
                dt.datetime.now().isoformat(timespec="seconds"),
                str(inv.path),
                results,
            )
        except Exception as exc:  # noqa: BLE001 - surface, never crash the server
            with LOCK:
                STATE["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with LOCK:
                STATE["running"] = False

    threading.Thread(target=worker, name="warden-run", daemon=True).start()
    return True


def serve(host: str = "127.0.0.1", port: int = 8770, debug: bool = False, inventory: str | None = None) -> None:
    store.init_db()
    config.ensure_dirs()
    app = create_app(inventory)
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
