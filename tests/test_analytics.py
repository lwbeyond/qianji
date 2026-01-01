"""Synthetic lifecycle, provenance and failure recovery for analytical facts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qianji import analytics
from qianji.analysis import Analysis
from qianji.mcp_reader import Reader, ReadError
from qianji.normalize import canonical
from qianji.store import Store
from test_mcp import bill
from test_analysis import FakeModel


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qianji-analytics-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.reader = Reader(self.store.root)
        self.input = self.root / 'synthetic.json'

    def publish(self, rows, **kwargs):
        self.input.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        self.store.import_file(self.input, **kwargs)
        result = self.store.export()
        self.assertEqual(result['analytics']['state'], 'ready', result.get('analytics'))
        ref = result['analytics']['analytics_ref']
        return ref, analytics.load_publication(self.reader, ref)

    def test_export_builds_facts_without_initializing_optional_label_store(self):
        ref, doc = self.publish([bill('a', category='三餐', remark='lunch'),
                                 bill('b', category='零食', remark='milk tea')])
        self.assertFalse((self.store.root / 'analysis').exists())
        self.assertEqual(doc['facts'][0]['classification']['category_l3']['state'], 'known')
        self.assertEqual(doc['quality']['records'], 2)
        self.assertEqual({r['id'] for r in doc['facts']},
                         {r['id'] for r in self.reader._open(doc['source_snapshot_ref'])['datasets']['transactions']})
        self.assertEqual(self.store.export()['analytics']['analytics_ref'], ref)

    def test_preview_and_idempotent_apply_never_change_source(self):
        ref, _ = self.publish([bill(category='咖啡', remark='coffee')])
        paths = [self.store.database, self.store.root / 'exports' / 'current.json']
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        preview = analytics.reprocess(self.store.root)
        self.assertFalse(preview['applied'])
        self.assertEqual(preview['changes']['changed'], 0)
        self.assertEqual(analytics.snapshot(self.reader)['analytics_ref'], ref)
        applied = analytics.reprocess(self.store.root, apply=True, expected_plan=preview['plan_ref'])
        self.assertEqual(applied['state'], 'unchanged')
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_plan_rejects_changed_source_and_keeps_historical_facts(self):
        old_ref, old = self.publish([bill('a', category='三餐', remark='lunch')])
        preview = analytics.reprocess(self.store.root)
        new_ref, _ = self.publish([bill('b', category='三餐', remark='dinner')])
        with self.assertRaisesRegex(ReadError, 'analytics_plan_changed'):
            analytics.reprocess(self.store.root, apply=True, expected_plan=preview['plan_ref'])
        self.assertEqual(analytics.load_publication(self.reader, old_ref), old)
        self.assertEqual(analytics.snapshot(self.reader)['analytics_ref'], new_ref)

    def test_plan_required_and_cannot_be_fabricated_without_preview(self):
        self.publish([bill()])
        with self.assertRaisesRegex(ReadError, 'expected_plan_required'):
            analytics.reprocess(self.store.root, apply=True)
        with self.assertRaisesRegex(ReadError, 'analytics_plan_changed'):
            analytics.reprocess(self.store.root, apply=True, expected_plan='0' * 64)

    def test_model_proposals_visible_but_do_not_become_group_values(self):
        self.publish([bill(category='三餐', remark='外卖')])
        result = Analysis(self.store.root).process(model=FakeModel())
        self.assertEqual(result['analytics']['state'], 'ready', result['analytics'])
        doc = analytics.load_publication(self.reader, result['analytics']['analytics_ref'])
        fact = doc['facts'][0]
        self.assertIsNone(fact['dimensions']['channel'])
        self.assertEqual(fact['classification']['channel']['state'], 'unknown')
        self.assertEqual(fact['proposed_labels'][0]['label'], '外卖')
        self.assertEqual(doc['quality']['proposed_records_by_dimension']['channel'], 1)

    def test_confirmed_review_refreshes_analytics_without_changing_source(self):
        _, before = self.publish([bill(category='三餐', remark='外卖')])
        analysis = Analysis(self.store.root)
        result = analysis.process(model=FakeModel())
        row = before['facts'][0]
        analysis.review(row['id'], row['revision'], [{'dimension': 'channel', 'label': '外卖',
            'action': 'accept', 'field': 'remark', 'evidence': '外卖'}], 'synthetic', result['store_revision'], 'review-1')
        result = analysis.publish()
        doc = analytics.load_publication(self.reader, result['analytics']['analytics_ref'])
        self.assertEqual(doc['facts'][0]['dimensions']['channel'], '外卖')
        self.assertEqual(doc['source_snapshot_ref'], before['source_snapshot_ref'])
        self.assertEqual(doc['facts'][0]['amount'], row['amount'])

    def test_prior_labels_are_explicitly_reused_only_for_matching_inputs(self):
        self.publish([bill('a', category='三餐', remark='外卖')])
        Analysis(self.store.root).process(model=FakeModel())
        _, doc = self.publish([bill('b', category='咖啡', remark='coffee')])
        self.assertEqual(doc['label_source_state'], 'partial')
        self.assertIsNotNone(doc['analysis_ref'])
        self.assertTrue(doc['observed_analysis_ref'])
        self.assertNotEqual(doc['analysis_source_snapshot_ref'], doc['source_snapshot_ref'])
        rows = {row['source_key']: row for row in doc['facts']}
        self.assertTrue(rows['a']['proposed_labels'])
        self.assertIsNotNone(rows['a']['label_basis'])
        self.assertFalse(rows['b']['proposed_labels'])
        self.assertIsNone(rows['b']['label_basis'])
        self.assertEqual(doc['quality']['label_coverage'], {'reused': 1, 'fallback': 1})

    def test_incremental_import_preserves_rejections_and_confirmations(self):
        _, original = self.publish([bill('a', category='咖啡', remark='coffee'),
                                    bill('b', category='三餐', remark='外卖')])
        rows = {row['source_key']: row for row in original['facts']}
        analysis = Analysis(self.store.root)
        result = analysis.process(model=FakeModel())
        review = analysis.review(rows['a']['id'], 1, [{'dimension': 'item', 'label': '咖啡',
            'action': 'reject'}], 'synthetic rejection', result['store_revision'], 'reject-1')
        analysis.review(rows['b']['id'], 1, [{'dimension': 'channel', 'label': '外卖',
            'action': 'accept', 'field': 'remark', 'evidence': '外卖'}], 'synthetic confirmation',
            review['store_revision'], 'accept-1')
        analysis.publish()
        _, added = self.publish([bill('c', category='三餐', remark='lunch')])
        rows = {row['source_key']: row for row in added['facts']}
        self.assertIsNone(rows['a']['dimensions']['category_l1'])
        self.assertEqual(rows['b']['dimensions']['channel'], '外卖')
        self.assertEqual(added['quality']['label_coverage'], {'reused': 2, 'fallback': 1})
        _, changed = self.publish([bill('b', category='三餐', remark='新备注')], accept_updates=True,
                                  expected_generation=self.store.snapshot()['snapshot']['generation'])
        row = next(row for row in changed['facts'] if row['source_key'] == 'b')
        self.assertIsNone(row['dimensions']['channel'])
        self.assertIsNone(row['label_basis'])

    def test_quality_amount_coverage_does_not_depend_on_decimal_context(self):
        from decimal import localcontext
        _, doc = self.publish([bill('a', money=1234567.89), bill('b', money=0.01)])
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(analytics.quality_report(doc['facts']), doc['quality'])

    def test_corruption_and_rehashed_semantic_tampering_are_rejected(self):
        ref, doc = self.publish([bill(category='三餐', remark='lunch')])
        for mutate in (lambda d: d['facts'][0].update(amount='999.00'),
                       lambda d: d['facts'][0].update(revision=True),
                       lambda d: d['facts'][0].update(source_row=True),
                       lambda d: d['facts'][0]['dimensions'].update(month='2020-01'),
                       lambda d: d['quality'].update(records=True),
                       lambda d: d['quality'].update(records=5),
                       lambda d: d['facts'][0]['classification']['category_l1']['evidence'][0].update(evidence='invented'),
                       lambda d: d['facts'].append(deepcopy(d['facts'][0]))):
            changed = deepcopy(doc)
            mutate(changed)
            payload = canonical(changed).encode()
            sha = hashlib.sha256(payload).hexdigest()
            path = self.store.root / 'analytics' / 'exports' / 'versions' / (sha + '.json')
            path.write_bytes(payload)
            with self.assertRaises(ReadError):
                analytics.load_publication(self.reader, sha)
        path = self.store.root / 'analytics' / 'exports' / 'versions' / (ref + '.json')
        path.write_bytes(b'{}')
        with self.assertRaisesRegex(ReadError, 'analytics_checksum'):
            analytics.load_publication(self.reader, ref)

    def test_fabricated_confirmations_and_rules_cannot_claim_literal_quote_as_proof(self):
        _, doc = self.publish([bill(category='三餐', remark='lunch')])
        for state, method in [('confirmed', 'manual'), ('automatic', 'rule')]:
            changed = deepcopy(doc)
            fact = changed['facts'][0]
            fact['dimensions']['channel'] = '外卖'
            fact['classification']['channel'] = {'value': '外卖', 'state': 'known', 'candidates': ['外卖'],
                'evidence': [{'dimension': 'channel', 'label': '外卖', 'field': 'remark', 'evidence': 'lunch',
                              'state': state, 'method': method}]}
            changed['quality'] = analytics.quality_report(changed['facts'])
            with self.assertRaisesRegex(ReadError, 'analytics_classification'):
                analytics.validate_publication(self.reader, changed)

    def test_unknown_version_and_changed_recipe_cannot_be_read_as_current_rules(self):
        _, doc = self.publish([bill()])
        for key, value in [('taxonomy_version', 'future/999'), ('recipe', '0' * 64)]:
            changed = deepcopy(doc)
            changed[key] = value
            with self.assertRaises(ReadError):
                analytics.validate_publication(self.reader, changed)

    def test_refresh_failure_preserves_last_good_publication_and_reports_staleness(self):
        ref, _ = self.publish([bill('a')])
        self.input.write_text(json.dumps([bill('b')]), encoding='utf-8')
        self.store.import_file(self.input)
        with patch('qianji.analytics.build', side_effect=ReadError('synthetic_failure: unavailable')):
            result = self.store.export()
        self.assertEqual(result['analytics']['state'], 'needs_attention')
        state = analytics.snapshot(self.reader)
        self.assertEqual(state['analytics_ref'], ref)
        self.assertFalse(state['freshness']['source_current'])
        plan = analytics.reprocess(self.store.root)
        result = analytics.reprocess(self.store.root, apply=True, expected_plan=plan['plan_ref'])
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(analytics.snapshot(self.reader)['freshness']['source_current'])

    def test_failed_switch_leaves_previous_pointer_and_preview_can_recover(self):
        ref, _ = self.publish([bill('a')])
        self.input.write_text(json.dumps([bill('b')]), encoding='utf-8')
        self.store.import_file(self.input)
        with patch('qianji.analytics.refresh', return_value={'state': 'needs_attention'}):
            self.store.export()
        plan = analytics.reprocess(self.store.root)
        original = analytics.atomic_write
        def fail_pointer(path, payload):
            if path.name == 'current.json':
                raise OSError('synthetic switch failure')
            return original(path, payload)
        with patch('qianji.analytics.atomic_write', side_effect=fail_pointer):
            with self.assertRaises(OSError):
                analytics.reprocess(self.store.root, apply=True, expected_plan=plan['plan_ref'])
        self.assertEqual(analytics.snapshot(self.reader)['analytics_ref'], ref)
        result = analytics.reprocess(self.store.root, apply=True, expected_plan=plan['plan_ref'])
        self.assertEqual(result['state'], 'completed')

    def test_read_does_not_create_missing_directories(self):
        missing = self.root / 'missing'
        self.assertFalse(analytics.snapshot(Reader(missing))['available'])
        self.assertFalse(missing.exists())

    def test_no_new_model_calls_when_reprocessing(self):
        self.publish([bill(category='三餐', remark='lunch')])
        with patch('qianji.analysis_core.LocalModel', side_effect=AssertionError('must not call model')):
            plan = analytics.reprocess(self.store.root)
            analytics.reprocess(self.store.root, apply=True, expected_plan=plan['plan_ref'])


if __name__ == '__main__':
    unittest.main()
