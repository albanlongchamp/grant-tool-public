"""Synthetic public API records only; all network requests are replaced."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from grantlib import publication_refresh as pub
from grantlib import references


def article(pmid='101', title='Fictional soil study', *, abstract='Invented methods and findings.', notice=''):
    return f'''<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID>
    <DateRevised><Year>2026</Year><Month>10</Month><Day>30</Day></DateRevised>
    <Article><ArticleTitle>{title}</ArticleTitle><Abstract><AbstractText>{abstract}</AbstractText></Abstract>
    <Journal><Title>Invented Journal</Title><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>
    <AuthorList><Author><ForeName>Example</ForeName><LastName>Author</LastName></Author></AuthorList>
    </Article><MeshHeadingList><MeshHeading><DescriptorName>Fictional Vessels</DescriptorName></MeshHeading></MeshHeadingList>
    <KeywordList><Keyword>fictional</Keyword></KeywordList>
    <CommentsCorrectionsList>{notice}</CommentsCorrectionsList></MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">10.9999/fictional{pmid}</ArticleId></ArticleIdList></PubmedData>
    </PubmedArticle>'''


def xml(*records):
    return ('<PubmedArticleSet>' + ''.join(records) + '</PubmedArticleSet>').encode()


class PublicationRefreshTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = {'queries': ['fictional soil topic'], 'max_records': 10,
                         'initial_days': 30, 'overlap_days': 7, 'reconcile_records': 0}
        self.calls = []

    def request(self, ids=('101',), total=None, records=None):
        def network(url):
            self.calls.append(url)
            query = parse_qs(urlsplit(url).query)
            if urlsplit(url).path.endswith('/esearch.fcgi'):
                return json.dumps({'esearchresult': {'count': str(len(ids) if total is None else total),
                                                     'idlist': list(ids)}}).encode()
            requested = query['id'][0].split(',')
            return xml(*(records.get(i, article(i)) if records is not None else article(i) for i in requested))
        return patch.object(pub, '_request', side_effect=network)

    def db(self):
        conn = sqlite3.connect(self.data / 'publications.sqlite3')
        self.addCleanup(conn.close)
        return conn

    def test_initial_refresh_has_public_provenance_and_overlapping_date_fields(self):
        with self.request(ids=('101', '102')):
            result = pub.refresh(self.data, self.settings)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts']['new'], 2)
        self.assertEqual(result['counts']['discovered'], 2)
        term = parse_qs(urlsplit(self.calls[0]).query)['term'][0]
        self.assertIn('[lr] OR ', term)
        self.assertIn('[crdt]', term)
        self.assertIn('fictional soil topic', term)
        self.assertEqual(len(self.calls), 2)
        with patch.object(pub, '_request', side_effect=AssertionError('Search must be local')):
            hits = pub.search(self.data, 'soil')
        self.assertEqual(len(hits['results']), 2)
        self.assertFalse(hits['retained_as_proposal_evidence'])
        hit = hits['results'][0]
        self.assertFalse(hit['verification']['semantic_support'])
        self.assertEqual(hit['date_revised'], '2026/10/30')
        self.assertTrue(hit['source_url'].startswith('https://pubmed.ncbi.nlm.nih.gov/'))
        self.assertEqual(hit['mesh_terms'], ['Fictional Vessels'])
        self.assertEqual(len(hit['record_sha256']), 64)
        self.assertIsNone(hit['publication_notices']['retracted'])
        self.assertEqual({p.name for p in self.data.iterdir()}, {'publications.sqlite3'})

    def test_unchanged_content_does_not_duplicate_versions_changed_notice_does(self):
        with self.request():
            first = pub.refresh(self.data, self.settings)
            again = pub.refresh(self.data, self.settings, first['checkpoint'])
        self.assertEqual(again['counts']['new'], 0)
        self.assertEqual(again['counts']['unchanged'], 1)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM pubmed_versions').fetchone()[0], 1)
        old_json = self.db().execute('SELECT record_json FROM pubmed_versions').fetchone()[0]
        retraction = '<CommentsCorrections RefType="RetractionIn"><PMID>999</PMID><RefSource>Invented notice.</RefSource></CommentsCorrections>'
        with self.request(records={'101': article(notice=retraction)}):
            changed = pub.refresh(self.data, self.settings, again['checkpoint'])
        self.assertEqual(changed['counts']['changed'], 1)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM pubmed_versions').fetchone()[0], 2)
        self.assertIn(old_json, [row[0] for row in self.db().execute('SELECT record_json FROM pubmed_versions')])
        latest = pub.search(self.data, 'soil')['results'][0]
        self.assertTrue(latest['publication_notices']['retracted'])
        self.assertIsNone(latest['publication_notices']['is_retraction_notice'])
        through = datetime.now(timezone.utc).date()
        from_day = (through - timedelta(days=7)).strftime('%Y/%m/%d')
        self.assertIn(from_day, parse_qs(urlsplit(self.calls[2]).query)['term'][0])

    def test_partial_cap_does_not_advance_watermark(self):
        settings = dict(self.settings, max_records=1)
        prior = {'version': 1, 'queries': {}}
        with self.request(total=20):
            result = pub.refresh(self.data, settings, prior)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['queries_partial'], 1)
        self.assertEqual(result['checkpoint'], prior)
        self.assertEqual(result['coverage'][0]['total'], 20)
        self.assertTrue(result['warnings'])
        self.assertEqual(len(pub.search(self.data, '')['results']), 1)

    def test_expanding_initial_history_refetches_baseline_instead_of_reusing_narrow_checkpoint(self):
        with self.request():
            initial = pub.refresh(self.data, self.settings)
            expanded = pub.refresh(self.data, dict(self.settings, initial_days=365), initial['checkpoint'])
        start = (datetime.now(timezone.utc).date() - timedelta(days=365)).isoformat()
        self.assertEqual(expanded['coverage'][0]['window_start'], start)
        checkpoint = expanded['checkpoint']['queries'][pub._sha(self.settings['queries'][0])]
        self.assertEqual(checkpoint['initial_from'], start)
        self.assertEqual(checkpoint['initial_days'], 365)
        with self.request():
            narrowed = pub.refresh(self.data, self.settings, expanded['checkpoint'])
        overlap_start = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
        self.assertEqual(narrowed['coverage'][0]['window_start'], overlap_start)
        self.assertEqual(narrowed['checkpoint']['queries'][pub._sha(self.settings['queries'][0])]['initial_from'], start)

    def test_batch_boundaries_and_reconciliation_skip_records_just_discovered(self):
        ids = tuple(str(i) for i in range(101, 152))
        settings = dict(self.settings, max_records=100, reconcile_records=100)
        with self.request(ids=ids):
            first = pub.refresh(self.data, settings)
        self.assertEqual(len(self.calls), 3)  # One search plus batches of 50 and 1.
        self.calls.clear()
        with self.request(ids=ids):
            result = pub.refresh(self.data, settings, first['checkpoint'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result['counts']['reconciled'], 0)
        self.assertEqual(result['counts']['unchanged'], 51)
        self.assertEqual(result['status'], 'complete')

    def test_discovered_omission_and_failure_leave_previous_snapshot_intact(self):
        with self.request():
            first = pub.refresh(self.data, self.settings)
        old = self.db().execute('SELECT record_json FROM pubmed_records').fetchone()[0]
        calls = 0
        def omitted(url):
            nonlocal calls
            calls += 1
            if 'esearch.fcgi' in url:
                return b'{"esearchresult":{"count":"2","idlist":["101","102"]}}'
            return xml(article('101', title='Changed but uncommitted'))
        with patch.object(pub, '_request', side_effect=omitted):
            with self.assertRaises(pub.PublicationRefreshError):
                pub.refresh(self.data, self.settings, first['checkpoint'])
        self.assertEqual(calls, 2)
        self.assertEqual(self.db().execute('SELECT record_json FROM pubmed_records').fetchone()[0], old)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM pubmed_versions').fetchone()[0], 1)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM pubmed_refresh_runs WHERE status='failed'").fetchone()[0], 1)

    def test_known_records_reconcile_even_when_they_no_longer_match_topic(self):
        with self.request():
            first = pub.refresh(self.data, self.settings)
        settings = dict(self.settings, reconcile_records=1)
        notice = '<CommentsCorrections RefType="ErratumIn"><PMID>998</PMID></CommentsCorrections>'
        with self.request(ids=(), records={'101': article(notice=notice)}):
            result = pub.refresh(self.data, settings, first['checkpoint'])
        self.assertEqual(result['counts']['reconciled'], 1)
        self.assertEqual(result['counts']['changed'], 1)
        self.assertTrue(pub.search(self.data, 'soil')['results'][0]['publication_notices']['has_correction'])
        self.assertEqual(result['status'], 'complete')

    def test_known_missing_record_is_retained_with_unavailability_flag(self):
        with self.request():
            first = pub.refresh(self.data, self.settings)
        def missing(url):
            return b'{"esearchresult":{"count":"0","idlist":[]}}' if 'esearch.fcgi' in url else xml()
        with patch.object(pub, '_request', side_effect=missing):
            result = pub.refresh(self.data, dict(self.settings, reconcile_records=1), first['checkpoint'])
        self.assertEqual(result['counts']['unavailable'], 1)
        item = pub.search(self.data, 'soil')['results'][0]
        self.assertIsNotNone(item['unavailable_at'])
        self.assertIsNone(item['publication_notices']['retracted'])
        self.assertIn('absent', item['limitations'][-1])
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM pubmed_versions').fetchone()[0], 1)

    def test_query_failure_does_not_block_other_query_and_checkpoints_are_per_query(self):
        settings = dict(self.settings, queries=['failing fictional topic', 'successful fictional topic'])
        def network(url):
            if 'esearch.fcgi' in url:
                if 'failing' in url:
                    raise pub.PublicationRefreshError('Synthetic failure')
                return b'{"esearchresult":{"count":"1","idlist":["101"]}}'
            return xml(article())
        with patch.object(pub, '_request', side_effect=network):
            result = pub.refresh(self.data, settings)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['queries_failed'], 1)
        self.assertEqual(result['counts']['queries_complete'], 1)
        self.assertNotIn(pub._sha('failing fictional topic'), result['checkpoint']['queries'])
        self.assertIn(pub._sha('successful fictional topic'), result['checkpoint']['queries'])

    def test_malformed_search_results_and_unsolicited_records_are_rejected(self):
        for body in (b'{}', b'{"esearchresult":{"count":"2","idlist":["101"]}}',
                     b'{"esearchresult":{"count":"1","idlist":["101"],"errorlist":{"fieldnotfound":["bad"]}}}',
                     b'{"esearchresult":{"count":"2","idlist":["101","101"]}}'):
            with self.subTest(body=body), patch.object(pub, '_request', return_value=body):
                with self.assertRaises(pub.PublicationRefreshError):
                    pub._discover('fictional', datetime.now().date(), datetime.now().date(), 10)
        for body in (xml(article('999')), b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///private">]><PubmedArticleSet>&y;</PubmedArticleSet>',
                     b'<unexpected/>', xml(article(), article())):
            with self.subTest(body=body), patch.object(pub, '_request', return_value=body):
                with self.assertRaises(pub.PublicationRefreshError):
                    pub._fetch(['101'])

    def test_reconciliation_failure_marks_partial_without_replacing_records(self):
        with self.request():
            first = pub.refresh(self.data, self.settings)
        def fail_recheck(url):
            if 'esearch.fcgi' in url:
                return b'{"esearchresult":{"count":"0","idlist":[]}}'
            raise pub.PublicationRefreshError('Synthetic retry failure')
        with patch.object(pub, '_request', side_effect=fail_recheck):
            result = pub.refresh(self.data, dict(self.settings, reconcile_records=1), first['checkpoint'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['reconciled'], 0)
        self.assertEqual(len(pub.search(self.data, '')['results']), 1)

    def test_invalid_settings_fail_before_network_and_search_is_read_only(self):
        for change in ({'queries': []}, {'queries': ['']}, {'max_records': True}, {'max_records': 10001},
                       {'initial_days': 0}, {'overlap_days': 0}, {'reconcile_records': -1}):
            with self.subTest(change=change), patch.object(pub, '_request', side_effect=AssertionError('Unexpected request')):
                with self.assertRaises(ValueError):
                    pub.refresh(self.data, dict(self.settings, **change))
        self.assertEqual(pub.search(self.data, 'fictional')['results'], [])
        self.assertFalse((self.data / 'publications.sqlite3').exists())
        with self.assertRaises(ValueError):
            pub.search(self.data, 'x', limit=21)

    def test_retry_is_bounded_and_response_bodies_are_not_errors(self):
        transient = references.Response(503, {'retry-after': '1'}, b'private-looking response ignored')
        with patch.object(references, '_request_official', side_effect=[transient, references.Response(200, {}, b'ok')]) as req, patch.object(pub.time, 'sleep'):
            self.assertEqual(pub._request(pub.API_BASE + 'esearch.fcgi'), b'ok')
            self.assertEqual(req.call_count, 2)
        with patch.object(references, '_request_official', return_value=references.Response(302, {}, b'untrusted')) as req:
            with self.assertRaises(pub.PublicationRefreshError) as caught:
                pub._request(pub.API_BASE + 'esearch.fcgi')
            self.assertEqual(req.call_count, 1)
            self.assertNotIn('untrusted', str(caught.exception))
