"""Digest tests: the numbers have to be defensible, not just present."""

import datetime as dt
import os
import tempfile
import unittest

os.environ.setdefault("WARDEN_DATA_DIR", tempfile.mkdtemp(prefix="warden-test-"))

from warden import digest as digestmod, report, store  # noqa: E402
from warden.prober import CheckResult  # noqa: E402


def result(name: str, status: str, latency: float | None = 10.0, cert: int | None = None,
           detail: str = "") -> CheckResult:
    return CheckResult(
        service=name, check="tcp", address="127.0.0.1", port=80, status=status,
        latency_ms=latency, detail=detail or status, tls_days_left=cert,
    )


def record(started: str, results: list[CheckResult]) -> None:
    run_id = store.new_run_id()
    store.save_run(run_id, started, started, "warden.toml", results)


class TestDigest(unittest.TestCase):
    def setUp(self):
        os.environ["WARDEN_DATA_DIR"] = tempfile.mkdtemp(prefix="warden-digest-")
        store.init_db()

    def test_empty_window(self):
        d = digestmod.build(7)
        self.assertEqual(d.runs, 0)
        self.assertEqual(d.services, [])
        self.assertIn("No runs", d.headline())

    def test_counts_and_rates(self):
        now = dt.datetime.now()
        for i, status in enumerate(["ok", "ok", "fail", "ok"]):
            record((now - dt.timedelta(hours=6 * i)).isoformat(timespec="seconds"),
                   [result("web", status)])
        d = digestmod.build(7)
        self.assertEqual(d.runs, 4)
        web = d.services[0]
        self.assertEqual(web.samples, 4)
        self.assertEqual(web.ok, 3)
        self.assertEqual(web.fail, 1)
        self.assertEqual(web.rate, 75.0)

    def test_window_excludes_old_runs(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(days=30)).isoformat(timespec="seconds"), [result("old", "ok")])
        record(now.isoformat(timespec="seconds"), [result("new", "ok")])
        d = digestmod.build(7)
        self.assertEqual([s.service for s in d.services], ["new"])

    def test_current_status_is_the_latest(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"), [result("db", "fail")])
        record(now.isoformat(timespec="seconds"), [result("db", "ok")])
        d = digestmod.build(1)
        self.assertEqual(d.services[0].current, "ok")
        self.assertEqual(d.services[0].first, "fail")

    def test_flapping_detected(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"), [result("flip", "ok")])
        record(now.isoformat(timespec="seconds"), [result("flip", "fail")])
        d = digestmod.build(1)
        self.assertEqual([s.service for s in d.flapping], ["flip"])

    def test_always_down(self):
        now = dt.datetime.now()
        for i in range(2):
            record((now - dt.timedelta(hours=i)).isoformat(timespec="seconds"),
                   [result("dead", "fail")])
        d = digestmod.build(1)
        self.assertEqual([s.service for s in d.always_down], ["dead"])

    def test_changed_marks_direction(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(hours=3)).isoformat(timespec="seconds"), [result("up", "fail")])
        record(now.isoformat(timespec="seconds"), [result("up", "ok")])
        d = digestmod.build(1)
        self.assertEqual(len(d.changed), 1)
        self.assertTrue(d.changed[0]["improved"])

    def test_latency_average_and_worst(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"), [result("s", "ok", 10.0)])
        record(now.isoformat(timespec="seconds"), [result("s", "ok", 30.0)])
        d = digestmod.build(1)
        entry = d.services[0]
        self.assertEqual(entry.avg_latency_ms, 20.0)
        self.assertEqual(entry.worst_latency_ms, 30.0)

    def test_cert_days_carried(self):
        now = dt.datetime.now()
        record(now.isoformat(timespec="seconds"), [result("tls", "ok", cert=12)])
        d = digestmod.build(1)
        self.assertEqual(d.services[0].cert_days, 12)

    def test_headline_mentions_worst(self):
        now = dt.datetime.now()
        record((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"), [result("a", "ok")])
        record((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"), [result("b", "fail")])
        d = digestmod.build(1)
        self.assertIn("worst", d.headline())

    def test_to_dict_is_json_ready(self):
        import json
        now = dt.datetime.now()
        record(now.isoformat(timespec="seconds"), [result("a", "ok")])
        json.dumps(digestmod.build(1).to_dict())

    def test_markdown_has_the_table_and_the_caveat(self):
        now = dt.datetime.now()
        record(now.isoformat(timespec="seconds"), [result("a", "ok"), result("b", "fail")])
        md = report.digest_markdown(digestmod.build(1))
        self.assertIn("## Services", md)
        self.assertIn("`a`", md)
        self.assertIn("Availability only", md)

    def test_markdown_without_runs_says_so(self):
        md = report.digest_markdown(digestmod.build(7))
        self.assertIn("No runs recorded", md)


if __name__ == "__main__":
    unittest.main()
