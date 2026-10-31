"""Process locks shared by manual and scheduled archive writers (macOS/Linux)."""
from contextlib import contextmanager
from functools import wraps
import fcntl
from pathlib import Path


class BusyError(ValueError):
    pass


@contextmanager
def exclusive(data_dir, name, *, wait=False):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (name + '.lock')).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise BusyError(f'{name} is already running; retry after it finishes.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def archive_writer(function):
    @wraps(function)
    def call(first, *args, **kwargs):
        directory = first['data_dir'] if isinstance(first, dict) else Path(first).parent
        with exclusive(directory, 'archive-write'):
            return function(first, *args, **kwargs)
    return call
