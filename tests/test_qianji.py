"""Synthetic data only. Never load personal exports in the test suite."""

from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from integrations.qianji_client import QianjiAPIError, QianjiClient, load_snapshot
from qianji.api import Server
from qianji.normalize import DataError, load_export, record_id
from qianji.store import Store


def bill(key="synthetic-1", **changes):
    return {"key": key, "date": "2026-01-02 12:30:00", "category": "测试分类",
            "type": "支出", "money": 12.34, "currency": "CNY", "asset": 9007199254740993,
            "remark": "纯虚构测试", "hasbx": 0, "username": None, "billflag": None,
            "sourceid": "", **changes}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")

    def source(self, rows, name="input.json"):
        path = self.root / name
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8-sig")
        return path

    def rows(self):
        return self.store.snapshot()["datasets"]["transactions"]


class StoreTests(StoreCase):
    def test_idempotent_and_byte_exact_archive(self):
        path = self.source([bill(), bill()])
        first = self.store.import_file(path)
        self.assertEqual((first["added"], first["duplicates_in_file"]), (1, 1))
        archive = self.store.root / "archive" / (first["source_sha256"] + ".json")
        self.assertEqual(archive.read_bytes(), path.read_bytes())
        snapshot = self.store.snapshot()
        second = self.store.import_file(path)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(snapshot, self.store.snapshot())
        self.assertEqual(self.store.status()["record_revisions"], 1)
        self.assertTrue(self.store.verify()["ok"])

    def test_preview_does_not_import_or_archive(self):
        result = self.store.import_file(self.source([bill()]), preview=True)
        self.assertEqual(result["added"], 1)
        self.assertFalse(result["applied"])
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.store.root / "archive").exists())

    def test_conflicts_block_entire_batch_and_preserve_history(self):
        self.store.import_file(self.source([bill()]))
        snapshot = self.store.snapshot()
        path = self.source([bill(money=20), bill("new")], "changed.json")
        blocked = self.store.import_file(path)
        self.assertFalse(blocked["applied"])
        self.assertEqual(blocked["conflicts"][0]["changed_fields"], ["money"])
        self.assertEqual(snapshot, self.store.snapshot())
        with self.assertRaises(DataError):
            self.store.import_file(path, accept_updates=True)
        accepted = self.store.import_file(path, accept_updates=True,
                                           expected_generation=blocked["generation_before"])
        self.assertTrue(accepted["applied"])
        history = self.store.history(record_id("main", "synthetic-1"))
        self.assertEqual([r["amount"] for r in history], ["12.34", "20.00"])
        self.assertEqual([r["revision"] for r in history], [1, 2])
        self.assertTrue(self.store.verify()["ok"])

    def test_stale_generation_prevents_accepting_changes(self):
        self.store.import_file(self.source([bill()]))
        path = self.source([bill(money=20)], "updated.json")
        preview = self.store.import_file(path, preview=True)
        self.store.import_file(self.source([bill("new")]))
        with self.assertRaises(DataError):
            self.store.import_file(path, accept_updates=True,
                                   expected_generation=preview["generation_before"])

    def test_reimport_old_file_cannot_silently_revert_new_version(self):
        original = self.source([bill()], "old.json")
        self.store.import_file(original)
        path = self.source([bill(money=100)], "new.json")
        generation = self.store.snapshot()["snapshot"]["generation"]
        self.store.import_file(path, accept_updates=True, expected_generation=generation)
        result = self.store.import_file(original)
        self.assertFalse(result["applied"])
        self.assertEqual(self.rows()[0]["amount"], "100.00")

    def test_same_key_conflicting_inside_file_rejected(self):
        with self.assertRaises(DataError):
            self.store.import_file(self.source([bill(), bill(money=15)]))
        self.assertEqual(self.rows(), [])

    def test_identical_content_different_keys_remain_distinct(self):
        self.store.import_file(self.source([bill("a"), bill("b")]))
        self.assertEqual(len(self.rows()), 2)

    def test_ledger_identity_and_offset_are_stable(self):
        path = self.source([bill()])
        self.store.import_file(path, ledger="one")
        self.store.import_file(path, ledger="two")
        self.assertEqual(len({r["id"] for r in self.rows()}), 2)
        with self.assertRaises(DataError):
            self.store.import_file(path, ledger="one", utc_offset="+00:00")

    def test_missing_rows_are_not_deleted(self):
        self.store.import_file(self.source([bill("a"), bill("b")]))
        self.store.import_file(self.source([bill("a")]))
        self.assertEqual(len(self.rows()), 2)

    def test_order_and_numeric_representation_do_not_create_revisions(self):
        self.store.import_file(self.source([bill("a", money=10), bill("b")]))
        snapshot = self.store.snapshot()
        result = self.store.import_file(self.source([bill("b"), bill("a", money=10.0)]))
        self.assertEqual(result["unchanged"], 2)
        self.assertEqual(snapshot, self.store.snapshot())

    def test_unknown_fields_preserved_and_revisioned(self):
        self.store.import_file(self.source([bill(extension={"x": 1})]))
        result = self.store.import_file(self.source([bill(extension={"x": 2})]), preview=True)
        self.assertEqual(result["conflicts"][0]["changed_fields"], ["extension"])

    def test_decimal_summaries_refunds_transfers_and_currency(self):
        rows = [bill("expense", money=0.1), bill("expense2", money=0.2),
                bill("refund", type="退款", money=0.1, sourceid="expense"),
                bill("income", type="收入", money=10),
                bill("transfer", type="转账", money=1000, **{"from": 1, "target": 2}),
                bill("usd", money=5, currency="USD")]
        self.store.import_file(self.source(rows))
        summaries = self.store.snapshot()["datasets"]["daily_summary"]
        self.assertEqual(len(summaries), 2)
        cny = summaries[0]
        self.assertEqual((cny["expense"], cny["refund"], cny["net_expense"], cny["net_cashflow"]),
                         ("0.30", "0.10", "0.20", "9.80"))
        self.assertEqual(cny["transfer"], "1000.00")
        refund = next(r for r in self.rows() if r["kind"] == "refund")
        self.assertEqual(refund["related_record_id"], record_id("main", "expense"))

    def test_large_identifiers_and_explicit_timezone(self):
        self.store.import_file(self.source([bill()]))
        row = self.rows()[0]
        self.assertEqual(row["asset_id"], "9007199254740993")
        self.assertEqual(row["occurred_at"], "2026-01-02T12:30:00+08:00")
        self.assertEqual(row["timezone_basis"], "configured_fixed_offset")

    def test_invalid_input_does_not_partially_import(self):
        for changes in ({"key": ""}, {"key": 123}, {"date": "2026-02-30 12:30:00"},
                        {"money": -1}, {"money": 0.001}, {"money": True}, {"money": "12.34"},
                        {"type": "未知"}, {"currency": "cny"}, {"type": "转账"}, {"remark": 12}):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                self.store.import_file(self.source([bill("valid"), bill(**changes)]))
        self.assertEqual(self.rows(), [])

    def test_invalid_json_duplicate_fields_and_nonfinite_rejected(self):
        path = self.root / "bad.json"
        for content in ('[{"key":"a","key":"b"}]', '[NaN]', '[Infinity]', '{}', '[', 'null'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(DataError):
                load_export(path)

    def test_archive_tampering_detected(self):
        result = self.store.import_file(self.source([bill()]))
        archive = self.store.root / "archive" / (result["source_sha256"] + ".json")
        archive.write_bytes(b"[]")
        with self.assertRaises(DataError):
            self.store.verify()
        with self.assertRaises(DataError):
            self.store.import_file(self.source([bill()]))

    def test_export_checksum_and_atomic_refresh(self):
        self.store.import_file(self.source([bill()]))
        self.store.export()
        path = self.store.root / "exports" / "current.json"
        self.assertEqual(load_snapshot(path), self.store.snapshot())
        before = path.read_bytes()
        self.store.export()
        self.assertEqual(path.read_bytes(), before)
        tampered = json.loads(before)
        tampered["datasets"]["transactions"][0]["amount"] = "999.00"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(QianjiAPIError):
            load_snapshot(path)

    def test_failed_database_write_rolls_back_all_rows(self):
        with closing(self.store.connect()) as db, db:
            db.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON revisions BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.import_file(self.source([bill()]))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.status()["import_runs"], 0)

    def test_cli_import_repeat_and_conflict_exit_codes(self):
        path = self.source([bill()])
        def run(*args):
            if args[0] == 'import':
                args = (*args, '--no-analysis')
            return subprocess.run([sys.executable, "-m", "qianji", "--data-dir", str(self.root / "cli"), *args],
                                  capture_output=True, encoding="utf-8", cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run("import", str(path)).returncode, 0)
        self.assertTrue((self.root / "cli" / "exports" / "current.json").is_file())
        self.assertEqual(json.loads(run("import", str(path)).stdout)["unchanged"], 1)
        changed = self.source([bill(money=30)])
        self.assertEqual(run("import", str(changed)).returncode, 2)
        self.assertEqual(run("verify").returncode, 0)


class APITests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.import_file(self.source([bill(str(i), date=f"2026-01-0{i + 1} 12:00:00") for i in range(4)]))
        self.server = Server(self.store, 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(cleanup)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path, headers=None, method="GET"):
        with urlopen(Request(self.base + path, headers=headers or {}, method=method), timeout=5) as response:
            return json.load(response)

    def test_http_pagination_filters_and_standalone_client(self):
        client = QianjiClient(self.base)
        self.assertEqual(len(client.read_all(page_size=1)), 4)
        selected = client.read_all(start_date="2026-01-02", end_date="2026-01-03", page_size=1)
        self.assertEqual([r["date_local"] for r in selected], ["2026-01-02", "2026-01-03"])
        self.assertEqual(len(client.read_all("daily_summary", page_size=2)), 4)
        self.assertEqual(client.read_all(ledger_id="missing"), [])

    def test_http_snapshot_change_fails_instead_of_mixing_pages(self):
        client = QianjiClient(self.base)
        self.store.import_file(self.source([bill("new")]))
        with self.assertRaises(QianjiAPIError):
            client.read_all()

    def test_http_change_between_pages_returns_no_partial_result(self):
        client = QianjiClient(self.base)
        original = client._get
        def updating_get(*args, **kwargs):
            result = original(*args, **kwargs)
            if result["pagination"]["offset"] == 0:
                self.store.import_file(self.source([bill("new")]))
            return result
        client._get = updating_get
        with self.assertRaises(QianjiAPIError):
            client.read_all(page_size=1)

    def test_http_rejects_bad_host_origin_and_write_methods(self):
        for headers, method, code in (({"Host": "evil.example"}, "GET", 403),
                                      ({"Origin": "https://evil.example"}, "GET", 403),
                                      ({"Sec-Fetch-Site": "cross-site"}, "GET", 403),
                                      ({}, "POST", 405), ({}, "DELETE", 405)):
            with self.subTest(headers=headers, method=method), self.assertRaises(HTTPError) as error:
                self.get("/api/v1/snapshot", headers, method)
            self.assertEqual(error.exception.code, code)
            error.exception.close()

    def test_http_validation(self):
        generation = self.store.snapshot()["snapshot"]["generation"]
        for query in ("", "?generation=stale", f"?generation={generation}&limit=0",
                      f"?generation={generation}&start_date=20260102",
                      f"?generation={generation}&offset=-1", f"?generation={generation}&offset=1&offset=2",
                      f"?generation={generation}&unknown=yes", f"?generation={generation}&kind=unknown",
                      f"?generation={generation}&start_date=2026-02-01&end_date=2026-01-01"):
            with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                self.get("/api/v1/datasets/transactions" + query)
            self.assertIn(error.exception.code, (400, 409))
            error.exception.close()


if __name__ == "__main__":
    unittest.main()
