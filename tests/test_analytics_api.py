"""Synthetic integration checks for fixed analytical HTTP/MCP adapter reads."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from integrations.analytics_client import AnalyticsAPIError, AnalyticsClient, analytics_schema
from integrations.qianji_client import snapshot_schema
from qianji.api import Server
from qianji.mcp_reader import ReadError
from qianji.mcp_server import bounded_analytics_page
from qianji.store import Store


def bill(key, **updates):
    return {'key': key, 'type': '支出', 'date': '2026-01-02 12:00:00',
            'money': 0.10, 'currency': 'CNY', 'category': '餐饮',
            'remark': 'SYNTHETIC', **updates}


class AnalyticsAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qianji-analytics-api-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.publish([bill('one'), bill('two', money=0.20, date='2026-02-02 12:00:00'),
                      bill('three', money=0.30, currency='USD')])
        self.server = Server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = AnalyticsClient(self.base)
        self.ref = self.client.snapshot()['analytics_ref']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def publish(self, rows):
        from qianji.analytics import reprocess
        source = self.root / 'synthetic.json'
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        self.store.import_file(source)
        self.store.export()
        preview = reprocess(self.store.root)
        reprocess(self.store.root, apply=True, expected_plan=preview['plan_ref'])

    def get(self, endpoint, params=None, headers=None, method='GET'):
        path = '/api/v1/analytics/' + endpoint
        if params:
            path += '?' + urlencode(params)
        with urlopen(Request(self.base + path, headers=headers or {}, method=method), timeout=5) as response:
            return json.load(response)

    def test_complete_facts_cube_catalog_and_quality(self):
        facts = self.client.read_all(self.ref, page_size=1)
        self.assertEqual(facts['pagination']['total'], 3)
        self.assertEqual(len({row['id'] for row in facts['data']}), 3)
        self.assertTrue(all('classification' in row and 'source_fingerprint' in row for row in facts['data']))
        cube = self.client.cube_all(self.ref, group_by=['month', 'category_l1'], page_size=1)
        self.assertEqual(sum(row['count'] for row in cube['data']), 3)
        self.assertEqual([(row['currency'], row['expense']) for row in cube['totals']],
                         [('CNY', '0.30'), ('USD', '0.30')])
        self.assertEqual(self.client.quality(self.ref)['analytics_ref'], self.ref)
        self.assertEqual(self.client.catalog()['dataset'], 'catalog')
        for row in cube['data']:
            detail = self.client.read_all(self.ref, filters=row['trace']['filters'])
            self.assertEqual(len(detail['data']), row['count'])

    def test_old_reference_survives_new_publication(self):
        old = self.client.read_all(self.ref)
        self.publish([bill('new', money=9.99)])
        new_ref = self.client.snapshot()['analytics_ref']
        self.assertNotEqual(new_ref, self.ref)
        self.assertEqual(self.client.read_all(self.ref), old)
        self.assertEqual(self.client.read_all(new_ref)['pagination']['total'], 4)

    def test_client_keeps_partial_label_basis_separate_from_current_source(self):
        from qianji.analysis import Analysis
        Analysis(self.store.root).process()
        matched = self.client.snapshot()
        self.assertEqual(matched['label_source_state'], 'matched')
        self.publish([bill('new-with-old-labels')])
        partial = self.client.snapshot()
        self.assertEqual(partial['label_source_state'], 'partial')
        self.assertEqual(partial['analysis_source_snapshot_ref'], matched['source_snapshot_ref'])
        self.assertNotEqual(partial['source_snapshot_ref'], partial['analysis_source_snapshot_ref'])
        facts = self.client.read_all(partial['analytics_ref'], page_size=1)
        self.assertEqual(sum(row['label_basis'] is not None for row in facts['data']), 3)
        bad = {**partial, 'label_source_state': 'matched'}
        with patch.object(self.client, '_get', return_value=bad), self.assertRaises(AnalyticsAPIError):
            self.client.snapshot()

    def test_current_change_between_pages_keeps_fixed_publication(self):
        original = self.client._get
        def get(endpoint, params=None):
            result = original(endpoint, params)
            if endpoint == 'facts' and params['offset'] == 0:
                self.publish([bill('concurrent', money=1.00)])
            return result
        with patch.object(self.client, '_get', side_effect=get):
            result = self.client.read_all(self.ref, page_size=1)
        self.assertEqual(len(result['data']), 3)

    def test_invalid_queries_rejected(self):
        cases = [('facts', {}), ('quality', {'analytics_ref': self.ref, 'limit': 1}),
                 ('snapshot', {'analytics_ref': self.ref}), ('catalog', {'unknown': 'x'}),
                 ('facts', {'analytics_ref': self.ref, 'filters': '[]'}),
                 ('facts', {'analytics_ref': self.ref, 'filters': '{"kind":"income","kind":"expense"}'}),
                 ('facts', {'analytics_ref': self.ref, 'filters': '{"amount_min":NaN}'}),
                 ('facts', {'analytics_ref': self.ref, 'filters': '{"typo":"x"}'}),
                 ('facts', {'analytics_ref': self.ref, 'offset': '-1'}),
                 ('facts', {'analytics_ref': self.ref, 'limit': '51'}),
                 ('cube', {'analytics_ref': self.ref, 'group_by': 'month,month'}),
                 ('cube', {'analytics_ref': self.ref, 'group_by': 'month,'}),
                 ('facts', [('analytics_ref', self.ref), ('analytics_ref', self.ref)])]
        for endpoint, params in cases:
            with self.subTest(endpoint=endpoint, params=params), self.assertRaises(HTTPError) as caught:
                self.get(endpoint, params)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

    def test_read_is_file_only_and_does_not_mutate_data(self):
        def manifest():
            return {str(path.relative_to(self.store.root)): (path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in self.store.root.rglob('*') if path.is_file()}
        before = manifest()
        with patch.object(self.store, 'snapshot', side_effect=AssertionError('must not read live database')):
            self.client.snapshot()
            self.client.catalog()
            self.client.read_all(self.ref)
            self.client.cube_all(self.ref)
            self.client.quality(self.ref)
        self.assertEqual(before, manifest())

    def test_security_and_missing_are_explicit(self):
        for headers, method in (({'Host': 'invalid.example'}, 'GET'),
                                ({'Origin': 'https://example.com'}, 'GET'), ({}, 'POST')):
            with self.assertRaises(HTTPError) as caught:
                self.get('snapshot', headers=headers, method=method)
            self.assertEqual(caught.exception.code, 405 if method == 'POST' else 403)
            caught.exception.close()
        with patch.object(self.server, 'store', SimpleNamespace(root=self.root / 'absent')):
            self.assertFalse(self.get('snapshot')['available'])
            with self.assertRaises(HTTPError) as caught:
                self.get('facts', {'analytics_ref': self.ref})
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        self.assertFalse((self.root / 'absent').exists())

    def test_corrupt_publication_pointer_is_a_source_failure(self):
        pointer = self.store.root / 'analytics' / 'exports' / 'current.json'
        pointer.write_bytes(b'{')
        with self.assertRaises(HTTPError) as caught:
            self.get('snapshot')
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(json.loads(caught.exception.read())['error']['code'], 'source_unavailable')
        caught.exception.close()

    def test_client_rejects_pin_or_pagination_corruption(self):
        first = self.get('facts', {'analytics_ref': self.ref, 'limit': 1})
        changes = ({'analytics_ref': '0' * 64}, {'source_snapshot_ref': 'wrong'},
                   {'pagination': {**first['pagination'], 'next_offset': 0}},
                   {'pagination': {**first['pagination'], 'returned': 2}},
                   {'pagination': {**first['pagination'], 'next_offset': None}},
                   {'pagination': {**first['pagination'], 'total': True}})
        for changeset in changes:
            with self.subTest(changeset=changeset), patch.object(self.client, '_get', return_value={**first, **changeset}):
                with self.assertRaises(AnalyticsAPIError):
                    self.client.read_all(self.ref, page_size=1)
        next_page = self.get('facts', {'analytics_ref': self.ref, 'limit': 1, 'offset': 1})
        next_page = copy.deepcopy(next_page)
        next_page['recipe'] = '0' * 64
        with patch.object(self.client, '_get', side_effect=[first, next_page]):
            with self.assertRaises(AnalyticsAPIError):
                self.client.read_all(self.ref, page_size=1)


class AnalyticsMCPBoundTests(unittest.TestCase):
    def test_reduce_whole_rows_and_keep_all_fields(self):
        def query(reader, ref, *, limit, offset=0):
            return {'data': [{'id': str(i), 'remark': 'x' * 6000} for i in range(limit)],
                    'pagination': {'next_offset': offset + limit}}
        result = bounded_analytics_page(query, None, 'fixed', limit=5)
        self.assertEqual(len(result['data']), 2)
        self.assertEqual(result['pagination']['next_offset'], 2)
        self.assertEqual(result['data'][0]['remark'], 'x' * 6000)
        with self.assertRaises(ReadError):
            bounded_analytics_page(lambda *args, **kwargs: {'data': ['x' * 17000]}, None, 'fixed', limit=1)

    def test_machine_schema_matches_authority_and_source_fields(self):
        schema = analytics_schema()
        saved = json.loads((Path(__file__).resolve().parents[1] / 'integrations' / 'analytics.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, schema)
        source = snapshot_schema()['properties']['datasets']['properties']['transactions']['items']['properties']
        fact = schema['$defs']['fact']['properties']
        self.assertEqual({key: fact[key] for key in source}, source)
        self.assertIn('analysis_source_snapshot_ref', schema['required'])
        self.assertIn('label_basis', schema['$defs']['fact']['required'])


if __name__ == '__main__':
    unittest.main()
