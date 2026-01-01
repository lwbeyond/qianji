"""Synthetic classifier input reuse; no actual Ollama or personal records."""
import unittest
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from qianji.batch_analysis import ExactInputCache, run
from qianji.analysis import Analysis
from qianji.store import Store
from qianji.mcp_reader import ReadError
from test_mcp import bill


class Model:
    model='synthetic';model_digest='a'*64;recipe='synthetic-v1'
    def __init__(self):self.calls=0;self.fail=False
    def predict(self,row):
        self.calls+=1
        if self.fail:raise ReadError('invalid_prediction: synthetic failure')
        return [{'dimension':'meal','label':'午餐','field':'remark','evidence':'lunch'}]


class CacheTests(unittest.TestCase):
    def test_identical_text_reuses_model_but_different_text_does_not(self):
        inner=Model();m=ExactInputCache(inner);r={'id':'synthetic-a','kind':'expense','category':'三餐','remark':'lunch'}
        first=m.predict(r);first[0]['label']='changed outside cache'
        second=m.predict({**r,'id':'synthetic-b'})
        self.assertEqual(inner.calls,1);self.assertEqual(second[0]['label'],'午餐')
        m.predict({**r,'remark':'synthetic lunch'});self.assertEqual(inner.calls,2)

    def test_failure_reused_only_within_one_pass(self):
        inner=Model();inner.fail=True;m=ExactInputCache(inner)
        r={'kind':'expense','category':'三餐','remark':'lunch'}
        for _ in range(2):
            with self.assertRaises(ReadError):m.predict(r)
        self.assertEqual(inner.calls,1)
        m.errors.clear()
        with self.assertRaises(ReadError):m.predict(r)
        self.assertEqual(inner.calls,2)


class FullRunTests(unittest.TestCase):
    def source(self,count):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);source=root/'synthetic.json'
        source.write_text(json.dumps([bill('synthetic-'+str(i),remark='lunch at a synthetic venue') for i in range(count)]),encoding='utf-8')
        data=root/'data';store=Store(data);store.import_file(source);store.export()
        return data

    def test_full_success_reuses_input_but_preserves_individual_records_and_resume(self):
        root=self.source(2);model=Model()
        with patch('qianji.batch_analysis.LocalModel',return_value=model),redirect_stdout(io.StringIO()):
            self.assertEqual(run(root,'synthetic'),0)
            self.assertEqual(run(root,'synthetic'),0)
        self.assertEqual(model.calls,1)
        with closing(Analysis(root).connect()) as db:
            self.assertEqual(db.execute("SELECT count(DISTINCT record_id) FROM evaluations WHERE method='model'").fetchone()[0],2)
        status=json.loads((root/'analysis/full-run-progress.json').read_text())
        self.assertEqual(status['unresolved_records'],0)

    def test_failure_retries_once_per_record_per_pass_across_batch_boundary(self):
        root=self.source(201);model=Model();model.fail=True
        with patch('qianji.batch_analysis.LocalModel',return_value=model),redirect_stdout(io.StringIO()):
            self.assertEqual(run(root,'synthetic'),2)
        self.assertEqual(model.calls,3)
        with closing(Analysis(root).connect()) as db:
            counts=[r[0] for r in db.execute('SELECT count(*) FROM failures GROUP BY record_id')]
        self.assertEqual(counts,[3]*201)
        with patch('qianji.batch_analysis.LocalModel',return_value=model),redirect_stdout(io.StringIO()):
            self.assertEqual(run(root,'synthetic'),2)
        self.assertEqual(model.calls,3)
        model.fail=False
        with patch('qianji.batch_analysis.LocalModel',return_value=model),redirect_stdout(io.StringIO()):
            self.assertEqual(run(root,'synthetic',retry_failed=True),0)
        self.assertEqual(model.calls,4)

    def test_invalid_batch_selection_does_not_write(self):
        root=self.source(2);a=Analysis(root)
        with self.assertRaisesRegex(ReadError,'invalid_selection'):
            a.process(record_ids=['not-in-source'])
        self.assertFalse(a.database.exists())


if __name__=='__main__':unittest.main()
