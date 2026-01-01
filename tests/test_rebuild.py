"""Synthetic source replay, publication recovery, and processing provenance."""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

from integrations.qianji_client import QianjiAPIError, QianjiClient, load_snapshot
from qianji import processing
from qianji.api import Server
from qianji.mcp_reader import Reader
from qianji.normalize import DataError, canonical, record_id
from qianji.normalization_v1 import normalize
from qianji.store import Store, atomic_write
from test_qianji import StoreCase, bill


@contextmanager
def normalizer_upgrade(version="synthetic-normalizer-2", transform=None):
    def upgraded(raw, ledger, utc_offset):
        row, raw_json = normalize(raw, ledger, utc_offset)
        if transform is None:
            row["category"] = "reprocessed/" + row["category"]
        else:
            transform(row)
        return row, raw_json

    with patch.object(processing, "NORMALIZER_VERSION", version), \
            patch.dict(processing.NORMALIZERS, {version: upgraded}):
        yield version


class RebuildTests(StoreCase):
    def business_rows(self):
        with closing(self.store.connect()) as db:
            return {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                for table in ("records", "revisions", "imports", "ledgers")
            }

    def reports(self):
        return [json.loads(path.read_bytes())
                for path in (self.store.root / "processing" / "runs").glob("*.json")]

    def publish(self, rows):
        self.store.import_file(self.source(rows))
        self.store.export()
        return self.store.snapshot()

    def make_legacy_library(self):
        self.store.import_file(self.source([bill()]))
        with closing(self.store.connect()) as db, db:
            db.execute("DELETE FROM metadata WHERE key NOT IN ('library_id', 'revision', 'updated_at')")
            db.execute("PRAGMA user_version=1")
        self.store = Store(self.store.root)

    def serve(self):
        server = Server(self.store, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

        self.addCleanup(cleanup)
        return f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def source_revision(snapshot):
        meta = snapshot["snapshot"]
        return meta.get("processing", {}).get("source_revision", meta["revision"])

    def test_preview_reports_exact_changes_without_changing_business_data(self):
        before = self.publish([bill("a"), bill("b", money=0.01)])
        business_before = self.business_rows()
        files_before = {str(path): path.read_bytes()
                        for folder in ("archive", "exports")
                        for path in (self.store.root / folder).rglob("*") if path.is_file()}

        with normalizer_upgrade() as version:
            preview = self.store.rebuild()
            repeat = self.store.rebuild()

        self.assertEqual(preview["state"], "preview")
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["records"], 2)
        self.assertEqual(preview["generation_before"], before["snapshot"]["generation"])
        self.assertEqual(preview["source_revision"], self.source_revision(before))
        self.assertEqual(preview["target_processing"]["normalizer_version"], version)
        self.assertEqual(preview["changes"]["records_changed"], 2)
        self.assertEqual(preview["changes"]["fields"], {"category": 2})
        self.assertFalse(preview["changes"]["daily_summary_changed"])
        self.assertRegex(preview["plan_ref"], r"^[a-f0-9]{64}$")
        self.assertEqual(preview["plan_ref"], repeat["plan_ref"])
        self.assertEqual(self.business_rows(), business_before)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(files_before, {str(path): path.read_bytes()
                        for folder in ("archive", "exports")
                        for path in (self.store.root / folder).rglob("*") if path.is_file()})
        self.assertTrue(self.reports())
        self.assertEqual({report["state"] for report in self.reports()}, {"preview"})
        self.assertEqual(self.store.status()["rebuild"]["plan_ref"], preview["plan_ref"])

    def test_rebuild_uses_latest_accepted_rows_and_keeps_missing_export_rows(self):
        first = self.source([bill("a", money=1), bill("b", money=2)], "original.json")
        self.store.import_file(first)
        update = self.source([bill("a", money=7)], "update.json")
        self.store.import_file(update, accept_updates=True,
                               expected_generation=self.store.snapshot()["snapshot"]["generation"])
        self.assertFalse(self.store.import_file(first)["applied"])
        # An unreferenced archive may remain after an interrupted import. It must
        # never become accepted data merely because a rebuild scans the archive.
        orphan = self.source([bill("unaccepted", money=999)], "orphan.json").read_bytes()
        (self.store.root / "archive" / (hashlib.sha256(orphan).hexdigest() + ".json")).write_bytes(orphan)
        self.store.export()
        before = self.store.snapshot()
        tables = self.business_rows()
        histories = {row["id"]: self.store.history(row["id"])
                     for row in before["datasets"]["transactions"]}

        with normalizer_upgrade() as version:
            plan = self.store.rebuild()
            result = self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            after = self.store.snapshot()
            self.assertTrue(self.store.verify()["ok"])

        self.assertEqual(result["state"], "completed")
        self.assertNotEqual(result["generation_after"], before["snapshot"]["generation"])
        rows = {row["source_key"]: row for row in after["datasets"]["transactions"]}
        self.assertEqual(set(rows), {"a", "b"})
        self.assertEqual((rows["a"]["amount"], rows["a"]["revision"]), ("7.00", 2))
        self.assertEqual((rows["b"]["amount"], rows["b"]["revision"]), ("2.00", 1))
        self.assertEqual(after["datasets"]["daily_summary"][0]["expense"], "9.00")
        self.assertEqual(after["snapshot"]["contract_version"], "1.1")
        metadata = after["snapshot"]["processing"]
        self.assertEqual(metadata["normalizer_version"], version)
        self.assertEqual(metadata["source_revision"], self.source_revision(before))
        self.assertTrue(metadata["source_updated_at"])
        self.assertTrue(metadata["processed_at"])
        self.assertTrue(metadata["summary_version"])
        for table in ("imports", "revisions", "ledgers"):
            self.assertEqual(self.business_rows()[table], tables[table])
        for row in before["datasets"]["transactions"]:
            rebuilt = rows[row["source_key"]]
            for field in ("id", "source_file_sha256", "source_row", "revision"):
                self.assertEqual(rebuilt[field], row[field])
            self.assertEqual(self.store.history(row["id"]), histories[row["id"]])
        self.assertEqual(load_snapshot(self.store.root / "exports" / "current.json"), after)
        self.assertIn("completed", {report["state"] for report in self.reports()})

    def test_repeated_apply_is_idempotent(self):
        self.publish([bill()])
        with normalizer_upgrade():
            plan = self.store.rebuild()
            self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            before = self.store.snapshot()
            publication = (self.store.root / "exports" / "current.json").read_bytes()
            unchanged = self.store.rebuild()
            self.assertFalse(unchanged["changed"])
            self.assertEqual(unchanged["changes"]["records_changed"], 0)
            result = self.store.rebuild(apply=True, expected_plan=unchanged["plan_ref"])
            self.assertEqual(result["state"], "unchanged")
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual((self.store.root / "exports" / "current.json").read_bytes(), publication)

    def test_missing_or_wrong_plan_cannot_apply(self):
        before = self.publish([bill()])
        with normalizer_upgrade():
            for plan in (None, "0" * 64):
                with self.subTest(plan=plan), self.assertRaises(DataError):
                    self.store.rebuild(apply=True, expected_plan=plan)
                self.assertEqual(self.store.snapshot(), before)

    def test_new_source_data_invalidates_preview(self):
        self.publish([bill()])
        with normalizer_upgrade():
            stale = self.store.rebuild()
        self.store.import_file(self.source([bill("new")]))
        self.store.export()
        before = self.store.snapshot()
        with normalizer_upgrade(), self.assertRaises(DataError):
            self.store.rebuild(apply=True, expected_plan=stale["plan_ref"])
        self.assertEqual(self.store.snapshot(), before)

    def test_target_processing_change_invalidates_preview(self):
        before = self.publish([bill()])
        with normalizer_upgrade("synthetic-normalizer-2"):
            stale = self.store.rebuild()
        with normalizer_upgrade("synthetic-normalizer-3"), self.assertRaises(DataError):
            self.store.rebuild(apply=True, expected_plan=stale["plan_ref"])
        self.assertEqual(self.store.snapshot(), before)

    def test_missing_or_corrupt_archives_fail_without_partial_changes(self):
        before = self.publish([bill("a"), bill("b")])
        row = before["datasets"]["transactions"][0]
        archive = self.store.root / "archive" / (row["source_file_sha256"] + ".json")
        original = archive.read_bytes()
        business = self.business_rows()
        publication = (self.store.root / "exports" / "current.json").read_bytes()
        with normalizer_upgrade():
            for bad_bytes in (None, b"[]"):
                if bad_bytes is None:
                    archive.unlink()
                else:
                    archive.write_bytes(bad_bytes)
                with self.subTest(missing=bad_bytes is None), self.assertRaises(DataError):
                    self.store.rebuild()
                self.assertEqual(self.business_rows(), business)
                self.assertEqual(self.store.snapshot(), before)
                self.assertEqual((self.store.root / "exports" / "current.json").read_bytes(), publication)
                archive.write_bytes(original)
        self.assertIn("failed", {report["state"] for report in self.reports()})

    def test_wrong_archived_row_cannot_reassign_record_identity(self):
        before = self.publish([bill("a"), bill("b")])
        row = before["datasets"]["transactions"][0]
        row["source_row"] = 3 - row["source_row"]
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE records SET normalized_json=? WHERE id=?", (canonical(row), row["id"]))
        current = (self.store.root / "exports" / "current.json").read_bytes()
        with normalizer_upgrade(), self.assertRaises(DataError):
            self.store.rebuild()
        self.assertEqual((self.store.root / "exports" / "current.json").read_bytes(), current)

    def test_invalid_candidate_amount_or_identity_never_publishes(self):
        before = self.publish([bill()])
        business = self.business_rows()
        current = (self.store.root / "exports" / "current.json").read_bytes()
        transforms = (
            lambda row: row.update(amount="0.001"),
            lambda row: row.update(id=record_id("main", "wrong-record")),
        )
        for transform in transforms:
            with self.subTest(transform=transform), normalizer_upgrade(transform=transform):
                with self.assertRaises(DataError):
                    plan = self.store.rebuild()
                    self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            self.assertEqual(self.business_rows(), business)
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual((self.store.root / "exports" / "current.json").read_bytes(), current)

    def test_current_publish_failure_keeps_old_snapshot_and_export_recovers(self):
        before = self.publish([bill()])
        reader = Reader(self.store.root)
        old_ref = reader.snapshot()["snapshot_ref"]
        current = self.store.root / "exports" / "current.json"
        old_bytes = current.read_bytes()

        def fail_current(path, content):
            if Path(path).resolve() == current.resolve():
                raise OSError("synthetic current pointer failure")
            return atomic_write(path, content)

        with normalizer_upgrade():
            plan = self.store.rebuild()
            with patch("qianji.store.atomic_write", side_effect=fail_current):
                with self.assertRaises(DataError):
                    self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            # A restart must preserve both the old public view and the durable
            # candidate; a cached Store/Reader object cannot provide recovery.
            self.store = Store(self.store.root)
            self.assertEqual(current.read_bytes(), old_bytes)
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual(Reader(self.store.root).snapshot()["snapshot_ref"], old_ref)
            self.assertEqual(reader.read(old_ref)["data"], before["datasets"]["transactions"])
            base_url = self.serve()
            http_before = QianjiClient(base_url)
            self.assertEqual(http_before.snapshot, before["snapshot"])
            self.assertEqual(http_before.read_all(), before["datasets"]["transactions"])
            self.assertTrue(self.store.status()["publication_pending"])
            with self.assertRaises(DataError):
                self.store.rebuild()
            with self.assertRaises(DataError):
                self.store.import_file(self.source([bill("new")]))
            recovered = self.store.export()
            after = self.store.snapshot()
            self.assertNotEqual(after["snapshot"]["generation"], before["snapshot"]["generation"])
            self.assertEqual(recovered["generation"], after["snapshot"]["generation"])
            self.assertEqual(load_snapshot(current), after)
            self.assertEqual(QianjiClient(base_url).read_all(), after["datasets"]["transactions"])
            with self.assertRaisesRegex(QianjiAPIError, "409"):
                http_before.read_all()
            self.assertEqual(after["datasets"]["transactions"][0]["category"], "reprocessed/测试分类")
            self.assertEqual(reader.read(old_ref)["data"], before["datasets"]["transactions"])
            self.assertTrue(self.store.verify()["ok"])
            self.assertFalse(self.store.status()["publication_pending"])
            self.assertEqual(self.store.status()["rebuild"]["state"], "completed")

    def test_fixed_snapshot_original_uses_its_processing_version(self):
        before = self.publish([bill()])
        reader = Reader(self.store.root)
        old_ref = reader.snapshot()["snapshot_ref"]
        rid = before["datasets"]["transactions"][0]["id"]
        original = reader.original(old_ref, rid)["json_text"]
        with normalizer_upgrade():
            plan = self.store.rebuild()
            self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            fresh = Reader(self.store.root)
            new_ref = fresh.snapshot()["snapshot_ref"]
            self.assertNotEqual(old_ref, new_ref)
            self.assertEqual(fresh.original(old_ref, rid)["json_text"], original)
            self.assertEqual(fresh.original(new_ref, rid)["json_text"], original)
            self.assertEqual(fresh.read(old_ref)["data"][0]["category"], "测试分类")
            self.assertEqual(fresh.read(new_ref)["data"][0]["category"], "reprocessed/测试分类")

    def test_summary_version_change_has_distinct_publication_even_for_equal_values(self):
        before = self.publish([bill(money=0.1), bill("b", money=0.2)])
        version = "synthetic-summary-2"
        summary = processing.SUMMARIZERS[processing.SUMMARY_VERSION]
        with patch.object(processing, "SUMMARY_VERSION", version), \
                patch.dict(processing.SUMMARIZERS, {version: summary}):
            plan = self.store.rebuild()
            self.assertTrue(plan["changed"])
            self.assertEqual(plan["changes"]["records_changed"], 0)
            self.assertFalse(plan["changes"]["daily_summary_changed"])
            self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            after = self.store.snapshot()
        self.assertEqual(after["datasets"], before["datasets"])
        self.assertEqual(after["snapshot"]["processing"]["summary_version"], version)
        self.assertEqual(self.source_revision(after), self.source_revision(before))
        self.assertNotEqual(after["snapshot"]["generation"], before["snapshot"]["generation"])

    def test_normalization_date_change_rebuilds_and_reports_daily_summary(self):
        before = self.publish([bill("a", money=0.1), bill("b", money=0.2)])

        def corrected_date(row):
            if row["source_key"] == "b":
                row.update(date_local="2026-01-03", occurred_at="2026-01-03T12:30:00+08:00")

        with normalizer_upgrade(transform=corrected_date):
            plan = self.store.rebuild()
            self.assertEqual(plan["changes"]["records_changed"], 1)
            self.assertEqual(plan["changes"]["fields"], {"date_local": 1, "occurred_at": 1})
            self.assertTrue(plan["changes"]["daily_summary_changed"])
            self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            after = self.store.snapshot()
        summaries = after["datasets"]["daily_summary"]
        self.assertEqual([(row["date_local"], row["expense"]) for row in summaries],
                         [("2026-01-02", "0.10"), ("2026-01-03", "0.20")])
        self.assertEqual(self.source_revision(after), self.source_revision(before))

    def test_import_cannot_mix_processing_versions_before_rebuild(self):
        before = self.publish([bill()])
        source = self.source([bill("new")], "new.json")
        with normalizer_upgrade():
            with self.assertRaises(DataError):
                self.store.import_file(source)
            self.assertEqual(self.store.snapshot(), before)
            plan = self.store.rebuild()
            self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
            self.assertTrue(self.store.import_file(source)["applied"])
            after = self.store.snapshot()
            self.assertEqual(len(after["datasets"]["transactions"]), 2)
            self.assertEqual({row["category"] for row in after["datasets"]["transactions"]},
                             {"reprocessed/测试分类"})

    def test_legacy_library_upgrades_without_changing_sources_or_old_publication(self):
        self.make_legacy_library()
        old = self.store.export()
        before = self.store.snapshot()
        self.assertEqual(before["snapshot"]["contract_version"], "1.0")
        self.assertNotIn("processing", before["snapshot"])
        fixed = Path(old["publication"]["path"])
        fixed_bytes = fixed.read_bytes()
        old_reader = Reader(self.store.root)
        old_ref = old_reader.snapshot()["snapshot_ref"]
        rid = before["datasets"]["transactions"][0]["id"]
        history = self.store.history(rid)

        plan = self.store.rebuild()
        self.assertTrue(plan["changed"])
        self.assertEqual(plan["changes"]["records_changed"], 0)
        self.assertFalse(plan["changes"]["daily_summary_changed"])
        self.store.rebuild(apply=True, expected_plan=plan["plan_ref"])
        after = self.store.snapshot()
        self.assertEqual(after["snapshot"]["contract_version"], "1.1")
        self.assertEqual(after["datasets"], before["datasets"])
        self.assertEqual(self.source_revision(after), self.source_revision(before))
        self.assertEqual(after["snapshot"]["processing"]["source_updated_at"],
                         before["snapshot"]["updated_at"])
        self.assertEqual(self.store.history(rid), history)
        self.assertEqual(fixed.read_bytes(), fixed_bytes)
        self.assertEqual(load_snapshot(fixed), before)
        self.assertIn("12.34", Reader(self.store.root).original(old_ref, rid)["json_text"])

    def test_cli_preview_apply_stale_plan_and_no_analysis(self):
        self.make_legacy_library()
        self.store.export()
        current = self.store.root / "exports" / "current.json"
        initial = current.read_bytes()

        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "qianji", "--data-dir", str(self.store.root), *args],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, encoding="utf-8",
                timeout=20,
            )

        preview = run("rebuild")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        stale = json.loads(preview.stdout)
        self.assertEqual(stale["state"], "preview")
        self.assertEqual(current.read_bytes(), initial)
        missing_plan = run("rebuild", "--apply", "--no-analysis")
        self.assertEqual(missing_plan.returncode, 1)
        self.assertIn("rebuild_plan_changed", json.loads(missing_plan.stderr)["error"])

        source = self.source([bill("new")], "cli-new.json")
        imported = run("import", str(source), "--no-analysis")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        source_publication = current.read_bytes()
        stale_apply = run("rebuild", "--apply", "--expected-plan", stale["plan_ref"], "--no-analysis")
        self.assertEqual(stale_apply.returncode, 1)
        self.assertIn("rebuild_plan_changed", json.loads(stale_apply.stderr)["error"])
        self.assertEqual(current.read_bytes(), source_publication)

        refreshed = run("rebuild")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        plan = json.loads(refreshed.stdout)
        applied = run("rebuild", "--apply", "--expected-plan", plan["plan_ref"], "--no-analysis")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["state"], "completed")
        self.assertNotIn("analysis", report)
        self.assertFalse(list(self.store.root.rglob("jobs.sqlite3")))
        self.assertEqual(load_snapshot(current)["snapshot"]["contract_version"], "1.1")
