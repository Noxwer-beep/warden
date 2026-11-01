"""Report rendering tests."""

import json
import unittest
from pathlib import Path

from warden import report
from warden.drift import diff_runs
from warden.inventory import Host, Inventory, Service
from warden.prober import CheckResult


def make_inventory() -> Inventory:
    return Inventory(
        path=Path("warden.toml"),
        hosts={"edge": Host("edge", "10.0.0.1"), "nas": Host("nas", "10.0.0.2")},
        services=[
            Service(name="web", host="edge", check="http", port=80, path="/"),
            Service(name="ssh", host="edge", check="tcp", port=22),
        ],
    )


def make_results() -> list[CheckResult]:
    return [
        CheckResult(service="web", check="http", address="10.0.0.1", port=80,
                    status="ok", latency_ms=42.4, detail="HTTP 200 in 42 ms", http_status=200),
        CheckResult(service="ssh", check="tcp", address="10.0.0.1", port=22,
                    status="fail", latency_ms=None, detail="nothing is listening",
                    error="connection refused"),
    ]


class TestSummary(unittest.TestCase):
    def test_counts(self):
        s = report.summary(make_results())
        self.assertEqual(s["ok"], 1)
        self.assertEqual(s["fail"], 1)
        self.assertEqual(s["total"], 2)

    def test_headline(self):
        line = report.headline(make_results())
        self.assertIn("2 services", line)
        self.assertIn("1 ok", line)
        self.assertIn("1 fail", line)


class TestMarkdown(unittest.TestCase):
    def test_contains_every_service(self):
        md = report.markdown(make_inventory(), make_results())
        self.assertIn("`web`", md)
        self.assertIn("`ssh`", md)

    def test_contains_status_and_latency(self):
        md = report.markdown(make_inventory(), make_results())
        self.assertIn("**ok**", md)
        self.assertIn("**FAIL**", md)
        self.assertIn("42 ms", md)

    def test_states_the_limits(self):
        md = report.markdown(make_inventory(), make_results())
        self.assertIn("Availability only", md)
        self.assertIn("Vantage", md)

    def test_has_a_timestamp(self):
        md = report.markdown(make_inventory(), make_results())
        self.assertIn("_Checked", md)

    def test_first_run_note(self):
        drift = diff_runs(None, [], [{"service": "web", "status": "ok"}])
        md = report.markdown(make_inventory(), make_results(), drift=drift)
        self.assertIn("First recorded run", md)

    def test_drift_section_when_changed(self):
        base = {"id": "r1", "started": "2026-01-01T10:00:00"}
        drift = diff_runs(base, [{"service": "web", "status": "fail", "tls_fingerprint": ""}],
                          [{"service": "web", "status": "ok", "tls_fingerprint": ""}])
        md = report.markdown(make_inventory(), make_results(), drift=drift)
        self.assertIn("Changes since the previous run", md)
        self.assertIn("improved", md)

    def test_uptime_table(self):
        uptimes = {"web": {"service": "web", "samples": 4, "ok": 3, "rate": 75.0,
                           "avg_latency_ms": 40.0, "streak": 2}}
        md = report.markdown(make_inventory(), make_results(), uptimes=uptimes)
        self.assertIn("## Uptime", md)
        self.assertIn("75.0%", md)

    def test_pipe_in_detail_is_escaped(self):
        results = make_results()
        results[0].detail = "weird | pipe"
        md = report.markdown(make_inventory(), results)
        self.assertIn("weird \\| pipe", md)


class TestJson(unittest.TestCase):
    def test_shape(self):
        payload = report.to_dict(make_inventory(), make_results(), "run-1", "a", "b")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(len(payload["services"]), 2)
        self.assertEqual(payload["summary"]["total"], 2)
        json.dumps(payload)

    def test_render_json_is_parseable(self):
        payload = report.to_dict(make_inventory(), make_results())
        self.assertIn("services", json.loads(report.render_json(payload)))


if __name__ == "__main__":
    unittest.main()
