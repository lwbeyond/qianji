"""Synthetic-only checks for versioned processing and archived source reads."""

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qianji import normalize, processing
from qianji.mcp_reader import Reader, ReadError
from qianji.normalize import DataError, canonical, digest


def bill(key='synthetic-1', **changes):
    return {'key': key, 'type': '支出', 'date': '2026-01-02 12:00:00',
            'money': 12, 'currency': 'CNY', 'category': '合成分类',
            'remark': 'Synthetic only', 'asset': 9007199254740993, **changes}


def normalizer_v2(row, ledger, utc_offset):
    value, raw = processing.normalize_record(row, ledger, utc_offset, 'qianji-normalize/1')
    value['category'] = 'v2:' + value['category']
    return value, raw


class ProcessingVersionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qianji-processing-synthetic-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def publication(self, source_rows, normalizer_version=None, summary_version=None):
        """Write an independent fixture, including a genuine legacy contract 1.0."""
        content = json.dumps(source_rows, ensure_ascii=False).encode('utf-8')
        sha = hashlib.sha256(content).hexdigest()
        archive = self.root / 'archive'
        archive.mkdir(exist_ok=True)
        (archive / (sha + '.json')).write_bytes(content)
        values = json.loads(content, parse_float=Decimal)
        rows = []
        for index, value in enumerate(values, 1):
            row, _ = processing.normalize_record(value, 'main', '+08:00',
                                                  normalizer_version or 'qianji-normalize/1')
            rows.append({**row, 'revision': 1, 'source_file_sha256': sha, 'source_row': index})
        datasets = {'transactions': rows,
                    'daily_summary': processing.summarize(rows, summary_version or 'qianji-summary/1')}
        library = '00000000-0000-4000-8000-000000000001'
        stamp = '2026-01-03T00:00:00+00:00'
        snapshot = {'generation': library + ':1', 'library_id': library,
                    'revision': 1, 'updated_at': stamp, 'contract_version': '1.0',
                    'ledgers': [{'id': 'main', 'utc_offset': '+08:00'}],
                    'available_datasets': list(datasets), 'data_sha256': digest(canonical(datasets)),
                    'counts': {key: len(value) for key, value in datasets.items()}}
        if normalizer_version is not None or summary_version is not None:
            snapshot['contract_version'] = '1.1'
            snapshot['processing'] = {
                'normalizer_version': normalizer_version or 'qianji-normalize/1',
                'summary_version': summary_version or 'qianji-summary/1',
                'source_revision': 1, 'source_updated_at': stamp, 'processed_at': stamp}
        document = {'format': 'qianji-normalized', 'format_version': 1,
                    'snapshot': snapshot, 'datasets': datasets}
        exports = self.root / 'exports'
        exports.mkdir(exist_ok=True)
        (exports / 'current.json').write_text(canonical(document), encoding='utf-8')
        return document

    def test_v1_preserves_exact_helpers_and_original_comparison_document(self):
        source = bill(money=Decimal('0.10'), extension=Decimal('0.1234567890123456789'))
        with patch('qianji.normalize.money', side_effect=AssertionError('current helper changed')):
            value, raw = processing.normalize_record(source, 'main', '+08:00', 'qianji-normalize/1')
        self.assertEqual(value['amount'], '0.10')
        self.assertEqual(value['asset_id'], '9007199254740993')
        self.assertEqual(value['occurred_at'], '2026-01-02T12:00:00+08:00')
        self.assertEqual(json.loads(raw)['extension'], '0.1234567890123456789')
        with self.assertRaises(DataError):
            processing.normalize_record(bill(money=Decimal('0.001')), 'main', '+08:00')

    def test_current_normalize_and_import_route_new_version_without_changing_v1(self):
        source = self.root / 'source.json'
        source.write_text(json.dumps([bill(), bill()], ensure_ascii=False), encoding='utf-8')
        with patch.dict(processing.NORMALIZERS, {'qianji-normalize/2': normalizer_v2}), \
             patch.object(processing, 'NORMALIZER_VERSION', 'qianji-normalize/2'):
            self.assertEqual(normalize.normalize(bill(), 'main', '+08:00')[0]['category'], 'v2:合成分类')
            _, incoming, duplicates = normalize.load_export(source)
            self.assertEqual((incoming[0][0]['category'], duplicates), ('v2:合成分类', 1))
            old, _ = processing.normalize_record(bill(), 'main', '+08:00', 'qianji-normalize/1')
            self.assertEqual(old['category'], '合成分类')

    def test_v1_summary_keeps_precision_and_transfer_separation(self):
        rows = []
        for item in (bill(money=Decimal('0.10')), bill('income', type='收入', money=Decimal('0.20')),
                     bill('refund', type='退款', money=Decimal('0.03')),
                     bill('transfer', type='转账', money=200, **{'from': 1, 'target': 2}),
                     bill('usd', currency='USD', money=3)):
            rows.append(processing.normalize_record(item, 'main', '+08:00')[0])
        rows.append(processing.normalize_record(bill('other', money=1), 'other', '+08:00')[0])
        result = processing.summarize(rows, 'qianji-summary/1')
        self.assertEqual(len(result), 3)
        cny = next(row for row in result if row['ledger_id'] == 'main' and row['currency'] == 'CNY')
        self.assertEqual((cny['count'], cny['expense'], cny['refund'], cny['transfer'],
                          cny['net_expense'], cny['net_cashflow']),
                         (4, '0.10', '0.03', '200.00', '0.07', '0.13'))

    def test_legacy_original_and_totals_remain_v1_after_current_version_upgrade(self):
        self.publication([bill(money=0.1)])
        unused_summary = Mock(side_effect=AssertionError('legacy read used current summary'))
        with patch.dict(processing.NORMALIZERS, {'qianji-normalize/2': normalizer_v2}), \
             patch.dict(processing.SUMMARIZERS, {'qianji-summary/2': unused_summary}), \
             patch.object(processing, 'NORMALIZER_VERSION', 'qianji-normalize/2'), \
             patch.object(processing, 'SUMMARY_VERSION', 'qianji-summary/2'):
            reader = Reader(self.root)
            ref = reader.snapshot()['snapshot_ref']
            row = reader.read(ref)['data'][0]
            self.assertEqual(json.loads(reader.original(ref, row['id'])['json_text'])['category'], '合成分类')
            self.assertEqual(reader.totals(ref)['data'][0]['expense'], '0.10')
            unused_summary.assert_not_called()

    def test_explicit_versions_route_original_and_filtered_totals(self):
        summary_v2 = Mock(wraps=processing.summarize_v1)
        with patch.dict(processing.NORMALIZERS, {'qianji-normalize/2': normalizer_v2}), \
             patch.dict(processing.SUMMARIZERS, {'qianji-summary/2': summary_v2}):
            self.publication([bill(), bill('second', money=5)], 'qianji-normalize/2', 'qianji-summary/2')
            reader = Reader(self.root)
            ref = reader.snapshot()['snapshot_ref']
            row = reader.read(ref)['data'][0]
            self.assertEqual(row['category'], 'v2:合成分类')
            self.assertEqual(json.loads(reader.original(ref, row['id'])['json_text'])['category'], '合成分类')
            summary_v2.reset_mock()
            total = reader.totals(ref, {'source_key': 'second'})['data'][0]
            self.assertEqual((total['count'], total['expense']), (1, '5.00'))
            self.assertEqual(summary_v2.call_args.args[0][0]['source_key'], 'second')
            self.assertEqual(len(summary_v2.call_args.args[0]), 1)

    def test_unknown_versions_fail_without_falling_back(self):
        for unknown in ('qianji-normalize/999', '', [], None):
            metadata = {'processing': {'normalizer_version': unknown, 'summary_version': 'qianji-summary/1'}}
            with self.subTest(version=unknown), self.assertRaisesRegex(DataError, 'unsupported_normalizer_version'):
                processing.snapshot_versions(metadata)
        with self.assertRaisesRegex(DataError, 'unsupported_summary_version'):
            processing.summarize([], 'qianji-summary/999')
        document = self.publication([bill()], 'qianji-normalize/1', 'qianji-summary/1')
        document['snapshot']['processing']['normalizer_version'] = 'qianji-normalize/999'
        content = canonical(document).encode('utf-8')
        (self.root / 'exports' / 'current.json').write_bytes(content)
        reader = Reader(self.root)
        with self.assertRaisesRegex(ReadError, 'unsupported_normalizer_version'):
            reader.snapshot()
        reference = document['snapshot']['generation'] + '@' + hashlib.sha256(content).hexdigest()
        with self.assertRaisesRegex(ReadError, 'unsupported_normalizer_version'):
            reader._open(reference)


if __name__ == '__main__':
    unittest.main()
