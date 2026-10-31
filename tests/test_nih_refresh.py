from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from grantlib import nih, nih_refresh


def record(identifier=1001, text='We propose a synthetic seedling study.'):
    return {'appl_id': identifier, 'fiscal_year': 2026,
            'project_title': 'Synthetic seedling research', 'abstract_text': text,
            'core_project_num': 'R01GM000001', 'activity_code': 'R01'}


def response(rows, total=None, search_id=None):
    return {'meta': {'total': len(rows) if total is None else total, 'search_id': search_id}, 'results': rows}


class NIHRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = {'queries': ['synthetic seedling'], 'fiscal_years': [2026],
                         'max_records': 5, 'max_reconcile_records': 100}
        self.day = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def refresh(self, responses, checkpoint=None, day=None, settings=None):
        with patch.object(nih_refresh, '_now', return_value=day or self.day), \
                patch.object(nih_refresh, '_request_json', side_effect=responses) as request:
            result = nih_refresh.refresh(self.root, settings or self.settings, checkpoint)
        return result, [c.args[0] for c in request.call_args_list]

    def scalar(self, sql):
        with closing(sqlite3.connect(self.root / 'nih.sqlite3')) as conn:
            return conn.execute(sql).fetchone()[0]

    def test_baseline_then_incremental_retains_versions_without_reimporting_unchanged(self):
        baseline, calls = self.refresh([response([record()])])
        self.assertEqual(baseline['status'], 'complete')
        self.assertEqual(baseline['counts']['new'], 1)
        self.assertNotIn('date_added', calls[0]['criteria'])
        sources = self.scalar('SELECT count(*) FROM nih_sources')
        next_day = datetime(2026, 10, 8, tzinfo=timezone.utc)
        result, calls = self.refresh([response([record()]), response([record()])],
                                     baseline['checkpoint'], next_day)
        self.assertEqual(calls[0]['criteria']['date_added'], {'from_date': '2026-09-24', 'to_date': '2026-10-08'})
        self.assertEqual(calls[1]['criteria'], {'appl_ids': [1001]})
        self.assertEqual(result['counts']['unchanged'], 1)
        self.assertEqual(result['counts']['changed'], 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_record_versions'), 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_sources'), sources)
        hits = nih_refresh.annotate_results(self.root, nih.search(self.root / 'nih.sqlite3', 'seedling')['results'])
        self.assertEqual(hits[0]['reporter_presence'], 'present')
        self.assertTrue(hits[0]['observed_at'].startswith('2026-10-08'))

    def test_correction_keeps_original_version_and_explicit_blank_abstract(self):
        first, _ = self.refresh([response([record()])])
        changed = record(text='')
        result, _ = self.refresh([response([]), response([changed])], first['checkpoint'])
        self.assertEqual(result['counts']['changed'], 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_record_versions'), 2)
        self.assertEqual(self.scalar('SELECT abstract FROM nih_records'), '')
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_record_versions WHERE record_json LIKE \'%We propose%\''), 1)

    def test_disappearing_by_id_record_is_preserved_and_flagged(self):
        first, _ = self.refresh([response([record()])])
        result, _ = self.refresh([response([]), response([])], first['checkpoint'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts']['missing_by_id'], 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_records'), 1)
        hits = nih_refresh.annotate_results(self.root, nih.search(self.root / 'nih.sqlite3', 'seedling')['results'])
        self.assertEqual(hits[0]['reporter_presence'], 'not_returned_by_id')

    def test_monthly_topic_reconciliation_and_changed_scope_force_full_search(self):
        first, _ = self.refresh([response([record()])])
        _, calls = self.refresh([response([]), response([record()])], first['checkpoint'],
                                datetime(2026, 10, 30, tzinfo=timezone.utc))
        self.assertNotIn('date_added', calls[0]['criteria'])
        settings = dict(self.settings, queries=['different public topic'])
        _, calls = self.refresh([response([]), response([record()])], first['checkpoint'], settings=settings)
        self.assertNotIn('date_added', calls[0]['criteria'])

    def test_cap_retains_partial_records_without_advancing_successful_checkpoint(self):
        first, _ = self.refresh([response([])])
        settings = dict(self.settings, max_records=1)
        result, _ = self.refresh([response([record()], total=2)], first['checkpoint'], settings=settings)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['checkpoint'], first['checkpoint'])
        self.assertIn('requested_cap_reached', result['warnings'])
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_records'), 1)

    def test_transport_failure_after_completed_query_keeps_data_but_not_checkpoint(self):
        settings = dict(self.settings, queries=['synthetic seedling', 'second topic'])
        result, _ = self.refresh([response([record()]), TimeoutError('synthetic timeout')], settings=settings)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['checkpoint'], {})
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_records'), 1)
        self.assertIn('request_failed:TimeoutError', result['warnings'])

    def test_total_failure_raises_and_does_not_change_existing_records(self):
        self.refresh([response([record()])])
        before = self.scalar('SELECT raw_json FROM nih_records')
        with self.assertRaises(TimeoutError):
            self.refresh([TimeoutError('synthetic timeout')])
        self.assertEqual(self.scalar('SELECT raw_json FROM nih_records'), before)

    def test_duplicate_pages_are_partial_and_use_search_id_only(self):
        settings = dict(self.settings, page_size=1)
        result, calls = self.refresh([response([record()], 2, 'synthetic-search'), response([record()], 2)], settings=settings)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('duplicate_application_ids_in_query', result['warnings'])
        self.assertEqual(calls[1]['search_id'], 'synthetic-search')
        self.assertNotIn('criteria', calls[1])

    def test_request_budget_is_bounded_and_preserves_previous_success(self):
        settings = dict(self.settings, max_requests=1, page_size=1)
        result, calls = self.refresh([response([record()], 2)], settings=settings)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['checkpoint'], {})

    def test_rotation_checks_bounded_known_ids(self):
        first, _ = self.refresh([response([record(1001), record(1002)])])
        settings = dict(self.settings, max_reconcile_records=1)
        second, calls = self.refresh([response([]), response([record(1001)])], first['checkpoint'], settings=settings)
        self.assertEqual(calls[1]['criteria']['appl_ids'], [1001])
        third, calls = self.refresh([response([]), response([record(1002)])], second['checkpoint'], settings=settings)
        self.assertEqual(calls[1]['criteria']['appl_ids'], [1002])
        _, calls = self.refresh([response([]), response([record(1001)])], third['checkpoint'], settings=settings)
        self.assertEqual(calls[1]['criteria']['appl_ids'], [1001])

    def test_invalid_settings_fail_before_network_and_private_db_untouched(self):
        for settings in [dict(self.settings, queries=[]), dict(self.settings, max_records=True),
                         dict(self.settings, fiscal_years=[True]), dict(self.settings, page_size=501)]:
            with self.assertRaises(ValueError), patch.object(nih_refresh, '_request_json') as request:
                nih_refresh.refresh(self.root, settings)
                request.assert_not_called()
        studio = self.root / 'studio.sqlite3'
        studio.write_bytes(b'synthetic draft sentinel')
        self.refresh([response([record()])])
        self.assertEqual(studio.read_bytes(), b'synthetic draft sentinel')
        self.assertFalse((self.root / 'library.sqlite3').exists())

    def test_transport_retries_respect_rate_and_long_retry_after(self):
        error = urllib.error.HTTPError(nih.API_URL, 429, 'busy', {'Retry-After': '120'}, None)
        with patch.object(nih_refresh.time, 'sleep') as sleep, \
                patch.object(nih_refresh.urllib.request, 'urlopen', side_effect=error) as opener:
            with self.assertRaisesRegex(RuntimeError, 'more than 60'):
                nih_refresh._request_json({})
        self.assertEqual(opener.call_count, 1)
        sleep.assert_called_once_with(1.05)

    def test_rolling_years_follow_federal_fiscal_year_and_force_new_baseline(self):
        settings = dict(self.settings, rolling_years=3)
        del settings['fiscal_years']
        before, calls = self.refresh([response([])], day=datetime(2026, 9, 30, tzinfo=timezone.utc), settings=settings)
        self.assertEqual(calls[0]['criteria']['fiscal_years'], [2024, 2025, 2026])
        _, calls = self.refresh([response([])], checkpoint=before['checkpoint'], settings=settings)
        self.assertEqual(calls[0]['criteria']['fiscal_years'], [2025, 2026, 2027])
        self.assertNotIn('date_added', calls[0]['criteria'])
        with self.assertRaisesRegex(ValueError, 'not both'):
            nih_refresh.refresh(self.root, dict(self.settings, rolling_years=3))

    def test_explicit_metadata_clear_supersedes_old_value(self):
        original = record() | {'award_amount': 10000}
        first, _ = self.refresh([response([original])])
        cleared = record() | {'project_title': '', 'award_amount': None}
        self.refresh([response([]), response([cleared])], first['checkpoint'])
        self.assertEqual(self.scalar('SELECT title FROM nih_records'), '')
        self.assertIsNone(self.scalar('SELECT award_amount FROM nih_records'))

    def test_invalid_id_response_cannot_import_unrequested_record(self):
        first, _ = self.refresh([response([record()])])
        result, _ = self.refresh([response([]), response([record(9999)])], first['checkpoint'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['checkpoint'], first['checkpoint'])
        self.assertEqual(self.scalar('SELECT count(*) FROM nih_records'), 1)


if __name__ == '__main__':
    unittest.main()
