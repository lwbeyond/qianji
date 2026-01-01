"""Query contract checks independent of publication creation and model execution."""
from copy import deepcopy
from datetime import date
from decimal import localcontext
import unittest
from unittest.mock import patch

from qianji import analytics_query as query
from qianji.mcp_reader import ReadError, Reader


def fact(identifier, amount='0.10', kind='expense', ledger='main', currency='CNY',
         day='2026-10-23', channel='外卖', channel_state='known', asset='wallet'):
    stamp = date.fromisoformat(day)
    classification = {}
    values = {'category_l1': 'food', 'category_l2': 'meal', 'category_l3': 'lunch',
              'channel': channel, 'context': 'workday', 'meal': 'lunch', 'merchant': None}
    for name in query.CLASSIFIED_DIMENSIONS:
        state = channel_state if name == 'channel' else 'known' if values[name] is not None else 'unknown'
        classification[name] = {'value': values[name], 'state': state,
                                'candidates': ['外卖', '到店'] if state == 'ambiguous' else [],
                                'evidence': [{'field': 'category', 'value': '餐饮'}] if state == 'known' else []}
    iso = stamp.isocalendar()
    return {'id': identifier, 'ledger_id': ledger, 'source_key': identifier,
            'occurred_at': day + 'T12:30:00+08:00', 'date_local': day,
            'source_date': day + ' 12:30:00', 'utc_offset': '+08:00',
            'timezone_basis': 'configured_fixed_offset', 'kind': kind, 'amount': amount,
            'currency': currency, 'category': '餐饮', 'remark': 'Lunch Coffee' if identifier == 'one' else None,
            'asset_id': asset, 'from_asset_id': None, 'to_asset_id': None,
            'related_source_key': None, 'related_record_id': None,
            'source_type': {'expense': '支出', 'income': '收入', 'refund': '退款', 'transfer': '转账'}[kind],
            'reimbursement_code': None, 'revision': 1, 'source_file_sha256': 'a' * 64,
            'source_row': 1, 'source_fingerprint': identifier + '-fingerprint',
            'dimensions': {'month': day[:7], 'week': f'{iso.year}-W{iso.week:02}',
                           'weekday': str(iso.weekday), **values},
            'classification': classification}


def publication():
    rows = [fact('one'), fact('two', '0.20', day='2026-10-24', channel=None, channel_state='unknown'),
            fact('refund', '0.03', kind='refund'),
            fact('income', '1.00', kind='income', channel=None, channel_state='not_applicable'),
            fact('transfer', '200.00', kind='transfer', channel=None, channel_state='not_applicable'),
            fact('usd', '3.00', currency='USD'), fact('other', '2.00', ledger='other'),
            fact('ambiguous', '0.00', channel=None, channel_state='ambiguous', asset=None)]
    return {'format': 'qianji-analytics', 'contract_version': '1.0',
            'source_snapshot_ref': 'source@fixed', 'analysis_ref': 'analysis@fixed',
            'analysis_source_snapshot_ref': 'source@fixed', 'label_source_state': 'matched',
            'projection_version': 'qianji-analytics/1',
            'taxonomy_version': 'qianji-taxonomy/1', 'recipe': {'version': 'recipe/1'},
            'facts': rows, 'quality': {'records': 8}, 'taxonomy': {'nodes': []}}


class AnalyticsQueryTests(unittest.TestCase):
    def setUp(self):
        self.reader = Reader('.')
        self.doc = publication()
        self.patcher = patch.object(query, '_load', return_value=self.doc)
        self.loader = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_exact_totals_keep_ledgers_currencies_and_all_kinds_separate(self):
        result = query.cube(self.reader, 'analytics@fixed', ['category_l1', 'channel', 'weekday'], limit=1)
        main = next(row for row in result['totals'] if (row['ledger_id'], row['currency']) == ('main', 'CNY'))
        self.assertEqual((main['expense'], main['income'], main['refund'], main['transfer']),
                         ('0.30', '1.00', '0.03', '200.00'))
        self.assertEqual((main['net_expense'], main['net_cashflow'], main['avg_expense']),
                         ('0.27', '0.73', '0.10'))
        self.assertEqual((main['count'], main['expense_count'], main['expense_share_percent']), (6, 3, '100.00'))
        self.assertEqual(len(result['totals']), 3)
        self.assertEqual(result['matched_records'], 8)
        self.assertEqual(result['pagination']['returned'], 1)
        self.assertEqual(result['effective_group_by'], ['ledger_id', 'currency', 'category_l1', 'channel', 'weekday'])

    def test_null_buckets_and_coverage_distinguish_missing_ambiguous_and_not_applicable(self):
        result = query.cube(self.reader, 'ref', ['channel'], filters={'ledger_id': 'main', 'currency': 'CNY'})
        self.assertEqual(len(result['data']), 2)
        missing = result['data'][0]
        self.assertIsNone(missing['channel'])
        self.assertEqual((missing['count'], missing['expense'], missing['expense_share_percent']), (4, '0.20', '66.67'))
        channel = result['coverage']['dimensions']['channel']
        self.assertEqual({k: channel[k] for k in query.STATES},
                         {'known': 2, 'unknown': 1, 'ambiguous': 1, 'not_applicable': 2})
        self.assertEqual((channel['known_percent'], channel['applicable_known_percent']), ('33.33', '50.00'))
        self.assertEqual(result['data'][1]['expense_share_percent'], '33.33')

    def test_fact_pages_preserve_evidence_and_fixed_metadata_without_mutating_publication(self):
        before = deepcopy(self.doc)
        first = query.read_facts(self.reader, 'ref', limit=2)
        second = query.read_facts(self.reader, 'ref', offset=2, limit=2)
        for key in first:
            if key not in ('data', 'pagination'):
                self.assertEqual(first[key], second[key], key)
        self.assertEqual(first['pagination'], {'offset': 0, 'limit': 2, 'requested_limit': 2,
                                            'returned': 2, 'total': 8, 'next_offset': 2})
        for item in first['data']:
            expected = next(row for row in self.doc['facts'] if row['id'] == item['id'])
            self.assertEqual({k: v for k, v in item.items() if k != 'trace'}, expected)
            self.assertEqual(item['trace']['record_id'], expected['id'])
            self.assertEqual(item['trace']['source_snapshot_ref'], 'source@fixed')
        self.assertEqual(before, self.doc)
        self.assertEqual(first['format'], 'qianji-analytics-query')
        self.assertEqual(first['contract_version'], '1.0')

    def test_proposed_labels_are_preserved_without_entering_effective_dimensions(self):
        proposal = {'dimension': 'merchant', 'label': 'Coffee', 'field': 'remark',
                    'evidence': 'Coffee', 'state': 'proposed', 'method': 'model'}
        self.doc['facts'][0]['proposed_labels'] = [proposal]
        result = query.read_facts(self.reader, 'ref', filters={'id': 'one'})
        self.assertEqual(result['data'][0]['proposed_labels'], [proposal])
        selected = query.cube(self.reader, 'ref', ['merchant'], filters={'id': 'one'})
        self.assertIsNone(selected['data'][0]['merchant'])
        self.assertEqual(selected['data'][0]['count'], 1)
        self.assertEqual(query.read_facts(self.reader, 'ref', filters={'merchant': 'Coffee'})['data'], [])

    def test_partial_label_publication_has_explicit_dual_source_and_per_fact_basis(self):
        self.doc['analysis_source_snapshot_ref'] = 'source@older'
        self.doc['label_source_state'] = 'partial'
        basis = {'analysis_ref': 'analysis@fixed', 'source_snapshot_ref': 'source@older',
                 'input_hash': 'one-input-hash'}
        self.doc['facts'][0]['label_basis'] = basis
        self.doc['facts'][1]['label_basis'] = None
        first = query.read_facts(self.reader, 'ref', filters={'id': 'one'})
        second = query.read_facts(self.reader, 'ref', filters={'id': 'two'})
        self.assertEqual(first['data'][0]['label_basis'], basis)
        self.assertIsNone(second['data'][0]['label_basis'])
        for response in (first, second, query.cube(self.reader, 'ref'), query.quality(self.reader, 'ref')):
            self.assertEqual(response['source_snapshot_ref'], 'source@fixed')
            self.assertEqual(response['analysis_source_snapshot_ref'], 'source@older')
            self.assertEqual(response['analysis_ref'], 'analysis@fixed')
            self.assertEqual(response['label_source_state'], 'partial')
            self.assertEqual(response['projection_version'], 'qianji-analytics/1')

    def test_cube_pagination_totals_denominators_and_trace_are_complete(self):
        first = query.cube(self.reader, 'ref', ['channel', 'asset_id'], limit=1)
        second = query.cube(self.reader, 'ref', ['channel', 'asset_id'], offset=1, limit=1)
        for key in first:
            if key not in ('data', 'pagination'):
                self.assertEqual(first[key], second[key], key)
        for item in query.cube(self.reader, 'ref', ['channel', 'asset_id'])['data']:
            facts = query.read_facts(self.reader, item['trace']['analytics_ref'], filters=item['trace']['filters'])
            self.assertEqual(item['count'], facts['matched_records'])
            self.assertEqual(item['count'], item['trace']['matched_records'])
        self.assertEqual(len(first['data']), 1)
        self.assertEqual(first['coverage']['total_records'], 8)

    def test_dimensions_states_null_and_original_filters_compose(self):
        selected = query.read_facts(self.reader, 'ref', filters={'channel': None, 'channel_state': 'unknown',
                                   'month': '2026-10', 'week': '2026-W43', 'weekday': '6',
                                   'amount_min': '0.20', 'amount_max': '0.20', 'kind': 'expense'})
        self.assertEqual([row['id'] for row in selected['data']], ['two'])
        remark = query.read_facts(self.reader, 'ref', filters={'remark_contains': 'cOfFeE',
                                 'start_date': '2026-10-23', 'end_date': '2026-10-23',
                                 'date_local': '2026-10-23', 'category': '餐饮', 'asset_id': 'wallet',
                                 'source_key': 'one'})
        self.assertEqual([row['id'] for row in remark['data']], ['one'])
        ambiguous = query.read_facts(self.reader, 'ref', filters={'channel_state': 'ambiguous', 'asset_id': None})
        self.assertEqual([row['id'] for row in ambiguous['data']], ['ambiguous'])
        self.assertEqual(ambiguous['data'][0]['classification']['channel']['candidates'], ['外卖', '到店'])
        self.assertEqual(query.cube(self.reader, 'ref', ['channel'], filters={'channel_state': 'ambiguous'})['data'][0]['count'], 1)

    def test_invalid_filters_and_grouping_fail_with_read_errors(self):
        invalid_filters = [[], {'no_such_dimension': 'x'}, {'channel': []}, {'channel_state': None},
                           {'channel_state': 'proposed'}, {'kind': 'unknown'}, {'month': '2026-13'},
                           {'month': '2026-1'}, {'week': '2026-W54'}, {'week': '2021-W53'},
                           {'weekday': '0'}, {'start_date': '2026-02-30'}, {'date_local': 'tomorrow'},
                           {'start_date': '2026-10-24', 'end_date': '2026-10-23'},
                           {'amount_min': '0.001'}, {'amount_max': 'NaN'}, {'amount_min': '2', 'amount_max': '1'},
                           {'currency': None}, {'category_l1': ''}, {'remark_contains': True}]
        for filters in invalid_filters:
            with self.subTest(filters=filters), self.assertRaises(ReadError):
                query.read_facts(self.reader, 'ref', filters=filters)
        for grouping in ([], ['channel', 'channel'], ['ledger_id'], ['currency'], 'channel',
                         ['channel', 'context', 'meal', 'merchant'], [None], [['channel']], {'channel': 1}):
            with self.subTest(grouping=grouping), self.assertRaises(ReadError):
                query.cube(self.reader, 'ref', group_by=grouping)
        for options in ({'limit': 0}, {'limit': True}, {'limit': 51}, {'offset': -1}, {'offset': 0.0}):
            with self.subTest(options=options), self.assertRaises(ReadError):
                query.read_facts(self.reader, 'ref', **options)
        with self.assertRaises(ReadError):
            query.cube(self.reader, 'ref', limit=101)

    def test_empty_results_have_no_invented_zero_totals_and_empty_coverage_is_null(self):
        filters = {'merchant': 'never-present'}
        result = query.cube(self.reader, 'ref', filters=filters)
        self.assertEqual((result['data'], result['totals'], result['matched_records']), ([], [], 0))
        self.assertIsNone(result['pagination']['next_offset'])
        self.assertIsNone(result['coverage']['dimensions']['merchant']['known_percent'])
        self.assertIsNone(result['coverage']['dimensions']['channel']['applicable_known_percent'])
        self.assertEqual(query.read_facts(self.reader, 'ref', filters=filters)['data'], [])
        past_end = query.cube(self.reader, 'ref', offset=1000)
        self.assertEqual(past_end['data'], [])
        self.assertEqual(past_end['matched_records'], 8)
        self.assertIsNone(past_end['pagination']['next_offset'])

    def test_zero_expense_and_no_expense_are_distinct(self):
        zero = query.cube(self.reader, 'ref', ['channel'], filters={'id': 'ambiguous'})['totals'][0]
        no_expense = query.cube(self.reader, 'ref', ['kind'], filters={'kind': 'income'})['totals'][0]
        self.assertEqual(zero['avg_expense'], '0.00')
        self.assertIsNone(zero['expense_share_percent'])
        self.assertIsNone(no_expense['avg_expense'])
        self.assertIsNone(no_expense['expense_share_percent'])
        self.assertEqual(no_expense['net_cashflow'], '1.00')

    def test_exact_large_amounts_ignore_low_ambient_decimal_precision_and_round_half_up(self):
        self.doc['facts'] = [fact('large', '999999999999999.00'), fact('small', '0.01')]
        with localcontext() as ctx:
            ctx.prec = 6
            total = query.cube(self.reader, 'ref')['totals'][0]
            self.assertEqual(ctx.prec, 6)
        self.assertEqual(total['expense'], '999999999999999.01')
        self.assertEqual(total['avg_expense'], '499999999999999.51')
        self.assertEqual(total['net_cashflow'], '-999999999999999.01')

    def test_sorting_is_deterministic_and_does_not_depend_on_publication_order(self):
        first = query.cube(self.reader, 'ref', ['weekday', 'channel'])
        facts_first = query.read_facts(self.reader, 'ref')
        self.doc['facts'].reverse()
        self.assertEqual(first, query.cube(self.reader, 'ref', ['weekday', 'channel']))
        self.assertEqual(facts_first, query.read_facts(self.reader, 'ref'))

    def test_quality_uses_fixed_publication_and_catalog_describes_contract(self):
        result = query.quality(self.reader, 'ref')
        self.loader.assert_called_with(self.reader, 'ref')
        self.assertEqual(result['publication_quality'], {'records': 8})
        self.assertEqual(result['dataset'], 'quality')
        self.assertEqual(result['coverage']['dimensions']['channel']['ambiguous'], 1)
        taxonomy = {'version': 'qianji-taxonomy/1', 'nodes': [{'id': 'food', 'label': '餐饮'}]}
        with patch.object(query, '_taxonomy_catalog', return_value=taxonomy):
            catalog = query.catalog()
        self.assertEqual(catalog['dataset'], 'catalog')
        self.assertEqual(catalog['taxonomy'], taxonomy)
        self.assertEqual(catalog['taxonomy_version'], taxonomy['version'])
        self.assertIsNone(catalog['analytics_ref'])
        self.assertIn('expense_share_percent', catalog['metrics'])
        self.assertIn('channel_state', catalog['filters'])
        self.assertEqual(catalog['grouping']['always'], ['ledger_id', 'currency'])


if __name__ == '__main__':
    unittest.main()
