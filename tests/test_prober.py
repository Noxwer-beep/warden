"""Probe tests against real local sockets.

No mocking: a check that has never connected to a socket is a guess. Every
server here binds to 127.0.0.1 on an ephemeral port.
"""

import http.server
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

os.environ.setdefault("WARDEN_DATA_DIR", tempfile.mkdtemp(prefix="warden-test-"))

from warden.config import Defaults  # noqa: E402
from warden.inventory import Service  # noqa: E402
from warden import prober  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TcpServer:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"warden-ok"
    redirect_to = ""

    def do_GET(self):  # noqa: N802
        if self.redirect_to and self.path == "/":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class HttpServer:
    def __init__(self, status=200, body=b"warden-ok", redirect_to=""):
        handler = type("H", (Handler,), {"status": status, "body": body, "redirect_to": redirect_to})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_service(**kw) -> Service:
    base = dict(name="svc", host="local", check="tcp", port=0)
    base.update(kw)
    return Service(**base)


class TestTcp(unittest.TestCase):
    def setUp(self):
        self.server = TcpServer()
        self.defaults = Defaults(timeout_s=2.0)

    def tearDown(self):
        self.server.close()

    def test_open_port_is_ok(self):
        result = prober.probe(make_service(port=self.server.port), "127.0.0.1", self.defaults)
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.latency_ms)

    def test_closed_port_fails(self):
        result = prober.probe(make_service(port=free_port()), "127.0.0.1", self.defaults)
        self.assertEqual(result.status, "fail")
        self.assertIn("refused", result.error)

    def test_slow_threshold_marks_warn(self):
        strict = Defaults(timeout_s=2.0, warn_latency_ms=0)
        result = prober.probe(make_service(port=self.server.port), "127.0.0.1", strict)
        self.assertEqual(result.status, "warn")
        self.assertIn("threshold", result.detail)


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.defaults = Defaults(timeout_s=2.0)

    def test_200_is_ok(self):
        server = HttpServer()
        try:
            result = prober.probe(
                make_service(check="http", port=server.port, path="/"), "127.0.0.1", self.defaults
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.http_status, 200)
        finally:
            server.close()

    def test_expected_status_mismatch_fails(self):
        server = HttpServer()
        try:
            result = prober.probe(
                make_service(check="http", port=server.port, expect_status=201),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "fail")
            self.assertIn("expected HTTP 201", result.detail)
        finally:
            server.close()

    def test_500_fails_without_expectation(self):
        server = HttpServer(status=500)
        try:
            result = prober.probe(
                make_service(check="http", port=server.port), "127.0.0.1", self.defaults
            )
            self.assertEqual(result.status, "fail")
            self.assertIn("500", result.detail)
        finally:
            server.close()

    def test_expect_body_match(self):
        server = HttpServer(body=b"hello warden world")
        try:
            result = prober.probe(
                make_service(check="http", port=server.port, expect_body="warden"),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "ok")
        finally:
            server.close()

    def test_expect_body_mismatch(self):
        server = HttpServer(body=b"nope")
        try:
            result = prober.probe(
                make_service(check="http", port=server.port, expect_body="warden"),
                "127.0.0.1", self.defaults,
            )
            self.assertEqual(result.status, "fail")
            self.assertIn("does not contain", result.detail)
        finally:
            server.close()

    def test_closed_port_fails(self):
        result = prober.probe(
            make_service(check="http", port=free_port()), "127.0.0.1", self.defaults
        )
        self.assertEqual(result.status, "fail")


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class TestTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="warden-tls-")
        cls.cert = os.path.join(cls.dir, "c.pem")
        cls.key = os.path.join(cls.dir, "k.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", cls.key,
             "-out", cls.cert, "-days", "2", "-nodes", "-subj", "/CN=localhost"],
            check=True, capture_output=True,
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, cls.key)
        cls.sock = socket.socket()
        cls.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(5)
        cls.port = cls.sock.getsockname()[1]
        cls._stop = False
        threading.Thread(target=cls._serve, args=(ctx,), daemon=True).start()

    @classmethod
    def _serve(cls, ctx):
        while not cls._stop:
            try:
                raw, _ = cls.sock.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(64)
            except (ssl.SSLError, OSError):
                raw.close()

    @classmethod
    def tearDownClass(cls):
        cls._stop = True
        cls.sock.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_tls_handshake_reads_expiry(self):
        result = prober.probe(
            make_service(check="tls", port=self.port), "127.0.0.1", Defaults(timeout_s=3.0)
        )
        self.assertIn(result.status, ("ok", "warn"))
        self.assertIsNotNone(result.tls_days_left)
        self.assertGreater(result.tls_days_left, 0)
        self.assertTrue(result.tls_fingerprint)

    def test_short_lived_cert_warns(self):
        strict = Defaults(timeout_s=3.0, cert_warn_days=30)
        result = prober.probe(make_service(check="tls", port=self.port), "127.0.0.1", strict)
        self.assertEqual(result.status, "warn")
        self.assertIn("expires in", result.detail)

    def test_plain_port_is_not_tls(self):
        server = TcpServer()
        try:
            result = prober.probe(
                make_service(check="tls", port=server.port), "127.0.0.1", Defaults(timeout_s=2.0)
            )
            self.assertEqual(result.status, "fail")
        finally:
            server.close()


class TestProbeRobustness(unittest.TestCase):
    def test_unknown_check_type_does_not_raise(self):
        result = prober.probe(make_service(check="bogus"), "127.0.0.1", Defaults())
        self.assertEqual(result.status, "unknown")

    def test_unresolvable_host_fails_cleanly(self):
        result = prober.probe(
            make_service(port=9), "no-such-host.invalid", Defaults(timeout_s=1.0)
        )
        self.assertEqual(result.status, "fail")

    def test_result_serialises(self):
        server = TcpServer()
        try:
            data = prober.probe(make_service(port=server.port), "127.0.0.1", Defaults()).to_dict()
            self.assertEqual(data["status"], "ok")
            self.assertIn("latency_ms", data)
            import json

            json.dumps(data)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
