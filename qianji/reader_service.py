"""Public, owner-scoped read-only HTTP service. See READER_SERVICE.md."""
import argparse
from pathlib import Path
import socket
import sqlite3
import sys
import threading
from http.server import ThreadingHTTPServer

def readonly(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


class ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if sys.platform == 'win32':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def qianji_server(root, port, instance):
    from qianji.api import Handler
    from qianji.store import Store

    class ReadOnlyStore(Store):
        def __init__(self):
            # Reuse the provider's snapshot logic without its schema/init writes.
            self.root = root / 'data'
            self.database = (self.root / 'qianji.sqlite3').resolve(strict=True)

        def connect(self):
            return readonly(self.database)

    class IdentifiedHandler(Handler):
        timeout = 5

        def _json(self, status, payload):
            if payload.get('app') == 'qianji-local':
                payload = {**payload, 'instance_id': instance, 'service_contract': 'local-source-service/1'}
            super()._json(status, payload)

    store = ReadOnlyStore()
    server = ExclusiveServer(('127.0.0.1', port), IdentifiedHandler)
    server.store = store
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--instance-id', default='manual')
    parser.add_argument('--owner-stdin', action='store_true', help='Exit when the launching owner closes stdin')
    args = parser.parse_args()
    server = qianji_server(args.root, args.port, args.instance_id)
    if args.owner_stdin:
        def owner_closed():
            sys.stdin.buffer.read()
            server.shutdown()
        threading.Thread(target=owner_closed, daemon=True).start()
    with server:
        try:
            server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
