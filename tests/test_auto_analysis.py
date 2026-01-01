"""Synthetic inputs only. Unit tests never depend on a running model server."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from qianji.__main__ import main
from qianji.analysis import Analysis, load_publication, label_query
from qianji.analysis_core import LocalModel, decode_note_labels, rules, RULE_RECIPE
from qianji.analysis_lock import analysis_lock
from qianji.auto_analysis import enqueue, worker, status, settings, process_available
from qianji.label_rules import note_resolved
from qianji.mcp_reader import ReadError
from qianji.store import Store
from test_mcp import bill


class FakeModel:
    model='synthetic'; model_digest='a'*64; recipe='synthetic-v2'
    def __init__(self, error=None, before=None):
        self.calls=0; self.error=error; self.before=before
    def predict(self,row):
        self.calls+=1
        if self.before:
            callback,self.before=self.before,None
            callback()
        if self.error:
            raise ReadError(self.error+': synthetic')
        return [{'dimension':'meal','label':'午餐','field':'remark','evidence':'lunch'}]


class NoteRuleTests(unittest.TestCase):
    def row(self,remark,category='其它',kind='expense'):
        return {'kind':kind,'category':category,'remark':remark}

    def test_complete_case_and_whitespace_aliases_keep_exact_evidence(self):
        row=self.row('  Coke  COLA  ','零食')
        tags=rules(row)
        self.assertIn({'dimension':'item','label':'饮料','field':'remark','evidence':'Coke  COLA'},tags)
        self.assertTrue(note_resolved(row))
        self.assertEqual(row['category'],'零食')

    def test_negation_mixed_items_and_ambiguous_words_are_not_exact_rules(self):
        for note in ('not beer','not beer, cola','coffee and milk','balance','ice','milk tea for a friend'):
            self.assertFalse(note_resolved(self.row(note)),note)

    def test_context_and_kind_prevent_water_and_phone_overgeneralization(self):
        self.assertFalse(note_resolved(self.row('water')))
        self.assertEqual(rules(self.row('water','水电煤'))[0]['label'],'水电燃气')
        self.assertIn('饮料',[t['label'] for t in rules(self.row('water','零食'))])
        self.assertFalse(note_resolved(self.row('lunch',kind='refund')))
        self.assertFalse(note_resolved(self.row('subway','三餐')))
        self.assertFalse(note_resolved(self.row('notebook','电器数码')))
        self.assertFalse(note_resolved(self.row('coke'+' '*250+'cola')))

    def test_schema_rejects_translated_quotes_duplicates_and_injected_merchant(self):
        row=self.row('lunch')
        for output,code in [
            ({'labels':[{'tag':'meal:午餐','evidence':'午餐'}],'merchants':[]},'evidence'),
            ({'labels':[{'tag':'meal:午餐','evidence':'lunch'}]*2,'merchants':[]},'duplicate'),
            ({'labels':[],'merchants':[{'name':'Invented','evidence':'lunch'}]},'invalid_prediction'),
        ]:
            with self.assertRaisesRegex(ReadError,code):decode_note_labels(output,row)

    def test_prompt_options_and_thinking_are_part_of_recipe_and_input_is_minimal(self):
        with patch.object(LocalModel,'installed_digest',return_value='a'*64):
            first=LocalModel('synthetic')
            second=LocalModel('synthetic',think=True)
            third=LocalModel('synthetic',num_ctx=16384)
            self.assertEqual(len({first.recipe,second.recipe,third.recipe}),3)
            value={'done':True,'done_reason':'stop','message':{'content':'{"labels":[],"merchants":[]}'}}
            with patch.object(first,'request',return_value=value) as request:
                first.predict({**self.row('a synthetic note'),'amount':'999.00','asset_id':'secret'})
            payload=request.call_args.args[1]
            source=json.loads(payload['messages'][1]['content'])
            self.assertEqual(set(source),{'category','remark'})
            self.assertFalse(payload['think'])
            self.assertEqual(payload['options']['presence_penalty'],0)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qianji-auto-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'data'
        self.source=self.root/'input.json';self.store=Store(self.data)

    def publish(self,rows,**kwargs):
        self.source.write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8')
        self.store.import_file(self.source,**kwargs);self.store.export()

    def factory(self,model):
        return lambda *args,**kwargs:model

    def test_cli_import_and_repair_export_enqueue_but_preview_and_conflict_do_not(self):
        self.source.write_text(json.dumps([bill(remark='lunch')]),encoding='utf-8')
        with patch('qianji.auto_analysis.start_worker') as launch,redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--data-dir',str(self.data),'preview',str(self.source)]),0)
            self.assertEqual(status(self.data)['state'],'not_started')
            self.assertEqual(main(['--data-dir',str(self.data),'import',str(self.source)]),0)
            self.assertEqual(status(self.data)['requested'],1)
            self.assertEqual(main(['--data-dir',str(self.data),'export']),0)
            self.assertEqual(status(self.data)['requested'],2)
            self.source.write_text(json.dumps([bill(remark='changed')]),encoding='utf-8')
            self.assertEqual(main(['--data-dir',str(self.data),'import',str(self.source)]),2)
            self.assertEqual(launch.call_count,2)

    def test_enqueue_failure_does_not_report_committed_import_as_failed(self):
        self.source.write_text(json.dumps([bill()]),encoding='utf-8')
        output=io.StringIO()
        with patch('qianji.auto_analysis.enqueue',side_effect=OSError('synthetic')),redirect_stdout(output):
            self.assertEqual(main(['--data-dir',str(self.data),'import',str(self.source)]),0)
        self.assertTrue(json.loads(output.getvalue())['applied'])
        self.assertEqual(json.loads(output.getvalue())['analysis']['state'],'needs_attention')
        self.assertEqual(len(self.store.snapshot()['datasets']['transactions']),1)

    def test_exact_rules_need_no_ollama_and_do_not_create_manual_reviews(self):
        self.publish([bill('a',remark='Lunch'),bill('b',remark='midnight meal')])
        enqueue(self.data,launch=False)
        with patch('qianji.auto_analysis.LocalModel',side_effect=AssertionError('no model call')):
            self.assertEqual(worker(self.data,once=True,model_factory=lambda *a,**k:self.fail('No model needed')),0)
        job=status(self.data)
        self.assertFalse(job['pending'])
        self.assertEqual(job['report']['coverage']['model_status_counts']['rule_covered'],2)
        self.assertIn('英文备注规则覆盖2笔',job['summary'])
        a=Analysis(self.data)
        with closing(a.connect()) as db:self.assertEqual(db.execute('SELECT count(*) FROM reviews').fetchone()[0],0)
        query=label_query(a.reader,job['source_ref'],job['report']['analysis_ref'],tags=['meal:午餐'])
        self.assertEqual(query['pagination']['total'],1)

    def test_input_reuse_incremental_resume_and_no_original_changes(self):
        self.publish([bill('a',remark='synthetic lunch'),bill('b',remark='synthetic lunch')])
        # Keep checking source files and immutable analytics versions; current receipts may advance.
        mutable={self.data/'analytics'/'exports'/'current.json', self.data/'analytics'/'last-run.json'}
        baseline={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in self.data.rglob('*') if p.is_file() and p not in mutable}
        model=FakeModel();enqueue(self.data,launch=False)
        self.assertEqual(worker(self.data,once=True,model_factory=self.factory(model)),0)
        self.assertEqual(model.calls,1)
        enqueue(self.data,launch=False)
        self.assertEqual(worker(self.data,once=True,model_factory=lambda *a,**k:self.fail('cached inputs need no model')),0)
        for p,meta in baseline.items():self.assertEqual((p.stat().st_size,p.stat().st_mtime_ns),meta)

    def test_model_outage_leaves_job_durable_and_does_not_exhaust_record_retries(self):
        self.publish([bill(remark='synthetic lunch')]);enqueue(self.data,launch=False)
        model=FakeModel('model_unavailable')
        self.assertEqual(worker(self.data,once=True,model_factory=self.factory(model)),2)
        self.assertEqual(status(self.data)['state'],'waiting_model')
        self.assertTrue(status(self.data)['pending'])
        with closing(Analysis(self.data).connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM failures').fetchone()[0],0)
        model.error=None
        self.assertEqual(worker(self.data,once=True,model_factory=self.factory(model)),0)
        self.assertFalse(status(self.data)['pending'])

    def test_manual_model_recipe_change_does_not_poison_automatic_config_cache(self):
        self.publish([bill(remark='synthetic lunch')]);model=FakeModel()
        process_available(self.data,model_factory=self.factory(model))
        other=FakeModel();other.recipe='different-manual-model'
        Analysis(self.data).process(model=other)
        created=[]
        def factory(*args,**kwargs):created.append(True);return model
        result=process_available(self.data,model_factory=factory)
        self.assertEqual(created,[True])
        doc=load_publication(Analysis(self.data).reader,result['source_snapshot_ref'],result['analysis_ref'])
        self.assertEqual(doc['model_recipe'],model.recipe)

    def test_process_lock_excludes_another_process_and_recovers_after_crash(self):
        script=('from pathlib import Path; from qianji.analysis_lock import analysis_lock; '
                'import sys,time; '
                'cm=analysis_lock(Path(sys.argv[1]));cm.__enter__();print("locked",flush=True);time.sleep(60)')
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(self.data)],
                               cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(ReadError,'analysis_busy'):
                with analysis_lock(self.data):pass
        finally:
            child.terminate();child.communicate(timeout=10)
        with analysis_lock(self.data):pass

    def test_validation_failures_are_bounded_and_visible_without_auto_acceptance(self):
        self.publish([bill(remark='synthetic lunch')]);enqueue(self.data,launch=False)
        model=FakeModel('invalid_prediction_evidence')
        self.assertEqual(worker(self.data,once=True,model_factory=self.factory(model)),0)
        self.assertEqual(model.calls,3)
        self.assertEqual(status(self.data)['state'],'completed_with_failures')
        enqueue(self.data,launch=False)
        worker(self.data,once=True,model_factory=self.factory(model))
        self.assertEqual(model.calls,3)

    def test_new_import_while_working_is_coalesced_and_not_marked_completed_early(self):
        self.publish([bill('a',remark='synthetic lunch')]);enqueue(self.data,launch=False)
        def update():
            self.publish([bill('b',remark='another synthetic lunch')]);enqueue(self.data,launch=False)
        model=FakeModel(before=update)
        self.assertEqual(worker(self.data,model_factory=self.factory(model)),0)
        job=status(self.data)
        self.assertEqual((job['requested'],job['completed']),(2,2))
        self.assertEqual(job['report']['coverage']['records'],2)

    def test_manual_correction_is_preserved(self):
        self.publish([bill(remark='synthetic lunch')]);a=Analysis(self.data)
        p=a.process();row=a.reader._open(p['source_snapshot_ref'])['datasets']['transactions'][0]
        a.review(row['id'],row['revision'],[{'dimension':'meal','label':'午餐','action':'reject'}],'synthetic rejection',p['store_revision'],'test-review')
        enqueue(self.data,launch=False)
        worker(self.data,once=True,model_factory=lambda *a,**k:self.fail('reviewed input must not infer'))
        report=status(self.data)['report']
        self.assertEqual(report['coverage']['model_status_counts']['reviewed'],1)
        with closing(a.connect()) as db:self.assertEqual(db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)

    def test_detached_worker_finishes_after_import_parent_exits(self):
        self.source.write_text(json.dumps([bill(remark='breakfast')]),encoding='utf-8')
        result=subprocess.run([sys.executable,'-B','-m','qianji','--data-dir',str(self.data),'import',str(self.source)],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            job=status(self.data)
            if job['state']=='completed':
                with analysis_lock(self.data,'worker',wait=True):pass
                break
            time.sleep(0.1)
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['report']['actual_model_calls'],0)


if __name__=='__main__':unittest.main()
