"""UDP checks against real local servers — a UDP check that never sent a packet
is a guess, and UDP is exactly where guessing is easiest."""

import os
import tempfile
import threading
import unittest

os.environ.setdefault("WARDEN_DATA_DIR", tempfile.mkdtemp(prefix="warden-test-"))

from warden import prober  # noqa: E402
from warden.config import Defaults  # noqa: E402
from warden.inventory import Service, dns_query  # noqa: E402


def free_udp_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UdpServer:
    """A UDP socket that optionally answers."""

    def __init__(self, reply: bytes | None = b"pong"):
        import socket
        self.reply = reply
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(512)
            except (TimeoutError, OSError):
                continue
            self.received.append(data)
            if self.reply:
                try:
                    self.sock.sendto(self.reply, addr)
                except OSError:
                    return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def service(**kw) -> Service:
    base = dict(name="udp-svc", host="local", check="udp", port=9)
    base.update(kw)
    return Service(**base)


class TestUdpCheck(unittest.TestCase):
    def setUp(self):
        self.defaults = Defaults(timeout_s=1.0, warn_latency_ms=5000)

    def test_reply_is_ok(self):
        server = UdpServer(b"pong")
        try:
            result = prober.probe(service(port=server.port, payload="ping"), "127.0.0.1", self.defaults)
            self.assertEqual(result.status, "ok")
            self.assertIn("replied", result.detail)
        finally:
            server.close()

    def test_payload_is_actually_sent(self):
        server = UdpServer(b"pong")
        try:
            prober.probe(service(port=server.port, payload="hello"), "127.0.0.1", self.defaults)
        finally:
            server.close()
        self.assertEqual(server.received, [b"hello"])

    def test_silence_is_warn_by_default(self):
        server = UdpServer(reply=None)
        try:
            result = prober.probe(service(port=server.port, payload="ping"), "127.0.0.1", self.defaults)
            self.assertEqual(result.status, "fail")  # payload means a reply was expected
        finally:
            server.close()

    def test_silence_without_expectation_is_warn(self):
        server = UdpServer(reply=None)
        try:
            result = prober.probe(
                service(port=server.port, expect_reply=False), "127.0.0.1", self.defaults
            )
            self.assertEqual(result.status, "warn")
            self.assertIn("normal for many UDP services", result.detail)
        finally:
            server.close()

    def test_explicit_false_overrides_payload(self):
        server = UdpServer(reply=None)
        try:
            result = prober.probe(
                service(port=server.port, payload="ping", expect_reply=False),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "warn")
        finally:
            server.close()

    def test_icmp_unreachable_is_fail(self):
        result = prober.probe(
            service(port=free_udp_port(), payload="ping"), "127.0.0.1", self.defaults
        )
        self.assertEqual(result.status, "fail")
        self.assertIn("unreachable", result.detail)

    def test_expect_body_match(self):
        server = UdpServer(b"pong-42")
        try:
            result = prober.probe(
                service(port=server.port, payload="ping", expect_body="pong"),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "ok")
        finally:
            server.close()

    def test_expect_body_mismatch_fails(self):
        server = UdpServer(b"nothing useful")
        try:
            result = prober.probe(
                service(port=server.port, payload="ping", expect_body="pong"),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "fail")
            self.assertIn("not with", result.detail)
        finally:
            server.close()

    def test_hex_payload(self):
        server = UdpServer(b"\x01\x02")
        try:
            result = prober.probe(
                service(port=server.port, payload_hex="aabb"), "127.0.0.1", self.defaults
            )
            self.assertEqual(result.status, "ok")
        finally:
            server.close()
        self.assertEqual(server.received, [b"\xaa\xbb"])


class TestDnsQuery(unittest.TestCase):
    def test_query_shape(self):
        q = dns_query("example.com")
        self.assertGreater(len(q), 20)
        self.assertEqual(q[2:4], b"\x01\x00")     # standard query, recursion desired
        self.assertEqual(q[4:6], b"\x00\x01")     # one question
        self.assertIn(b"\x07example\x03com\x00", q)
        self.assertTrue(q.endswith(b"\x00\x01\x00\x01"))  # type A, class IN

    def test_txid_differs(self):
        self.assertNotEqual(dns_query("a.example")[:2], dns_query("a.example")[:2])

    def test_bad_label(self):
        from warden.inventory import InventoryError
        with self.assertRaises(InventoryError):
            dns_query("x" * 70 + ".com")

    def test_dns_name_drives_payload(self):
        svc = service(dns_name="lab.internal", port=53)
        # the transaction id is random per packet, so compare everything after it
        self.assertEqual(svc.udp_payload()[2:], dns_query("lab.internal")[2:])


if __name__ == "__main__":
    unittest.main()
