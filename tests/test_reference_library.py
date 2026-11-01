"""Shared reference library built from an invented archive; every public call is faked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from grantlib import library, reference_library as lib, references as ref
from grantlib.studio import Service, Store
from tests.test_references import article, crossref, pubmed

GRANT = '''SPECIFIC AIMS

We will test an invented irrigation buffer on fictional seedlings.

REFERENCES

1. Author E, Writer S. A fictional paired study of nothing. Invent J Test. 2024;12:100-110. doi:10.9999/fictional

2. Person T. Another invented paper. J Fict. 2020;3:1-9. PMID: 12345678

3. Mysterious M, Unknown U. An unidentified fictional report on seedling irrigation. Invent J. 2019;7:55-60.

4. Short entry 2018.
'''

RENEWAL = '''BACKGROUND

Earlier work (doi:10.9999/fictional) remains proposed, not demonstrated.
'''


def crossref_search(items):
    return ref.Response(200, {}, json.dumps({'status': 'ok', 'message': {'items': items}}).encode())


class FakeNetwork:
    """Routes fixed-host URLs to invented responses and records what left the machine."""
    def __init__(self, *, bibliographic=None):
        self.calls = []
        self.bibliographic = bibliographic if bibliographic is not None else [
            {'DOI': '10.9999/unidentified', 'title': ['An unidentified fictional report on seedling irrigation'],
             'author': [{'family': 'Mysterious', 'given': 'M'}], 'container-title': ['Invent J'],
             'issued': {'date-parts': [[2019]]}, 'score': 80.5},
            {'DOI': '10.9999/wrong', 'title': ['Something else entirely'], 'author': [{'family': 'Other'}],
             'issued': {'date-parts': [[2019]]}, 'score': 20.0}]

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        parts = urlsplit(url)
        if parts.hostname == 'eutils.ncbi.nlm.nih.gov':
            query = parse_qs(parts.query)
            if 'esearch' in parts.path:
                # Only the unidentified entry's DOI is indexed in this invented PubMed.
                ids = ['87654321'] if query['term'] == ['10.9999/unidentified[doi]'] else []
                return ref.Response(200, {}, json.dumps({'esearchresult': {'idlist': ids}}).encode())
            pmid = query['id'][0]
            if pmid == '12345678':
                return pubmed(article(pmid, identifiers='<ArticleId IdType="doi">10.9999/pubmedonly</ArticleId>'))
            if pmid == '87654321':
                return pubmed(article(pmid, identifiers='<ArticleId IdType="doi">10.9999/unidentified</ArticleId>'))
            return ref.Response(200, {}, b'<PubmedArticleSet></PubmedArticleSet>')
        if parts.path == '/works':
            return crossref_search(self.bibliographic)
        doi = unquote(parts.path.rsplit('/', 1)[-1])
        if doi == '10.9999/unidentified':
            return crossref(DOI=doi, title=['An unidentified fictional report on seedling irrigation'],
                            author=[{'family': 'Mysterious', 'given': 'M'}, {'family': 'Unknown', 'given': 'U'}],
                            issued={'date-parts': [[2019]]}, volume='7', page='55-60')
        if doi == '10.9999/fictional':
            return crossref(volume='12', page='100-110')
        return ref.Response(404, {}, b'')


class ReferenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source = root / 'archive'
        (source / 'R01_2022').mkdir(parents=True)
        (source / 'R01_2022' / 'Research_strategy.txt').write_text(GRANT)
        (source / 'Renewal').mkdir()
        (source / 'Renewal' / 'Background.txt').write_text(RENEWAL)
        self.data = root / 'data'
        config = {'sources': [{'name': 'fictional', 'path': str(source), 'project_from_subfolder': True}],
                  'data_dir': str(self.data), 'workers': 1}
        library.ingest(config)
        self.archive = self.data / 'library.sqlite3'
        self.store = Store(self.data)

    def test_archive_scan_groups_identifiers_and_reference_entries_by_document(self):
        found = lib.observed_identifiers(self.archive)
        self.assertEqual([i['identity_key'] for i in found], ['doi:10.9999/fictional', 'pmid:12345678'])
        self.assertEqual(sorted({o['project'] for o in found[0]['occurrences']}), ['R01_2022', 'Renewal'])
        self.assertEqual([i['identity_key'] for i in lib.observed_identifiers(self.archive, project='Renewal')], ['doi:10.9999/fictional'])
        entries = lib.reference_entries_without_identifiers(self.archive)
        self.assertEqual([e['entry_text'] for e in entries],
                         ['Mysterious M, Unknown U. An unidentified fictional report on seedling irrigation. Invent J. 2019;7:55-60.'])
        self.assertEqual(entries[0]['occurrences'][0]['project'], 'R01_2022')

    def test_match_confidence_requires_title_author_and_year_agreement(self):
        entry = 'Mysterious M, Unknown U. An unidentified fictional report on seedling irrigation. Invent J. 2019;7:55-60.'
        good = {'title': 'An unidentified fictional report on seedling irrigation', 'first_author_family': 'Mysterious', 'year': 2019}
        self.assertGreaterEqual(lib.match_confidence(entry, good), lib.MATCH_THRESHOLD)
        self.assertEqual(lib.match_confidence(entry, {**good, 'first_author_family': 'Nobody'}), 0.0)
        self.assertEqual(lib.match_confidence(entry, {**good, 'year': 2005}), 0.0)
        self.assertLess(lib.match_confidence(entry, {**good, 'title': 'Completely different words about mitochondria'}), lib.MATCH_THRESHOLD)
        self.assertEqual(lib.match_confidence(entry, {'title': None}), 0.0)

    def test_dry_run_previews_without_network_and_harvest_resolves_in_pages(self):
        network = FakeNetwork()
        with patch('grantlib.references._network', network):
            preview = lib.harvest(self.store, self.archive, limit=5, include_unidentified=True, dry_run=True)
            self.assertEqual(network.calls, [])
            self.assertEqual((preview['pending_identifiers'], preview['pending_unidentified_entries'], preview['remaining']), (2, 1, 3))
            self.assertEqual(preview['preview'][0], {'identity_key': 'doi:10.9999/fictional', 'documents': 2})
            first = lib.harvest(self.store, self.archive, limit=1)
            self.assertEqual([a['identity_key'] for a in first['added']], ['doi:10.9999/fictional'])
            self.assertEqual(first['added'][0]['documents'], 2)
            self.assertEqual((first['remaining'], first['library_total']), (1, 1))
            self.assertEqual(len(network.calls), 2)   # PubMed [doi] mapping (none) then Crossref
            second = lib.harvest(self.store, self.archive, limit=5, include_unidentified=True)
            self.assertEqual([(a['identity_key'], a['identity_basis']) for a in second['added']],
                             [('pmid:12345678', 'explicit_identifier'), ('pmid:87654321', 'crossref_bibliographic_match')])
            fuzzy_record = lib.snapshot(self.store, second['added'][1]['id'])
            self.assertEqual((fuzzy_record['provider'], fuzzy_record['canonical']), ('pubmed', {'pmid': '87654321', 'doi': '10.9999/unidentified'}))
            self.assertGreaterEqual(second['added'][1]['confidence'], lib.MATCH_THRESHOLD)
            self.assertEqual((second['already_in_library'], second['remaining'], second['library_total']), (1, 0, 3))
            sent = [parse_qs(urlsplit(c).query)['query.bibliographic'][0] for c in network.calls if '/works?' in c]
            self.assertEqual(sent, ['Mysterious M, Unknown U. An unidentified fictional report on seedling irrigation. Invent J. 2019;7:55-60.'])
            self.assertFalse(any('invented irrigation buffer' in c for c in network.calls))
            third = lib.harvest(self.store, self.archive, limit=5, include_unidentified=True)
            self.assertEqual((third['added'], third['remaining'], third['already_in_library']), ([], 0, 2))
        found = lib.search(self.store, 'seedling irrigation')
        self.assertEqual(found['total'], 1)
        fuzzy = found['items'][0]
        self.assertEqual(fuzzy['identity_basis'], 'crossref_bibliographic_match')
        self.assertIn('confirm against the original', fuzzy['match']['caution'])
        self.assertEqual(fuzzy['cited_in']['projects'], ['R01_2022'])
        everything = lib.search(self.store)
        self.assertEqual(everything['total'], 3)
        self.assertEqual(everything['items'][0]['identity_key'], 'doi:10.9999/fictional')
        self.assertEqual(everything['items'][0]['cited_in']['documents'], 2)
        self.assertEqual(lib.search(self.store, project='Renewal')['total'], 1)
        self.assertNotIn('abstract', everything['items'][0])
        self.assertIn('abstract', lib.search(self.store, include_abstract=True)['items'][0])

    def test_failed_lookups_are_remembered_and_only_retried_on_request(self):
        def failing(url, **kwargs):
            parts = urlsplit(url)
            if parts.hostname == 'eutils.ncbi.nlm.nih.gov':
                return ref.Response(200, {}, b'<PubmedArticleSet></PubmedArticleSet>')
            return ref.Response(404, {}, b'')
        with patch('grantlib.references._network', failing):
            result = lib.harvest(self.store, self.archive, limit=5)
            self.assertEqual([u['status'] for u in result['unresolved']], ['not_found', 'not_found'])
            self.assertEqual(result['remaining'], 0)
            again = lib.harvest(self.store, self.archive, limit=5)
            self.assertEqual((again['unresolved'], again['skipped_after_earlier_failure']), ([], 2))
        with patch('grantlib.references._network', FakeNetwork()):
            retried = lib.harvest(self.store, self.archive, limit=5, retry_failed=True)
            self.assertEqual(len(retried['added']), 2)
            weak = FakeNetwork(bibliographic=[{'DOI': '10.9999/wrong', 'title': ['Something else entirely'],
                                               'author': [{'family': 'Other'}], 'issued': {'date-parts': [[2019]]}}])
        with patch('grantlib.references._network', weak):
            fuzzy = lib.harvest(self.store, self.archive, limit=5, include_unidentified=True)
            self.assertEqual(fuzzy['unresolved'][0]['status'], 'no_confident_match')
            self.assertEqual(fuzzy['unresolved'][0]['best_candidate']['doi'], '10.9999/wrong')
            self.assertEqual(lib.count(self.store), 2)
        with self.assertRaises(ValueError):
            lib.harvest(self.store, self.archive, limit=0)

    def test_proposals_adopt_library_records_and_resolutions_feed_the_library(self):
        with patch('grantlib.references._network', FakeNetwork()):
            lib.harvest(self.store, self.archive, limit=5)
        entry = lib.search(self.store, 'paired study')['items'][0]
        proposal = self.store.create({'title': 'Invented project', 'question': 'An invented research question'})
        adopted = lib.add_to_proposal(self.store, proposal['id'], proposal['revision'], entry['id'])
        self.assertFalse(adopted['already_present'])
        self.assertTrue(adopted['reference_id'].startswith('R_'))
        reference = next(r for r in adopted['proposal']['references'] if r['id'] == adopted['reference_id'])
        self.assertEqual((reference['canonical']['doi'], reference['volume']), ('10.9999/fictional', '12'))
        again = lib.add_to_proposal(self.store, proposal['id'], adopted['proposal']['revision'], entry['id'])
        self.assertTrue(again['already_present'])
        self.assertEqual(again['reference_id'], adopted['reference_id'])
        self.assertEqual(lib.search(self.store, 'paired study')['items'][0]['used_by_proposals'], [proposal['id']])
        with self.assertRaises(KeyError):
            lib.add_to_proposal(self.store, proposal['id'], again['proposal']['revision'], 'L_missing')
        service = Service(self.archive, generate=lambda *a, **k: None, packet_builder=lambda *a, **k: {'sources': []})
        network = FakeNetwork()
        with patch('grantlib.references._network', network):
            updated = service.resolve_reference(proposal['id'], again['proposal']['revision'], '12345678')
        self.assertEqual(len(network.calls), 1)
        # The harvested PubMed record is reused rather than duplicated.
        self.assertEqual(lib.count(self.store), 2)
        pubmed_entry = lib.search(self.store, 'paired experiment')['items'][0]
        self.assertEqual(pubmed_entry['identity_key'], 'pmid:12345678')
        self.assertEqual(pubmed_entry['used_by_proposals'], [proposal['id']])
        self.assertEqual(pubmed_entry['cited_in']['documents'], 1)
        self.assertEqual(updated['revision'], again['proposal']['revision'] + 1)

    def test_remember_merges_identities_and_rejects_unresolved_snapshots(self):
        snapshot = {'status': 'resolved', 'title': 'Fictional', 'identifiers': {'pmid': None, 'doi': '10.9999/x'}}
        first = lib.remember(self.store, snapshot, identity_basis='crossref_bibliographic_match', match={'confidence': 0.7})
        self.assertEqual(first['identity_basis'], 'crossref_bibliographic_match')
        second = lib.remember(self.store, {**snapshot, 'identifiers': {'pmid': '777', 'doi': '10.9999/x'}})
        self.assertEqual(second['id'], first['id'])
        self.assertEqual((second['identity_basis'], second['match']), ('explicit_identifier', None))
        self.assertEqual(lib.count(self.store), 1)
        for bad in ({'status': 'not_found', 'title': 'x', 'identifiers': {'doi': '10.9999/y'}},
                    {'status': 'resolved', 'title': 'No identity', 'identifiers': {}}, 'text'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lib.remember(self.store, bad)


class RetractionRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.proposal = self.store.create({'title': 'Invented project', 'question': 'An invented research question'})
        clean = ref.resolve('12345678', transport=lambda *a, **k: pubmed(article()))
        self.proposal = self.store.add_reference(self.proposal['id'], self.proposal['revision'], clean)
        lib.remember(self.store, clean)
        doi_only = ref.resolve('10.9999/other', transport=lambda *a, **k: crossref(DOI='10.9999/other', title=['Other invented work']))
        lib.remember(self.store, doi_only)

    def test_recheck_records_new_retraction_and_export_status_flags_it(self):
        from datetime import datetime, timedelta, timezone
        retraction = '<CommentsCorrections RefType="RetractionIn"><RefSource>Invent J. 2026</RefSource><PMID>1</PMID></CommentsCorrections>'
        calls = []
        def network(url, **kwargs):
            calls.append(url)
            if 'esearch' in url:
                return ref.Response(200, {}, json.dumps({'esearchresult': {'idlist': []}}).encode())
            if 'eutils' in url:
                return pubmed(article(links=retraction))
            return crossref(DOI='10.9999/other', title=['Other invented work'])
        fresh = datetime.now(timezone.utc)
        self.assertTrue(network)
        with patch('grantlib.references._network', network):
            untouched = lib.recheck(self.store, proposal_id=self.proposal['id'], now=fresh)
            self.assertEqual((untouched['due'], untouched['checked'], calls), (0, [], []))
            later = fresh + timedelta(days=60)
            result = lib.recheck(self.store, proposal_id=self.proposal['id'], now=later)
        self.assertEqual(result['due'], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('id=12345678', calls[0])
        self.assertEqual(result['checked'][0]['flags'], ['retracted'])
        self.assertTrue(result['checked'][0]['changed'])
        self.assertEqual([e['identity_key'] for e in result['newly_flagged']], ['pmid:12345678'])
        self.assertEqual(result['flagged'][0]['labels'], ['Retracted'])
        self.assertEqual(result['remaining'], 0)
        proposal = self.store.get(self.proposal['id'])
        self.assertEqual(proposal['reference_checks']['pmid:12345678']['flags'], ['retracted'])
        self.assertIsNone(proposal['references'][0]['publication_notices']['retracted'])  # snapshot untouched
        entry = lib.search(self.store, 'paired')['items'][0]
        self.assertEqual((entry['flags'], entry['last_checked_at']), (['retracted'], later.isoformat()))
        with self.store.connect() as c:
            status = lib.status_for(proposal['references'], c, now=later)
        self.assertEqual(status['flagged'][0]['identity_key'], 'pmid:12345678')
        self.assertEqual(status['stale_identity_keys'], [])
        with patch('grantlib.references._network', network):
            whole = lib.recheck(self.store, limit=1, force=True, now=later)
        self.assertEqual((whole['references'], whole['due'], len(whole['checked']), whole['remaining']), (2, 2, 1, 1))
        self.assertEqual(whole['newly_flagged'], [])
        with self.assertRaises(ValueError):
            lib.recheck(self.store, limit=0)

    def test_unresolvable_identifier_and_errors_are_reported_without_flags(self):
        from datetime import datetime, timedelta, timezone
        later = datetime.now(timezone.utc) + timedelta(days=60)
        def network(url, **kwargs):
            if 'esearch' in url:
                return ref.Response(200, {}, json.dumps({'esearchresult': {'idlist': []}}).encode())
            if 'eutils' in url:
                return ref.Response(200, {}, b'<PubmedArticleSet></PubmedArticleSet>')
            return ref.Response(500, {}, b'')
        with patch('grantlib.references._network', network):
            result = lib.recheck(self.store, now=later, limit=5)
        statuses = {c['identity_key']: c['status'] for c in result['checked']}
        self.assertEqual(statuses, {'pmid:12345678': 'not_found', 'doi:10.9999/other': 'error'})
        self.assertEqual(result['flagged'], [])
        with self.store.connect() as c:
            recorded = lib.checks(c)
        self.assertEqual(recorded['pmid:12345678']['status'], 'not_found')
        self.assertNotIn('doi:10.9999/other', recorded)

    def test_doi_only_reference_is_rechecked_through_its_pubmed_record(self):
        from datetime import datetime, timedelta, timezone
        retraction = '<CommentsCorrections RefType="RetractionIn"><RefSource>Invent J. 2026</RefSource><PMID>1</PMID></CommentsCorrections>'
        calls = []
        def network(url, **kwargs):
            calls.append(url)
            if 'esearch' in url:
                ids = ['55555555'] if '10.9999%2Fother%5Bdoi%5D' in url or '10.9999/other[doi]' in unquote(url) else []
                return ref.Response(200, {}, json.dumps({'esearchresult': {'idlist': ids}}).encode())
            if 'efetch' in url:
                return pubmed(article('55555555', links=retraction, identifiers='<ArticleId IdType="doi">10.9999/other</ArticleId>'))
            return crossref(DOI='10.9999/other', title=['Other invented work'])
        later = datetime.now(timezone.utc) + timedelta(days=60)
        with patch('grantlib.references._network', network):
            result = lib.recheck(self.store, now=later, limit=5)
        other = next(c for c in result['checked'] if c['identity_key'] == 'doi:10.9999/other')
        self.assertEqual((other['provider'], other['resolved_pmid'], other['flags']), ('pubmed', '55555555', ['retracted']))
        self.assertFalse(any('api.crossref.org/works/10.9999%2Fother' in c for c in calls))
        with self.store.connect() as c:
            self.assertEqual(lib.checks(c)['doi:10.9999/other']['resolved_pmid'], '55555555')
        self.assertIsNone(ref.pmid_for_doi('10.9999/none', transport=lambda *a, **k: ref.Response(200, {}, json.dumps({'esearchresult': {'idlist': ['1', '2']}}).encode())))
        with self.assertRaises(ref.ReferenceInputError):
            ref.pmid_for_doi('12345678')
        with self.assertRaises(ref.ReferenceProtocolError):
            ref.pmid_for_doi('10.9999/x', transport=lambda *a, **k: ref.Response(200, {}, b'junk'))
        snapshot, pmid = lib.resolve_with_pubmed_preference('10.9999/other', transport=lambda *a, **k: ref.Response(500, {}, b'') if 'esearch' in a[0] else crossref(DOI='10.9999/other'))
        self.assertEqual((snapshot['provider'], pmid), ('crossref', None))

    def test_export_marks_flagged_entries(self):
        from grantlib.bibliography import Bibliography
        with self.store.connect(True) as c:
            c.execute('INSERT INTO reference_checks VALUES (?,?,?,?,?,?,?,?,?)',
                      ('pmid:12345678', '2026-03-01T00:00:00+00:00', 'checked', 'pubmed', '["retracted"]', None, '[]', 1, None))
        proposal = self.store.get(self.proposal['id'])
        reference = proposal['references'][0]
        from grantlib.claim_review import reference_source
        source = reference_source(reference)
        cited = Bibliography('Claim [[' + reference['id'] + ']].', {reference['id']: source}, proposal['references'], 'vancouver',
                             checks=proposal['reference_checks'])
        self.assertEqual(cited.flags, {reference['id']: ['retracted']})
        self.assertTrue(cited.entries()[0][2].startswith('**[RETRACTED]** Author E, Invented Group.'))
        plain = Bibliography('Claim [[' + reference['id'] + ']].', {reference['id']: source}, proposal['references'], 'vancouver')
        self.assertEqual(plain.flags, {})


class BibliographicLookupTests(unittest.TestCase):
    def test_lookup_returns_ranked_candidates_and_validates_input(self):
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            return crossref_search([{'DOI': '10.9999/A', 'title': ['Title <i>one</i>'], 'author': [{'family': 'Smith', 'given': 'J'}],
                                     'container-title': ['J Fict'], 'issued': {'date-parts': [[2019, 4]]}, 'score': 55.1},
                                    {'DOI': 'not a doi'}, 'junk'])
        result = ref.lookup_bibliographic('Smith J. Title one. J Fict. 2019;1:2-3.', transport=transport)
        self.assertEqual(result, [{'doi': '10.9999/a', 'title': 'Title one', 'first_author_family': 'Smith',
                                   'journal': 'J Fict', 'year': 2019, 'score': 55.1}])
        self.assertIn('query.bibliographic=Smith+J.+Title+one', calls[0])
        self.assertIn('rows=3', calls[0])
        for bad in ('short', 'x' * 601, 'two\nlines', None):
            with self.subTest(bad=bad), self.assertRaises(ref.ReferenceInputError):
                ref.lookup_bibliographic(bad, transport=transport)
        with self.assertRaises(ref.ReferenceProtocolError):
            ref.lookup_bibliographic('Smith J. Title one. J Fict. 2019;1:2-3.', transport=lambda *a, **k: ref.Response(200, {}, b'nope'))
        with self.assertRaises(ref.ReferenceRateLimited):
            ref.lookup_bibliographic('Smith J. Title one. J Fict. 2019;1:2-3.', transport=lambda *a, **k: ref.Response(429, {'Retry-After': '2'}, b''))
        with self.assertRaises(ref.ReferenceNetworkError):
            ref.lookup_bibliographic('Smith J. Title one. J Fict. 2019;1:2-3.', transport=lambda *a, **k: ref.Response(500, {}, b''))


if __name__ == '__main__':
    unittest.main()
