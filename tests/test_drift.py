"""Drift tests: the value is in what changed, not in what is green."""

import unittest

from warden import drift as driftmod


def row(name, status="ok", cert="", detail=""):
    return {"service": name, "status": status, "tls_fingerprint": cert, "detail": detail}


BASE = {"id": "run-1", "started": "2026-01-01T10:00:00"}


class TestDrift(unittest.TestCase):
    def test_nothing_changed(self):
        d = driftmod.diff_runs(BASE, [row("a"), row("b")], [row("a"), row("b")])
        self.assertFalse(d.noisy)
        self.assertEqual(d.unchanged, 2)
        self.assertFalse(d.first_run)

    def test_worsened(self):
        d = driftmod.diff_runs(BASE, [row("a", "ok")], [row("a", "fail", detail="refused")])
        self.assertEqual(len(d.worsened), 1)
        self.assertEqual(d.worsened[0].before, "ok")
        self.assertEqual(d.worsened[0].after, "fail")
        self.assertEqual(d.worsened[0].detail, "refused")

    def test_improved(self):
        d = driftmod.diff_runs(BASE, [row("a", "fail")], [row("a", "ok")])
        self.assertEqual(len(d.improved), 1)

    def test_warn_to_ok_is_improvement(self):
        d = driftmod.diff_runs(BASE, [row("a", "warn")], [row("a", "ok")])
        self.assertEqual(len(d.improved), 1)

    def test_ok_to_unknown_is_worse(self):
        d = driftmod.diff_runs(BASE, [row("a", "ok")], [row("a", "unknown")])
        self.assertEqual(len(d.worsened), 1)

    def test_appeared(self):
        d = driftmod.diff_runs(BASE, [row("a")], [row("a"), row("b")])
        self.assertEqual([c.service for c in d.appeared], ["b"])

    def test_vanished(self):
        d = driftmod.diff_runs(BASE, [row("a"), row("old")], [row("a")])
        self.assertEqual([c.service for c in d.vanished], ["old"])

    def test_certificate_change(self):
        d = driftmod.diff_runs(BASE, [row("tls", cert="aaaa")], [row("tls", cert="bbbb")])
        self.assertEqual(len(d.certs), 1)
        self.assertEqual(d.certs[0].kind, "cert")
        # a certificate rotation is not a status change
        self.assertEqual(d.unchanged, 1)

    def test_cert_not_reported_when_missing(self):
        d = driftmod.diff_runs(BASE, [row("tls", cert="")], [row("tls", cert="bbbb")])
        self.assertEqual(d.certs, [])

    def test_first_run_has_no_changes(self):
        d = driftmod.diff_runs(None, [], [row("a"), row("b")])
        self.assertTrue(d.first_run)
        self.assertFalse(d.noisy)
        self.assertEqual(d.declared, 2)

    def test_ordering_puts_worsening_first(self):
        base = [row("a"), row("b"), row("c")]
        current = [row("a", "fail"), row("b", "ok"), row("c", "fail")]
        d = driftmod.diff_runs(BASE, base, current)
        self.assertEqual([c.service for c in d.changes][:2], ["a", "c"])

    def test_counts(self):
        d = driftmod.diff_runs(BASE, [row("a", "ok"), row("gone")],
                               [row("a", "fail"), row("new")])
        counts = d.counts()
        self.assertEqual(counts["worsened"], 1)
        self.assertEqual(counts["vanished"], 1)
        self.assertEqual(counts["appeared"], 1)

    def test_to_dict_serialises(self):
        import json

        d = driftmod.diff_runs(BASE, [row("a", "ok")], [row("a", "fail")])
        json.dumps(driftmod.to_dict(d))


if __name__ == "__main__":
    unittest.main()
