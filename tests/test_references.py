"""Invented API responses only; these tests make no network requests."""
import json
from unittest import TestCase
from unittest.mock import patch

from grantlib import references as ref


def crossref(**updates):
    work = {'DOI': '10.9999/fictional', 'title': ['Fictional <i>paired</i> study'],
            'author': [{'given': 'Example', 'family': 'Author'}, {'name': 'Fictional Consortium'}],
            'container-title': ['Invented Journal'], 'issued': {'date-parts': [[2025, 2]]},
            'abstract': '<jats:p>Invented <jats:italic>abstract</jats:italic>.</jats:p>'}
    work.update(updates)
    return ref.Response(200, {}, json.dumps({'status': 'ok', 'message': work}).encode())


def article(pmid='12345678', *, links='', types='', identifiers='', author=''):
    return f'''<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
      <ArticleTitle>Fictional <i>paired</i> experiment</ArticleTitle>
      <Journal><Title>Invented Journal</Title><JournalIssue><PubDate><Year>2024</Year><Month>Jan</Month></PubDate></JournalIssue></Journal>
      <AuthorList>{author or '<Author><ForeName>Example</ForeName><LastName>Author</LastName></Author><Author><CollectiveName>Invented Group</CollectiveName></Author>'}</AuthorList>
      <Abstract><AbstractText Label="METHODS">Fictional methods.</AbstractText><AbstractText Label="RESULTS">No real results.</AbstractText></Abstract>
      <PublicationTypeList>{types}</PublicationTypeList></Article>
      <CommentsCorrectionsList>{links}</CommentsCorrectionsList></MedlineCitation>
      <PubmedData><ArticleIdList>{identifiers or '<ArticleId IdType="doi">10.9999/fictional</ArticleId>'}</ArticleIdList></PubmedData></PubmedArticle>'''


def pubmed(*records):
    return ref.Response(200, {}, ('<PubmedArticleSet>' + ''.join(records) + '</PubmedArticleSet>').encode())


class ReferenceTests(TestCase):
    def resolve(self, identifier, response):
        calls = []
        def transport(url, **kwargs):
            calls.append((url, kwargs))
            return response
        result = ref.resolve(identifier, transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {'timeout': ref.TIMEOUT, 'max_bytes': ref.MAX_BYTES})
        self.assertFalse(result['verification']['semantic_support'])
        self.assertFalse(result['verification']['full_text'])
        self.assertTrue(result['verification']['metadata_only'])
        self.assertEqual(len(result['response_sha256']), 64)
        return result, calls[0][0]

    def test_only_one_normalized_identifier_is_accepted(self):
        self.assertEqual(ref.normalize_identifier(' PMID:12345678 '), {'kind': 'pmid', 'value': '12345678'})
        self.assertEqual(ref.normalize_identifier('DOI:10.9999/Fictional'), {'kind': 'doi', 'value': '10.9999/fictional'})
        for invalid in (None, 123, '', '0', '0012', '123,456', '123 OR 456', '10.9/x',
                        'https://doi.org/10.9999/x', 'https://localhost/x', 'PMID:10.9999/x',
                        '10.9999/a\nb', '10.9999/a\\b', '10.9999/' + 'x' * 1024):
            with self.subTest(identifier=invalid), self.assertRaises(ref.ReferenceInputError):
                ref.resolve(invalid, transport=lambda *a, **k: self.fail('Invalid input reached transport'))

    def test_doi_metadata_is_plain_text_and_has_unknown_not_clean_notices(self):
        result, url = self.resolve('10.9999/FICTIONAL', crossref())
        self.assertEqual(url, 'https://api.crossref.org/works/10.9999%2Ffictional')
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['identifiers'], {'pmid': None, 'doi': '10.9999/fictional'})
        self.assertEqual(result['title'], 'Fictional paired study')
        self.assertEqual(result['authors'], ['Example Author', 'Fictional Consortium'])
        self.assertEqual(result['author_parts'], [{'family': 'Author', 'given': 'Example'}, {'literal': 'Fictional Consortium'}])
        self.assertEqual(result['abstract'], 'Invented abstract.')
        self.assertEqual(result['year'], 2025)
        self.assertEqual((result['volume'], result['issue'], result['pages'], result['journal_abbreviation'], result['work_type']),
                         (None, None, None, None, None))
        result, _ = self.resolve('10.9999/FICTIONAL', crossref(volume='12', issue='3', page='100-110',
                                                                **{'short-container-title': ['Invent J'], 'type': 'journal-article'}))
        self.assertEqual((result['volume'], result['issue'], result['pages'], result['journal_abbreviation'], result['work_type']),
                         ('12', '3', '100-110', 'Invent J', 'journal-article'))
        self.assertIsNone(result['publication_notices']['retracted'])
        self.assertFalse(result['publication_notices']['complete'])
        self.assertIn('no reverse-update search', result['publication_notices']['scope'])

    def test_doi_suffix_cannot_change_host_or_query(self):
        identifier = '10.9999/a?b#c/https://localhost'
        _, url = self.resolve(identifier, crossref(DOI=identifier))
        self.assertTrue(url.startswith('https://api.crossref.org/works/'))
        self.assertNotIn('?', url)
        self.assertNotIn('#', url)
        self.assertIn('%3A%2F%2Flocalhost', url)

    def test_pubmed_primary_id_is_not_confused_with_linked_notice_id(self):
        links = '<CommentsCorrections RefType="RetractionIn"><RefSource>Invented notice.</RefSource><PMID>87654321</PMID></CommentsCorrections>'
        result, url = self.resolve('12345678', pubmed(article(links=links)))
        self.assertIn('db=pubmed&id=12345678&retmode=xml', url)
        self.assertEqual(result['identifiers'], {'pmid': '12345678', 'doi': '10.9999/fictional'})
        self.assertEqual(result['year'], 2024)
        self.assertEqual(result['abstract'], 'METHODS: Fictional methods.\n\nRESULTS: No real results.')
        self.assertEqual(result['authors'], ['Example Author', 'Invented Group'])
        self.assertEqual(result['author_parts'], [{'family': 'Author', 'given': 'Example'}, {'literal': 'Invented Group'}])
        self.assertEqual(result['work_type'], 'journal-article')
        self.assertIsNone(result['volume'])
        self.assertTrue(result['publication_notices']['retracted'])
        self.assertIsNone(result['publication_notices']['is_retraction_notice'])
        self.assertEqual(result['publication_notices']['relations'][0]['pmid'], '87654321')

    def test_pubmed_volume_pages_abbreviation_and_suffix_are_captured(self):
        xml = article().replace('<JournalIssue>', '<JournalIssue><Volume>12</Volume><Issue>3</Issue>').replace(
            '<Journal><Title>Invented Journal</Title>', '<Journal><ISOAbbreviation>Invent J</ISOAbbreviation><Title>Invented Journal</Title>').replace(
            '</AuthorList>', '</AuthorList><Pagination><MedlinePgn>100-10</MedlinePgn></Pagination>').replace(
            '<LastName>Author</LastName>', '<LastName>Author</LastName><Suffix>Jr</Suffix>')
        result, _ = self.resolve('12345678', pubmed(xml))
        self.assertEqual((result['volume'], result['issue'], result['pages'], result['journal_abbreviation']), ('12', '3', '100-10', 'Invent J'))
        self.assertEqual(result['author_parts'][0], {'family': 'Author', 'given': 'Example', 'suffix': 'Jr'})
        self.assertEqual(result['authors'][0], 'Example Author Jr')
        xml = article().replace('</AuthorList>', '</AuthorList><Pagination><StartPage>5</StartPage><EndPage>9</EndPage></Pagination>')
        self.assertEqual(self.resolve('12345678', pubmed(xml))[0]['pages'], '5-9')
        xml = article().replace('</AuthorList>', '</AuthorList><ELocationID EIdType="pii">e0001</ELocationID>')
        self.assertEqual(self.resolve('12345678', pubmed(xml))[0]['pages'], 'e0001')

    def test_notice_directions_and_publication_types_are_distinct(self):
        relations = ''.join(f'<CommentsCorrections RefType="{kind}"><PMID>87654321</PMID></CommentsCorrections>'
                            for kind in ('RetractionOf', 'ErratumFor', 'ExpressionOfConcernFor'))
        result, _ = self.resolve('12345678', pubmed(article(links=relations)))
        notice = result['publication_notices']
        self.assertTrue(notice['is_retraction_notice'])
        self.assertTrue(notice['is_correction_notice'])
        self.assertTrue(notice['is_expression_of_concern_notice'])
        self.assertIsNone(notice['retracted'])
        self.assertIsNone(notice['has_correction'])
        result, _ = self.resolve('12345678', pubmed(article(types='<PublicationType>Retracted Publication</PublicationType>')))
        self.assertTrue(result['publication_notices']['retracted'])

    def test_crossref_update_to_is_outbound_not_a_retraction_of_this_record(self):
        response = crossref(**{'update-to': [{'DOI': '10.9999/other', 'type': 'retraction'}]})
        result, _ = self.resolve('10.9999/fictional', response)
        self.assertTrue(result['publication_notices']['is_retraction_notice'])
        self.assertIsNone(result['publication_notices']['retracted'])
        self.assertEqual(result['publication_notices']['relations'][0]['direction'], 'this_record_updates')

    def test_incoming_correction_and_concern_are_flags_on_the_article(self):
        links = ''.join(f'<CommentsCorrections RefType="{kind}"><PMID>87654321</PMID></CommentsCorrections>'
                        for kind in ('ErratumIn', 'ExpressionOfConcernIn'))
        result, _ = self.resolve('12345678', pubmed(article(links=links)))
        notices = result['publication_notices']
        self.assertTrue(notices['has_correction'])
        self.assertTrue(notices['expression_of_concern'])
        self.assertIsNone(notices['is_correction_notice'])
        self.assertIsNone(notices['is_expression_of_concern_notice'])

    def test_absent_multiple_and_mismatched_records_are_not_accepted(self):
        result, _ = self.resolve('12345678', pubmed())
        self.assertEqual(result['status'], 'not_found')
        result, _ = self.resolve('12345678', pubmed(article(), article('87654321')))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['title'])
        for identifier, response in [('12345678', pubmed(article('87654321'))),
                                     ('10.9999/fictional', crossref(DOI='10.9999/other'))]:
            result, _ = self.resolve(identifier, response)
            self.assertEqual(result['status'], 'mismatch')
            self.assertIsNone(result['title'])
            self.assertIsNone(result['identifiers']['doi'])
        result, _ = self.resolve('10.9999/fictional', ref.Response(404, {}, b'Not found'))
        self.assertEqual(result['status'], 'not_found')
        self.assertTrue(any('other bibliographic registries' in x for x in result['limitations']))

    def test_multiple_reported_dois_do_not_force_canonical_choice(self):
        identifiers = '<ArticleId IdType="doi">10.9999/first</ArticleId><ArticleId IdType="doi">10.9999/second</ArticleId>'
        result, _ = self.resolve('12345678', pubmed(article(identifiers=identifiers)))
        self.assertEqual(result['status'], 'resolved')
        self.assertIsNone(result['identifiers']['doi'])
        self.assertEqual(result['reported_dois'], ['10.9999/first', '10.9999/second'])

    def test_book_citation_and_missing_abstract_are_supported(self):
        record = '<PubmedBookArticle><BookDocument><PMID>12345678</PMID><ArticleTitle>Fictional chapter</ArticleTitle><Book><BookTitle>Fictional book</BookTitle><PubDate><MedlineDate>2023-2024</MedlineDate></PubDate></Book></BookDocument></PubmedBookArticle>'
        result, _ = self.resolve('12345678', pubmed(record))
        self.assertEqual(result['book_title'], 'Fictional book')
        self.assertEqual(result['year'], 2023)
        self.assertIsNone(result['journal'])
        self.assertIsNone(result['abstract'])

    def test_redirects_rate_errors_and_oversize_never_retry(self):
        for response, error in [
            (ref.Response(302, {'Location': 'http://127.0.0.1/private'}, b''), ref.ReferenceProtocolError),
            (ref.Response(429, {'Retry-After': '120'}, b''), ref.ReferenceRateLimited),
            (ref.Response(503, {}, b'private response body'), ref.ReferenceNetworkError),
            (ref.Response(200, {}, b'x' * (ref.MAX_BYTES + 1)), ref.ReferenceProtocolError),
        ]:
            calls = []
            def transport(*args, **kwargs):
                calls.append(args)
                return response
            with self.subTest(status=response.status), self.assertRaises(error) as caught:
                ref.resolve('12345678', transport=transport)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('private response body', str(caught.exception))
        with self.assertRaises(ref.ReferenceRateLimited) as caught:
            ref.resolve('12345678', transport=lambda *a, **k: ref.Response(429, {'retry-after': '120'}, b''))
        self.assertEqual(caught.exception.retry_after, 120)

    def test_malformed_documents_and_xml_entities_fail_closed(self):
        cases = [
            ('12345678', b'<PubmedArticleSet><unexpected/></PubmedArticleSet>'),
            ('12345678', b'<eFetchResult><ERROR>provider error</ERROR></eFetchResult>'),
            ('12345678', b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private">]><PubmedArticleSet>&x;</PubmedArticleSet>'),
            ('12345678', b'<PubmedArticleSet>' + b'<a>' * 101 + b'</a>' * 101 + b'</PubmedArticleSet>'),
            ('10.9999/fictional', b'{invalid json'),
            ('10.9999/fictional', b'{"status":"ok","message":{"items":[]}}'),
        ]
        for identifier, body in cases:
            with self.subTest(body=body[:60]), self.assertRaises(ref.ReferenceProtocolError):
                ref.resolve(identifier, transport=lambda *a, **k: ref.Response(200, {}, body))

    def test_external_pubmed_dtd_is_not_downloaded(self):
        response = pubmed(article())
        response = ref.Response(200, {}, b'<!DOCTYPE PubmedArticleSet SYSTEM "https://example.invalid/private.dtd">' + response.body)
        with patch('http.client.HTTPSConnection', side_effect=AssertionError('No additional fetch permitted')):
            result, _ = self.resolve('12345678', response)
        self.assertEqual(result['status'], 'resolved')

    def test_rate_throttle_is_deterministic_and_long_cooldown_fails_promptly(self):
        now, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        throttle = ref._Throttle(clock=lambda: now[0], sleep=sleep)
        throttle.wait()
        throttle.wait()
        self.assertEqual(sleeps, [1.0])
        throttle.cooldown(60)
        with self.assertRaises(ref.ReferenceRateLimited) as caught:
            throttle.wait()
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertEqual(sleeps, [1.0])

    def test_default_transport_rejects_nonofficial_hosts_before_connection(self):
        with patch('http.client.HTTPSConnection', side_effect=AssertionError('Must not connect')):
            for url in ('http://api.crossref.org/works/x', 'https://localhost/x',
                        'https://api.crossref.org.evil.test/x', 'https://api.crossref.org:443/x',
                        'https://user:pass@api.crossref.org/x'):
                with self.subTest(url=url), self.assertRaises(ref.ReferenceInputError):
                    ref._network(url, timeout=1, max_bytes=100)

    def test_transport_timeout_is_controlled(self):
        with self.assertRaises(ref.ReferenceNetworkError):
            ref.resolve('12345678', transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
