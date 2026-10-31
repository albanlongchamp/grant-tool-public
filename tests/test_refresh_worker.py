from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from grantlib import refresh, refresh_worker
from grantlib.locking import exclusive


class RefreshWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'data_dir': 'data', 'sources': [{'path': '/synthetic/cloud'}]}))
        self.now = datetime(2026, 10, 31, tzinfo=timezone.utc)
        self.started = self.now - timedelta(seconds=1)
        self.old_success = (self.now - timedelta(days=1)).isoformat()
        self.layer = {'status': 'running', 'last_attempt': self.now.isoformat(),
                      'last_success': self.old_success, 'checkpoint': {'cursor': 'preserved'},
                      'manifest': {'quick': 'kept'}}
        refresh._save(self.data, {'version': 1, 'layers': {'archive': dict(self.layer)}})
        self.child = Mock(pid=123456)
        self.child.poll.return_value = None

    def execute(self, kind='archive', **kwargs):
        with patch.object(refresh_worker.subprocess, 'Popen', return_value=self.child) as popen, \
                patch.object(refresh_worker.os, 'killpg') as kill, \
                patch.object(refresh_worker, '_now', return_value=self.started), redirect_stderr(io.StringIO()):
            code = refresh_worker.run(self.config, kind, **kwargs)
        return code, popen, kill

    def test_success_preserves_state_and_starts_isolated_child_without_shell(self):
        before = refresh._read(self.data)
        self.child.wait.return_value = 0
        code, popen, kill = self.execute()
        self.assertEqual(code, 0)
        kill.assert_not_called()
        args, options = popen.call_args
        self.assertEqual(args[0][-1], 'refresh-watch')
        self.assertEqual(args[0][-2], str(self.config))
        self.assertTrue(options['start_new_session'])
        self.assertTrue(options['close_fds'])
        self.assertNotIn('shell', options)
        self.child.wait.assert_called_once_with(timeout=600)
        self.assertEqual(refresh._read(self.data), before)

    def test_public_child_has_due_flag_and_preserves_exit_code(self):
        self.child.wait.return_value = 7
        code, popen, kill = self.execute(kind='public')
        self.assertEqual(code, 7)
        self.assertEqual(popen.call_args.args[0][-2:], ['refresh', '--due'])
        self.child.wait.assert_called_once_with(timeout=3600)
        kill.assert_not_called()

    def test_timeout_terminates_owned_group_and_preserves_success_checkpoint_and_index(self):
        database = self.data / 'library.sqlite3'
        database.write_bytes(b'synthetic usable index')
        self.child.wait.side_effect = [subprocess.TimeoutExpired('child', 10), 0]
        code, _, kill = self.execute(timeout=10)
        self.assertEqual(code, 124)
        kill.assert_called_once_with(self.child.pid, signal.SIGTERM)
        layer = refresh._read(self.data)['layers']['archive']
        self.assertEqual(layer['status'], 'failed')
        self.assertEqual(layer['last_success'], self.old_success)
        self.assertEqual(layer['checkpoint'], {'cursor': 'preserved'})
        self.assertEqual(layer['manifest'], {'quick': 'kept'})
        self.assertIn('Google Drive', layer['error'])
        self.assertEqual(database.read_bytes(), b'synthetic usable index')

    def test_stubborn_child_gets_kill_only_for_its_own_group_and_is_reaped(self):
        self.child.wait.side_effect = [subprocess.TimeoutExpired('child', 10),
                                      subprocess.TimeoutExpired('child', 5), -9]
        code, _, kill = self.execute(timeout=10)
        self.assertEqual(code, 124)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(self.child.pid, signal.SIGTERM), (self.child.pid, signal.SIGKILL)])
        self.assertEqual(self.child.wait.call_count, 3)

    def test_unreapable_child_does_not_claim_state_or_lock_recovery(self):
        before = refresh._read(self.data)
        self.child.wait.side_effect = subprocess.TimeoutExpired('child', 5)
        code, _, _ = self.execute(timeout=10)
        self.assertEqual(code, 124)
        self.assertEqual(refresh._read(self.data), before)

    def test_older_or_completed_attempt_is_not_marked_failed(self):
        for updates in [{'last_attempt': (self.started - timedelta(seconds=1)).isoformat()}, {'status': 'complete'}]:
            layer = dict(self.layer, **updates)
            refresh._save(self.data, {'version': 1, 'layers': {'archive': layer}})
            changed = refresh_worker._mark_timeout(self.data, 'archive', self.started,
                                                   refresh._read(self.data), 10)
            self.assertEqual(changed, [])
            self.assertEqual(refresh._read(self.data)['layers']['archive'], layer)

    def test_newer_job_cannot_be_marked_as_our_timed_out_job(self):
        observed = refresh._read(self.data)
        newer = dict(self.layer, last_attempt=(self.now + timedelta(seconds=1)).isoformat())
        refresh._save(self.data, {'version': 1, 'layers': {'archive': newer}})
        self.assertEqual(refresh_worker._mark_timeout(self.data, 'archive', self.started, observed, 10), [])
        self.assertEqual(refresh._read(self.data)['layers']['archive'], newer)

    def test_another_writer_is_neither_killed_nor_overwritten(self):
        before = refresh._read(self.data)
        self.child.wait.side_effect = [subprocess.TimeoutExpired('child', 10), 0]
        with exclusive(self.data, 'refresh'):
            code, _, kill = self.execute(timeout=10)
        self.assertEqual(code, 124)
        kill.assert_called_once_with(self.child.pid, signal.SIGTERM)
        self.assertEqual(refresh._read(self.data), before)

    def test_public_timeout_only_marks_running_relevant_layer(self):
        layers = {'archive': dict(self.layer), 'nih': dict(self.layer),
                  'publications': dict(self.layer, status='complete')}
        refresh._save(self.data, {'version': 1, 'layers': layers})
        self.child.wait.side_effect = [subprocess.TimeoutExpired('child', 10), 0]
        self.execute(kind='public', timeout=10)
        result = refresh._read(self.data)['layers']
        self.assertEqual(result['archive']['status'], 'running')
        self.assertEqual(result['publications']['status'], 'complete')
        self.assertEqual(result['nih']['status'], 'failed')
        self.assertIn('network', result['nih']['error'])

    def test_validation_happens_before_spawning(self):
        with patch.object(refresh_worker.subprocess, 'Popen') as popen:
            for args in [('relative.json', 'archive', 1), (self.config, 'invalid', 1),
                         (self.config, 'archive', 0), (self.config, 'archive', True)]:
                with self.assertRaises(ValueError):
                    refresh_worker.run(*args)
            popen.assert_not_called()

    def test_launchctl_interrupt_stops_owned_child_and_restores_handlers(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            before = refresh._read(self.data)
            original = signal.getsignal(signum)
            calls = 0
            def interrupted_wait(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    signal.getsignal(signum)(signum, None)
                return -15
            self.child.wait.side_effect = interrupted_wait
            code, _, kill = self.execute()
            self.assertEqual(code, 128 + signum)
            kill.assert_called_once_with(self.child.pid, signal.SIGTERM)
            self.assertEqual(signal.getsignal(signum), original)
            self.assertEqual(refresh._read(self.data), before)


if __name__ == '__main__':
    unittest.main()
