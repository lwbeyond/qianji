from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.request import ProxyHandler, Request, build_opener

from qianji.reader_service import qianji_server, readonly
from qianji.store import Store


class ReaderServiceTests(unittest.TestCase):
    def test_source_owned_reader_uses_existing_snapshot_without_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'data')
            expected = store.snapshot()
            original = hashlib.sha256(store.database.read_bytes()).hexdigest()
            server = qianji_server(root, 0, 'test-owner')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            http = build_opener(ProxyHandler({}))
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with http.open(base + '/health') as response:
                    health = json.load(response)
                self.assertEqual(health['instance_id'], 'test-owner')
                self.assertEqual(health['service_contract'], 'local-source-service/1')
                with http.open(base + '/api/v1/snapshot') as response:
                    compatible = json.load(response)['snapshot']
                    self.assertEqual(compatible['contract_version'], '1.0')
                    self.assertNotIn('processing', compatible)
                    self.assertEqual(compatible['generation'], expected['snapshot']['generation'])
                with http.open(Request(base + '/api/v1/snapshot',
                        headers={'X-Qianji-Contract-Version': '1.1'})) as response:
                    self.assertEqual(json.load(response)['snapshot'], expected['snapshot'])
                with closing(server.store.connect()) as connection:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute('DELETE FROM metadata')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertEqual(hashlib.sha256(store.database.read_bytes()).hexdigest(), original)

    def test_missing_database_is_not_created(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                qianji_server(Path(tmp), 0, 'test-owner')
            self.assertFalse((Path(tmp) / 'data').exists())


if __name__ == '__main__':
    unittest.main()
