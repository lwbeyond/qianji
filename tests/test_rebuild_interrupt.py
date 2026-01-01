"""Synthetic crash boundaries: SQLite decides whether a rebuild was applied."""

from contextlib import closing
import json
from unittest.mock import patch

from qianji import rebuild as rebuild_module
from qianji.mcp_reader import Reader
from qianji.normalize import DataError
from qianji.store import Store
from test_qianji import StoreCase, bill
from test_rebuild import normalizer_upgrade


class RebuildInterruptTests(StoreCase):
    def prepare(self):
        self.store.import_file(self.source([bill()]))
        self.store.export()
        before = self.store.snapshot()
        reader = Reader(self.store.root)
        return before, reader.snapshot()['snapshot_ref']

    def metadata(self):
        with closing(self.store.connect()) as db:
            return dict(db.execute('SELECT key, value FROM metadata'))

    def business_rows(self):
        with closing(self.store.connect()) as db:
            return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1, 2')]
                    for table in ('records', 'revisions', 'imports', 'ledgers')}

    def last_receipt(self):
        return json.loads((self.store.root / 'processing' / 'last-run.json').read_bytes())

    def interrupt_after_commit(self, expected_plan, error):
        write = rebuild_module.write_report

        def fail_pending_receipt(store, report):
            if report['state'] == 'publication_pending':
                raise error('synthetic failure before committed receipt is written')
            return write(store, report)

        with patch('qianji.rebuild.write_report', side_effect=fail_pending_receipt):
            with self.assertRaises(error):
                self.store.rebuild(apply=True, expected_plan=expected_plan)

    def assert_pending_and_recover(self, before, old_ref):
        # A fresh Store must reconstruct the state from SQLite, despite the last
        # durable receipt still saying only candidate_prepared/applied=False.
        self.store = Store(self.store.root)
        pending = self.metadata()['pending_rebuild']
        receipt = json.loads((self.store.root / 'processing' / 'runs' / (pending + '.json')).read_bytes())
        self.assertEqual((receipt['state'], receipt['applied']), ('candidate_prepared', False))
        status = self.store.status()
        self.assertTrue(status['publication_pending'])
        self.assertEqual((status['rebuild']['run_id'], status['rebuild']['state'], status['rebuild']['applied']),
                         (pending, 'publication_pending', True))
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(Reader(self.store.root).snapshot()['snapshot_ref'], old_ref)
        result = self.store.export()
        self.assertNotIn('pending_rebuild', self.metadata())
        self.assertNotEqual(result['generation'], before['snapshot']['generation'])
        self.assertEqual(self.store.snapshot()['datasets']['transactions'][0]['category'],
                         'reprocessed/测试分类')
        self.assertEqual(Reader(self.store.root).read(old_ref)['data'], before['datasets']['transactions'])
        status = self.store.status()
        self.assertFalse(status['publication_pending'])
        self.assertEqual((status['rebuild']['run_id'], status['rebuild']['state'], status['rebuild']['applied']),
                         (pending, 'completed', True))
        self.assertTrue(self.store.verify()['ok'])

    def test_keyboard_interrupt_before_commit_rolls_back_and_reports_interrupted(self):
        before, old_ref = self.prepare()
        original_rows = self.business_rows()
        current = self.store.root / 'exports' / 'current.json'
        original_bytes = current.read_bytes()
        write = rebuild_module.write_report

        def interrupt_prepared(store, report):
            write(store, report)
            if report['state'] == 'candidate_prepared':
                raise KeyboardInterrupt('synthetic interruption before database commit')

        with normalizer_upgrade():
            plan = self.store.rebuild()
            with patch('qianji.rebuild.write_report', side_effect=interrupt_prepared):
                with self.assertRaises(KeyboardInterrupt):
                    self.store.rebuild(apply=True, expected_plan=plan['plan_ref'])
            self.store = Store(self.store.root)
            self.assertNotIn('pending_rebuild', self.metadata())
            self.assertEqual(self.business_rows(), original_rows)
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual(current.read_bytes(), original_bytes)
            self.assertEqual(Reader(self.store.root).snapshot()['snapshot_ref'], old_ref)
            status = self.store.status()
            self.assertFalse(status['publication_pending'])
            self.assertEqual((status['rebuild']['state'], status['rebuild']['applied']), ('interrupted', False))
            # The interrupted receipt cannot block a new preview or successful apply.
            retry = self.store.rebuild()
            completed = self.store.rebuild(apply=True, expected_plan=retry['plan_ref'])
            self.assertEqual(completed['state'], 'completed')

    def test_keyboard_interrupt_after_commit_is_pending_and_export_recovers(self):
        before, old_ref = self.prepare()
        with normalizer_upgrade():
            plan = self.store.rebuild()
            self.interrupt_after_commit(plan['plan_ref'], KeyboardInterrupt)
            self.assert_pending_and_recover(before, old_ref)

    def test_receipt_write_failure_after_commit_is_pending_and_export_recovers(self):
        before, old_ref = self.prepare()
        with normalizer_upgrade():
            plan = self.store.rebuild()
            self.interrupt_after_commit(plan['plan_ref'], OSError)
            self.assert_pending_and_recover(before, old_ref)

    def test_failed_other_preview_cannot_hide_pending_run(self):
        before, old_ref = self.prepare()
        with normalizer_upgrade():
            plan = self.store.rebuild()
            self.interrupt_after_commit(plan['plan_ref'], KeyboardInterrupt)
            pending = self.metadata()['pending_rebuild']
            with self.assertRaisesRegex(DataError, 'publication_pending'):
                self.store.rebuild()
            failed = self.last_receipt()
            self.assertEqual((failed['state'], failed['applied']), ('failed', False))
            self.assertNotEqual(failed['run_id'], pending)
            status = self.store.status()
            self.assertEqual((status['rebuild']['run_id'], status['rebuild']['state'], status['rebuild']['applied']),
                             (pending, 'publication_pending', True))
            self.assert_pending_and_recover(before, old_ref)
