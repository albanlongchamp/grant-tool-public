"""Live-search protocol handling with invented PubMed and iCite responses; no network."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from grantlib import publication_search as ps
from grantlib import references as ref
from tests.test_references import article, pubmed


def esearch(ids, count=None):
    return ref.Response(200, {}, json.dumps({'esearchresult': {'count': str(count if count is not None else len(ids)),
                                                                 'idlist': ids}}).encode())


def icite(rows):
    return ref.Response(200, {}, json.dumps({'data': rows}).encode())


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        parts = urlsplit(url)
        self.assert_host(parts.hostname)
        key = 'icite' if parts.hostname == 'icite.od.nih.gov' else parts.path.rsplit('/', 1)[-1].split('.')[0]
        response = self.responses[key]
        return response(url) if callable(response) else response

    @staticmethod
    def assert_host(host):
        assert host in ps.HOSTS, host


class PublicationSearchTests(unittest.TestCase):
    def test_query_validation_keeps_private_or_structured_text_off_the_network(self):
        for bad in (None, '', 'x', 'a' * 501, 'two\nlines', 'see [[R_1]]', 'https://example.org/x', r'\\fileserver\drafts\aims.docx'):
            with self.subTest(query=bad), self.assertRaises(ValueError):
                ps.search(bad, transport=lambda *a, **k: self.fail('Invalid query reached transport'))
        with self.assertRaises(ValueError):
            ps.search('kidney transplant', limit=0, transport=lambda *a, **k: self.fail('limit'))
        with self.assertRaises(ValueError):
            ps.search('kidney transplant', sort='random', transport=lambda *a, **k: self.fail('sort'))
        with self.assertRaises(ValueError):
            ps.search('kidney transplant', min_year=99, transport=lambda *a, **k: self.fail('year'))

    def test_candidates_follow_pubmed_order_with_icite_metrics_and_notice_flags(self):
        transport = FakeTransport({
            'esearch': esearch(['12345678', '87654321'], count=240),
            'efetch': pubmed(article('12345678', types='<PublicationType>Review</PublicationType><PublicationType>Journal Article</PublicationType>'),
                             article('87654321', links='<CommentsCorrections RefType="RetractionIn"><RefSource>Invent J. 2025</RefSource><PMID>1</PMID></CommentsCorrections>')),
            'icite': icite([{'pmid': 12345678, 'citation_count': 42, 'relative_citation_ratio': 2.5, 'nih_percentile': 91.0,
                             'is_research_article': 'No', 'is_clinical': 'No'},
                            {'pmid': '87654321', 'citation_count': 3, 'relative_citation_ratio': 0.4, 'nih_percentile': 20.0}])})
        result = ps.search('  fictional   seedling irrigation ', limit=2, min_year=2015, transport=transport)
        self.assertEqual(result['query'], 'fictional seedling irrigation')
        self.assertEqual(result['total_matches'], 240)
        self.assertEqual([c['pmid'] for c in result['candidates']], ['12345678', '87654321'])
        first, second = result['candidates']
        self.assertEqual((first['rank'], first['citation_count'], first['relative_citation_ratio']), (1, 42, 2.5))
        self.assertEqual(first['publication_types'], ['Review'])
        self.assertEqual(first['doi'], '10.9999/fictional')
        self.assertEqual(first['first_authors'], ['Example Author', 'Invented Group'])
        self.assertEqual(second['notice_flags'], ['retracted'])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['provenance']['metrics_provider'], 'icite')
        self.assertEqual(len(transport.calls), 3)
        search_query = parse_qs(urlsplit(transport.calls[0]).query)
        self.assertEqual(search_query['sort'], ['relevance'])
        self.assertEqual(search_query['term'], ['(fictional seedling irrigation) AND (2015[dp] : 3000[dp])'])
        self.assertEqual(parse_qs(urlsplit(transport.calls[1]).query)['id'], ['12345678,87654321'])
        self.assertIn('pmids=12345678%2C87654321', transport.calls[2])
        self.assertIn('grants_resolve_reference', result['next_step'])

    def test_icite_failure_degrades_to_a_warning_and_most_cited_reorders(self):
        transport = FakeTransport({
            'esearch': esearch(['11111111', '22222222', '33333333']),
            'efetch': pubmed(article('11111111'), article('22222222'), article('33333333')),
            'icite': icite([{'pmid': 22222222, 'citation_count': 9}, {'pmid': 33333333, 'citation_count': 20}])})
        result = ps.search('invented topic', limit=2, sort='most_cited', transport=transport)
        self.assertEqual([c['pmid'] for c in result['candidates']], ['33333333', '22222222'])
        self.assertEqual(parse_qs(urlsplit(transport.calls[0]).query)['retmax'], ['4'])
        broken = FakeTransport({'esearch': esearch(['11111111']), 'efetch': pubmed(article('11111111')),
                                'icite': ref.Response(500, {}, b'oops')})
        result = ps.search('invented topic', limit=1, transport=broken)
        self.assertEqual(result['candidates'][0]['pmid'], '11111111')
        self.assertNotIn('citation_count', result['candidates'][0])
        self.assertTrue(any('iCite' in w for w in result['warnings']))
        self.assertIsNone(result['provenance']['metrics_provider'])

    def test_unsolicited_or_invalid_records_and_rate_limits_fail_closed(self):
        transport = FakeTransport({'esearch': esearch(['11111111']), 'efetch': pubmed(article('99999999'))})
        with self.assertRaises(ps.PublicationSearchError):
            ps.search('invented topic', limit=1, transport=transport)
        transport = FakeTransport({'esearch': ref.Response(429, {'Retry-After': '7'}, b'')})
        with self.assertRaises(ref.ReferenceRateLimited):
            ps.search('invented topic', transport=transport)
        transport = FakeTransport({'esearch': ref.Response(200, {}, b'not json')})
        with self.assertRaises(ps.PublicationSearchError):
            ps.search('invented topic', transport=transport)
        empty = FakeTransport({'esearch': esearch([], count=0)})
        result = ps.search('nothing matches', transport=empty)
        self.assertEqual((result['returned'], result['candidates'], len(empty.calls)), (0, [], 1))

    def test_network_transport_only_accepts_fixed_hosts(self):
        with self.assertRaises(ref.ReferenceInputError):
            ps._network('https://example.org/esearch.fcgi', timeout=1, max_bytes=10)


if __name__ == '__main__':
    unittest.main()
