"""Source-owned derived labels. Explicit CLI writes; MCP reads immutable publications only."""
import argparse
from collections import Counter, OrderedDict
from contextlib import closing
from decimal import Decimal
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

from .analysis_core import (VERSION, TAXONOMY, RULE_RECIPE, LocalModel, digest, fingerprint,
                            inventory, rules, stamp, valid_label, validate_prediction)
from .mcp_reader import Reader, ReadError, bounded, integer, MAX_BYTES
from .normalize import canonical, _pairs, _bad_number
from .processing import summarize, snapshot_versions
from .analysis_lock import analysis_lock
from .label_rules import note_resolved

SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE evaluations(id TEXT PRIMARY KEY,record_id TEXT NOT NULL,source_revision INTEGER NOT NULL,
 input_hash TEXT NOT NULL,recipe TEXT NOT NULL,method TEXT NOT NULL,model TEXT NOT NULL,
 model_digest TEXT NOT NULL,labels TEXT NOT NULL,snapshot_ref TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX evaluation_record ON evaluations(record_id,source_revision,recipe);
CREATE TABLE failures(id INTEGER PRIMARY KEY,record_id TEXT NOT NULL,input_hash TEXT NOT NULL,
 recipe TEXT NOT NULL,error_code TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE reviews(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,revision INTEGER NOT NULL,
 record_id TEXT NOT NULL,source_revision INTEGER NOT NULL,input_hash TEXT NOT NULL,
 decisions TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Analysis:
    def __init__(self, data_dir):
        self.reader = Reader(data_dir)
        self.root = self.reader._path('analysis')
        self.database = self.reader._path('analysis', 'labels.sqlite3')

    def connect(self, write=False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.database, timeout=10)
        else:
            c = sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def initialize(self, library):
        with closing(self.connect(True)) as c:
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ReadError('analysis_schema: 非空未知标签库，拒绝初始化')
                c.executescript('BEGIN IMMEDIATE;'+SCHEMA+'PRAGMA user_version=1;')
                c.executemany('INSERT INTO meta VALUES(?,?)', [('library', library), ('revision','0'), ('model_recipe','')])
                c.commit()
            elif version != 1:
                raise ReadError('analysis_schema: 不支持此分析库版本')
            if c.execute("SELECT value FROM meta WHERE key='library'").fetchone()[0] != library:
                raise ReadError('analysis_library: 分析库属于另一来源资料库')

    @staticmethod
    def bump(c):
        c.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        return int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])

    def save_evaluation(self, row, snapshot_ref, recipe, method, labels, model='', model_digest='', review_revision=None):
        key = digest([row['id'], fingerprint(row), recipe])
        with closing(self.connect(True)) as c, c:
            c.execute('BEGIN IMMEDIATE')
            if review_revision is not None:
                latest = c.execute('SELECT coalesce(max(revision),0) FROM reviews WHERE record_id=?', (row['id'],)).fetchone()[0]
                if latest != review_revision:raise ReadError('review_changed: 推理期间人工复核变化，模型结果未写入')
            changed = c.execute('INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (key,row['id'],row['revision'],fingerprint(row),recipe,method,model,model_digest,
                 canonical(labels),snapshot_ref,stamp())).rowcount
            if changed:
                self.bump(c)
        return bool(changed)

    def process(self, filters=None, model=None, model_limit=10, retry_failed=False, progress=None, record_ids=None):
        with analysis_lock(self.reader.root):
            return self._process(filters, model, model_limit, retry_failed, progress, record_ids)

    def _process(self, filters=None, model=None, model_limit=10, retry_failed=False, progress=None, record_ids=None):
        integer(model_limit, 0, 200)
        ref = self.reader.snapshot()['snapshot_ref']
        doc = self.reader._open(ref)
        selected = self.reader._rows(doc, 'transactions', filters)
        if record_ids is not None:
            if not isinstance(record_ids, (list, tuple)) or not 1 <= len(record_ids) <= 200 or any(not isinstance(x,str) for x in record_ids):
                raise ReadError('invalid_selection: 全库批次需1至200个记录ID')
            ids=set(record_ids)
            if not ids.issubset({r['id'] for r in selected}):raise ReadError('invalid_selection: 记录不在当前来源筛选内')
            selected=[r for r in selected if r['id'] in ids]
        self.initialize(doc['snapshot']['library_id'])
        counts = Counter()
        for row in selected:
            counts['rules_new' if self.save_evaluation(row,ref,RULE_RECIPE,'rule',rules(row)) else 'rules_cached'] += 1
        if model is not None and model_limit:
            with closing(self.connect(True)) as c, c:
                if c.execute("SELECT value FROM meta WHERE key='model_recipe'").fetchone()[0] != model.recipe:
                    c.execute("UPDATE meta SET value=? WHERE key='model_recipe'",(model.recipe,));self.bump(c)
            # Most recent records first; process unmapped categories before already-mapped ones.
            selected = sorted(selected, key=lambda r: (r['date_local'],r['id']), reverse=True)
            selected.sort(key=lambda r: bool(rules(r)))
            for row in selected:
                if row['kind'] != 'expense' or not (row['remark'] or '').strip():
                    counts['model_ineligible'] += 1;continue
                if note_resolved(row):
                    counts['model_rule_covered'] += 1;continue
                with closing(self.connect()) as c:
                    existing = c.execute('SELECT 1 FROM evaluations WHERE id=?',
                        (digest([row['id'],fingerprint(row),model.recipe]),)).fetchone()
                    reviewed = c.execute('SELECT 1 FROM reviews WHERE record_id=? AND input_hash=?',
                        (row['id'],fingerprint(row))).fetchone()
                    attempts = c.execute('SELECT count(*) FROM failures WHERE record_id=? AND input_hash=? AND recipe=?',
                        (row['id'],fingerprint(row),model.recipe)).fetchone()[0]
                    review_revision = c.execute('SELECT coalesce(max(revision),0) FROM reviews WHERE record_id=?', (row['id'],)).fetchone()[0]
                if existing or reviewed:
                    counts['model_cached_or_reviewed'] += 1;continue
                if attempts >= 3 and not retry_failed:
                    counts['model_retry_exhausted'] += 1;continue
                if counts['model_attempted'] >= model_limit:
                    counts['model_pending'] += 1;continue
                counts['model_attempted'] += 1
                try:
                    labels = model.predict(row)
                    validate_prediction({'labels':labels,'insufficient':not labels},row)
                    current = Reader(self.reader.root)
                    current_ref = current.snapshot()['snapshot_ref']
                    current_row = next((x for x in current._open(current_ref)['datasets']['transactions'] if x['id']==row['id']),None)
                    if current_row is None or fingerprint(current_row) != fingerprint(row):
                        raise ReadError('source_changed: 推理期间原记录已变更')
                    self.save_evaluation(row,ref,model.recipe,'model',labels,model.model,model.model_digest,review_revision)
                    counts['model_proposed' if labels else 'model_insufficient'] += 1
                except (ReadError, OSError, ValueError, KeyError, TypeError) as exc:
                    # Persist a safe error code, not a transport body containing private material.
                    code = str(exc).split(':',1)[0] if isinstance(exc,ReadError) else type(exc).__name__
                    if code in ('model_unavailable', 'model_not_installed', 'model_changed') or code.startswith('model_http_'):
                        # Service outages must not consume each bill's validation retries.
                        raise
                    with closing(self.connect(True)) as c, c:
                        c.execute('INSERT INTO failures(record_id,input_hash,recipe,error_code,created_at) VALUES(?,?,?,?,?)',
                            (row['id'],fingerprint(row),model.recipe,code,stamp()));self.bump(c)
                    counts['model_failed'] += 1
                if progress:
                    progress(dict(counts))
        if Reader(self.reader.root).snapshot()['snapshot_ref'] != ref:
            raise ReadError('source_changed: 批次保留旧版处理记录；请重跑后发布，不能拼接新版')
        published = self.publish(ref)
        return {'processing':dict(counts), **published}

    def _entries(self, c, rows):
        model_recipe = c.execute("SELECT value FROM meta WHERE key='model_recipe'").fetchone()[0]
        failures={(r['record_id'],r['input_hash']):r['error_code'] for r in c.execute(
            'SELECT record_id,input_hash,error_code FROM failures WHERE recipe=? ORDER BY id',(model_recipe,))}
        evaluations = {}
        for value in c.execute('SELECT * FROM evaluations ORDER BY created_at,id'):
            evaluations.setdefault(value['record_id'],[]).append(dict(value))
        reviews = {}
        for value in c.execute('SELECT * FROM reviews ORDER BY revision'):
            reviews.setdefault(value['record_id'],[]).append(dict(value))
        entries = {}
        for row in rows:
            fp = fingerprint(row)
            candidates, decisions = {}, {}
            matching = [e for e in evaluations.get(row['id'],[]) if e['input_hash']==fp]
            for e in matching:
                if e['recipe'] != (RULE_RECIPE if e['method']=='rule' else model_recipe):
                    continue
                if e['method']=='model' and note_resolved(row):
                    # Keep past evaluations as history; exact-note rules now cover this input.
                    continue
                for tag in json.loads(e['labels']):
                    key = tag['dimension']+':'+tag['label']
                    value = {**tag,'state':'automatic' if e['method']=='rule' else 'proposed',
                             'method':e['method'],'recipe':e['recipe'],'evaluation_id':e['id'],
                             'model':e['model'],'model_digest':e['model_digest']}
                    if key not in candidates or value['state']=='automatic':
                        candidates[key] = value
            history = reviews.get(row['id'],[])
            for review in history:
                if review['input_hash'] == fp:
                    for decision in json.loads(review['decisions']):
                        decisions[decision['dimension']+':'+decision['label']] = decision
            manual_dimensions = {d['dimension'] for d in decisions.values() if d['action']=='accept'}
            for key in list(candidates):
                if candidates[key]['dimension'] in manual_dimensions or decisions.get(key,{}).get('action')=='reject':
                    del candidates[key]
            for key,d in decisions.items():
                if d['action']=='accept':
                    candidates[key] = {k:v for k,v in d.items() if k!='action'} | {'state':'confirmed','method':'manual'}
            model_results=[e for e in matching if e['recipe']==model_recipe and e['method']=='model']
            code=failures.get((row['id'],fp))
            if row['kind']!='expense' or not (row['remark'] or '').strip():model_status='ineligible'
            elif any(r['input_hash']==fp for r in history):model_status='reviewed'
            elif note_resolved(row):model_status='rule_covered'
            elif model_results:model_status='processed_with_labels' if any(json.loads(e['labels']) for e in model_results) else 'insufficient'
            elif code:model_status='failed'
            else:model_status='pending'
            failure_code=(code if re.fullmatch(r'[A-Za-z0-9_]{1,60}',code or '') else 'unknown_error') if model_status=='failed' else None
            entries[row['id']] = {'source_revision':row['revision'],'input_hash':fp,
                'labels':[candidates[k] for k in sorted(candidates)],
                'rules_processed':any(e['recipe']==RULE_RECIPE for e in matching),
                'model_processed':bool(model_results),'model_status':model_status,'model_failure_code':failure_code,
                'stale_evaluations':sum(e['input_hash']!=fp or e['recipe'] not in (RULE_RECIPE,model_recipe)
                                        for e in evaluations.get(row['id'],[])),
                'stale_reviews':sum(r['input_hash']!=fp for r in history)}
        return entries

    def publish(self, snapshot_ref=None):
        with analysis_lock(self.reader.root):
            return self._publish(snapshot_ref)

    def _publish(self, snapshot_ref=None):
        ref = snapshot_ref or self.reader.snapshot()['snapshot_ref']
        doc = self.reader._open(ref)
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            meta = dict(c.execute('SELECT key,value FROM meta'))
            if meta['library'] != doc['snapshot']['library_id']:
                raise ReadError('analysis_library: 分析库与来源身份不同')
            entries = self._entries(c,doc['datasets']['transactions'])
            payload = {'format':'qianji-analysis','format_version':1, 'source_snapshot_ref':ref,
                       'library_id':meta['library'],'store_revision':int(meta['revision']),
                       'rule_recipe':RULE_RECIPE,'model_recipe':meta['model_recipe'],
                       'taxonomy':TAXONOMY,'entries':entries}
        raw = canonical(payload).encode()
        if len(raw)>MAX_BYTES:
            raise ReadError('analysis_too_large: 分析发布超过有界读取上限')
        sha = hashlib.sha256(raw).hexdigest()
        path = self.reader._path('analysis','exports','versions',sha+'.json')
        from ._retention import publication_lock, retire
        with publication_lock(self.reader.root):
            if path.exists():
                if path.read_bytes()!=raw:raise ReadError('analysis_collision: 固定发布内容不一致')
            else:
                atomic_write(path,raw)
            if Reader(self.reader.root).snapshot()['snapshot_ref'] != ref:
                raise ReadError('source_changed: 原发布已变化，保留固定分析但不切换当前分析')
            pointer = self.reader._path('analysis','exports','current.json')
            if pointer.exists():
                old = json.loads(pointer.read_bytes())['analysis_ref']
                if old != sha:
                    retire(self.reader.root, path.parent, old+'.json')
            atomic_write(pointer,canonical({'analysis_ref':sha}).encode())
        from .analytics import refresh
        analytics = refresh(self.reader.root)
        return {'analysis_ref':sha,'source_snapshot_ref':ref,'store_revision':int(meta['revision']),
                'coverage':coverage(list(entries.values())), 'analytics':analytics}

    def review(self, record_id, source_revision, decisions, reason, expected_revision, request_id):
        if not self.database.is_file():raise ReadError('analysis_not_initialized: 请先运行process，复核不创建空库')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',request_id):
            raise ReadError('invalid_request_id: 使用稳定请求编号')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
            raise ReadError('invalid_reason: 复核需要简短原因')
        integer(expected_revision,0,10**12);integer(source_revision,1,10**12)
        payload_hash = digest([record_id,source_revision,decisions,reason,expected_revision])
        with closing(self.connect(True)) as c:
            c.execute('BEGIN IMMEDIATE')
            previous = c.execute('SELECT payload_hash,revision FROM reviews WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['payload_hash']!=payload_hash:raise ReadError('request_conflict: 同请求编号内容改变')
                return {'store_revision':previous['revision'],'replayed':True}
            revision = int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            if revision != expected_revision:raise ReadError('revision_conflict: 标签库已变化，请读取最新状态')
            current = Reader(self.reader.root);ref=current.snapshot()['snapshot_ref']
            doc=current._open(ref)
            if c.execute("SELECT value FROM meta WHERE key='library'").fetchone()[0]!=doc['snapshot']['library_id']:
                raise ReadError('analysis_library: 分析库与来源身份不同')
            row=next((x for x in doc['datasets']['transactions'] if x['id']==record_id),None)
            if row is None or row['revision']!=source_revision:raise ReadError('source_changed: 原记录不存在或修订变化')
            if not isinstance(decisions,list) or not 1<=len(decisions)<=20:raise ReadError('invalid_review: 提交1至20项决策')
            keys=set()
            for d in decisions:
                if not isinstance(d,dict) or set(d)-{'dimension','label','action','field','evidence'}:
                    raise ReadError('invalid_review: 不支持的决策字段')
                valid_label(d.get('dimension'),d.get('label'))
                key=(d['dimension'],d['label'])
                if key in keys:raise ReadError('invalid_review: 决策重复')
                keys.add(key)
                if d.get('action')=='accept':
                    validate_prediction({'labels':[{k:v for k,v in d.items() if k!='action'}],'insufficient':False},row)
                elif d.get('action')!='reject':raise ReadError('invalid_review: 仅accept/reject')
            revision=self.bump(c)
            c.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)',
                (request_id,payload_hash,revision,record_id,source_revision,fingerprint(row),canonical(decisions),reason,stamp()))
            c.commit()
        return {'store_revision':revision,'replayed':False,'publication':'请显式publish后供MCP读取'}

    def backup(self, target):
        target=Path(target)
        if target.exists():raise ReadError('backup_exists: 不覆盖已有备份')
        target.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as source,closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ReadError('backup_integrity: 备份校验失败')
        return {'backup':str(target),'integrity':'ok','restore_verified':False}


MODEL_STATUSES=('ineligible','reviewed','rule_covered','processed_with_labels','insufficient','failed','pending','unreported')


def coverage(entries):
    return {'records':len(entries),'rules_processed':sum(e['rules_processed'] for e in entries),
            'model_processed':sum(e['model_processed'] for e in entries),
            'model_status_counts':{state:sum(e.get('model_status','unreported')==state for e in entries) for state in MODEL_STATUSES},
            'with_effective_labels':sum(any(t['state'] in ('automatic','confirmed') for t in e['labels']) for e in entries),
            'with_automatic_labels':sum(any(t['state']=='automatic' for t in e['labels']) for e in entries),
            'with_confirmed_labels':sum(any(t['state']=='confirmed' for t in e['labels']) for e in entries),
            'with_proposed_labels':sum(any(t['state']=='proposed' for t in e['labels']) for e in entries),
            'with_stale_history':sum(bool(e['stale_reviews'] or e['stale_evaluations']) for e in entries)}


class AnalysisHandles:
    """Bounded process-local aliases: never recover an expired handle using latest data."""
    def __init__(self):self.entries=OrderedDict();self.serial=0;self.scope=uuid4().hex[:10]

    def remember(self, snapshot_ref, analysis_ref):
        pair=(snapshot_ref,analysis_ref)
        for key,value in self.entries.items():
            if value==pair:return key
        self.serial+=1;key='qa_'+self.scope+'_'+str(self.serial);self.entries[key]=pair
        if len(self.entries)>16:self.entries.popitem(last=False)
        return key

    def resolve(self, snapshot_ref, value):
        if isinstance(value,str) and value.startswith('qa_'):
            pair=self.entries.get(value)
            if pair is None or pair[0]!=snapshot_ref:
                raise ReadError('invalid_analysis_handle: 短引用失效或原版本不匹配，请重新调用analysis_snapshot；不代表源数据损坏')
            return pair[1]
        return value


def load_publication(reader, snapshot_ref, analysis_ref):
    if not isinstance(analysis_ref,str) or not re.fullmatch(r'[a-f0-9]{64}',analysis_ref):
        raise ReadError('invalid_analysis_ref: 使用analysis_snapshot返回的引用')
    raw=reader._bytes(reader._path('analysis','exports','versions',analysis_ref+'.json'))
    if hashlib.sha256(raw).hexdigest()!=analysis_ref:raise ReadError('analysis_checksum: 分析发布摘要不符')
    doc=json.loads(raw,object_pairs_hook=_pairs,parse_constant=_bad_number)
    source=reader._open(snapshot_ref)
    if doc.get('format')!='qianji-analysis' or doc.get('format_version')!=1:raise ReadError('analysis_schema: 分析版本不支持')
    if type(doc.get('store_revision')) is not int or doc['store_revision']<0:raise ReadError('analysis_schema: 分析修订无效')
    if doc.get('source_snapshot_ref')!=snapshot_ref or doc.get('library_id')!=source['snapshot']['library_id']:
        raise ReadError('analysis_stale: 原账单发布已变化，请重新运行分析；不能混用两个版本')
    rows=source['datasets']['transactions']
    taxonomy=doc.get('taxonomy')
    if not isinstance(taxonomy,dict) or set(taxonomy)!=set(TAXONOMY):raise ReadError('analysis_schema: 标签词表无效')
    for dimension,values in taxonomy.items():
        if dimension=='merchant':
            if not isinstance(values,str):raise ReadError('analysis_schema: 商家词表说明无效')
        elif not isinstance(values,list) or not values or len(values)>200 or any(not isinstance(v,str) or not 1<=len(v)<=40 for v in values):
            raise ReadError('analysis_schema: 标签词表无效')
    if not isinstance(doc.get('entries'),dict) or set(doc['entries'])!={r['id'] for r in rows}:
        raise ReadError('analysis_coverage: 分析记录身份集合不匹配')
    for row in rows:
        e=doc['entries'][row['id']]
        if not isinstance(e,dict) or any(type(e.get(k)) is not bool for k in ('rules_processed','model_processed')):
            raise ReadError('analysis_schema: 处理状态无效')
        if any(type(e.get(k)) is not int or e[k]<0 for k in ('stale_evaluations','stale_reviews')):
            raise ReadError('analysis_schema: 历史覆盖状态无效')
        if e['source_revision']!=row['revision'] or e['input_hash']!=fingerprint(row):
            raise ReadError('analysis_identity: 标签输入与源记录不符')
        if 'model_status' in e:
            status=e['model_status'];code=e.get('model_failure_code')
            if status not in MODEL_STATUSES[:-1]:raise ReadError('analysis_schema: 模型状态不支持')
            eligible=row['kind']=='expense' and bool((row['remark'] or '').strip())
            if (status=='ineligible')==eligible:raise ReadError('analysis_schema: 模型适用范围不符')
            if status in ('processed_with_labels','insufficient') and not e['model_processed']:
                raise ReadError('analysis_schema: 模型结果与处理状态矛盾')
            if status=='rule_covered' and not e['rules_processed']:
                raise ReadError('analysis_schema: 规则覆盖记录缺少规则处理结果')
            if status in ('failed','pending','ineligible') and e['model_processed']:
                raise ReadError('analysis_schema: 模型结果与处理状态矛盾')
            if status=='failed':
                if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,60}',code):raise ReadError('analysis_schema: 模型失败码无效')
            elif code is not None:raise ReadError('analysis_schema: 非失败记录带错误码')
        if not isinstance(e['labels'],list):raise ReadError('analysis_schema: 标签结构无效')
        if len(e['labels'])>40:raise ReadError('analysis_schema: 单条标签数量超限')
        keys=set()
        for tag in e['labels']:
            validate_prediction({'labels':[{k:tag[k] for k in ('dimension','label','field','evidence')}],'insufficient':False},row,taxonomy)
            if tag['state'] not in ('automatic','confirmed','proposed'):raise ReadError('analysis_schema: 标签状态无效')
            if (tag['dimension'],tag['label']) in keys:raise ReadError('analysis_schema: 标签重复')
            keys.add((tag['dimension'],tag['label']))
            if {'automatic':'rule','confirmed':'manual','proposed':'model'}[tag['state']]!=tag.get('method'):
                raise ReadError('analysis_schema: 标签来源与状态矛盾')
    return doc


def analysis_snapshot(reader, snapshot_ref):
    reader._open(snapshot_ref)
    current=reader._path('analysis','exports','current.json')
    if not current.exists():return {'available':False,'reason':'analysis_not_published','taxonomy':TAXONOMY}
    pointer=json.loads(reader._bytes(current),object_pairs_hook=_pairs)
    ref=pointer['analysis_ref'];doc=load_publication(reader,snapshot_ref,ref)
    return bounded({'available':True,'analysis_ref':ref,'source_snapshot_ref':snapshot_ref,
        'store_revision':doc['store_revision'],'taxonomy':doc['taxonomy'],'coverage':coverage(list(doc['entries'].values())),
        'citation':'qianji://analysis/'+ref,
        'semantics':'automatic为原分类或整条英文备注明确规则映射；proposed是模型补充建议，默认不进入生效标签筛选；confirmed为人工确认。原分类始终保留。model_status_counts将不适用ineligible、已复核reviewed、整条备注规则覆盖rule_covered、模型有输出processed_with_labels、信息不足insufficient、失败failed、待处理pending分开；旧发布为unreported。有模型输出不代表被接受，reviewed也可以是拒绝。'})


def review_table(rows):
    def cell(value):
        value=html.escape(str(value),quote=False)
        for char in ('\\','|','`','*','_','[',']'):value=value.replace(char,'\\'+char)
        return value.replace('\r','\\r').replace('\n','\\n')
    states={'automatic':'规则生效（非人工确认）','proposed':'模型候选（未确认）','confirmed':'人工确认'}
    dimensions={'item':'品类','meal':'餐次','channel':'渠道','context':'场景','record_note':'备注标记','merchant':'商家'}
    lines=['| 本页序号 | 维度 | 标签 | 依据字段 | 原文依据 | 状态 |','| ---: | --- | --- | --- | --- | --- |']
    for number,row in enumerate(rows,1):
        for tag in row['labels']:
            values=[number,dimensions[tag['dimension']],tag['label'],tag['field'],tag['evidence'],states[tag['state']]]
            lines.append('| '+' | '.join(cell(v) for v in values)+' |')
    return '\n'.join(lines) if len(lines)>2 else '本页没有符合当前显示条件的标签；不表示现实中没有消费。'


def label_query(reader,snapshot_ref,analysis_ref,filters=None,tags=None,tag_mode='all',include_proposed=False,offset=0,limit=5,only_proposed=False):
    integer(offset,0,1000000);integer(limit,1,30)
    if type(include_proposed) is not bool or tag_mode not in ('all','any','not'):raise ReadError('invalid_query: 标签模式不合规')
    if type(only_proposed) is not bool or (only_proposed and not include_proposed):raise ReadError('invalid_query: 仅看候选需要显式include_proposed=true')
    if tags is not None and (not isinstance(tags,list) or len(tags)>10):raise ReadError('invalid_tags: 最多10个标签')
    doc=load_publication(reader,snapshot_ref,analysis_ref)
    for tag in tags or []:
        if not isinstance(tag,str) or ':' not in tag:raise ReadError('invalid_tag: 使用dimension:label')
        valid_label(*tag.split(':',1),taxonomy=doc['taxonomy'])
    source=reader._open(snapshot_ref)
    rows=reader._rows(source,'transactions',filters)
    selected=[];data=[]
    for row in rows:
        entry=doc['entries'][row['id']]
        if only_proposed and not any(t['state']=='proposed' for t in entry['labels']):continue
        labels=[t for t in entry['labels'] if include_proposed or t['state']!='proposed']
        keys={t['dimension']+':'+t['label'] for t in labels}
        if tags:
            matches = all(t in keys for t in tags) if tag_mode=='all' else any(t in keys for t in tags)
            if tag_mode=='not':matches=not matches
            if not matches:continue
        selected.append(row)
        data.append({**{k:row[k] for k in ('id','ledger_id','source_key','revision','source_file_sha256','source_row',
                     'date_local','kind','amount','currency','category')},'labels':labels,
                     'analysis_status':{k:v for k,v in entry.items() if k not in ('labels','input_hash','source_revision')}})
    totals={}
    for day in summarize(selected, snapshot_versions(source['snapshot'])[1]):
        key=(day['ledger_id'],day['currency'])
        g=totals.setdefault(key,{'ledger_id':key[0],'currency':key[1],'count':0,
                              **{k:Decimal(0) for k in ('expense','income','refund','transfer','net_expense','net_cashflow')}})
        g['count']+=day['count']
        for k in ('expense','income','refund','transfer','net_expense','net_cashflow'):g[k]+=Decimal(day[k])
    query=digest([filters or {},tags or [],tag_mode,include_proposed,only_proposed])
    result={'snapshot_ref':snapshot_ref,'analysis_ref':analysis_ref,'citation':'qianji://analysis/'+analysis_ref+'/query/'+query,
            'filters':filters or {},'tags':tags or [],'tag_mode':tag_mode,'include_proposed':include_proposed,'only_proposed':only_proposed,
            'coverage_before_tag_filter':coverage([doc['entries'][r['id']] for r in rows]),
            'totals':[{k:format(v,'.2f') if isinstance(v,Decimal) else v for k,v in g.items()} for _,g in sorted(totals.items())],
            'data':data[offset:offset+limit], 'pagination':{'offset':offset,'total':len(data)},
            'semantics':'同一记录无论命中几个标签仅统计一次。金额主单位；不同查询的重叠标签合计不可再相加。退款仅按自身原分类映射，不自动继承原消费标签；标签净额不是已核对全部退款的最终消费成本。模型proposed未确认；未处理/无标签不代表没有该类消费。备注正文可按id另读原记录。'}
    result['review_table_markdown']=review_table(result['data'])
    result['coverage_text']=f'日期等原筛选范围共{len(rows)}条记录；经过标签及候选条件筛选后匹配{len(data)}条。'
    while len(canonical(result))>15800 and len(result['data'])>1:
        result['data'].pop();result['review_table_markdown']=review_table(result['data'])
    n=len(result['data']);result['pagination'].update(returned=n,next_offset=offset+n if offset+n<len(data) else None)
    return bounded(result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data')
    subs=parser.add_subparsers(dest='command',required=True)
    run=subs.add_parser('process');run.add_argument('--model');run.add_argument('--model-limit',type=int,default=10)
    run.add_argument('--think',action='store_true',help='显式启用模型思考；该设置参与配方指纹')
    run.add_argument('--start-date');run.add_argument('--end-date');run.add_argument('--retry-failed',action='store_true')
    subs.add_parser('publish');subs.add_parser('status');subs.add_parser('audit')
    review=subs.add_parser('review');review.add_argument('--file',type=Path,required=True)
    backup=subs.add_parser('backup');backup.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        a=Analysis(args.data_dir)
        if args.command=='process':
            model=LocalModel(args.model,think=args.think,num_predict=3072 if args.think else 1536) if args.model and args.model_limit else None
            filters={k:getattr(args,k) for k in ('start_date','end_date') if getattr(args,k)}
            result=a.process(filters,model,args.model_limit,args.retry_failed,
                lambda counts:print(canonical({'progress':counts}),flush=True))
        elif args.command=='publish':result=a.publish()
        elif args.command=='review':result=a.review(**json.loads(args.file.read_text(encoding='utf-8-sig')))
        elif args.command=='backup':result=a.backup(args.out)
        else:
            ref=a.reader.snapshot()['snapshot_ref']
            if args.command=='status':result=analysis_snapshot(a.reader,ref)
            else:
                pages=[];offset=0
                while offset is not None:
                    page=inventory(a.reader,ref,offset);pages.extend(page['data']);offset=page['pagination']['next_offset']
                result={'snapshot_ref':ref,'fields':pages,'verified_records':page['verified_records'],'scope':page['scope']}
                target=a.reader._path('analysis','audits',digest(ref)+'.json')
                atomic_write(target,canonical(result).encode())
                result={'audit_file':str(target),'raw_fields':len(pages),'mapped_fields':sum(bool(f['standardized_as']) for f in pages),
                        'unmapped_fields':[f['source_field'] for f in pages if not f['standardized_as']]}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ReadError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        message=str(exc) if isinstance(exc,ReadError) else type(exc).__name__+': 分析失败，来源账单未修改'
        print(json.dumps({'error':message},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
