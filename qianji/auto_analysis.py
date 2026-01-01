"""Durable, coalescing import-triggered analysis queue; local Ollama only."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .analysis import Analysis
from .analysis_core import LocalModel, canonical, stamp, digest, LABEL_PROMPT, LABEL_SCHEMA
from .analysis_lock import analysis_lock
from .batch_analysis import ExactInputCache, pending_rows
from .label_rules import note_resolved
from .mcp_reader import Reader, ReadError

DEFAULTS = {'enabled': True, 'model': 'qwen3.5:9b-32k', 'think': False,
            'num_ctx': 8192, 'num_predict': 1536}
QUEUE_SCHEMA = '''CREATE TABLE IF NOT EXISTS job (
 id INTEGER PRIMARY KEY CHECK(id=1), requested INTEGER NOT NULL, completed INTEGER NOT NULL,
 source_ref TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT, retry_at REAL NOT NULL,
 updated_at TEXT NOT NULL, report TEXT NOT NULL);
 INSERT OR IGNORE INTO job VALUES(1,0,0,'','idle',NULL,0,'','{}');'''


def settings(data_dir):
    path = Reader(data_dir)._path('analysis', 'config.json')
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ReadError('invalid_model_config: 分析配置含未知字段')
    value = {**DEFAULTS, **value}
    if type(value['enabled']) is not bool or type(value['think']) is not bool:
        raise ReadError('invalid_model_config: enabled/think必须是布尔值')
    if not isinstance(value['model'], str) or not value['model']:
        raise ReadError('invalid_model_config: model必须是本机模型名')
    if type(value['num_ctx']) is not int or not 4096 <= value['num_ctx'] <= 32768:
        raise ReadError('invalid_model_config: num_ctx范围为4096至32768')
    if type(value['num_predict']) is not int or not 256 <= value['num_predict'] <= 8192:
        raise ReadError('invalid_model_config: num_predict范围为256至8192')
    return value


def _connect(data_dir, write=False):
    path = Reader(data_dir)._path('analysis', 'jobs.sqlite3')
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        db.executescript(QUEUE_SCHEMA)
    else:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def status(data_dir):
    reader = Reader(data_dir)
    if not reader._path('analysis', 'jobs.sqlite3').exists():
        return {'state': 'not_started', 'pending': False}
    with closing(_connect(data_dir)) as db:
        result = dict(db.execute('SELECT * FROM job WHERE id=1').fetchone())
    result.pop('id')
    result['pending'] = result['requested'] > result['completed']
    result['report'] = json.loads(result['report'])
    coverage = result['report'].get('coverage', {})
    states = coverage.get('model_status_counts', {})
    if result['state'] in ('completed', 'completed_with_failures') and not result['pending']:
        result['summary'] = (f"已处理{coverage.get('records', 0)}笔；英文备注规则覆盖{states.get('rule_covered', 0)}笔，"
                             f"模型候选{coverage.get('with_proposed_labels', 0)}笔（未确认），"
                             f"信息不足{states.get('insufficient', 0)}笔，失败{states.get('failed', 0)}笔。")
    else:
        result['summary'] = {
            'queued': '账单已发布，增量分类已排队。',
            'running': '后台分类处理中，原分类可继续使用。',
            'waiting_model': '分类暂时等待，任务已保留，将自动重试。',
            'needs_attention': '账单已保留，分类任务需要检查。',
            'disabled': '自动分类已关闭，未完成任务仍保留。',
        }.get(result['state'], '有新的分类请求等待处理。')
    # A persisted running state is the last observation, not a process liveness claim.
    return result


def start_worker(data_dir):
    kwargs = {'cwd': str(Path(__file__).resolve().parents[1]), 'stdin': subprocess.DEVNULL,
              'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-m', 'qianji.auto_analysis',
                             '--data-dir', str(Path(data_dir).resolve()), 'worker'], **kwargs)


def enqueue(data_dir, *, launch=True):
    config = settings(data_dir)
    if not config['enabled']:
        return {'state': 'disabled', 'pending': False}
    ref = Reader(data_dir).snapshot()['snapshot_ref']
    with closing(_connect(data_dir, True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE job SET requested=requested+1,source_ref=?,state='queued',error_code=NULL,retry_at=0,updated_at=? WHERE id=1", (ref, stamp()))
    if launch:
        try:
            start_worker(data_dir)
        except OSError:
            _update(data_dir, state='needs_attention', error_code='worker_start_failed')
    return status(data_dir)


def _update(data_dir, **values):
    values['updated_at'] = stamp()
    if 'report' in values:
        values['report'] = canonical(values['report'])
    allowed = {'completed', 'source_ref', 'state', 'error_code', 'retry_at', 'updated_at', 'report'}
    if set(values) - allowed:
        raise ValueError('Unknown queue column')
    with closing(_connect(data_dir, True)) as db, db:
        db.execute('UPDATE job SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=1', tuple(values.values()))


def process_available(data_dir, *, model_factory=LocalModel, config=None, report=None):
    """One current source snapshot, bounded retries. Returns only after publication."""
    config = config or settings(data_dir)
    analysis = Analysis(data_dir)
    with analysis_lock(data_dir):
        # Rules and the new source-bound view are usable even while Ollama is offline.
        result = analysis.process()
        ref = result['source_snapshot_ref']
        rows = analysis.reader._open(ref)['datasets']['transactions']
        unresolved = [r for r in rows if r['kind']=='expense' and (r['remark'] or '').strip() and not note_resolved(r)]
        with closing(analysis.connect()) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            reviewed = {(r['record_id'], r['input_hash']) for r in db.execute('SELECT record_id,input_hash FROM reviews')}
        from .analysis_core import fingerprint
        unresolved = [r for r in unresolved if (r['id'], fingerprint(r)) not in reviewed]
        signature = digest([config, LABEL_PROMPT, LABEL_SCHEMA, meta['model_recipe'], 'auto-config-v1'])
        if meta.get('model_configuration') == signature and not pending_rows(analysis, rows, meta['model_recipe']):
            return {**result, 'actual_model_calls': 0, 'exact_input_reused': 0}
        if not unresolved:
            return {**result, 'actual_model_calls': 0, 'exact_input_reused': 0}
        inner = model_factory(config['model'], think=config['think'], num_ctx=config['num_ctx'], num_predict=config['num_predict'])
        model = ExactInputCache(inner)
        model.seed(analysis, rows)
        signature = digest([config, LABEL_PROMPT, LABEL_SCHEMA, model.recipe, 'auto-config-v1'])
        # Select the recipe even if all records are already cached/reviewed/exhausted.
        with closing(analysis.connect(True)) as db, db:
            if db.execute("SELECT value FROM meta WHERE key='model_recipe'").fetchone()[0] != model.recipe:
                db.execute("UPDATE meta SET value=? WHERE key='model_recipe'", (model.recipe,))
                analysis.bump(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES('model_configuration',?)", (signature,))
        for attempt in range(3):
            model.errors.clear()
            pending = pending_rows(analysis, rows, model.recipe)
            if not pending:
                break
            for offset in range(0, len(pending), 50):
                if Reader(data_dir).snapshot()['snapshot_ref'] != ref:
                    raise ReadError('source_changed: 已有新导入，切换到最新发布继续')
                result = analysis.process(model=model, model_limit=50,
                                          record_ids=[r['id'] for r in pending[offset:offset+50]])
                if report:
                    report({**result, 'pass': attempt+1, 'actual_model_calls': model.calls})
        if Reader(data_dir).snapshot()['snapshot_ref'] != ref:
            raise ReadError('source_changed: 已有新导入，切换到最新发布继续')
        return {**analysis.publish(ref), 'actual_model_calls': model.calls, 'exact_input_reused': model.reused}


def worker(data_dir, *, once=False, model_factory=LocalModel, sleep=time.sleep):
    # Waiting contenders prevent the enqueue/worker-exit race from losing a wakeup.
    # Only the lock owner may call Ollama; after it exits contenders check the queue.
    with analysis_lock(data_dir, 'worker', wait=True):
        delay = 15
        while True:
            job = status(data_dir)
            if not job['pending']:
                return 0
            request = job['requested']
            try:
                config = settings(data_dir)
                if not config['enabled']:
                    _update(data_dir, state='disabled', error_code=None)
                    return 0
                _update(data_dir, state='running', error_code=None, retry_at=0)
                result = process_available(data_dir, model_factory=model_factory, config=config,
                                           report=lambda value: _update(data_dir, report=value))
                counts = result['coverage']['model_status_counts']
                state = 'completed_with_failures' if counts['failed'] else 'completed'
                _update(data_dir, completed=request, source_ref=result['source_snapshot_ref'],
                        state=state, error_code=None, retry_at=0, report=result)
                delay = 15
            except (ReadError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                code = str(error).split(':', 1)[0] if isinstance(error, ReadError) else type(error).__name__
                if code == 'source_changed':
                    _update(data_dir, state='queued', error_code=code)
                    if once:
                        return 2
                    continue
                transient = code in ('model_unavailable', 'model_not_installed', 'model_changed', 'analysis_busy')
                _update(data_dir, state='waiting_model' if transient else 'needs_attention',
                        error_code=code, retry_at=time.time()+delay if transient else 0)
                if once or not transient:
                    return 2
                # Polling in short intervals also notices a fresh import/config disable.
                deadline = time.monotonic()+delay
                while time.monotonic() < deadline:
                    if status(data_dir)['requested'] != request or not settings(data_dir)['enabled']:
                        break
                    sleep(min(2, max(0, deadline-time.monotonic())))
                delay = min(delay*2, 300)
            if once:
                return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1]/'data')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('enqueue')
    run = commands.add_parser('worker'); run.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'worker':
            return worker(args.data_dir, once=args.once)
        print(json.dumps(status(args.data_dir) if args.command=='status' else enqueue(args.data_dir), ensure_ascii=False, indent=2))
        return 0
    except (ReadError, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
