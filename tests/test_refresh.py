"""Synthetic maintenance tests: failures, concurrency, stable inputs and history."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import plistlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from grantlib import library, refresh
from grantlib.locking import exclusive, BusyError
from grantlib.refresh_cli import install


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'sources'
        self.source.mkdir()
        self.file = self.source / 'aims.txt'
        self.file.write_text('We will study invented seedling irrigation.')
        self.data = self.root / 'data'
        self.config = {'data_dir': str(self.data), 'workers': 1,
                       'sources': [{'name': 'fictional', 'path': str(self.source)}],
                       'refresh': {'enabled': True, 'canonical_host': 'test-host'}}
        self.now = datetime.now(timezone.utc)

    def test_debounce_updates_index_and_keeps_snapshot_bytes(self):
        history = self.data / 'studio.sqlite3'
        self.data.mkdir()
        history.write_bytes(b'invented immutable draft source snapshot')
        before = history.read_bytes()
        first = refresh.watch(self.config, now=self.now)
        self.assertEqual(first['status'], 'pending')
        self.assertFalse((self.data / 'library.sqlite3').exists())
        done = refresh.watch(self.config, now=self.now + timedelta(seconds=61))
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(len(library.search(self.data / 'library.sqlite3', 'irrigation')), 1)
        built = done['index_built_at']
        unchanged = refresh.watch(self.config, now=self.now + timedelta(seconds=120))
        self.assertFalse(unchanged['changed'])
        self.assertEqual(unchanged['index_built_at'], built)
        self.file.write_text('We will study fictional chloroplasts.')
        self.assertEqual(refresh.watch(self.config, now=self.now + timedelta(seconds=130))['status'], 'pending')
        changed = refresh.watch(self.config, now=self.now + timedelta(seconds=200))
        self.assertTrue(changed['changed'])
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(len(library.search(self.data / 'library.sqlite3', 'chloroplasts')), 1)

    def test_missing_drive_preserves_index_and_last_success(self):
        done = refresh.watch(self.config, force=True)
        before = (self.data / 'library.sqlite3').read_bytes()
        self.config['sources'][0]['path'] = str(self.root / 'unmounted')
        failed = refresh.watch(self.config)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['last_success'], done['last_success'])
        self.assertEqual((self.data / 'library.sqlite3').read_bytes(), before)
        self.assertTrue(refresh.status(self.data)['layers']['archive']['stale'])

    def test_racing_change_rejects_new_index_before_publish(self):
        refresh.watch(self.config, force=True)
        before = (self.data / 'library.sqlite3').read_bytes()
        original = library._extract_cached
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.file.write_text('A file changed during extraction.')
            return result
        with patch('grantlib.library._extract_cached', side_effect=race):
            failed = refresh.watch(self.config, force=True)
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('changed during indexing', failed['error'])
        self.assertEqual((self.data / 'library.sqlite3').read_bytes(), before)

    def test_empty_mounted_drive_preserves_previous_index(self):
        refresh.watch(self.config, force=True)
        before = (self.data / 'library.sqlite3').read_bytes()
        self.file.unlink()
        failed = refresh.watch(self.config, force=True)
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('became empty', failed['error'])
        self.assertEqual((self.data / 'library.sqlite3').read_bytes(), before)

    def test_new_extraction_error_preserves_previous_index(self):
        refresh.watch(self.config, force=True)
        before = (self.data / 'library.sqlite3').read_bytes()
        with patch('grantlib.library._extract_cached', side_effect=OSError('synthetic read failure')):
            failed = refresh.watch(self.config, force=True)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual((self.data / 'library.sqlite3').read_bytes(), before)

    def test_change_just_after_publish_is_detected_on_next_tick(self):
        original = library.ingest
        def after_publish(*args, **kwargs):
            result = original(*args, **kwargs)
            self.file.write_text('We will study fictional chloroplasts.')
            return result
        with patch('grantlib.library.ingest', side_effect=after_publish):
            self.assertEqual(refresh.watch(self.config, force=True)['status'], 'complete')
        self.assertEqual(refresh.watch(self.config)['status'], 'pending')
        self.assertEqual(refresh.watch(self.config, now=self.now + timedelta(seconds=100))['status'], 'complete')
        self.assertEqual(len(library.search(self.data / 'library.sqlite3', 'chloroplasts')), 1)

    def test_daily_full_hash_detects_content_even_when_quick_inventory_is_unchanged(self):
        refresh.watch(self.config, force=True, now=self.now)
        state = refresh._read(self.data)
        quick = state['layers']['archive']['manifest']['quick']
        self.file.write_text('We will study fictional replacement tissue.')
        original = refresh._inventory
        def masked(*args, **kwargs):
            result = original(*args, **kwargs)
            result['quick'] = quick
            return result
        with patch('grantlib.refresh._inventory', side_effect=masked):
            self.assertEqual(refresh.watch(self.config, now=self.now + timedelta(days=1, seconds=1))['status'], 'pending')
            done = refresh.watch(self.config, now=self.now + timedelta(days=1, seconds=70))
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(len(library.search(self.data / 'library.sqlite3', 'replacement')), 1)

    def test_archive_and_curation_writers_share_lock(self):
        library.ingest(self.config)
        with exclusive(self.data, 'archive-write'):
            with self.assertRaises(BusyError):
                library.ingest(self.config)
            with self.assertRaises(BusyError):
                library.curate_unit(self.data / 'library.sqlite3', 'fake', 'proposed', 'test')
        with exclusive(self.data, 'refresh'):
            with self.assertRaises(BusyError):
                refresh.watch(self.config)

    def test_partial_failure_cannot_advance_success_and_other_layers_continue(self):
        with patch('grantlib.publication_refresh.refresh', return_value={'status': 'complete', 'checkpoint': {'date': 'old'}}):
            refresh.run(self.config, ['publications'])
        old = refresh.status(self.data)['layers']['publications']['last_success']
        with patch('grantlib.publication_refresh.refresh', return_value={'status': 'partial', 'counts': {'received': 2}}), \
             patch('grantlib.nih_refresh.refresh', side_effect=OSError('synthetic offline')), \
             patch('grantlib.guidance_refresh.refresh', return_value={'status': 'complete'}):
            result = refresh.run(self.config)
        self.assertEqual(result['publications']['last_success'], old)
        self.assertEqual(result['nih']['status'], 'failed')
        self.assertEqual(result['guidance']['status'], 'complete')
        self.assertEqual(refresh._read(self.data)['layers']['publications']['checkpoint'], {'date': 'old'})
        status = refresh.status(self.data)
        self.assertTrue(status['layers']['publications']['stale'])
        self.assertFalse(status['layers']['guidance']['stale'])

    def test_due_skips_successful_layer_and_retries_failure(self):
        with patch('grantlib.publication_refresh.refresh', return_value={'status': 'complete'}) as adapter:
            refresh.run(self.config, ['publications'])
            self.assertEqual(refresh.run(self.config, ['publications'], due=True)['publications']['status'], 'not_due')
            self.assertEqual(adapter.call_count, 1)
        self.assertTrue(refresh.status(self.data, now=self.now + timedelta(days=9))['layers']['publications']['stale'])

    def test_public_refresh_waits_for_archive_instead_of_skipping_daily_check(self):
        entered = threading.Event()
        def run():
            entered.set()
            return refresh.run(self.config, ['publications'])
        with patch('grantlib.publication_refresh.refresh', return_value={'status': 'complete'}) as adapter:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with exclusive(self.data, 'refresh'):
                    future = pool.submit(run)
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(future.done())
                    adapter.assert_not_called()
                self.assertEqual(future.result(timeout=2)['publications']['status'], 'complete')

    def test_launchd_uses_absolute_config_and_bounded_check_schedule(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config))
        with patch('grantlib.refresh_cli.sys.platform', 'darwin'):
            result = install(self.config, path, home=self.root / 'home', interpreter='/fictional/python')
        jobs = {item['label'].split('.')[-1]: plistlib.loads(Path(item['path']).read_bytes()) for item in result['jobs']}
        self.assertEqual(jobs['archive']['StartInterval'], 300)
        self.assertEqual(jobs['public']['StartCalendarInterval'], {'Hour': 3, 'Minute': 15})
        self.assertEqual(jobs['public']['ProgramArguments'][-2:], ['--kind', 'public'])
        self.assertIn('grantlib.refresh_worker', jobs['archive']['ProgramArguments'])
        self.assertIn(str(path.resolve()), jobs['public']['ProgramArguments'])
        self.assertEqual(jobs['archive']['Umask'], 0o077)
        self.assertIn('/opt/homebrew/bin', jobs['archive']['EnvironmentVariables']['PATH'])


if __name__ == '__main__':
    unittest.main()
