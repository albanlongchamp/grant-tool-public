"""Single-article full-text invariants using invented provider responses only.

The injectable transport never calls a public API. These tests prove mechanical
identity, provenance and extraction boundaries, not scientific claim support.
"""
import base64
from copy import deepcopy
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from grantlib import publication_text as fulltext
from grantlib.references import ReferenceError, Response


DOI = '10.9999/fictional-paired'
LICENSE_URL = 'https://creativecommons.org/licenses/by/4.0/'


def jats(*, pmcid='PMC123', pmid='456', doi=DOI, body=None,
         license_url=LICENSE_URL, license_text='Fictional article licensed under CC BY 4.0.',
         article_type='research-article', prefix=''):
    body = body if body is not None else '''
      <sec id="methods"><title>Methods</title>
        <p id="paired">Six fictional subjects provided paired specimens; twelve aliquots were technical subsamples.</p>
        <sec id="limits"><title>Interpretation</title>
          <p>The planned assay <italic>does not establish</italic> a completed result. A negative result remains conditional on adequate precision.</p>
        </sec>
      </sec>'''
    return (prefix + f'''<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="{article_type}">
      <front><journal-meta><journal-title-group><journal-title>Fictional Journal</journal-title></journal-title-group></journal-meta>
        <article-meta>
          <article-id pub-id-type="pmc">{pmcid}</article-id>
          <article-id pub-id-type="pmid">{pmid}</article-id>
          <article-id pub-id-type="doi">{doi}</article-id>
          <title-group><article-title>Fictional <italic>paired</italic> study</article-title></title-group>
          <contrib-group><contrib contrib-type="author"><name><surname>Example</surname><given-names>Fictional</given-names></name></contrib></contrib-group>
          <pub-date pub-type="epub"><year>2025</year></pub-date>
          <permissions><license license-type="open-access" xlink:href="{license_url}"><license-p>{license_text}</license-p></license></permissions>
          <abstract><p id="abstract-p">FICTIONAL: no real participants or findings are described.</p></abstract>
        </article-meta></front>
      <body>{body}</body>
      <back><ref-list><ref id="ref1"><mixed-citation><article-title>Unrelated fictional citation</article-title><pub-id pub-id-type="doi">10.9999/not-the-article</pub-id></mixed-citation></ref></ref-list></back>
    </article>''').encode('utf-8')


def fixture(*, xml=None, versions=None, metadata_changes=None, record_changes=None):
    xml = xml if xml is not None else jats()
    versions = versions if versions is not None else [
        {'aiid': 123, 'pmcid': 'PMC123.1', 'doi': DOI, 'current': True},
    ]
    record = {'aiid': 123, 'pmcid': 'PMC123', 'pmid': 456, 'doi': DOI,
              'requested-id': 'PMC123', 'versions': deepcopy(versions)}
    record.update(record_changes or {})
    identity = {'status': 'ok', 'records': [record]}
    metadata = {'pmcid': 'PMC123', 'version': 1, 'pmid': 456, 'doi': DOI,
                'title': 'Fictional paired study', 'citation': 'Fictional Journal. 2025.',
                'is_pmc_openaccess': True, 'is_manuscript': False,
                'is_retracted': False, 'license_code': 'CC BY',
                'xml_url': 'https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.xml?md5='
                           + hashlib.md5(xml).hexdigest()}
    metadata.update(metadata_changes or {})
    return identity, metadata, xml


class FakeTransport:
    def __init__(self, identity, metadata, xml, *, replacements=None):
        self.identity, self.metadata, self.xml = identity, metadata, xml
        self.calls = []
        self.replacements = replacements or {}

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append({'url': url, 'timeout': timeout, 'max_bytes': max_bytes})
        parsed = urlsplit(url)
        if parsed.hostname == 'pmc.ncbi.nlm.nih.gov' and parsed.path == '/tools/idconv/api/v1/articles/':
            stage, body = 'identity', json.dumps(self.identity).encode()
        elif parsed.hostname == 'pmc-oa-opendata.s3.amazonaws.com' and parsed.path.endswith('.json'):
            stage, body = 'metadata', json.dumps(self.metadata).encode()
        elif parsed.hostname == 'pmc-oa-opendata.s3.amazonaws.com' and parsed.path.endswith('.xml'):
            stage, body = 'xml', self.xml
        else:
            raise AssertionError('Unexpected request outside the three fixed public resources: ' + url)
        return self.replacements.get(stage, Response(200, {}, body))


class PublicationTextTests(TestCase):
    def fetch(self, pmcid='PMC123', *, data=None, replacements=None):
        transport = FakeTransport(*(data or fixture()), replacements=replacements)
        with patch('http.client.HTTPSConnection', side_effect=AssertionError('No network in synthetic tests')):
            result = fulltext.fetch(pmcid, transport=transport)
        return result, transport

    def test_invalid_identifiers_fail_before_transport(self):
        self.assertTrue(issubclass(fulltext.PublicationTextError, ReferenceError))
        for value in (None, 123, '', '123', 'PMC0', 'PMC0123', 'PMC123.0',
                      'PMC123,PMC456', 'PMC123/../../private', 'https://pmc.ncbi.nlm.nih.gov/articles/PMC123/',
                      'PMC123?query=private', 'PMC123#x', 'PMC123\nPMC456'):
            with self.subTest(value=value), self.assertRaises(ReferenceError):
                fulltext.fetch(value, transport=lambda *a, **k: self.fail('Invalid identifier reached transport'))

    def test_exact_extraction_hashes_and_raw_snapshot_keep_scope(self):
        data = fixture()
        before = deepcopy(data)
        result, transport = self.fetch(data=data)
        self.assertEqual(data, before)
        self.assertEqual(result['status'], 'retrieved')
        self.assertEqual(result['article_version'], 'PMC123.1')
        self.assertEqual(result['canonical']['pmcid'], 'PMC123')
        self.assertEqual(result['canonical']['doi'], DOI)
        self.assertEqual(result['title'], 'Fictional paired study')
        self.assertEqual(result['article_type'], 'research-article')
        self.assertEqual(result['license']['open_access'], True)
        self.assertEqual(result['license']['url'], LICENSE_URL)
        self.assertEqual(result['provenance']['version_selection'], 'pmc_current')
        self.assertIs(result['provenance']['current_version'], True)
        self.assertEqual(base64.b64decode(result['raw_xml_b64']), data[2])
        self.assertEqual(result['provenance']['xml_sha256'], hashlib.sha256(data[2]).hexdigest())
        self.assertEqual(result['provenance']['provider_md5'], hashlib.md5(data[2]).hexdigest())
        self.assertEqual(result['provenance']['identity_sha256'], hashlib.sha256(json.dumps(data[0]).encode()).hexdigest())
        self.assertEqual(result['provenance']['metadata_sha256'], hashlib.sha256(json.dumps(data[1]).encode()).hexdigest())
        paragraphs = result['paragraphs']
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual([p['kind'] for p in paragraphs], ['abstract', 'body', 'body'])
        self.assertEqual(paragraphs[1]['text'], 'Six fictional subjects provided paired specimens; twelve aliquots were technical subsamples.')
        self.assertEqual(paragraphs[2]['text'], 'The planned assay does not establish a completed result. A negative result remains conditional on adequate precision.')
        self.assertEqual(paragraphs[1]['element_id'], 'paired')
        self.assertEqual(paragraphs[1]['xml_path'], '/article[1]/body[1]/sec[1]/p[1]')
        self.assertEqual(paragraphs[2]['xml_path'], '/article[1]/body[1]/sec[1]/sec[1]/p[1]')
        self.assertIn('Methods', paragraphs[1]['section'])
        self.assertIn('Interpretation', paragraphs[2]['section'])
        self.assertEqual(len({p['xml_path'] for p in paragraphs}), 3)
        self.assertEqual(len({p['id'] for p in paragraphs}), 3)
        for p in paragraphs:
            self.assertTrue(p['locator'])
            self.assertTrue(p['xml_path'])
            self.assertEqual(p['text_sha256'], hashlib.sha256(p['text'].encode()).hexdigest())
        self.assertNotIn('Unrelated fictional citation', '\n'.join(p['text'] for p in paragraphs))
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(all(0 < call['timeout'] <= transport.calls[0]['timeout'] for call in transport.calls))
        self.assertTrue(all(call['max_bytes'] > 0 for call in transport.calls))
        query = parse_qs(urlsplit(transport.calls[0]['url']).query)
        self.assertEqual(query['versions'], ['yes'])
        self.assertEqual(query['ids'], ['PMC123'])
        json.dumps(result, allow_nan=False)

    def test_new_jats_versioned_ids_agree_and_http_cc_license_is_preserved(self):
        xml = jats(license_url=LICENSE_URL.replace('https:', 'http:')).replace(
            b'<article-id pub-id-type="pmc">PMC123</article-id>',
            b'<article-id pub-id-type="pmcid">PMC123</article-id>'
            b'<article-id pub-id-type="pmcid-ver">PMC123.1</article-id>'
            b'<article-id pub-id-type="pmcaid">123</article-id>'
            b'<article-id pub-id-type="pmcaiid">123</article-id>')
        result, _ = self.fetch(data=fixture(xml=xml))
        self.assertEqual(result['article_version'], 'PMC123.1')
        self.assertEqual(result['license']['url'], 'http://creativecommons.org/licenses/by/4.0/')
        for bad_xml in (xml.replace(b'PMC123.1</article-id>', b'PMC123.2</article-id>'),
                        xml.replace(b'pub-id-type="pmcaiid">123', b'pub-id-type="pmcaiid">999')):
            with self.subTest(bad_xml=bad_xml[:40]), self.assertRaises(ReferenceError):
                self.fetch(data=fixture(xml=bad_xml))

    def test_current_version_not_highest_and_explicit_old_version_distinct(self):
        versions = [
            {'aiid': 123, 'pmcid': 'PMC123.1', 'doi': DOI, 'current': True},
            {'aiid': 789, 'pmcid': 'PMC123.2', 'doi': '10.9999/fictional-later', 'current': False},
        ]
        result, transport = self.fetch(data=fixture(versions=versions))
        self.assertEqual(result['article_version'], 'PMC123.1')
        self.assertIn('PMC123.1.json', transport.calls[1]['url'])
        # The actual ID Converter echoes the base PMCID for an explicit version.
        explicit_data = fixture(versions=versions, xml=jats(doi='10.9999/fictional-later'), metadata_changes={
            'version': 2, 'doi': '10.9999/fictional-later',
        })
        digest = hashlib.md5(explicit_data[2]).hexdigest()
        explicit_data[1]['xml_url'] = f'https://pmc-oa-opendata.s3.amazonaws.com/PMC123.2/PMC123.2.xml?md5={digest}'
        result, _ = self.fetch('PMC123.2', data=explicit_data)
        self.assertEqual(result['article_version'], 'PMC123.2')
        self.assertEqual(result['canonical']['doi'], '10.9999/fictional-later')
        self.assertEqual(result['provenance']['version_selection'], 'explicit')
        self.assertIs(result['provenance']['current_version'], False)
        for echoed in ('PMC999', 'PMC123.3'):
            explicit_data[0]['records'][0]['requested-id'] = echoed
            with self.subTest(echoed=echoed), self.assertRaises(ReferenceError):
                self.fetch('PMC123.2', data=explicit_data)

    def test_missing_ambiguous_or_string_current_never_selects_implicitly(self):
        for flags in ([False], ['true'], [True, True]):
            versions = [{'pmcid': f'PMC123.{i + 1}', 'current': flag, 'doi': DOI} for i, flag in enumerate(flags)]
            transport = FakeTransport(*fixture(versions=versions))
            with self.subTest(flags=flags), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=transport)
            self.assertEqual(len(transport.calls), 1)
        with self.assertRaises(ReferenceError):
            self.fetch('PMC123.99')

    def test_identity_metadata_and_article_ids_must_agree(self):
        cases = [
            fixture(record_changes={'pmcid': 'PMC999'}),
            fixture(metadata_changes={'pmcid': 'PMC999'}),
            fixture(metadata_changes={'version': 2}),
            fixture(metadata_changes={'pmid': 999}),
            fixture(metadata_changes={'doi': '10.9999/different'}),
            fixture(xml=jats(pmcid='PMC999')),
            fixture(xml=jats(pmid='999')),
            fixture(xml=jats(doi='10.9999/different')),
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ReferenceError):
                self.fetch(data=data)

    def test_duplicate_primary_ids_or_multiple_identity_records_are_not_collapsed(self):
        data = fixture()
        data[0]['records'].append(deepcopy(data[0]['records'][0]))
        with self.assertRaises(ReferenceError):
            self.fetch(data=data)
        xml = jats().replace(b'<title-group>',
                             b'<article-id pub-id-type="pmcid">PMC999</article-id><title-group>')
        with self.assertRaises(ReferenceError):
            self.fetch(data=fixture(xml=xml))

    def test_non_oa_tdm_and_missing_license_do_not_reach_xml(self):
        for change in ({'is_pmc_openaccess': False}, {'is_pmc_openaccess': 'false'},
                       {'license_code': 'TDM'}, {'license_code': ''}, {'license_code': None}):
            transport = FakeTransport(*fixture(metadata_changes=change))
            with self.subTest(change=change), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=transport)
            self.assertEqual(len(transport.calls), 2)

    def test_metadata_only_license_is_disclosed_and_contradictory_xml_rejected(self):
        result, _ = self.fetch(data=fixture(xml=jats(license_url='', license_text='')))
        self.assertEqual(result['license']['code'], 'CC BY')
        self.assertEqual(result['license']['source'], 'metadata_only')
        self.assertEqual(result['license']['statement'], '')
        self.assertTrue(any('license' in note.lower() and 'metadata' in note.lower()
                            for note in result['limitations']))
        xml = jats(license_url='https://creativecommons.org/licenses/by-nc/4.0/',
                   license_text='Fictional article licensed for noncommercial reuse only.')
        with self.assertRaises(ReferenceError):
            self.fetch(data=fixture(xml=xml))

    def test_checksum_or_external_xml_url_cannot_be_accepted(self):
        bad_urls = (
            'https://example.invalid/private.xml',
            'http://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.xml',
            'https://pmc-oa-opendata.s3.amazonaws.com.evil.invalid/PMC123.1.xml',
            'https://user:pass@pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.xml',
            'https://pmc-oa-opendata.s3.amazonaws.com:443/PMC123.1/PMC123.1.xml',
            'https://pmc-oa-opendata.s3.amazonaws.com/PMC999.1/PMC999.1.xml',
        )
        for url in bad_urls:
            transport = FakeTransport(*fixture(metadata_changes={'xml_url': url}))
            with self.subTest(url=url), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=transport)
            self.assertEqual(len(transport.calls), 2)
        data = fixture()
        data[1]['xml_url'] = data[1]['xml_url'].split('?')[0] + '?md5=' + '0' * 32
        with self.assertRaises(ReferenceError):
            self.fetch(data=data)

    def test_official_s3_object_is_converted_only_to_fixed_https_host(self):
        data = fixture()
        provider_url = data[1]['xml_url'].replace('https://pmc-oa-opendata.s3.amazonaws.com/',
                                                's3://pmc-oa-opendata/')
        data[1]['xml_url'] = provider_url
        result, transport = self.fetch(data=data)
        expected_url = provider_url.replace('s3://pmc-oa-opendata/',
                                            'https://pmc-oa-opendata.s3.amazonaws.com/')
        self.assertEqual(transport.calls[-1]['url'], expected_url)
        self.assertEqual(result['provenance']['xml_url'], expected_url)
        self.assertEqual(result['provenance']['provider_xml_url'], provider_url)
        for invalid in (provider_url.replace('s3://pmc-oa-opendata/', 's3://different-bucket/'),
                        provider_url.replace('/PMC123.1/PMC123.1.xml', '/PMC999.1/PMC999.1.xml'),
                        provider_url.replace('s3://pmc-oa-opendata/', 's3://user@pmc-oa-opendata/'),
                        provider_url.replace('s3://pmc-oa-opendata/', 's3://pmc-oa-opendata:443/')):
            transport = FakeTransport(*fixture(metadata_changes={'xml_url': invalid}))
            with self.subTest(url=invalid), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=transport)
            self.assertEqual(len(transport.calls), 2)

    def test_redirect_http_failure_and_oversize_do_not_retry(self):
        for stage in ('identity', 'metadata', 'xml'):
            expected_calls = ('identity', 'metadata', 'xml').index(stage) + 1
            for status in (302, 404, 429, 503):
                transport = FakeTransport(*fixture(), replacements={stage: Response(status, {'location': 'https://example.invalid/private'}, b'UNTRUSTED BODY')})
                with self.subTest(stage=stage, status=status), self.assertRaises(ReferenceError) as caught:
                    fulltext.fetch('PMC123', transport=transport)
                self.assertEqual(len(transport.calls), expected_calls)
                self.assertNotIn('UNTRUSTED BODY', str(caught.exception))
        for stage in ('identity', 'metadata', 'xml'):
            transport = FakeTransport(*fixture())
            def oversized(url, *, timeout, max_bytes):
                response = transport(url, timeout=timeout, max_bytes=max_bytes)
                current = ('identity', 'metadata', 'xml')[len(transport.calls) - 1]
                return Response(200, {}, b'x' * (max_bytes + 1)) if current == stage else response
            with self.subTest(oversize=stage), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=oversized)

    def test_xml_entities_malformed_and_structure_limits_fail(self):
        variants = [
            b'<!DOCTYPE article [<!ENTITY injected SYSTEM "file:///private">]>' + jats(body='<p>&injected;</p>'),
            b'<!DOCTYPE article [<!ENTITY injected "invented entity">]>' + jats(body='<p>&injected;</p>'),
            b'<article><unclosed>',
            jats(body='<sec>' * 200 + '<p>Deep fictional text.</p>' + '</sec>' * 200),
        ]
        for encoding in ('UTF-16', 'UTF-32'):
            declaration = '<?xml version="1.0" encoding="' + encoding + '"?>'
            entity = '<!DOCTYPE article [<!ENTITY injected "invented entity">]>'
            variants.append((declaration + entity + jats(body='<p>&injected;</p>').decode('utf-8')).encode(encoding))
        for xml in variants:
            with self.subTest(prefix=xml[:60]), self.assertRaises(ReferenceError):
                self.fetch(data=fixture(xml=xml))

    def test_malformed_identity_or_metadata_fails_without_article_retrieval(self):
        variants = [
            ('identity', b'{invalid json'),
            ('identity', b'{"status":"ok","records":[]}'),
            ('identity', b'{"status":"error","records":[]}'),
            ('metadata', b'{invalid json'),
            ('metadata', b'[]'),
        ]
        for stage, payload in variants:
            transport = FakeTransport(*fixture(), replacements={stage: Response(200, {}, payload)})
            with self.subTest(stage=stage, payload=payload), self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=transport)
            self.assertLessEqual(len(transport.calls), 2)

    def test_malformed_metadata_types_return_controlled_errors(self):
        for value in (None, 17, [], {}):
            with self.subTest(field='requested-id', value=value), self.assertRaises(ReferenceError):
                self.fetch(data=fixture(record_changes={'requested-id': value}))
        for value in (17, [], {}, True):
            with self.subTest(field='license_code', value=value), self.assertRaises(ReferenceError):
                self.fetch(data=fixture(metadata_changes={'license_code': value}))

    def test_shared_deadline_reduces_request_budget_and_stops_later_fetches(self):
        clock = [100.0]
        transport = FakeTransport(*fixture())
        def slow(url, **kwargs):
            response = transport(url, **kwargs)
            clock[0] += 20 if len(transport.calls) < 3 else 1
            return response
        with patch.object(fulltext.time, 'monotonic', side_effect=lambda: clock[0]):
            result = fulltext.fetch('PMC123', transport=slow)
        self.assertEqual(result['status'], 'retrieved')
        self.assertEqual([call['timeout'] for call in transport.calls], [15, 15, 5])
        clock[0] = 100.0
        transport = FakeTransport(*fixture())
        def expired(url, **kwargs):
            response = transport(url, **kwargs)
            clock[0] += 46
            return response
        with patch.object(fulltext.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ReferenceError):
                fulltext.fetch('PMC123', transport=expired)
        self.assertEqual(len(transport.calls), 1)
        with self.assertRaises(ReferenceError):
            fulltext.fetch('PMC123', transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))

    def test_external_doctype_is_not_fetched(self):
        xml = jats(prefix='<!DOCTYPE article SYSTEM "https://example.invalid/never-fetch.dtd">')
        result, transport = self.fetch(data=fixture(xml=xml))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(result['paragraphs']), 3)

    def test_unsupported_science_is_omitted_whole_with_visible_coverage(self):
        body = '''<sec><title>Fictional findings</title>
          <p id="safe">A valid literal paragraph remains available.</p>
          <p id="formula">A conclusion depends on <inline-formula><math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math></inline-formula> and cannot be safely flattened.</p>
          <fig id="fig1"><caption><p>FICTIONAL FIGURE CAPTION, not body evidence.</p></caption></fig>
          <table-wrap id="tab1"><table><tr><td>FICTIONAL TABLE CELL, not paragraph evidence.</td></tr></table></table-wrap>
          <supplementary-material><p>FICTIONAL SUPPLEMENT, not fetched evidence.</p></supplementary-material>
        </sec>'''
        result, _ = self.fetch(data=fixture(xml=jats(body=body)))
        texts = '\n'.join(p['text'] for p in result['paragraphs'])
        self.assertIn('A valid literal paragraph remains available.', texts)
        for omitted in ('A conclusion depends on', 'FIGURE CAPTION', 'TABLE CELL', 'FICTIONAL SUPPLEMENT'):
            self.assertNotIn(omitted, texts)
        self.assertTrue(result['limitations'])
        self.assertEqual(result['coverage']['paragraph_count'], 2)
        omissions = result['coverage']['omitted_counts']
        self.assertEqual(omissions['figures'], 1)
        self.assertEqual(omissions['tables'], 1)
        self.assertEqual(omissions['supplements'], 1)
        self.assertEqual(omissions['formulas'], 1)
        self.assertEqual(omissions['paragraphs_with_unsupported_content'], 1)
        self.assertEqual(omissions['reference_entries'], 1)
        self.assertTrue(result['coverage']['warnings'])

    def test_subscripts_exponents_quantities_and_negation_remain_readable(self):
        body = '''<sec><title>Fictional results</title><p id="chemistry">CO<sub>2</sub> was not measured in six fictional subjects. The threshold was 10<sup>−3</sup> units; twelve aliquots <italic>did not</italic> increase the biological sample size.</p></sec>'''
        result, _ = self.fetch(data=fixture(xml=jats(body=body)))
        retained = next(p for p in result['paragraphs'] if p['kind'] == 'body')
        expected = 'CO_{2} was not measured in six fictional subjects. The threshold was 10^{−3} units; twelve aliquots did not increase the biological sample size.'
        self.assertEqual(retained['text'], expected)
        self.assertEqual(retained['text_sha256'], hashlib.sha256(expected.encode()).hexdigest())
        self.assertTrue(any('subscript' in item.lower() for item in result['coverage']['warnings']))

    def test_direct_mathml_and_tex_are_not_flattened_into_scientific_prose(self):
        body = '''<sec><title>Fictional calculations</title>
          <p>A supported control paragraph remains.</p>
          <p>A ratio is <mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mfrac><mml:mi>a</mml:mi><mml:mi>b</mml:mi></mml:mfrac></mml:math>; the denominator must be nonzero.</p>
          <p>A second ratio is <tex-math>\\frac{c}{d}</tex-math>, with separate assumptions.</p>
        </sec>'''
        result, _ = self.fetch(data=fixture(xml=jats(body=body)))
        self.assertEqual([p['text'] for p in result['paragraphs'] if p['kind'] == 'body'],
                         ['A supported control paragraph remains.'])
        self.assertEqual(result['coverage']['omitted_counts']['formulas'], 2)
        self.assertEqual(result['coverage']['omitted_counts']['paragraphs_with_unsupported_content'], 2)

    def test_nested_excluded_content_never_merges_into_retained_body(self):
        body = '''<sec><title>Fictional results</title>
          <p>A supported control paragraph remains.</p>
          <p>Six subjects were eligible.<fn><p>Only four contributed pairs.</p></fn> This qualification must not silently disappear.</p>
          <p>A second result.<fn-group><fn><p>Fictional footnote qualification.</p></fn></fn-group></p>
          <p>A third result.<ref-list><ref><mixed-citation>Not this article's own result.</mixed-citation></ref></ref-list></p>
          <p>A fourth result.<caption><p>Fictional caption context.</p></caption></p>
          <fn><p>A standalone footnote is not a body paragraph.</p></fn>
          <caption><p>A standalone caption is not a body paragraph.</p></caption>
        </sec>'''
        result, _ = self.fetch(data=fixture(xml=jats(body=body)))
        self.assertEqual([p['text'] for p in result['paragraphs'] if p['kind'] == 'body'],
                         ['A supported control paragraph remains.'])
        self.assertEqual(result['coverage']['omitted_counts']['paragraphs_with_unsupported_content'], 4)

    def test_article_text_and_paragraph_limits_do_not_silently_truncate(self):
        variants = [
            jats(body='<p>' + 'x' * 500001 + '</p>'),
            jats(body=''.join(f'<p>Fictional paragraph {i}.</p>' for i in range(2001))),
        ]
        for xml in variants:
            with self.subTest(bytes=len(xml)), self.assertRaises(ReferenceError):
                self.fetch(data=fixture(xml=xml))

    def test_missing_notice_flags_remain_unknown(self):
        data = fixture()
        del data[1]['is_retracted']
        del data[1]['is_manuscript']
        result, _ = self.fetch(data=data)
        self.assertIsNone(result['publication_notices']['retracted'])
        self.assertIsNone(result['publication_notices']['is_manuscript'])
        self.assertTrue(result['publication_notices']['scope'])
        self.assertTrue(result['publication_notices']['limitations'])
