"""Rebuild current accepted records, preserving source revisions and old releases."""

from collections import Counter
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from integrations.qianji_client import QianjiAPIError, load_snapshot, validate_snapshot
from . import processing
from .normalize import DataError, MAX_BYTES, _bad_number, _pairs, canonical, digest, json_safe, money
from .store import atomic_write, immutable_write, now


def accepted_rows(store, db, normalizer_version):
    """Use each current accepted revision's exact archived row, never file order."""
    records = db.execute("""SELECT c.*, r.normalized_json AS accepted_json,
        r.raw_json AS accepted_raw, i.source_sha256 AS archive_sha, i.ledger_id AS import_ledger,
        l.utc_offset, (SELECT max(v.revision) FROM revisions v WHERE v.record_id=c.id) AS latest
        FROM records c LEFT JOIN revisions r ON r.record_id=c.id AND r.revision=c.revision
        LEFT JOIN imports i ON i.id=r.import_id LEFT JOIN ledgers l ON l.id=c.ledger_id
        ORDER BY c.id""").fetchall()
    archives, before, after = {}, [], []
    for record in records:
        if (not record['accepted_json'] or record['revision'] != record['latest']
                or record['raw_json'] != record['accepted_raw']
                or record['ledger_id'] != record['import_ledger']):
            raise DataError('source_history_mismatch: 当前记录不是完整的最新已接纳修订。')
        accepted = json.loads(record['accepted_json'])
        current = json.loads(record['normalized_json'])
        sha, index = accepted['source_file_sha256'], accepted['source_row']
        if (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)
                or sha != record['archive_sha'] or type(index) is not int or index < 1):
            raise DataError('archive_identity: 已接纳修订的原件引用无效。')
        identity = {'id': record['id'], 'ledger_id': record['ledger_id'],
                    'source_key': record['source_key'], 'revision': record['revision'],
                    'source_file_sha256': sha, 'source_row': index}
        if any(accepted.get(k) != v or current.get(k) != v for k, v in identity.items()):
            raise DataError('source_identity: 当前记录与原始修订身份不一致。')
        if sha not in archives:
            path = store.root / 'archive' / (sha + '.json')
            with path.open('rb') as stream:
                payload = stream.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != sha:
                raise DataError('archive_checksum: 原件归档大小或摘要不匹配。')
            rows = json.loads(payload.decode('utf-8-sig'), parse_float=Decimal,
                              parse_constant=_bad_number, object_pairs_hook=_pairs)
            if not isinstance(rows, list):
                raise DataError('archive_format: 原件必须是数组。')
            archives[sha] = rows
        source = archives[sha]
        if index > len(source) or not isinstance(source[index - 1], dict):
            raise DataError('archive_row: 原件行号无效。')
        raw = source[index - 1]
        # The source comparison document is independent of the target algorithm.
        comparison = json_safe(raw)
        comparison['money'] = money(raw.get('money'))
        if canonical(comparison) != record['raw_json'] or raw.get('key') != record['source_key']:
            raise DataError('archive_identity: 原件行与已接纳原文不一致。')
        normalized, _ = processing.normalize_record(raw, record['ledger_id'],
                                                    record['utc_offset'], normalizer_version)
        for key in ('id', 'ledger_id', 'source_key'):
            if normalized.get(key) != identity[key]:
                raise DataError('identity_changed: 重建不能改变账单或账本身份。')
        normalized.update(identity)
        before.append(current)
        after.append(normalized)
    sort_key = lambda row: (row['date_local'], row['occurred_at'], row['id'])
    return sorted(before, key=sort_key), sorted(after, key=sort_key)


def write_report(store, report):
    payload = (canonical(report) + '\n').encode('utf-8')
    atomic_write(store.root / 'processing' / 'runs' / (report['run_id'] + '.json'), payload)
    atomic_write(store.root / 'processing' / 'last-run.json', payload)


def finish_publication(store, run_id, publication):
    path = store.root / 'processing' / 'runs' / (run_id + '.json')
    report = json.loads(path.read_bytes())
    report.update(state='completed', applied=True, finished_at=now(), generation_after=publication['generation'],
                  publication=publication['publication'])
    report.pop('error', None)
    write_report(store, report)
    return report


def preserve_previous(store, base):
    """Materialize a fallback before committing a candidate; never rewrite history."""
    current = store.root / 'exports' / 'current.json'
    if current.exists():
        previous = load_snapshot(current, expected_library_id=base['snapshot']['library_id'])
        if previous['snapshot']['revision'] > base['snapshot']['revision']:
            raise DataError('publication_ahead: 发布版本比业务库更新，拒绝回滚。')
        payload = current.read_bytes()
    else:
        previous = base
        validate_snapshot(previous)
        payload = (canonical(previous) + '\n').encode('utf-8')
    name = digest(previous['snapshot']['generation']) + '.json'
    immutable_write(current.parent / 'versions' / name, payload)
    if not current.exists():
        atomic_write(current, payload)


def rebuild(store, *, apply=False, expected_plan=None):
    run_id = uuid4().hex
    report = {'run_id': run_id, 'operation': 'rebuild', 'state': 'started', 'started_at': now(),
              'applied': False, 'report_path': str(store.root / 'processing' / 'runs' / (run_id + '.json'))}
    committed = False
    try:
        with closing(store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            meta = store._metadata(db)
            if meta.get('pending_rebuild'):
                raise DataError('publication_pending: 请先运行 python -m qianji export 恢复已有重建发布。')
            base = store._snapshot(db, meta)
            target = {'normalizer_version': processing.NORMALIZER_VERSION,
                      'summary_version': processing.SUMMARY_VERSION}
            before, rows = accepted_rows(store, db, target['normalizer_version'])
            summaries = processing.summarize(rows, target['summary_version'])
            old = {row['id']: row for row in before}
            changes, fields = [], Counter()
            for row in rows:
                previous = old[row['id']]
                diff = {k: {'before': previous.get(k), 'after': row.get(k)}
                        for k in previous.keys() | row.keys() if previous.get(k) != row.get(k)}
                if diff:
                    fields.update(diff.keys())
                    changes.append({'id': row['id'], 'source_revision': row['revision'], 'fields': diff})
            datasets = {'transactions': rows, 'daily_summary': summaries}
            summary_changed = base['datasets']['daily_summary'] != summaries
            changed = bool(changes or summary_changed or any(meta.get(k) != v for k, v in target.items()))
            plan_ref = digest(canonical({'base': base, 'target': target, 'datasets': datasets}))
            report.update(plan_ref=plan_ref, generation_before=base['snapshot']['generation'],
                          generation_after=base['snapshot']['generation'], target_processing=target,
                          source_revision=int(meta['source_revision']), records=len(rows), changed=changed,
                          changes={'records_changed': len(changes), 'fields': dict(sorted(fields.items())),
                                   'daily_summary_changed': summary_changed},
                          validation={'accepted_sources': 'ok', 'record_identity': 'ok'})
            timestamp = now()
            candidate = {'format': 'qianji-normalized', 'format_version': 1,
                         'snapshot': {**base['snapshot'], 'contract_version': '1.1',
                                      'processing': {**target, 'source_revision': int(meta['source_revision']),
                                                     'source_updated_at': meta['source_updated_at'],
                                                     'processed_at': timestamp if changed else meta['processed_at']},
                                      'revision': int(meta['revision']) + int(changed),
                                      'generation': meta['library_id'] + ':' + str(int(meta['revision']) + int(changed)),
                                      'updated_at': timestamp if changed else meta['updated_at'],
                                      'data_sha256': digest(canonical(datasets)),
                                      'counts': {k: len(v) for k, v in datasets.items()}},
                         'datasets': datasets}
            validate_snapshot(candidate, expected_library_id=meta['library_id'])
            report['validation']['candidate_contract_and_totals'] = 'ok'
            if apply and (not expected_plan or expected_plan != plan_ref):
                raise DataError('rebuild_plan_changed: 应用需要最新预览的 --expected-plan；数据或加工版本已变化请重新预览。')
            diff_path = store.root / 'processing' / 'diffs' / (run_id + '.json')
            atomic_write(diff_path, (canonical({'plan_ref': plan_ref, 'records': changes,
                'daily_summary_before': base['datasets']['daily_summary'] if summary_changed else None,
                'daily_summary_after': summaries if summary_changed else None}) + '\n').encode('utf-8'))
            report['diff_path'] = str(diff_path)
            if not apply or not changed:
                report.update(state='preview' if not apply else 'unchanged', finished_at=now())
                write_report(store, report)
                return report
            preserve_previous(store, base)
            report.update(state='candidate_prepared', applied=False,
                          generation_after=candidate['snapshot']['generation'])
            # Save a recovery receipt before the database commit. A crash can leave a
            # candidate receipt, but only pending_rebuild in SQLite marks a commit.
            write_report(store, report)
            for row in rows:
                db.execute('UPDATE records SET normalized_json=? WHERE id=?', (canonical(row), row['id']))
            updates = {**target, 'processed_at': timestamp, 'updated_at': timestamp,
                       'revision': str(candidate['snapshot']['revision']), 'pending_rebuild': run_id}
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?, ?)', updates.items())
            # Reconstruct from the exact rows to be committed and check the full view.
            if store._snapshot(db) != candidate:
                raise DataError('candidate_mismatch: 事务内数据与已校验候选不一致。')
        committed = True
        report.update(state='publication_pending', applied=True)
        write_report(store, report)
        try:
            store.export()
        except (OSError, sqlite3.Error) as exc:
            raise DataError('publication_pending: 重建已保存，旧发布继续可读；运行 python -m qianji export 恢复发布。') from exc
        return json.loads(Path(report['report_path']).read_bytes())
    except Exception as exc:
        error = DataError('rebuild_failed: ' + str(exc)) if isinstance(exc, (OSError, QianjiAPIError)) else exc
        report.update(state='publication_pending' if committed else 'failed', finished_at=now(),
                      applied=committed, error=str(error)[:600])
        write_report(store, report)
        if error is not exc:
            raise error from exc
        raise
