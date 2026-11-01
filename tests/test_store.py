"""Storage tests: history is the only proof a service was ever up."""

import os
import tempfile
import unittest

os.environ.setdefault("WARDEN_DATA_DIR", tempfile.mkdtemp(prefix="warden-store-"))

from warden import store  # noqa: E402
from warden.prober import CheckResult  # noqa: E402


def result(name="svc", status="ok", latency=12.0, ts="2026-01-01T10:00:00", cert=""):
    return CheckResult(
        service=name, check="tcp", address="127.0.0.1", port=80,
        status=status, latency_ms=latency, detail="ok" if status == "ok" else "broken",
        tls_fingerprint=cert, ts=ts,
    )


class TestStore(unittest.TestCase):
    def setUp(self):
        os.environ["WARDEN_DATA_DIR"] = tempfile.mkdtemp(prefix="warden-db-")
        store.init_db()

    def test_save_and_read_back(self):
        run_id = store.new_run_id()
        store.save_run(run_id, "2026-01-01T10:00:00", "2026-01-01T10:00:05", "warden.toml",
                       [result(), result("db", "fail")])
        rows = store.results_for(run_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["service"] for r in rows}, {"svc", "db"})

    def test_run_counters(self):
        run_id = store.new_run_id()
        store.save_run(run_id, "2026-01-01T10:00:00", "2026-01-01T10:00:05", "warden.toml",
                       [result(), result("a", "warn"), result("b", "fail"), result("c", "unknown")])
        latest = store.latest_run()
        self.assertEqual(latest["n_ok"], 1)
        self.assertEqual(latest["n_warn"], 1)
        self.assertEqual(latest["n_fail"], 1)
        self.assertEqual(latest["n_unknown"], 1)
        self.assertEqual(latest["n_services"], 4)

    def test_latest_and_previous(self):
        first = store.new_run_id()
        store.save_run(first, "2026-01-01T10:00:00", "", "w", [result()])
        second = store.new_run_id()
        store.save_run(second, "2026-01-01T11:00:00", "", "w", [result()])
        self.assertEqual(store.latest_run()["id"], second)
        self.assertEqual(store.previous_run(second)["id"], first)

    def test_previous_of_first_is_none(self):
        run_id = store.new_run_id()
        store.save_run(run_id, "2026-01-01T10:00:00", "", "w", [result()])
        self.assertIsNone(store.previous_run(run_id))

    def test_uptime_rate_and_streak(self):
        for i, status in enumerate(["ok", "ok", "fail", "ok"]):
            run_id = store.new_run_id()
            store.save_run(run_id, f"2026-01-01T1{i}:00:00", "", "w", [result(status=status)])
        up = store.uptime("svc")
        self.assertEqual(up["samples"], 4)
        self.assertEqual(up["ok"], 3)
        self.assertEqual(up["rate"], 75.0)
        self.assertEqual(up["streak"], 1)

    def test_uptime_without_history(self):
        up = store.uptime("never-checked")
        self.assertEqual(up["samples"], 0)
        self.assertIsNone(up["rate"])

    def test_history_order(self):
        for i in (1, 2, 3):
            run_id = store.new_run_id()
            store.save_run(run_id, f"2026-01-0{i}T10:00:00", "", "w", [result()])
        rows = store.history("svc")
        self.assertEqual([r["started"] for r in rows],
                         ["2026-01-03T10:00:00", "2026-01-02T10:00:00", "2026-01-01T10:00:00"])

    def test_prune_keeps_newest(self):
        for i in range(6):
            run_id = store.new_run_id()
            store.save_run(run_id, f"2026-01-0{i + 1}T10:00:00", "", "w", [result()])
        removed = store.prune(keep_runs=2)
        self.assertEqual(removed, 4)
        self.assertEqual(len(store.list_runs()), 2)

    def test_stats(self):
        run_id = store.new_run_id()
        store.save_run(run_id, "2026-01-01T10:00:00", "", "w", [result(), result("db")])
        stats = store.stats()
        self.assertEqual(stats["runs"], 1)
        self.assertEqual(stats["services"], 2)

    def test_avg_latency(self):
        for lat in (10.0, 20.0):
            run_id = store.new_run_id()
            store.save_run(run_id, f"2026-01-01T1{int(lat)}:00:00", "", "w", [result(latency=lat)])
        up = store.uptime("svc")
        self.assertAlmostEqual(up["avg_latency_ms"], 15.0, places=1)


if __name__ == "__main__":
    unittest.main()
