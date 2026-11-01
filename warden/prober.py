"""Health probes.

Read-only by design: a TCP connect, one HTTP request, one TLS handshake. Nothing
is written to any target, no credentials are used, and request bodies are capped.

Warden checks **availability**, not trust. Certificates are parsed for expiry and
fingerprint, but chains are not validated — a homelab full of self-signed certs
would otherwise be permanently red. Use Vantage for trust and crypto policy.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .config import Defaults
from .inventory import Inventory, Service

MAX_BODY = 64 * 1024
MAX_REDIRECTS = 5


@dataclass
class CheckResult:
    service: str
    check: str
    address: str
    port: int
    status: str = "unknown"
    latency_ms: float | None = None
    detail: str = ""
    http_status: int | None = None
    tls_days_left: int | None = None
    tls_fingerprint: str = ""
    error: str = ""
    ts: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "service": self.service, "check": self.check, "address": self.address,
            "port": self.port, "status": self.status,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "detail": self.detail, "http_status": self.http_status,
            "tls_days_left": self.tls_days_left,
            "tls_fingerprint": self.tls_fingerprint,
            "error": self.error, "ts": self.ts,
        }


# ---------------------------------------------------------------- helpers

def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


def _cert_info(tls_sock: ssl.SSLSocket) -> tuple[int | None, str]:
    """Days until expiry and a stable fingerprint of the presented certificate."""
    try:
        der = tls_sock.getpeercert(binary_form=True)
    except (ValueError, OSError):
        return None, ""
    if not der:
        return None, ""
    fingerprint = hashlib.sha256(der).hexdigest()[:16]
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        decoded = ssl._ssl._test_decode_cert  # noqa: SLF001 - no public API
    except (AttributeError, ValueError):
        return None, fingerprint
    import tempfile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=True) as fh:
            fh.write(pem)
            fh.flush()
            info = decoded(fh.name)
    except (OSError, ValueError):
        return None, fingerprint
    not_after = info.get("notAfter")
    if not not_after:
        return None, fingerprint
    try:
        when = dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None, fingerprint
    days = (when - dt.datetime.now(dt.timezone.utc)).days
    return days, fingerprint


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # Availability, not trust: self-signed homelab certificates must not read as
    # failed health. Vantage is the tool that judges trust.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------- checks

def _check_tcp(service: Service, address: str, timeout: float, defaults: Defaults) -> CheckResult:
    result = CheckResult(service.name, service.check, address, service.port or 0)
    start = time.monotonic()
    try:
        with socket.create_connection((address, service.port), timeout=timeout):
            result.latency_ms = _elapsed_ms(start)
    except socket.timeout:
        result.status = "fail"
        result.error = f"timed out after {timeout:g}s"
        result.detail = "no answer within the timeout"
        return result
    except ConnectionRefusedError:
        result.status = "fail"
        result.error = "connection refused"
        result.detail = "nothing is listening on that port"
        return result
    except OSError as exc:
        result.status = "fail"
        result.error = str(exc)
        result.detail = "could not connect"
        return result

    result.status = "ok"
    result.detail = f"connected in {result.latency_ms:.0f} ms"
    if result.latency_ms and result.latency_ms > defaults.warn_latency_ms:
        result.status = "warn"
        result.detail += f" (above the {defaults.warn_latency_ms} ms threshold)"
    return result


def _check_http(service: Service, address: str, timeout: float, defaults: Defaults) -> CheckResult:
    secure = service.check == "https"
    result = CheckResult(service.name, service.check, address, service.port or 0)
    conn_class = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    context = _tls_context() if secure else None

    path = service.path or "/"
    start = time.monotonic()
    hops = 0
    try:
        while True:
            kwargs = {"timeout": timeout}
            if secure:
                kwargs["context"] = context
            conn = conn_class(address, service.port, **kwargs)
            conn.request(
                "GET", path,
                headers={
                    "User-Agent": defaults.user_agent,
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            if secure and getattr(conn, "sock", None):
                result.tls_days_left, result.tls_fingerprint = _cert_info(conn.sock)
            body = response.read(MAX_BODY)
            status = response.status
            location = response.getheader("Location") or ""
            conn.close()

            if (
                not service.no_redirect
                and status in (301, 302, 303, 307, 308)
                and location
                and hops < MAX_REDIRECTS
            ):
                hops += 1
                if location.startswith(("http://", "https://")):
                    # follow only the path on the same endpoint; a cross-host
                    # redirect is reported rather than silently chased
                    parsed = urllib_split(location)
                    if parsed[0] != address and parsed[0] not in ("localhost", "127.0.0.1"):
                        result.detail = f"redirects to {location}"
                        break
                    path = parsed[1] or "/"
                else:
                    path = location if location.startswith("/") else "/" + location
                continue
            break
    except socket.timeout:
        result.status = "fail"
        result.error = f"timed out after {timeout:g}s"
        result.detail = "the request never completed"
        return result
    except ssl.SSLError as exc:
        result.status = "fail"
        result.error = f"TLS: {exc.reason or exc}"
        result.detail = "the TLS handshake failed"
        return result
    except ConnectionRefusedError:
        result.status = "fail"
        result.error = "connection refused"
        result.detail = "nothing is listening on that port"
        return result
    except (OSError, http.client.HTTPException) as exc:
        result.status = "fail"
        result.error = f"{type(exc).__name__}: {exc}"
        result.detail = "the request failed"
        return result

    result.latency_ms = _elapsed_ms(start)
    result.http_status = status

    if service.expect_status is not None:
        if status != service.expect_status:
            result.status = "fail"
            result.detail = f"expected HTTP {service.expect_status}, got {status}"
            return result
    elif status >= 400:
        result.status = "fail"
        result.detail = f"HTTP {status} — the service is answering with an error"
        return result

    expected = service.expect_body
    if expected and expected.encode() not in body:
        result.status = "fail"
        result.detail = f"HTTP {status}, but the body does not contain {expected!r}"
        return result

    result.status = "ok"
    result.detail = f"HTTP {status} in {result.latency_ms:.0f} ms"
    if hops:
        result.detail += f" ({hops} redirect{'s' if hops > 1 else ''})"
    if result.latency_ms > defaults.warn_latency_ms:
        result.status = "warn"
        result.detail += f" — above the {defaults.warn_latency_ms} ms threshold"

    if result.tls_days_left is not None:
        if result.tls_days_left < 0:
            result.status = "fail"
            result.detail += f"; certificate expired {abs(result.tls_days_left)} day(s) ago"
        elif result.tls_days_left <= defaults.cert_warn_days and result.status == "ok":
            result.status = "warn"
            result.detail += f"; certificate expires in {result.tls_days_left} day(s)"
    return result


def _check_tls(service: Service, address: str, timeout: float, defaults: Defaults) -> CheckResult:
    result = CheckResult(service.name, service.check, address, service.port or 0)
    context = _tls_context()
    start = time.monotonic()
    try:
        with socket.create_connection((address, service.port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=address) as tls:
                result.latency_ms = _elapsed_ms(start)
                result.tls_days_left, result.tls_fingerprint = _cert_info(tls)
                version = tls.version() or "unknown"
    except socket.timeout:
        result.status = "fail"
        result.error = f"timed out after {timeout:g}s"
        result.detail = "the TLS handshake did not complete"
        return result
    except ssl.SSLError as exc:
        result.status = "fail"
        result.error = f"TLS: {exc.reason or exc}"
        result.detail = "the TLS handshake was rejected"
        return result
    except OSError as exc:
        result.status = "fail"
        result.error = str(exc)
        result.detail = "could not connect"
        return result

    days = result.tls_days_left
    if days is None:
        result.status = "warn"
        result.detail = f"{version} handshake, certificate expiry unreadable"
        return result
    if days < 0:
        result.status = "fail"
        result.detail = f"{version}, certificate expired {abs(days)} day(s) ago"
        return result
    if days <= defaults.cert_warn_days:
        result.status = "warn"
        result.detail = f"{version}, certificate expires in {days} day(s)"
        return result
    result.status = "ok"
    result.detail = f"{version}, certificate valid for {days} day(s)"
    return result


def urllib_split(url: str) -> tuple[str, str]:
    """Split an absolute URL into (host, path). Kept tiny on purpose."""
    rest = url.split("://", 1)[-1]
    host, _, path = rest.partition("/")
    host = host.split(":")[0]
    return host, "/" + path if path else "/"


CHECKS = {"tcp": _check_tcp, "http": _check_http, "https": _check_http, "tls": _check_tls}


def probe(service: Service, address: str, defaults: Defaults, timeout: float | None = None) -> CheckResult:
    """Run one service check. Never raises: an unknown failure becomes 'unknown'."""
    handler = CHECKS.get(service.check)
    if handler is None:  # validated at load time; belt and braces
        result = CheckResult(service.name, service.check, address, service.port or 0)
        result.status = "unknown"
        result.error = f"unsupported check type {service.check!r}"
        return result
    effective = service.timeout if service.timeout is not None else timeout or defaults.timeout_s
    try:
        return handler(service, address, effective, defaults)
    except Exception as exc:  # noqa: BLE001 - a probe must never take the run down
        result = CheckResult(service.name, service.check, address, service.port or 0)
        result.status = "unknown"
        result.error = f"{type(exc).__name__}: {exc}"
        result.detail = "the check itself failed to run"
        return result


def run_checks(
    inventory: Inventory,
    services: Iterable[Service] | None = None,
    on_result: Callable[[CheckResult], None] | None = None,
    defaults: Defaults | None = None,
) -> list[CheckResult]:
    """Probe every service concurrently, calling `on_result` as each finishes."""
    defaults = defaults or Defaults()
    targets = list(services if services is not None else inventory.services)
    results: list[CheckResult] = []
    if not targets:
        return results

    workers = max(1, min(inventory.workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warden") as pool:
        futures = {
            pool.submit(probe, service, inventory.address_of(service), defaults, inventory.timeout): service
            for service in targets
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_result is not None:
                on_result(result)
    results.sort(key=lambda r: r.service)
    return results
