"""Copyable standard-library client for immutable Qianji analytical queries.

Call snapshot() once and explicitly pass its analytics_ref to every read. A
failed, incomplete or inconsistent multi-page read raises without returning rows.
"""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class AnalyticsAPIError(RuntimeError):
    pass


PINS = ('analytics_ref', 'source_snapshot_ref', 'analysis_ref', 'analysis_source_snapshot_ref',
        'label_source_state', 'taxonomy_version', 'projection_version', 'recipe')
SOURCE_REF = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-9]+@[0-9a-f]{64}'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Invalid JSON constant: ' + value)


class AnalyticsClient:
    def __init__(self, base_url='http://127.0.0.1:8790', *, timeout=15):
        self.base_url, self.timeout = base_url.rstrip('/'), timeout
        self._pins = {}

    def _get(self, endpoint, params=None):
        url = self.base_url + '/api/v1/analytics/' + endpoint
        if params:
            url += '?' + urlencode(params)
        try:
            with urlopen(Request(url, headers={'Accept': 'application/json'}), timeout=self.timeout) as response:
                result = json.loads(response.read(), object_pairs_hook=_pairs, parse_constant=_constant)
            if not isinstance(result, dict):
                raise ValueError('Expected object')
            return result
        except HTTPError as exc:
            try:
                code = json.loads(exc.read())['error']['code']
            except (ValueError, KeyError, TypeError):
                code = 'http_error'
            finally:
                exc.close()
            raise AnalyticsAPIError(f'{code} (HTTP {exc.code})') from exc
        except (URLError, TimeoutError, OSError, ValueError, RecursionError) as exc:
            raise AnalyticsAPIError('Analytical API unavailable or invalid JSON') from exc

    def _remember(self, result, analytics_ref):
        if not isinstance(analytics_ref, str) or not re.fullmatch(r'[0-9a-f]{64}', analytics_ref):
            raise AnalyticsAPIError('Invalid analytics_ref')
        if any(key not in result for key in PINS):
            raise AnalyticsAPIError('Missing publication identity')
        pin = {key: result[key] for key in PINS}
        if (pin['analytics_ref'] != analytics_ref
                or not isinstance(pin['source_snapshot_ref'], str)
                or not re.fullmatch(SOURCE_REF, pin['source_snapshot_ref'])
                or not isinstance(pin['taxonomy_version'], str) or not pin['taxonomy_version']
                or not isinstance(pin['projection_version'], str) or not pin['projection_version']
                or not isinstance(pin['recipe'], str) or not re.fullmatch(r'[0-9a-f]{64}', pin['recipe'])
                or (pin['analysis_ref'] is not None and (not isinstance(pin['analysis_ref'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', pin['analysis_ref'])))):
            raise AnalyticsAPIError('Invalid publication identity')
        if (pin['label_source_state'] not in ('matched', 'partial', 'absent')
                or (pin['label_source_state'] == 'absent') != (pin['analysis_ref'] is None)
                or (pin['analysis_ref'] is None) != (pin['analysis_source_snapshot_ref'] is None)):
            raise AnalyticsAPIError('Invalid label publication identity')
        if pin['analysis_source_snapshot_ref'] is not None:
            if (not isinstance(pin['analysis_source_snapshot_ref'], str)
                    or not re.fullmatch(SOURCE_REF, pin['analysis_source_snapshot_ref'])
                    or (pin['label_source_state'] == 'matched') !=
                       (pin['analysis_source_snapshot_ref'] == pin['source_snapshot_ref'])):
                raise AnalyticsAPIError('Invalid label source basis')
        if analytics_ref in self._pins and self._pins[analytics_ref] != pin:
            raise AnalyticsAPIError('Publication identity changed')
        self._pins[analytics_ref] = pin

    def _query(self, dataset, analytics_ref, params=None):
        result = self._get(dataset, {'analytics_ref': analytics_ref, **(params or {})})
        if (result.get('format') != 'qianji-analytics-query'
                or result.get('contract_version') != '1.0' or result.get('dataset') != dataset):
            raise AnalyticsAPIError('Unsupported analytical query contract')
        self._remember(result, analytics_ref)
        return result

    def snapshot(self):
        """Discover current publication; existing explicit references remain pinned."""
        result = self._get('snapshot')
        if (result.get('format') != 'qianji-analytics-snapshot'
                or result.get('contract_version') != '1.0' or type(result.get('available')) is not bool):
            raise AnalyticsAPIError('Unsupported analytical snapshot contract')
        if result['available']:
            self._remember(result, result.get('analytics_ref'))
        return result

    def catalog(self):
        result = self._get('catalog')
        if result.get('format') != 'qianji-analytics-query' or result.get('contract_version') != '1.0':
            raise AnalyticsAPIError('Unsupported analytical catalog contract')
        return result

    def quality(self, analytics_ref):
        return self._query('quality', analytics_ref)

    def read_all(self, analytics_ref, *, filters=None, page_size=20):
        return self._collect('facts', analytics_ref, filters, None, page_size)

    def cube_all(self, analytics_ref, *, group_by=None, filters=None, page_size=30):
        return self._collect('cube', analytics_ref, filters, group_by, page_size)

    def _collect(self, dataset, analytics_ref, filters, group_by, page_size):
        maximum = 50 if dataset == 'facts' else 100
        if type(page_size) is not int or not 1 <= page_size <= maximum:
            raise ValueError(f'page_size must be 1..{maximum}')
        params = {'limit': page_size}
        if filters is not None:
            if not isinstance(filters, dict):
                raise ValueError('filters must be an object')
            params['filters'] = json.dumps(filters, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if group_by is not None:
            if (not isinstance(group_by, list) or not 1 <= len(group_by) <= 3
                    or any(not isinstance(value, str) or not value or ',' in value for value in group_by)):
                raise ValueError('group_by must contain one to three dimension names')
            params['group_by'] = ','.join(group_by)
        rows, offset, total, stable = [], 0, None, None
        while True:
            result = self._query(dataset, analytics_ref, {**params, 'offset': offset})
            page, data = result.get('pagination'), result.get('data')
            if not isinstance(page, dict) or not isinstance(data, list):
                raise AnalyticsAPIError('Missing pagination or rows')
            metadata = {key: value for key, value in result.items() if key not in ('data', 'pagination')}
            if stable is not None and stable != metadata:
                raise AnalyticsAPIError('Query metadata changed between pages')
            stable = metadata
            if (any(type(page.get(key)) is not int for key in ('offset', 'limit', 'returned', 'total'))
                    or page['offset'] != offset or page['limit'] != page_size
                    or page['returned'] != len(data) or len(data) > page_size or page['total'] < 0
                    or (total is not None and page['total'] != total)):
                raise AnalyticsAPIError('Invalid pagination')
            total = page['total']
            rows.extend(data)
            following = page.get('next_offset')
            if following is None:
                if len(rows) != total:
                    raise AnalyticsAPIError('Incomplete query result')
                return {**stable, 'data': rows, 'pagination': {
                    'offset': 0, 'returned': len(rows), 'total': total, 'next_offset': None}}
            if type(following) is not int or following != offset + len(data) or not offset < following < total:
                raise AnalyticsAPIError('Pagination did not advance')
            offset = following


def analytics_schema():
    """Authoritative JSON Schema for immutable files, independent of qianji imports.

    This describes shape and local constraints. Publication SHA-256, source
    identity, evidence quotes and exact totals still need semantic verification.
    Standard source fields intentionally match snapshot_schema() field for field.
    """
    def obj(properties):
        return {'type': 'object', 'required': list(properties), 'properties': properties,
                'additionalProperties': False}
    def array(items):
        return {'type': 'array', 'items': items}

    text = {'type': 'string'}
    nonempty = {'type': 'string', 'minLength': 1}
    nullable = {'type': ['string', 'null']}
    sha = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    nullable_sha = {**sha, 'type': ['string', 'null']}
    source_ref = {'type': 'string', 'pattern': '^' + SOURCE_REF + '$'}
    nullable_ref = {**source_ref, 'type': ['string', 'null']}
    money = {'type': 'string', 'pattern': '^(0|[1-9][0-9]*)\\.[0-9]{2}$'}
    count = {'type': 'integer', 'minimum': 0}
    currency = {'type': 'string', 'pattern': '^[A-Z]{3}$'}
    dimensions = ('category_l1', 'category_l2', 'category_l3', 'channel', 'context', 'meal', 'merchant')
    states = ('known', 'unknown', 'ambiguous', 'not_applicable')
    source = {
        'id': {'type': 'string', 'pattern': '^qj_[a-f0-9]{64}$'},
        'ledger_id': text, 'source_key': text, 'revision': {'type': 'integer', 'minimum': 1},
        'occurred_at': {'type': 'string', 'format': 'date-time'},
        'date_local': {'type': 'string', 'format': 'date'}, 'source_date': text,
        'utc_offset': {'type': 'string', 'pattern': '^[+-][0-9]{2}:[0-9]{2}$'},
        'timezone_basis': {'const': 'configured_fixed_offset'},
        'kind': {'enum': ['expense', 'income', 'refund', 'transfer']},
        'source_type': {'enum': ['支出', '收入', '退款', '转账']},
        'amount': money, 'currency': currency, 'category': text, 'remark': nullable,
        'asset_id': nullable, 'from_asset_id': nullable, 'to_asset_id': nullable,
        'related_source_key': nullable, 'related_record_id': nullable,
        'reimbursement_code': {'type': ['integer', 'null']},
        'source_file_sha256': sha, 'source_row': {'type': 'integer', 'minimum': 1},
    }
    label_dimensions = ['item', 'channel', 'context', 'meal', 'record_note', 'merchant']
    label = obj({'dimension': {'enum': [*label_dimensions, 'remark_detail', 'source_category']},
                 'label': nonempty, 'field': {'enum': ['category', 'remark']},
                 'evidence': nonempty, 'method': {'enum': ['rule', 'manual', 'model']},
                 'state': {'enum': ['automatic', 'confirmed', 'proposed']}})
    decision = obj({'value': nullable, 'state': {'enum': list(states)},
                    'candidates': {**array(nonempty), 'uniqueItems': True},
                    'evidence': array({'$ref': '#/$defs/evidence'})})
    decision.update({
        'if': {'properties': {'state': {'const': 'known'}}},
        'then': {'properties': {'value': nonempty, 'evidence': {'minItems': 1}}},
        'else': {'properties': {'value': {'type': 'null'}}},
    })
    proposed = obj({**label['properties'], 'recipe': sha,
                    'evaluation_id': sha,
                    'model': nullable, 'model_digest': nullable})
    proposed['required'] = list(label['required'])
    proposed['properties'] = {**proposed['properties'], 'dimension': {'enum': label_dimensions},
                              'method': {'const': 'model'}, 'state': {'const': 'proposed'}}
    label_basis = obj({'analysis_ref': sha, 'source_snapshot_ref': source_ref, 'input_hash': sha})
    label_basis['type'] = ['object', 'null']
    fact = obj({**source, 'source_fingerprint': sha, 'label_basis': {'$ref': '#/$defs/label_basis'},
                'dimensions': obj({'month': {'type': 'string', 'pattern': '^[0-9]{4}-[0-9]{2}$'},
                                   'week': {'type': 'string', 'pattern': '^[0-9]{4}-W[0-9]{2}$'},
                                   'weekday': {'enum': ['1', '2', '3', '4', '5', '6', '7']},
                                   **{name: nullable for name in dimensions}}),
                'classification': obj({name: {'$ref': '#/$defs/decision'} for name in dimensions}),
                'proposed_labels': array({'$ref': '#/$defs/proposed_label'})})
    quality = obj({
        'records': count,
        'classification': obj({name: obj({state: count for state in states}) for name in dimensions}),
        'expense_coverage': array(obj({'ledger_id': text, 'currency': currency,
            'dimension': {'enum': list(dimensions)},
            'states': obj({state: obj({'count': count, 'expense': money}) for state in states})})),
        'notes': obj({'present': count, 'missing': count}),
        'proposed_records_by_dimension': {'type': 'object',
            'propertyNames': {'enum': label_dimensions}, 'additionalProperties': count},
        'refund_links': obj({name: count for name in ('linked_expense', 'unlinked', 'target_missing', 'target_incompatible')}),
        'label_coverage': obj({'reused': count, 'fallback': count}),
        'semantics': text,
    })
    taxonomy = obj({'version': nonempty, 'recipe': sha,
        'nodes': array(obj({'id': nonempty, 'parent_id': nullable, 'level': {'enum': [1, 2, 3]}, 'label': nonempty})),
        'dimensions': {'const': list(dimensions)},
        'semantics': {'type': 'object', 'additionalProperties': text}})
    result = obj({'format': {'const': 'qianji-analytics'}, 'contract_version': {'const': '1.0'},
                  'source_snapshot_ref': source_ref, 'analysis_ref': nullable_sha,
                  'analysis_source_snapshot_ref': nullable_ref,
                  'label_source_state': {'enum': ['matched', 'partial', 'absent']},
                  'observed_analysis_ref': nullable_sha, 'taxonomy_version': nonempty,
                  'projection_version': nonempty, 'recipe': sha,
                  'taxonomy': {'$ref': '#/$defs/taxonomy'},
                  'facts': array({'$ref': '#/$defs/fact'}), 'quality': {'$ref': '#/$defs/quality'}})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$defs': {'fact': fact, 'decision': decision, 'evidence': label,
                      'proposed_label': proposed, 'label_basis': label_basis,
                      'taxonomy': taxonomy, 'quality': quality}, **result}
