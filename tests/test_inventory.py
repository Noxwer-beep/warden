"""Inventory tests: strict parsing is the point."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("WARDEN_DATA_DIR", tempfile.mkdtemp(prefix="warden-test-"))

from warden import inventory as inv  # noqa: E402

VALID = """
[general]
timeout_s = 3
workers = 4

[[host]]
name = "edge"
address = "127.0.0.1"

[[host]]
name = "nas"
address = "127.0.0.1"
tags = ["storage"]

[[service]]
name = "web"
host = "edge"
port = 8080
check = "http"
path = "/health"
expect_status = 200
tags = ["web"]

[[service]]
name = "db"
host = "edge"
check = "tcp"
port = 5432
"""


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="warden-inv-")

    def write(self, body: str) -> Path:
        path = Path(self.dir) / "warden.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_valid_inventory(self):
        inventory = inv.load(self.write(VALID))
        self.assertEqual(inventory.timeout, 3.0)
        self.assertEqual(inventory.workers, 4)
        self.assertEqual(len(inventory.hosts), 2)
        self.assertEqual(len(inventory.services), 2)
        web = next(s for s in inventory.services if s.name == "web")
        self.assertEqual(web.check, "http")
        self.assertEqual(web.path, "/health")
        self.assertEqual(web.expect_status, 200)
        self.assertEqual(inventory.address_of(web), "127.0.0.1")

    def test_defaults_when_general_missing(self):
        body = VALID.split("[[host]]")[0].split("[general]")[0] + "[[host]]" + VALID.split("[[host]]", 1)[1]
        inventory = inv.load(self.write(body))
        self.assertEqual(inventory.timeout, 5.0)
        self.assertEqual(inventory.workers, 8)

    def test_missing_file(self):
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(Path(self.dir) / "nope.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_bad_toml(self):
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write("[[host]\nname="))
        self.assertIn("warden.toml", str(ctx.exception))

    def test_no_services(self):
        body = VALID.split("[[service]]")[0]
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("no [[service]]", str(ctx.exception))

    def test_duplicate_host(self):
        body = VALID + '\n[[host]]\nname = "edge"\naddress = "127.0.0.1"\n'
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("duplicate host", str(ctx.exception))

    def test_duplicate_service(self):
        body = VALID + '\n[[service]]\nname = "web"\nhost = "edge"\nport = 1\n'
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("duplicate service", str(ctx.exception))

    def test_unknown_host_reference(self):
        body = VALID.replace('host = "edge"\ncheck = "tcp"', 'host = "ghost"\ncheck = "tcp"')
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("unknown host", str(ctx.exception))

    def test_missing_address(self):
        body = VALID.replace('address = "127.0.0.1"\n\n[[host]]', '\n[[host]]', 1)
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("address", str(ctx.exception))

    def test_bad_check_type(self):
        body = VALID.replace('check = "tcp"', 'check = "smoke"')
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("check must be one of", str(ctx.exception))

    def test_bad_port(self):
        body = VALID.replace("port = 5432", "port = 70000")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("out of range", str(ctx.exception))

    def test_missing_port(self):
        body = VALID.replace("port = 5432", "")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("missing `port`", str(ctx.exception))

    def test_bad_expect_status(self):
        body = VALID.replace("expect_status = 200", "expect_status = 999")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write(body))
        self.assertIn("100-599", str(ctx.exception))


class TestHelpers(unittest.TestCase):
    def test_template_roundtrips(self):
        directory = tempfile.mkdtemp(prefix="warden-tpl-")
        path = inv.write_template(Path(directory) / "warden.toml")
        inventory = inv.load(path)
        self.assertGreaterEqual(len(inventory.services), 1)

    def test_template_refuses_overwrite(self):
        directory = tempfile.mkdtemp(prefix="warden-tpl2-")
        path = inv.write_template(Path(directory) / "warden.toml")
        with self.assertRaises(inv.InventoryError):
            inv.write_template(path)
        inv.write_template(path, force=True)

    def test_find_walks_up(self):
        directory = Path(tempfile.mkdtemp(prefix="warden-find-"))
        (directory / "warden.toml").write_text(VALID, encoding="utf-8")
        nested = directory / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(inv.find(nested).resolve(), (directory / "warden.toml").resolve())

    def test_find_fails_cleanly(self):
        directory = tempfile.mkdtemp(prefix="warden-find-none-")
        with self.assertRaises(inv.InventoryError):
            inv.find(directory)

    def test_tags_collected(self):
        directory = tempfile.mkdtemp(prefix="warden-tags-")
        path = Path(directory) / "warden.toml"
        path.write_text(VALID, encoding="utf-8")
        inventory = inv.load(path)
        tags = inventory.tags()
        self.assertIn("web", tags)
        self.assertIn("storage", tags)

    def test_service_label(self):
        service = inv.Service(name="x", host="h", check="http", port=80, path="/")
        self.assertEqual(service.label, "http://h:80/")
        tcp = inv.Service(name="y", host="h", check="tcp", port=5432)
        self.assertEqual(tcp.label, "h:5432")


class TestAddressAndUdp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="warden-adv-")

    def write(self, extra: str) -> Path:
        body = """
[general]
timeout_s = 2

[[host]]
name = "box"
address = "10.0.0.5"

[[service]]
name = "svc"
host = "box"
port = 80
check = "tcp"
""" + extra
        path = Path(self.dir) / "warden.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_address_override_wins(self):
        path = self.write('address = "127.0.0.1"\n')
        inventory = inv.load(path)
        self.assertEqual(inventory.address_of(inventory.services[0]), "127.0.0.1")

    def test_address_falls_back_to_host(self):
        inventory = inv.load(self.write(""))
        self.assertEqual(inventory.address_of(inventory.services[0]), "10.0.0.5")

    def test_bad_address_rejected(self):
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(self.write('address = "10.0.0.0/24"\n'))
        self.assertIn("plain host", str(ctx.exception))

    def test_udp_service_accepted(self):
        body = """
[general]

[[host]]
name = "box"
address = "10.0.0.5"

[[service]]
name = "svc"
host = "box"
port = 53
check = "udp"
dns_name = "example.com"
"""
        path = Path(self.dir) / "udp.toml"
        path.write_text(body, encoding="utf-8")
        inventory = inv.load(path)
        self.assertEqual(inventory.services[0].check, "udp")
        self.assertEqual(inventory.services[0].dns_name, "example.com")

    def test_bad_hex_rejected(self):
        body = """
[general]

[[host]]
name = "box"
address = "10.0.0.5"

[[service]]
name = "svc"
host = "box"
port = 123
check = "udp"
payload_hex = "zzz"
"""
        path = Path(self.dir) / "w2.toml"
        path.write_text(body, encoding="utf-8")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(path)
        self.assertIn("valid hex", str(ctx.exception))

    def test_expect_reply_must_be_bool(self):
        body = """
[general]

[[host]]
name = "box"
address = "10.0.0.5"

[[service]]
name = "svc"
host = "box"
port = 161
check = "udp"
expect_reply = "yes"
"""
        path = Path(self.dir) / "w3.toml"
        path.write_text(body, encoding="utf-8")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(path)
        self.assertIn("true or false", str(ctx.exception))

    def test_udp_only_keys_rejected_on_tcp(self):
        body = """
[general]

[[host]]
name = "box"
address = "10.0.0.5"

[[service]]
name = "svc"
host = "box"
port = 22
check = "tcp"
dns_name = "example.com"
"""
        path = Path(self.dir) / "w4.toml"
        path.write_text(body, encoding="utf-8")
        with self.assertRaises(inv.InventoryError) as ctx:
            inv.load(path)
        self.assertIn('only apply to check = "udp"', str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
