"""Rebuildable, immutable analytical facts; source bills are never rewritten."""
from collections import Counter
from datetime import date
from decimal import localcontext
import hashlib
import json
import re

from .analysis_lock import analysis_lock
from .mcp_reader import Reader, ReadError, MAX_BYTES
from .normalize import canonical, _pairs, _bad_number
from .processing import summarize, snapshot_versions
from .store import atomic_write, immutable_write

DIMENSIONS = ('category_l1', 'category_l2', 'category_l3', 'channel', 'context', 'meal', 'merchant')
STATES = ('known', 'unknown', 'ambiguous', 'not_applicable')
PROJECTION_VERSION = 'qianji-analytics/1'


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _taxonomy(version):
    from . import taxonomy_v1
    # New classification semantics add a frozen module and registry entry.
    # Historical publications always replay their own version, never the latest.
    registry = {taxonomy_v1.TAXONOMY_VERSION: taxonomy_v1}
    if version not in registry:
        raise ReadError('unsupported_taxonomy_version: 未安装此固定分类版本')
    return registry[version]


def _recipe(taxonomy):
    return digest([PROJECTION_VERSION, taxonomy.RECIPE, 'iso-calendar;single-fact;quality-v1;partial-label-reuse-v1'])


def current_recipe():
    from .taxonomy_v1 import TAXONOMY_VERSION
    return _recipe(_taxonomy(TAXONOMY_VERSION))


def _reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ReadError('invalid_analytics_ref: 使用分析快照返回的完整固定引用')
    return value


def _pointer(reader, folder, key):
    path = reader._path(folder, 'exports', 'current.json')
    if not path.exists():
        return None
    value = json.loads(reader._bytes(path), object_pairs_hook=_pairs, parse_constant=_bad_number)
    if not isinstance(value, dict) or set(value) != {key}:
        raise ReadError('analytics_pointer: 派生发布指针格式错误')
    return _reference(value[key])


def calendar_dimensions(row):
    day = date.fromisoformat(row['date_local'])
    iso = day.isocalendar()
    return {'month': day.strftime('%Y-%m'), 'week': f'{iso.year:04d}-W{iso.week:02d}',
            'weekday': str(iso.weekday)}


def quality_report(facts):
    """Count missing values explicitly, with separate exact amount coverage."""
    coverage = {key: {state: 0 for state in STATES} for key in DIMENSIONS}
    monetary = {}
    proposed = Counter()
    notes = Counter()
    ids = {row['id']: row for row in facts}
    links = Counter()
    for row in facts:
        notes['present' if (row['remark'] or '').strip() else 'missing'] += 1
        for key, decision in row['classification'].items():
            coverage[key][decision['state']] += 1
            group = monetary.setdefault((row['ledger_id'], row['currency'], key),
                {state: {'count': 0, 'expense': 0} for state in STATES})
            group[decision['state']]['count'] += 1
            if row['kind'] == 'expense':
                group[decision['state']]['expense'] += int(row['amount'].replace('.', ''))
        for dimension in {tag['dimension'] for tag in row['proposed_labels']}:
            proposed[dimension] += 1
        if row['kind'] == 'refund':
            target = ids.get(row['related_record_id'])
            if not row['related_record_id']:
                links['unlinked'] += 1
            elif target is None:
                links['target_missing'] += 1
            elif target['kind'] != 'expense' or target['currency'] != row['currency']:
                links['target_incompatible'] += 1
            else:
                links['linked_expense'] += 1
    amounts = [{'ledger_id': ledger, 'currency': currency, 'dimension': dimension,
                'states': {state: {'count': values['count'], 'expense': f"{values['expense'] // 100}.{values['expense'] % 100:02d}"}
                           for state, values in groups.items()}}
               for (ledger, currency, dimension), groups in sorted(monetary.items())]
    return {'records': len(facts), 'classification': coverage, 'expense_coverage': amounts,
            'label_coverage': {'reused': sum(row['label_basis'] is not None for row in facts),
                               'fallback': sum(row['label_basis'] is None for row in facts)},
            'notes': {key: notes[key] for key in ('present', 'missing')},
            'proposed_records_by_dimension': dict(sorted(proposed.items())),
            'refund_links': {key: links[key] for key in ('linked_expense', 'unlinked', 'target_missing', 'target_incompatible')},
            'semantics': 'unknown不是零；ambiguous不展开金额；proposed不进入生效维度；退款仍计发生日，不据关联改写金额。'}


def build(reader):
    from .analysis import load_publication as load_labels
    from .analysis_core import fingerprint
    from .taxonomy_v1 import TAXONOMY_VERSION, taxonomy_catalog, classify
    source_ref = reader.snapshot()['snapshot_ref']
    source = reader._open(source_ref)
    observed = _pointer(reader, 'analysis', 'analysis_ref')
    labels, analysis_ref, analysis_source_ref, label_state = None, None, None, 'absent'
    if observed:
        raw = reader._bytes(reader._path('analysis', 'exports', 'versions', observed + '.json'))
        if hashlib.sha256(raw).hexdigest() != observed:
            raise ReadError('analysis_checksum: 固定标签发布摘要不符')
        header = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_number)
        analysis_source_ref = header['source_snapshot_ref']
        labels = load_labels(reader, analysis_source_ref, observed)
        if labels['library_id'] != source['snapshot']['library_id']:
            raise ReadError('analysis_library: 标签发布属于其它资料库')
        analysis_ref = observed
        label_state = 'matched' if analysis_source_ref == source_ref else 'partial'
    facts = []
    for row in source['datasets']['transactions']:
        entry = labels['entries'].get(row['id']) if labels else None
        if entry is not None and entry['input_hash'] != fingerprint(row):
            entry = None
        classified = classify(row, entry)
        dimensions = {**calendar_dimensions(row), **{key: classified[key]['value'] for key in DIMENSIONS}}
        facts.append({**row, 'source_fingerprint': digest(row), 'dimensions': dimensions,
                      'classification': classified,
                      'label_basis': {'analysis_ref': analysis_ref, 'source_snapshot_ref': analysis_source_ref,
                                      'input_hash': entry['input_hash']} if entry else None,
                      'proposed_labels': [tag for tag in entry['labels'] if tag['state'] == 'proposed'] if entry else []})
    doc = {'format': 'qianji-analytics', 'contract_version': '1.0',
           'source_snapshot_ref': source_ref, 'analysis_ref': analysis_ref,
           'analysis_source_snapshot_ref': analysis_source_ref,
           'label_source_state': label_state, 'observed_analysis_ref': observed,
           'taxonomy_version': TAXONOMY_VERSION, 'projection_version': PROJECTION_VERSION,
           'recipe': current_recipe(), 'taxonomy': taxonomy_catalog(),
           'facts': facts, 'quality': quality_report(facts)}
    validate_publication(reader, doc)
    return doc


def validate_publication(reader, doc):
    """Validate source facts and replay the publication's own frozen classifier."""
    required = {'format', 'contract_version', 'source_snapshot_ref', 'analysis_ref',
                'analysis_source_snapshot_ref', 'label_source_state', 'observed_analysis_ref', 'taxonomy_version',
                'projection_version', 'recipe', 'taxonomy', 'facts', 'quality'}
    if not isinstance(doc, dict) or set(doc) != required or doc['format'] != 'qianji-analytics' or doc['contract_version'] != '1.0':
        raise ReadError('analytics_schema: 不支持此分析发布结构')
    _reference(doc['recipe'])
    for key in ('taxonomy_version', 'projection_version'):
        if not isinstance(doc[key], str) or not doc[key]:
            raise ReadError('analytics_schema: 缺少加工版本')
    classifier = _taxonomy(doc['taxonomy_version'])
    if (doc['projection_version'] != PROJECTION_VERSION or doc['recipe'] != _recipe(classifier)
            or canonical(doc['taxonomy']) != canonical(classifier.taxonomy_catalog())):
        raise ReadError('analytics_recipe: 分类树或加工配方与固定版本不一致')
    for key in ('analysis_ref', 'observed_analysis_ref'):
        if doc[key] is not None:
            _reference(doc[key])
    if (doc['label_source_state'] not in ('matched', 'partial', 'absent')
            or (doc['label_source_state'] == 'absent') != (doc['observed_analysis_ref'] is None)
            or doc['analysis_ref'] != doc['observed_analysis_ref']
            or (doc['analysis_ref'] is None) != (doc['analysis_source_snapshot_ref'] is None)
            or (doc['label_source_state'] == 'matched' and doc['analysis_source_snapshot_ref'] != doc['source_snapshot_ref'])
            or (doc['label_source_state'] == 'partial' and doc['analysis_source_snapshot_ref'] == doc['source_snapshot_ref'])):
        raise ReadError('analytics_schema: 标签来源状态矛盾')
    source = reader._open(doc['source_snapshot_ref'])
    rows = source['datasets']['transactions']
    facts = doc['facts']
    if not isinstance(facts, list) or len(facts) != len(rows):
        raise ReadError('analytics_coverage: 一笔账单必须恰好对应一条分析事实')
    taxonomy = doc['taxonomy']
    if (not isinstance(taxonomy, dict) or not isinstance(taxonomy.get('nodes'), list)
            or taxonomy.get('version') != doc['taxonomy_version']):
        raise ReadError('analytics_schema: 分类树结构无效')
    nodes = {}
    for node in taxonomy['nodes']:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str)
                or not node['id'] or node['id'] in nodes or type(node.get('level')) is not int
                or node['level'] not in (1, 2, 3) or not isinstance(node.get('label'), str)):
            raise ReadError('analytics_schema: 分类节点无效')
        nodes[node['id']] = node
    for node in nodes.values():
        parent = nodes.get(node.get('parent_id'))
        if (node['level'] == 1 and node.get('parent_id') is not None) or (node['level'] > 1 and (not parent or parent['level'] != node['level'] - 1)):
            raise ReadError('analytics_schema: 分类父子关系错误')
    labels = None
    if doc['analysis_ref']:
        from .analysis import load_publication as load_labels
        labels = load_labels(reader, doc['analysis_source_snapshot_ref'], doc['analysis_ref'])
        if labels['library_id'] != source['snapshot']['library_id']:
            raise ReadError('analysis_library: 标签发布属于其它资料库')
    from .analysis_core import fingerprint
    extras = {'source_fingerprint', 'dimensions', 'classification', 'proposed_labels', 'label_basis'}
    for row, fact in zip(rows, facts):
        if (not isinstance(fact, dict) or set(fact) != set(row) | extras
                or canonical({key: fact[key] for key in row}) != canonical(row)
                or fact['source_fingerprint'] != digest(row)):
            raise ReadError('analytics_identity: 分析事实与来源账单不一致')
        decisions, dims = fact['classification'], fact['dimensions']
        entry = labels['entries'].get(row['id']) if labels else None
        if entry is not None and entry['input_hash'] != fingerprint(row):
            entry = None
        basis = {'analysis_ref': doc['analysis_ref'], 'source_snapshot_ref': doc['analysis_source_snapshot_ref'],
                 'input_hash': entry['input_hash']} if entry else None
        if fact['label_basis'] != basis:
            raise ReadError('analytics_identity: 标签复用输入与当前事实不一致')
        if decisions != classifier.classify(row, entry):
            raise ReadError('analytics_classification: 分类结果与固定规则或复核依据不一致')
        if (not isinstance(decisions, dict) or set(decisions) != set(DIMENSIONS)
                or not isinstance(dims, dict) or set(dims) != set(DIMENSIONS) | {'month', 'week', 'weekday'}
                or any(dims[key] != value for key, value in calendar_dimensions(row).items())):
            raise ReadError('analytics_schema: 分析维度结构错误')
        for key, decision in decisions.items():
            if (not isinstance(decision, dict) or set(decision) != {'value', 'state', 'candidates', 'evidence'}
                    or decision['state'] not in STATES or not isinstance(decision['candidates'], list)
                    or not isinstance(decision['evidence'], list)):
                raise ReadError('analytics_schema: 分类决策结构错误')
            value, state = decision['value'], decision['state']
            if ((state == 'known') != (isinstance(value, str) and bool(value))
                    or (state != 'known' and value is not None) or dims[key] != value
                    or any(not isinstance(item, str) or not item for item in decision['candidates'])
                    or len(set(decision['candidates'])) != len(decision['candidates'])):
                raise ReadError('analytics_schema: 分类值和状态矛盾')
            if state == 'known' and not decision['evidence']:
                raise ReadError('analytics_evidence: 已知分类必须提供证据')
            if key.startswith('category_l') and value is not None:
                level = int(key[-1])
                if value not in nodes or nodes[value]['level'] != level:
                    raise ReadError('analytics_schema: 分类不在本发布词表')
                if level > 1 and nodes[value]['parent_id'] != dims['category_l' + str(level - 1)]:
                    raise ReadError('analytics_schema: 分类路径父子不一致')
            for evidence in decision['evidence']:
                if (not isinstance(evidence, dict) or evidence.get('field') not in ('category', 'remark')
                        or not isinstance(evidence.get('evidence'), str) or not evidence['evidence'].strip()
                        or evidence['evidence'] not in (row[evidence['field']] or '')
                        or evidence.get('state') not in ('automatic', 'confirmed', 'proposed')
                        or evidence.get('method') != {'automatic': 'rule', 'confirmed': 'manual', 'proposed': 'model'}[evidence['state']]
                        or (state == 'known' and evidence.get('state') == 'proposed')):
                    raise ReadError('analytics_evidence: 分类证据不符合原文或生效状态')
                if evidence['state'] == 'proposed' and (not entry or not any(
                        all(tag.get(k) == evidence.get(k) for k in ('dimension', 'label', 'field', 'evidence', 'method', 'state'))
                        for tag in entry['labels'])):
                    raise ReadError('analytics_evidence: 候选证据不在固定标签发布中')
        expected_proposed = ([tag for tag in entry['labels'] if tag['state'] == 'proposed'] if entry else [])
        if fact['proposed_labels'] != expected_proposed:
            raise ReadError('analytics_evidence: 候选必须来自固定分析发布')
    with localcontext() as context:
        context.prec = max(50, max((len(row['amount']) for row in facts), default=1) + len(str(len(facts))) + 16)
        if summarize(facts, snapshot_versions(source['snapshot'])[1]) != source['datasets']['daily_summary']:
            raise ReadError('analytics_totals: 分析事实必须与基础精确汇总对账')
    if canonical(doc['quality']) != canonical(quality_report(facts)):
        raise ReadError('analytics_quality: 覆盖报告与事实不一致')
    return doc


def load_publication(reader, analytics_ref):
    _reference(analytics_ref)
    raw = reader._bytes(reader._path('analytics', 'exports', 'versions', analytics_ref + '.json'))
    if hashlib.sha256(raw).hexdigest() != analytics_ref:
        raise ReadError('analytics_checksum: 分析发布摘要不符')
    try:
        return validate_publication(reader, json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_number))
    except (KeyError, TypeError) as exc:
        raise ReadError('analytics_schema: 分析发布内容无效') from exc


def snapshot(reader):
    ref = _pointer(reader, 'analytics', 'analytics_ref')
    base = {'format': 'qianji-analytics-snapshot', 'contract_version': '1.0'}
    if ref is None:
        return {**base, 'available': False, 'reason': 'analytics_not_published'}
    doc = load_publication(reader, ref)
    return {**base, 'available': True, 'analytics_ref': ref,
            **{key: doc[key] for key in ('source_snapshot_ref', 'analysis_ref', 'analysis_source_snapshot_ref',
                                       'label_source_state', 'projection_version', 'taxonomy_version', 'recipe')},
            'counts': {'facts': len(doc['facts'])}, 'quality': doc['quality'],
            'freshness': {'source_current': reader.snapshot()['snapshot_ref'] == doc['source_snapshot_ref'],
                          'analysis_current': _pointer(reader, 'analysis', 'analysis_ref') == doc['observed_analysis_ref'],
                          'recipe_current': current_recipe() == doc['recipe']}}


def _plan(reader):
    doc = build(reader)
    old_ref = _pointer(reader, 'analytics', 'analytics_ref')
    old = load_publication(reader, old_ref) if old_ref else None
    before = {row['id']: row for row in old['facts']} if old else {}
    after = {row['id']: row for row in doc['facts']}
    ref = digest(doc)
    plan_ref = digest([doc['source_snapshot_ref'], doc['observed_analysis_ref'], old_ref, ref])
    changes = [{'id': key, 'fields': sorted(field for field in after[key]
                 if before[key].get(field) != after[key][field])}
               for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
    report = {'plan_ref': plan_ref, 'analytics_ref': ref, 'previous_analytics_ref': old_ref,
              'source_snapshot_ref': doc['source_snapshot_ref'], 'analysis_ref': doc['analysis_ref'],
              'label_source_state': doc['label_source_state'], 'recipe': doc['recipe'],
              'changes': {'added': len(after.keys() - before.keys()), 'removed': len(before.keys() - after.keys()),
                          'changed': len(changes), 'records': changes},
              'quality': doc['quality'], 'state': 'preview', 'applied': False}
    return doc, report


def reprocess(data_dir, *, apply=False, expected_plan=None):
    if type(apply) is not bool or (apply and not expected_plan) or (not apply and expected_plan is not None):
        raise ReadError('expected_plan_required: 先预览，再用 --apply --expected-plan 应用同一计划')
    reader = Reader(data_dir)
    with analysis_lock(reader.root, name='publication', subdirectory='analytics'):
        doc, report = _plan(reader)
        path = reader._path('analytics', 'runs', report['plan_ref'] + '.json')
        if not apply:
            immutable_write(path, canonical(report).encode())
            return {**report, 'report_path': str(path)}
        if expected_plan != report['plan_ref'] or not path.is_file():
            raise ReadError('analytics_plan_changed: 来源、标签、配方或上次发布已变化，请重新预览')
        stored = json.loads(reader._bytes(path), object_pairs_hook=_pairs, parse_constant=_bad_number)
        if stored != report:
            raise ReadError('analytics_plan_changed: 预览记录与当前计划不一致')
        _publish(reader, doc, report['analytics_ref'])
        result = {**report, 'state': 'unchanged' if report['previous_analytics_ref'] == report['analytics_ref'] else 'completed', 'applied': True}
        atomic_write(reader._path('analytics', 'last-run.json'), canonical(result).encode())
        return {**result, 'report_path': str(path)}


def _publish(reader, doc, ref):
    from ._retention import best_effort, publication_lock
    from .retention import prune_publications
    with publication_lock(reader.root):
        _publish_locked(reader, doc, ref)
        best_effort(prune_publications, reader.root)


def _publish_locked(reader, doc, ref):
    raw = canonical(doc).encode()
    if len(raw) > MAX_BYTES:
        raise ReadError('analytics_too_large: 分析发布超过读取上限')
    immutable_write(reader._path('analytics', 'exports', 'versions', ref + '.json'), raw)
    if (reader.snapshot()['snapshot_ref'] != doc['source_snapshot_ref']
            or _pointer(reader, 'analysis', 'analysis_ref') != doc['observed_analysis_ref']):
        raise ReadError('source_changed: 加工期间输入已变化，保留固定候选但不切换当前分析')
    pointer = reader._path('analytics', 'exports', 'current.json')
    if pointer.exists():
        old = json.loads(pointer.read_bytes())['analytics_ref']
        if old != ref:
            from ._retention import retire
            retire(reader.root, pointer.parent / 'versions', old + '.json')
    atomic_write(pointer, canonical({'analytics_ref': ref}).encode())


def refresh(data_dir):
    """Import/label publication hook: deterministic, no model calls, failures observable."""
    reader = Reader(data_dir)
    try:
        with analysis_lock(reader.root, name='publication', subdirectory='analytics'):
            doc = build(reader)
            ref = digest(doc)
            _publish(reader, doc, ref)
            result = {'state': 'ready', 'analytics_ref': ref, 'source_snapshot_ref': doc['source_snapshot_ref']}
            atomic_write(reader._path('analytics', 'last-run.json'), canonical(result).encode())
            return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        code = str(exc).split(':', 1)[0] if isinstance(exc, ReadError) else type(exc).__name__
        result = {'state': 'needs_attention', 'error_code': code,
                  'message': '基础发布已完成；分析刷新失败，请运行 reprocess 预览和恢复'}
        try:
            atomic_write(reader._path('analytics', 'last-run.json'), canonical(result).encode())
        except OSError:
            pass
        return result
