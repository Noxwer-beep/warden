"""The inventory: one TOML file describing what should be running.

Parsing is strict on purpose. A typo in a host name silently producing an
"unknown host" check is worse than a refusal: you would get a green dashboard
for a service nobody is actually watching.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import CHECK_TYPES, Defaults

TEMPLATE = """\
# warden.toml — what is supposed to be running.
#
#   warden check          run every check once and record it
#   warden status         latest result per service
#   warden drift          what changed since the previous run
#
# Checks are read-only: a TCP connect, an HTTP request, a TLS handshake.

[general]
timeout_s = 5
workers = 8
warn_latency_ms = 1200

[[host]]
name = "edge"
address = "127.0.0.1"
tags = ["network"]
notes = "Replace with a real host. Names are referenced by services."

[[service]]
name = "local-http"
host = "edge"
port = 8080
check = "http"
path = "/"
expect_status = 200
tags = ["web"]

# check = "tcp"    just a connect (databases, SSH, raw ports)
# check = "tls"    handshake + certificate expiry
# check = "https"  HTTP over TLS
"""


class InventoryError(Exception):
    """The inventory file is missing, unparsable, or inconsistent."""


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    tags: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "address": self.address,
                "tags": list(self.tags), "notes": self.notes}


@dataclass(frozen=True)
class Service:
    name: str
    host: str
    check: str = "tcp"
    port: int | None = None
    path: str = "/"
    expect_status: int | None = None
    expect_body: str = ""
    no_redirect: bool = False
    timeout: float | None = None
    tags: tuple[str, ...] = ()
    notes: str = ""

    @property
    def label(self) -> str:
        if self.check in ("http", "https", "tls") and self.port:
            scheme = "https" if self.check in ("https", "tls") else "http"
            return f"{scheme}://{self.host}:{self.port}{self.path if self.check != 'tls' else ''}"
        return f"{self.host}:{self.port or '-'}"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "host": self.host, "check": self.check,
            "port": self.port, "path": self.path,
            "expect_status": self.expect_status, "tags": list(self.tags),
            "notes": self.notes, "label": self.label,
        }


@dataclass
class Inventory:
    path: Path
    hosts: dict[str, Host] = field(default_factory=dict)
    services: list[Service] = field(default_factory=list)
    timeout: float = Defaults.timeout_s
    workers: int = Defaults.workers
    warn_latency_ms: int = Defaults.warn_latency_ms

    # -- queries ----------------------------------------------------------

    def address_of(self, service: Service) -> str:
        return self.hosts[service.host].address

    def services_for(self, host_name: str) -> list[Service]:
        return [s for s in self.services if s.host == host_name]

    def tags(self) -> list[str]:
        seen: set[str] = set()
        for host in self.hosts.values():
            seen.update(host.tags)
        for service in self.services:
            seen.update(service.tags)
        return sorted(seen)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "timeout": self.timeout,
            "workers": self.workers,
            "hosts": [h.to_dict() for h in self.hosts.values()],
            "services": [s.to_dict() for s in self.services],
        }


# ---------------------------------------------------------------- parsing

def _as_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise InventoryError(f"tags must be a string or a list, got {type(value).__name__}")


def _number(value, *, what: str, cast=float):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InventoryError(f"{what} must be a number, got {value!r}")
    return cast(value)


def load(path: str | Path) -> Inventory:
    """Read and validate an inventory file."""
    path = Path(path).expanduser()
    if not path.exists():
        raise InventoryError(f"inventory not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise InventoryError(f"{path.name}: {exc}") from None

    general = raw.get("general") or {}
    if not isinstance(general, dict):
        raise InventoryError("[general] must be a table")

    inv = Inventory(path=path)
    inv.timeout = float(_number(general.get("timeout_s"), what="timeout_s") or Defaults.timeout_s)
    inv.workers = int(_number(general.get("workers"), what="workers", cast=int) or Defaults.workers)
    inv.warn_latency_ms = int(
        _number(general.get("warn_latency_ms"), what="warn_latency_ms", cast=int)
        or Defaults.warn_latency_ms
    )
    if inv.workers < 1:
        raise InventoryError("workers must be at least 1")
    if inv.timeout <= 0:
        raise InventoryError("timeout_s must be positive")

    hosts = raw.get("host") or []
    if not isinstance(hosts, list):
        raise InventoryError("[[host]] entries must be an array of tables")
    for entry in hosts:
        if not isinstance(entry, dict):
            raise InventoryError("each [[host]] must be a table")
        name = str(entry.get("name") or "").strip()
        address = str(entry.get("address") or "").strip()
        if not name:
            raise InventoryError("a [[host]] entry is missing `name`")
        if not address:
            raise InventoryError(f"host {name!r} is missing `address`")
        if name in inv.hosts:
            raise InventoryError(f"duplicate host name {name!r}")
        inv.hosts[name] = Host(
            name=name,
            address=address,
            tags=_as_tuple(entry.get("tags")),
            notes=str(entry.get("notes") or ""),
        )

    services = raw.get("service") or []
    if not isinstance(services, list):
        raise InventoryError("[[service]] entries must be an array of tables")

    seen: set[str] = set()
    for entry in services:
        if not isinstance(entry, dict):
            raise InventoryError("each [[service]] must be a table")
        name = str(entry.get("name") or "").strip()
        host_name = str(entry.get("host") or "").strip()
        if not name:
            raise InventoryError("a [[service]] entry is missing `name`")
        if name in seen:
            raise InventoryError(f"duplicate service name {name!r}")
        seen.add(name)
        if not host_name:
            raise InventoryError(f"service {name!r} is missing `host`")
        if host_name not in inv.hosts:
            raise InventoryError(
                f"service {name!r} refers to unknown host {host_name!r} "
                f"(known: {', '.join(sorted(inv.hosts)) or 'none'})"
            )

        check = str(entry.get("check") or "tcp").lower()
        if check not in CHECK_TYPES:
            raise InventoryError(
                f"service {name!r}: check must be one of {', '.join(CHECK_TYPES)}, got {check!r}"
            )

        port = entry.get("port")
        if port is None:
            raise InventoryError(f"service {name!r} is missing `port`")
        port = int(_number(port, what=f"{name}.port", cast=int))
        if not 1 <= port <= 65535:
            raise InventoryError(f"service {name!r}: port {port} is out of range")

        expect_status = entry.get("expect_status")
        if expect_status is not None:
            expect_status = int(_number(expect_status, what=f"{name}.expect_status", cast=int))
            if not 100 <= expect_status <= 599:
                raise InventoryError(f"service {name!r}: expect_status must be 100-599")

        inv.services.append(
            Service(
                name=name,
                host=host_name,
                check=check,
                port=port,
                path=str(entry.get("path") or "/"),
                expect_status=expect_status,
                expect_body=str(entry.get("expect_body") or ""),
                no_redirect=bool(entry.get("no_redirect") or False),
                timeout=_number(entry.get("timeout_s"), what=f"{name}.timeout_s"),
                tags=_as_tuple(entry.get("tags")),
                notes=str(entry.get("notes") or ""),
            )
        )

    if not inv.services:
        raise InventoryError("the inventory declares no [[service]] entries — nothing to check")
    return inv


def find(start: str | Path = ".") -> Path:
    """Look for warden.toml upward from `start`."""
    current = Path(start).expanduser().resolve()
    for candidate in [current, *current.parents]:
        path = candidate / "warden.toml"
        if path.exists():
            return path
    raise InventoryError(
        "no warden.toml found here or in any parent directory — "
        "run `warden init` to create one"
    )


def write_template(path: str | Path = "warden.toml", force: bool = False) -> Path:
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise InventoryError(f"{target} already exists (use --force to overwrite)")
    target.write_text(TEMPLATE, encoding="utf-8")
    return target
