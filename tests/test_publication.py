"""Synthetic publication contracts and full-batch failures."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from test_qianji import StoreCase, bill
from integrations.qianji_client import QianjiClient, QianjiAPIError, load_snapshot, snapshot_schema
from qianji.normalize import DataError, canonical, digest


class PublicationTests(StoreCase):
    def test_fixed_references_survive_update_and_replay_is_identical(self):
        self.store.import_file(self.source([bill()]))
        first = self.store.export()
        fixed = Path(first["publication"]["path"])
        payload = fixed.read_bytes()
        self.assertEqual(first, self.store.export())
        self.store.import_file(self.source([bill("new", currency="USD", money=0.01)]))
        second = self.store.export()
        self.assertNotEqual(first["publication"], second["publication"])
        self.assertEqual(fixed.read_bytes(), payload)
        doc = load_snapshot(fixed, expected_sha256=first["publication"]["sha256"],
                            expected_library_id=first["library_id"])
        self.assertEqual(doc["snapshot"]["counts"]["transactions"], 1)
        self.assertEqual(doc["datasets"]["transactions"][0]["amount"], "12.34")
        with self.assertRaises(QianjiAPIError):
            load_snapshot(fixed, expected_sha256="0"*64)

    def test_schema_types_enums_currency_counts_and_hashes(self):
        self.store.import_file(self.source([bill()]))
        base = self.store.snapshot()
        target = self.root / "synthetic.json"
        for field, bad in [("amount", 12.34), ("amount", "0.001"), ("currency", "usd"),
                           ("kind", "unknown"), ("revision", True)]:
            doc = copy.deepcopy(base)
            doc["datasets"]["transactions"][0][field] = bad
            doc["snapshot"]["data_sha256"] = digest(canonical(doc["datasets"]))
            target.write_text(canonical(doc), encoding="utf8")
            with self.subTest(field=field, bad=bad), self.assertRaises(QianjiAPIError):
                load_snapshot(target)
        for field, bad in [("counts", {"transactions": 0, "daily_summary": 1}),
                           ("data_sha256", "0"*64), ("generation", "different")]:
            doc = copy.deepcopy(base); doc["snapshot"][field] = bad
            target.write_text(canonical(doc), encoding="utf8")
            with self.subTest(field=field), self.assertRaises(QianjiAPIError):
                load_snapshot(target)
        machine = Path(__file__).resolve().parents[1] / "integrations/snapshot.schema.json"
        self.assertEqual(json.loads(machine.read_bytes()), snapshot_schema())

    def test_same_generation_drift_and_failed_latest_preserve_original(self):
        self.store.import_file(self.source([bill()]))
        first = self.store.export()
        current = self.store.root / "exports/current.json"
        before = current.read_bytes()
        altered = self.store.snapshot()
        altered["datasets"]["transactions"][0]["amount"] = "99.99"
        with patch.object(self.store, "snapshot", return_value=altered):
            with self.assertRaises(DataError):
                self.store.export()
        self.assertEqual(current.read_bytes(), before)
        self.store.import_file(self.source([bill("new")]))
        with patch("qianji.store.atomic_write", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.store.export()
        self.assertEqual(current.read_bytes(), before)
        self.assertEqual(hashlib.sha256(before).hexdigest(), first["publication"]["sha256"])

    def test_409_on_later_table_returns_no_partial_batch(self):
        self.store.import_file(self.source([bill()]))
        client = QianjiClient.__new__(QianjiClient)
        client.snapshot = self.store.snapshot()["snapshot"]
        rows = self.store.snapshot()["datasets"]["transactions"]
        with patch.object(client, "read_all", side_effect=[rows, QianjiAPIError("snapshot_changed (HTTP 409)")]):
            with self.assertRaisesRegex(QianjiAPIError, "409"):
                client.read_batch({"transactions": {}, "daily_summary": {}})
