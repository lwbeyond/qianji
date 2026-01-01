"""Process locks released by the OS on exit, including after a crash."""
from contextlib import contextmanager
from pathlib import Path
import os
import threading
import time

from .mcp_reader import ReadError

_owned = threading.local()


@contextmanager
def analysis_lock(data_dir, name='processing', wait=False, *, subdirectory='analysis'):
    path = Path(data_dir).resolve() / subdirectory / (name + '.lock')
    held = getattr(_owned, 'paths', set())
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not wait:
                    raise ReadError('analysis_busy: 已有分类任务正在处理，稍后自动继续') from None
                time.sleep(0.2)
        _owned.paths = held | {path}
        try:
            yield
        finally:
            _owned.paths = held
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
