"""Bound scheduled maintenance even when a cloud-filesystem call blocks.

The supervisor does not inspect source directories or make network requests.
Only its newly created child's process group can receive termination signals.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from . import refresh
from .locking import BusyError

DEFAULT_TIMEOUTS = {'archive': 600, 'public': 3600}
STOP_GRACE_SECONDS = 5


class _Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _interrupt(signum, frame):
    raise _Interrupted(signum)


def _now():
    return datetime.now(timezone.utc)


def _data_dir(config_path):
    # Do not call library.load_config here: resolving cloud source paths belongs
    # in the supervised child, where an OS permission wait can be bounded.
    config = json.loads(config_path.read_text())
    data = Path(config.get('data_dir', '.local')).expanduser()
    return data if data.is_absolute() else config_path.parent / data


def _stop(child):
    """Reap our child before reporting timeout state or releasing supervision."""
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # A kernel-level uninterruptible wait cannot be solved by an unbounded
    # supervisor wait. If even SIGKILL cannot finish, do not claim locks cleared.
    child.wait(timeout=STOP_GRACE_SECONDS)


def _mark_timeout(data_dir, kind, started, observed, timeout):
    """Fail only the exact still-running attempt observed before termination."""
    relevant = ('archive',) if kind == 'archive' else refresh.PUBLIC_LAYERS
    changed = []
    with refresh.exclusive(data_dir, 'refresh'):
        state = refresh._read(data_dir)
        for name in relevant:
            current = state.get('layers', {}).get(name, {})
            before = observed.get('layers', {}).get(name, {})
            if current.get('status') != 'running' or before.get('status') != 'running':
                continue
            attempted = current.get('last_attempt')
            if not attempted or attempted != before.get('last_attempt'):
                continue
            try:
                attempted_at = datetime.fromisoformat(attempted)
                ours = attempted_at >= started
            except (TypeError, ValueError):
                ours = False
            if not ours:
                continue
            hint = ('Check the macOS permission prompt for Google Drive and confirm the source folders are available.'
                    if kind == 'archive' else 'Check network access and availability of the public source APIs.')
            current.update(status='failed', finished_at=_now().isoformat(),
                           error=f'Maintenance timed out after {timeout} seconds; its child process was stopped. {hint}')
            changed.append(name)
        if changed:
            refresh._save(data_dir, state)
    return changed


def _wait_for_child(child, data_dir, kind, started, timeout):
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            observed = refresh._read(data_dir)
        except (OSError, ValueError):
            observed = {}
        try:
            _stop(child)
        except subprocess.TimeoutExpired:
            print(json.dumps({'status': 'failed', 'error': 'Maintenance child did not exit after SIGKILL; '
                              'OS filesystem access may still be blocked. Locks are not confirmed released.'}),
                  file=sys.stderr, flush=True)
            return 124
        try:
            changed = _mark_timeout(data_dir, kind, started, observed, timeout)
            message = {'status': 'timed_out', 'kind': kind, 'timeout_seconds': timeout, 'failed_layers': changed}
        except BusyError:
            # Another writer acquired the released lock. Never stop it or
            # overwrite its state merely to publish our timeout receipt.
            message = {'status': 'timed_out', 'kind': kind, 'timeout_seconds': timeout,
                       'state_update': 'skipped_new_writer_holds_lock'}
        print(json.dumps(message), file=sys.stderr, flush=True)
        return 124


def run(config_path, kind, timeout=None):
    """Return child exit status, 124 on timeout, or 128+signal on interruption."""
    config_path = Path(config_path).expanduser()
    if not config_path.is_absolute():
        raise ValueError('The supervisor requires an absolute --config path.')
    if kind not in DEFAULT_TIMEOUTS:
        raise ValueError('Maintenance kind must be archive or public.')
    timeout = DEFAULT_TIMEOUTS[kind] if timeout is None else timeout
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError('Maintenance timeout must be an integer from 1 to 86400 seconds.')
    data_dir = _data_dir(config_path)
    args = ['refresh-watch'] if kind == 'archive' else ['refresh', '--due']
    command = [sys.executable, '-m', 'grantlib', '--config', str(config_path), *args]
    started = _now()
    child = subprocess.Popen(command, start_new_session=True, close_fds=True)
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, _interrupt)
        try:
            return _wait_for_child(child, data_dir, kind, started, timeout)
        except BaseException as error:
            # launchctl bootout sends SIGTERM to this supervisor. The child
            # has its own session, so stop it explicitly before exiting.
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            _stop(child)
            if isinstance(error, _Interrupted):
                return 128 + error.signum
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Supervise bounded grant maintenance')
    parser.add_argument('--config', required=True)
    parser.add_argument('--kind', required=True, choices=tuple(DEFAULT_TIMEOUTS))
    parser.add_argument('--timeout', type=int)
    args = parser.parse_args(argv)
    try:
        return run(args.config, args.kind, args.timeout)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    sys.exit(main())
