"""Standalone consumer and pre-publication contract checks; synthetic data only."""

import copy
from decimal import localcontext
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.qianji_client import QianjiAPIError, QianjiClient, load_snapshot, snapshot_schema, validate_snapshot


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(ledger, key):
    return "qj_" + hashlib.sha256(canonical([ledger, key]).encode("utf8")).hexdigest()


def rehash(doc):
    doc["snapshot"]["data_sha256"] = hashlib.sha256(canonical(doc["datasets"]).encode("utf8")).hexdigest()
    doc["snapshot"]["counts"] = {key: len(rows) for key, rows in doc["datasets"].items()}
    return doc


def document(version="1.1"):
    rows = []
    for key, kind, source_type, amount in [
        ("a", "expense", "支出", "0.10"), ("b", "expense", "支出", "0.20"),
        ("c", "refund", "退款", "0.10"), ("d", "income", "收入", "10.00"),
        ("e", "transfer", "转账", "1000.00"),
    ]:
        rows.append({"id": identity("main", key), "ledger_id": "main", "source_key": key,
                     "revision": 1, "occurred_at": "2026-01-02T12:30:00+08:00",
                     "date_local": "2026-01-02", "source_date": "2026-01-02 12:30:00",
                     "utc_offset": "+08:00", "timezone_basis": "configured_fixed_offset",
                     "kind": kind, "source_type": source_type, "amount": amount, "currency": "CNY",
                     "category": "synthetic", "remark": None, "asset_id": "1",
                     "from_asset_id": "1" if kind == "transfer" else None,
                     "to_asset_id": "2" if kind == "transfer" else None,
                     "related_source_key": "a" if kind == "refund" else None,
                     "related_record_id": identity("main", "a") if kind == "refund" else None,
                     "reimbursement_code": None, "source_file_sha256": "a"*64, "source_row": len(rows)+1})
    metadata = {"library_id": "11111111-1111-4111-8111-111111111111", "revision": 3,
                "generation": "11111111-1111-4111-8111-111111111111:3",
                "updated_at": "2026-01-04T00:00:00Z", "contract_version": version,
                "ledgers": [{"id": "main", "utc_offset": "+08:00"}],
                "available_datasets": ["transactions", "daily_summary"]}
    if version == "1.1":
        metadata["processing"] = {"normalizer_version": "future-normalizer/42", "summary_version": "summary/3",
                                  "source_revision": 2, "source_updated_at": "2026-01-03T00:00:00Z",
                                  "processed_at": "2026-01-04T00:00:00Z"}
    return rehash({"format": "qianji-normalized", "format_version": 1, "snapshot": metadata,
                   "datasets": {"transactions": rows, "daily_summary": [
                       {"ledger_id": "main", "date_local": "2026-01-02", "currency": "CNY", "count": 5,
                        "expense": "0.30", "income": "10.00", "refund": "0.10", "transfer": "1000.00",
                        "net_expense": "0.20", "net_cashflow": "9.80"}]}})


class ProcessingContractTests(unittest.TestCase):
    def test_old_and_processing_contracts_load_from_memory_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for version in ("1.0", "1.1"):
                doc = document(version)
                self.assertIs(validate_snapshot(doc), doc)
                payload = canonical(doc).encode("utf8")
                path.write_bytes(payload)
                self.assertEqual(load_snapshot(path, expected_sha256=hashlib.sha256(payload).hexdigest(),
                                               expected_library_id=doc["snapshot"]["library_id"]), doc)
                with self.assertRaisesRegex(QianjiAPIError, "Publication checksum"):
                    load_snapshot(path, expected_sha256="0"*64)
                with self.assertRaisesRegex(QianjiAPIError, "Library identity"):
                    validate_snapshot(doc, expected_library_id="other")

    def test_machine_schema_matches_generated_contract(self):
        path = Path(__file__).resolve().parents[1] / "integrations" / "snapshot.schema.json"
        self.assertEqual(json.loads(path.read_bytes()), snapshot_schema())

    def test_version_and_processing_presence_must_agree(self):
        cases = []
        doc = document(); doc["snapshot"]["contract_version"] = "1.0"; cases.append(doc)
        doc = document(); del doc["snapshot"]["processing"]; cases.append(doc)
        doc = document(); doc["snapshot"]["contract_version"] = "1.2"; cases.append(doc)
        for doc in cases:
            with self.subTest(snapshot=doc["snapshot"]), self.assertRaises(QianjiAPIError):
                validate_snapshot(doc)

    def test_processing_fields_are_strict_and_version_names_are_opaque(self):
        baseline = document()
        for field in baseline["snapshot"]["processing"]:
            doc = copy.deepcopy(baseline)
            del doc["snapshot"]["processing"][field]
            with self.subTest(missing=field), self.assertRaises(QianjiAPIError):
                validate_snapshot(doc)
        invalid = [("normalizer_version", ""), ("summary_version", "   "),
                   ("normalizer_version", 1), ("source_revision", -1), ("source_revision", True),
                   ("source_revision", 4), ("source_updated_at", "2026-01-03T00:00:00"),
                   ("processed_at", "2026-01-03"), ("processed_at", "2026-01-04T00:00:00+00:60"),
                   ("source_updated_at", None), ("processed_at", 5), ("extra", "unsupported")]
        for field, value in invalid:
            doc = copy.deepcopy(baseline)
            doc["snapshot"]["processing"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(QianjiAPIError):
                validate_snapshot(doc)
        self.assertEqual(validate_snapshot(baseline)["snapshot"]["processing"]["normalizer_version"],
                         "future-normalizer/42")

    def test_processing_chronology_compares_instants_with_timezone(self):
        doc = document()
        doc["snapshot"]["processing"].update(source_updated_at="2026-01-04T01:00:00+08:00",
                                             processed_at="2026-01-03T18:00:00Z")
        validate_snapshot(doc)
        doc["snapshot"]["processing"]["processed_at"] = "2026-01-03T16:59:59Z"
        with self.assertRaisesRegex(QianjiAPIError, "predates"):
            validate_snapshot(doc)

    def test_http_constructor_accepts_both_contracts_and_checks_processing(self):
        for version in ("1.0", "1.1"):
            doc = document(version)
            with patch.object(QianjiClient, "_get", return_value={"api_version": "1.0", "snapshot": doc["snapshot"]}):
                self.assertEqual(QianjiClient().snapshot, doc["snapshot"])
        doc = document(); doc["snapshot"]["processing"]["source_revision"] = 4
        with patch.object(QianjiClient, "_get", return_value={"api_version": "1.0", "snapshot": doc["snapshot"]}):
            with self.assertRaises(QianjiAPIError):
                QianjiClient()

    def test_http_client_requests_full_contract_and_can_receive_legacy_service(self):
        for version in ("1.0", "1.1"):
            doc = document(version)
            response = io.BytesIO(canonical({"api_version": "1.0", "snapshot": doc["snapshot"]}).encode("utf8"))
            with patch("integrations.qianji_client.urlopen", return_value=response) as opener:
                client = QianjiClient()
            self.assertEqual(client.snapshot, doc["snapshot"])
            request = opener.call_args.args[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["x-qianji-contract-version"], "1.1")

    def test_rehashed_transaction_identity_ledger_dates_and_types_are_checked(self):
        invalid = [("id", "qj_"+"0"*64), ("source_key", "different"), ("ledger_id", "missing"),
                   ("utc_offset", "+09:00"), ("date_local", "2026-01-03"),
                   ("occurred_at", "2026-01-02T12:30:00+09:00"), ("source_type", "收入"),
                   ("related_record_id", identity("main", "missing"))]
        for field, value in invalid:
            doc = document(); doc["datasets"]["transactions"][0][field] = value
            with self.subTest(field=field), self.assertRaises(QianjiAPIError):
                validate_snapshot(rehash(doc))
        doc = document(); doc["datasets"]["transactions"].append(copy.deepcopy(doc["datasets"]["transactions"][0]))
        with self.assertRaisesRegex(QianjiAPIError, "duplicate transaction"):
            validate_snapshot(rehash(doc))
        doc = document(); doc["snapshot"]["ledgers"].append(copy.deepcopy(doc["snapshot"]["ledgers"][0]))
        with self.assertRaisesRegex(QianjiAPIError, "duplicate ledger"):
            validate_snapshot(doc)

    def test_record_revision_cannot_exceed_accepted_source_revision(self):
        for version, revision in (("1.0", 4), ("1.1", 3)):
            doc = document(version)
            doc["datasets"]["transactions"][0]["revision"] = revision
            with self.subTest(version=version), self.assertRaisesRegex(QianjiAPIError, "exceeds source revision"):
                validate_snapshot(rehash(doc))

    def test_rehashed_summaries_require_exact_amounts_counts_keys_and_coverage(self):
        for field, value in [("expense", "0.31"), ("income", "0.00"), ("refund", "0.00"),
                             ("transfer", "0.00"), ("net_expense", "0.30"), ("net_cashflow", "9.70"),
                             ("count", 4), ("ledger_id", "other"), ("currency", "USD"),
                             ("date_local", "2026-01-03")]:
            doc = document(); doc["datasets"]["daily_summary"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(QianjiAPIError, "Daily summary"):
                validate_snapshot(rehash(doc))
        doc = document(); doc["datasets"]["daily_summary"] = []
        with self.assertRaisesRegex(QianjiAPIError, "Daily summary"):
            validate_snapshot(rehash(doc))
        doc = document(); doc["datasets"]["daily_summary"] *= 2
        with self.assertRaisesRegex(QianjiAPIError, "Duplicate daily summary"):
            validate_snapshot(rehash(doc))

    def test_decimal_totals_do_not_depend_on_process_decimal_context(self):
        doc = document()
        doc["datasets"]["transactions"][0]["amount"] = "999999999999999999999999999999.10"
        doc["datasets"]["daily_summary"][0].update(expense="999999999999999999999999999999.30",
                                                  net_expense="999999999999999999999999999999.20",
                                                  net_cashflow="-999999999999999999999999999989.20")
        with localcontext() as context:
            context.prec = 6
            validate_snapshot(rehash(doc))

    def test_empty_library_is_valid_and_orphan_summary_is_not(self):
        doc = document()
        doc["datasets"] = {"transactions": [], "daily_summary": []}
        validate_snapshot(rehash(doc))
        doc["datasets"]["daily_summary"] = document()["datasets"]["daily_summary"]
        with self.assertRaisesRegex(QianjiAPIError, "Daily summary"):
            validate_snapshot(rehash(doc))


if __name__ == "__main__":
    unittest.main()
