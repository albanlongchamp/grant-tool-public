"""Synthetic offline tests for immutable downloads and explicit live checks."""
from copy import deepcopy
from contextlib import closing
import hashlib
import socket
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib import guidance_refresh as refresh
from grantlib.guidance import load_guidance


URL = 'https://grants.nih.gov/example-guidance'
OTHER = 'https://www.nsf.gov/example-guidance'


def page(text='Invented guidance example.', *, etag='"v1"', mime='text/html; charset=utf-8'):
    headers = {'content-type': mime}
    if etag is not None:
        headers['etag'] = etag
    return 200, headers, ('<html><h1>Example</h1><p>' + text + '</p></html>').encode()


class GuidanceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def rows(self, table):
        with closing(sqlite3.connect(self.data / refresh.DB_NAME)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def test_refresh_versions_only_changed_bytes_and_preserves_catalog(self):
        catalog = deepcopy(load_guidance())
        with patch.object(refresh, '_http_request', side_effect=[page(), page(), page('Revised invented example.', etag='"v2"')]):
            first = refresh.refresh(self.data, {'urls': [URL]})
            same = refresh.refresh(self.data, {'urls': [URL]}, first['checkpoint'])
            revised = refresh.refresh(self.data, {'urls': [URL]}, same['checkpoint'])
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(same['counts']['unchanged'], 1)
        self.assertEqual(revised['counts']['changed'], 1)
        self.assertEqual(len(self.rows('guidance_snapshots')), 2)
        self.assertEqual(len(self.rows('guidance_checks')), 3)
        self.assertEqual(load_guidance(), catalog)
        self.assertFalse((self.data / 'studio.sqlite3').exists())

    def test_conditional_304_advances_check_not_snapshot(self):
        with patch.object(refresh, '_http_request', return_value=page()):
            refresh.refresh(self.data, {'urls': [URL]})
        original = self.rows('guidance_snapshots')[0]
        with patch.object(refresh, '_http_request', return_value=(304, {'etag': '"v1"'}, b'')) as request:
            result = refresh.refresh(self.data, {'urls': [URL]})
        self.assertEqual(request.call_args.args[1]['If-None-Match'], '"v1"')
        self.assertEqual(result['counts']['unchanged'], 1)
        self.assertEqual(self.rows('guidance_snapshots')[0], original)
        self.assertEqual(refresh.info(self.data)[0]['last_status'], 'unchanged')

    def test_live_always_requests_new_response_and_has_exact_provenance(self):
        response = page()
        with patch.object(refresh, '_http_request', return_value=response):
            refresh.refresh(self.data, {'urls': [URL]})
        with patch.object(refresh, '_http_request', return_value=response) as request:
            result = refresh.verify(self.data, URL, max_chars=8)
        headers = request.call_args.args[1]
        self.assertNotIn('If-None-Match', headers)
        self.assertIn('no-cache', headers['Cache-Control'])
        self.assertEqual(result['status'], 'retrieved_live')
        self.assertEqual(result['source_sha256'], hashlib.sha256(response[2]).hexdigest())
        self.assertEqual(result['text'], 'Example\n')
        self.assertTrue(result['text_window']['truncated'])
        self.assertFalse(result['requirements_verified'])
        self.assertFalse(result['retained_as_proposal_evidence'])
        self.assertEqual(self.rows('guidance_checks')[-1]['live'], 1)

    def test_saved_snapshot_stays_readable_after_source_changes(self):
        studio = self.data / 'studio.sqlite3'
        studio.write_bytes(b'Synthetic preexisting saved draft evidence sentinel')
        with patch.object(refresh, '_http_request', side_effect=[page(), page('Changed text.')]):
            first = refresh.verify(self.data, URL, max_chars=8)
            refresh.verify(self.data, URL)
        with patch.object(refresh, '_http_request') as request:
            window = refresh.read_snapshot(self.data, first['source_snapshot_id'], start=8, max_chars=9)
        request.assert_not_called()
        self.assertEqual(window['text'], 'Invented ')
        self.assertEqual(window['text_window']['next_start'], 17)
        self.assertEqual(window['source_sha256'], first['source_sha256'])
        self.assertEqual(studio.read_bytes(), b'Synthetic preexisting saved draft evidence sentinel')

    def test_snapshot_reader_rejects_paths_missing_ids_and_bad_ranges(self):
        for snapshot_id in ('../../other-file', 'GWEB_' + 'f' * 64):
            with self.assertRaises(ValueError):
                refresh.read_snapshot(self.data, snapshot_id)
        with patch.object(refresh, '_http_request', return_value=page()):
            first = refresh.verify(self.data, URL)
        for kwargs in ({'start': -1}, {'start': 100000}, {'max_chars': 0}, {'max_chars': 20001}):
            with self.assertRaises(ValueError):
                refresh.read_snapshot(self.data, first['source_snapshot_id'], **kwargs)

    def test_errors_preserve_usable_snapshot_and_last_success(self):
        with patch.object(refresh, '_http_request', return_value=page()):
            refresh.refresh(self.data, {'urls': [URL]})
        previous = refresh.info(self.data)[0]
        with patch.object(refresh, '_http_request', side_effect=TimeoutError('Request timed out')):
            with self.assertRaises(OSError):
                refresh.refresh(self.data, {'urls': [URL]})
        current = refresh.info(self.data)[0]
        self.assertEqual(current['snapshot_id'], previous['snapshot_id'])
        self.assertEqual(current['last_success_at'], previous['last_success_at'])
        self.assertEqual(current['last_status'], 'failed')
        self.assertIn('timed out', current['last_error'])
        self.assertEqual(len(self.rows('guidance_snapshots')), 1)

    def test_partial_run_does_not_advance_checkpoint(self):
        checkpoint = {'completed_at': '2026-01-01T00:00:00+00:00'}
        with patch.object(refresh, '_http_request', side_effect=[page(), OSError('HTTP 503')]):
            result = refresh.refresh(self.data, {'urls': [URL, OTHER]}, checkpoint)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['checkpoint'], checkpoint)
        self.assertEqual(result['counts']['succeeded'], 1)
        self.assertEqual(result['counts']['failed'], 1)
        self.assertEqual(len(result['warnings']), 1)

    def test_pdf_retained_but_never_misrepresented_as_verified_text(self):
        raw = b'%PDF-1.7\nSynthetic, not a real PDF.'
        with patch.object(refresh, '_http_request', return_value=(200, {'content-type': 'application/pdf'}, raw)):
            result = refresh.verify(self.data, URL)
        self.assertEqual(result['status'], 'retrieved_live_text_unsupported')
        self.assertEqual(result['extraction_status'], 'unsupported_pdf')
        self.assertEqual(result['text'], '')
        self.assertFalse(result['requirements_verified'])
        self.assertEqual(self.rows('guidance_snapshots')[0]['raw'], raw)

    def test_304_without_cache_and_live_304_are_rejected(self):
        with patch.object(refresh, '_http_request', return_value=(304, {}, b'')):
            with self.assertRaises(ValueError):
                refresh.verify(self.data, URL)
            with self.assertRaises(OSError):
                refresh.refresh(self.data, {'urls': [URL]})
        self.assertEqual(len(self.rows('guidance_snapshots')), 0)

    def test_url_allowlist_and_redirect_destination_are_validated(self):
        for url in ('http://grants.nih.gov/test', 'https://localhost/test', 'https://127.0.0.1/',
                    'https://grants.nih.gov.attacker.example/', 'https://user@grants.nih.gov/',
                    'https://grants.nih.gov:444/test', 'https://grants.nih.gov/\nfoo',
                    'https://grants.nih.gov\\@localhost/', 'https://[::1]/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                refresh.validate_url(url)
        with patch.object(refresh, '_http_request', return_value=(302, {'location': 'https://localhost/test'}, b'')) as request:
            with self.assertRaises(ValueError):
                refresh.verify(self.data, URL)
        self.assertEqual(request.call_count, 1)

    def test_redirect_chain_preserves_requested_and_canonical_urls(self):
        with patch.object(refresh, '_http_request', side_effect=[(302, {'location': '/moved'}, b''), page()]):
            result = refresh.verify(self.data, URL + '#section')
        self.assertEqual(result['url'], URL)
        self.assertEqual(result['canonical_url'], 'https://grants.nih.gov/moved')
        self.assertEqual(refresh.info(self.data, [URL])[0]['canonical_url'], result['canonical_url'])

    def test_redirect_does_not_forward_validators_to_other_source(self):
        with patch.object(refresh, '_http_request', return_value=page()):
            refresh.refresh(self.data, {'urls': [URL]})
        with patch.object(refresh, '_http_request', side_effect=[(302, {'location': OTHER}, b''), page()]) as request:
            refresh.refresh(self.data, {'urls': [URL]})
        self.assertIn('If-None-Match', request.call_args_list[0].args[1])
        self.assertNotIn('If-None-Match', request.call_args_list[1].args[1])

    def test_private_dns_is_rejected_before_connection(self):
        for address in ('127.0.0.1', '192.168.1.1', '169.254.169.254', '::1', 'fc00::1'):
            with self.subTest(address=address):
                with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]):
                    with patch.object(socket, 'create_connection') as connect:
                        with self.assertRaises(ValueError):
                            refresh._http_request(URL, {})
                        connect.assert_not_called()

    def test_html_excludes_executable_hidden_content_but_preserves_text(self):
        raw = b'<h1>Title</h1><script>secret()</script><style>color:red</style><p>Example &amp; evidence.</p>'
        text, status = refresh._extract(raw, 'text/html')
        self.assertEqual(status, 'html_text')
        self.assertEqual(text, 'Title\nExample & evidence.')

    def test_oversize_response_and_bad_encoding_are_explicit(self):
        with patch.object(refresh, 'MAX_BYTES', 3), patch.object(refresh, '_http_request', return_value=page()):
            with self.assertRaises(ValueError):
                refresh.verify(self.data, URL)
        self.assertEqual(refresh._extract(b'\xff', 'text/html')[1], 'unsupported_encoding')

    def test_large_official_html_report_stays_bounded_but_is_supported(self):
        # Annual reports can contain large embedded figures while their readable
        # text is small. Exercise a response beyond the former eight-MiB cap.
        raw = (b'<h1>Invented report</h1><img src="data:image/png;base64,'
               + b'a' * (9 * 1024 * 1024) + b'"><p>Invented seedling report.</p>')
        with patch.object(refresh, '_http_request', return_value=(200, {'content-type': 'text/html'}, raw)):
            result = refresh.verify(self.data, URL, max_chars=8)
        self.assertEqual(refresh.MAX_BYTES, 32 * 1024 * 1024)
        self.assertEqual(result['status'], 'retrieved_live')
        self.assertEqual(result['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(result['text']), 8)
        self.assertTrue(result['text_window']['truncated'])
        with patch.object(refresh, 'MAX_BYTES', len(raw) - 1), \
             patch.object(refresh, '_http_request', return_value=(200, {'content-type': 'text/html'}, raw)):
            with self.assertRaisesRegex(ValueError, 'download limit'):
                refresh.verify(self.data, URL)

    def test_info_is_read_only_and_missing_db_is_empty(self):
        self.assertEqual(refresh.info(self.data), [])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_200_without_validator_clears_previous_validator(self):
        with patch.object(refresh, '_http_request', side_effect=[page(), page(etag=None)]):
            refresh.refresh(self.data, {'urls': [URL]})
            refresh.refresh(self.data, {'urls': [URL]})
        self.assertIsNone(refresh.info(self.data)[0]['etag'])


if __name__ == '__main__':
    unittest.main()
