"""Explicit full-library derived-label processing; no collection or original export."""
import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path

from .analysis import Analysis, atomic_write
from .analysis_core import LocalModel, canonical, fingerprint, source_text, stamp, validate_prediction
from .mcp_reader import ReadError, Reader
from .label_rules import note_resolved
from .analysis_lock import analysis_lock


class ExactInputCache:
    """Reuse only the identical model input and recipe. Record identities stay separate."""
    def __init__(self, model):
        self.inner=model;self.model=model.model;self.model_digest=model.model_digest;self.recipe=model.recipe
        self.values={};self.errors={};self.calls=0;self.reused=0;self.error_reused=0

    def seed(self, analysis, rows):
        if not analysis.database.exists():return
        candidates={}
        with closing(analysis.connect()) as db:
            reviewed={(r['record_id'],r['input_hash']) for r in db.execute('SELECT record_id,input_hash FROM reviews')}
            by_id={r['id']:r for r in rows}
            for e in db.execute('SELECT * FROM evaluations WHERE recipe=? AND method=?',(self.recipe,'model')):
                row=by_id.get(e['record_id'])
                if row is None or e['input_hash']!=fingerprint(row) or (row['id'],e['input_hash']) in reviewed:continue
                labels=json.loads(e['labels'])
                validate_prediction({'labels':labels,'insufficient':not labels},row)
                candidates.setdefault(canonical(source_text(row)),set()).add(canonical(labels))
        for key,versions in candidates.items():
            if len(versions)==1:self.values[key]=json.loads(next(iter(versions)))

    def predict(self,row):
        key=canonical(source_text(row))
        if key in self.values:
            self.reused+=1;return deepcopy(self.values[key])
        if key in self.errors:
            self.error_reused+=1;raise ReadError(self.errors[key]+': 相同输入本轮已失败，等待下一轮再试')
        self.calls+=1
        try:
            labels=self.inner.predict(row)
            validate_prediction({'labels':labels,'insufficient':not labels},row)
        except ReadError as error:
            self.errors[key]=str(error).split(':',1)[0];raise
        self.values[key]=deepcopy(labels)
        return labels


def pending_rows(analysis, rows, recipe, retry_failed=False):
    with closing(analysis.connect()) as db:
        processed={(r['record_id'],r['input_hash']) for r in db.execute("SELECT record_id,input_hash FROM evaluations WHERE method='model' AND recipe=?",(recipe,))}
        reviewed={(r['record_id'],r['input_hash']) for r in db.execute('SELECT record_id,input_hash FROM reviews')}
        failures={(r[0],r[1]):r[2] for r in db.execute('SELECT record_id,input_hash,count(*) FROM failures WHERE recipe=? GROUP BY record_id,input_hash',(recipe,))}
    return [r for r in rows if r['kind']=='expense' and (r['remark'] or '').strip() and not note_resolved(r)
            and (r['id'],fingerprint(r)) not in processed and (r['id'],fingerprint(r)) not in reviewed
            and (retry_failed or failures.get((r['id'],fingerprint(r)),0)<3)]


def run(data_dir, model_name, retry_failed=False):
    with analysis_lock(data_dir):
        return _run(data_dir, model_name, retry_failed)


def _run(data_dir, model_name, retry_failed=False):
    analysis=Analysis(data_dir);ref=analysis.reader.snapshot()['snapshot_ref']
    rows=analysis.reader._open(ref)['datasets']['transactions'];model=ExactInputCache(LocalModel(model_name))
    model.seed(analysis,rows)
    progress_path=analysis.reader._path('analysis','full-run-progress.json')
    def report(value):
        value.update(source_snapshot_ref=ref,model=model.model,model_recipe=model.recipe,
                     actual_model_calls=model.calls,exact_input_reused=model.reused,error_reused=model.error_reused,updated_at=stamp())
        atomic_write(progress_path,canonical(value).encode())
        print(canonical(value),flush=True)
    # Complete rules first. Each pass visits a source identity once; cached errors must
    # not spend the same record's retry allowance repeatedly in successive batches.
    result=analysis.process()
    for attempt in range(1,4):
        model.errors.clear()
        pending=pending_rows(analysis,rows,model.recipe,retry_failed)
        if not pending:break
        for offset in range(0,len(pending),200):
            if Reader(data_dir).snapshot()['snapshot_ref']!=ref:raise ReadError('source_changed: 全库处理期间原发布变化，请重新运行')
            batch=offset//200+1
            result=analysis.process(model=model,model_limit=200,
                record_ids=[r['id'] for r in pending[offset:offset+200]],retry_failed=retry_failed,
                progress=lambda counts:report({'status':'running','pass':attempt,'batch':batch,'processing':counts}) if counts.get('model_attempted',0)%20==0 else None)
            report({'status':'batch_finished','pass':attempt,'batch':batch,**result})
    if Reader(data_dir).snapshot()['snapshot_ref']!=ref:
        raise ReadError('source_changed: 全库结束时原发布变化，未将新旧结果混作完成')
    with closing(analysis.connect()) as db:
        entries=analysis._entries(db,rows)
        reviewed={(r['record_id'],r['input_hash']) for r in db.execute('SELECT record_id,input_hash FROM reviews')}
        eligible=[r for r in rows if r['kind']=='expense' and (r['remark'] or '').strip()]
        unresolved=[r for r in eligible if not note_resolved(r) and not entries[r['id']]['model_processed'] and (r['id'],fingerprint(r)) not in reviewed]
        failure_codes={}
        for row in unresolved:
            latest=db.execute('SELECT error_code FROM failures WHERE record_id=? AND input_hash=? AND recipe=? ORDER BY id DESC LIMIT 1',
                              (row['id'],fingerprint(row),model.recipe)).fetchone()
            key=latest[0] if latest else 'unattempted';failure_codes[key]=failure_codes.get(key,0)+1
    report({'status':'completed_with_failures' if unresolved else 'completed','scope':'当前原发布全库；模型限有备注的支出；规则覆盖全库',
            'eligible_records':len(eligible),'ineligible_records':len(rows)-len(eligible),'unresolved_records':len(unresolved),
            'failure_codes':failure_codes,**result})
    return 2 if unresolved else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--model',default='qwen3.5:9b-32k')
    parser.add_argument('--retry-failed',action='store_true',help='显式再试已达到失败上限的记录；本次仍最多三轮')
    args=parser.parse_args()
    try:return run(args.data_dir,args.model,args.retry_failed)
    except (ReadError,OSError,ValueError) as error:
        print(canonical({'status':'stopped','error':str(error) if isinstance(error,ReadError) else type(error).__name__}),flush=True);return 1


if __name__=='__main__':raise SystemExit(main())
