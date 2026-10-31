"""Invented article snapshots only: no network, model, archive or application DB."""
import argparse
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from grantlib import publication_store, revision_check, studio_cli
from grantlib.studio import Conflict, Service, Store, export_markdown, make_prompt, prepare_context
from tests.test_claim_review import packet, response
from tests.test_revision_check import fixture, anchor, clean_report


def sha(value):
    return hashlib.sha256(value).hexdigest()


def article(*, version='PMC123456.1', body='Invented result: café α.\nLiteral [[not_a_source]] and ``` remain exact.', extra=None):
    texts = [('abstract', 'Abstract', 'An invented exploratory abstract; no general efficacy is established.'),
             ('body', 'Results', body),
             ('body', 'Discussion', 'UNSELECTED_ARTICLE_PARAGRAPH_CANARY: this is not part of any retained source.')]
    texts += [('body', 'Methods', value) for value in (extra or [])]
    xml = ('<article article-type="research-article"><body><p>' + version + '</p></body></article>').encode()
    paragraphs = [{'id': 'p%04d' % i, 'kind': kind, 'section': section, 'locator': section + ', paragraph ' + str(i),
                   'xml_path': '/article/body/sec/p[' + str(i) + ']', 'element_id': 'synthetic-' + str(i),
                   'text': text, 'text_sha256': sha(text.encode())}
                  for i, (kind, section, text) in enumerate(texts, 1)]
    return {'status': 'retrieved', 'requested_pmcid': 'PMC123456',
            'canonical': {'pmcid': 'PMC123456', 'pmid': '123456', 'doi': '10.0000/fiction'},
            'article_version': version, 'article_type': 'research-article',
            'title': 'Entirely invented article for local tests', 'authors': ['Invented A'],
            'journal': 'Fictional Journal', 'year': '2026', 'provider': 'ncbi_pmc_cloud',
            'source_url': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/',
            'retrieved_at': '2026-01-01T00:00:00+00:00',
            'license': {'code': 'CC BY', 'statement': 'Invented open-access terms.', 'url': 'https://creativecommons.org/licenses/by/4.0/', 'open_access': True},
            'publication_notices': {'retracted': False, 'is_manuscript': False, 'scope': 'Provider metadata only.', 'limitations': ['Fictional notice data.']},
            'provenance': {'identity_url': 'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/',
                           'metadata_url': 'https://pmc.ncbi.nlm.nih.gov/fictional', 'xml_url': 'https://pmc.ncbi.nlm.nih.gov/fictional.xml',
                           'identity_sha256': sha(b'fictional identity'), 'metadata_sha256': sha(b'fictional metadata'),
                           'xml_sha256': sha(xml), 'provider_md5': hashlib.md5(xml).hexdigest(),
                           'extractor_version': 'jats-paragraphs-v1', 'version_selection': 'pmc_current', 'current_version': True},
            'coverage': {'paragraph_count': len(paragraphs), 'included_scope': 'Abstract and body prose paragraphs only.',
                         'omitted_counts': {'figures': 1, 'tables': 1, 'supplements': 0, 'formulas': 0,
                                            'paragraphs_with_unsupported_content': 0, 'reference_entries': 2},
                         'warnings': ['Figures and tables are not retained.']},
            'limitations': ['Wholly invented fixture; not a real paper or scientific result.'],
            'paragraphs': paragraphs, 'raw_xml_b64': base64.b64encode(xml).decode()}


class PublicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Invented project', 'question': 'Private synthetic question must not leave this test.'})
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], packet(), response())
        self.p = self.store.get(self.p['id'])
        self.a = self.p['artifacts'][0]

    def tearDown(self):
        self.tmp.cleanup()

    def fetch_local(self, value=None):
        self.p = self.store.get(self.p['id'])
        result = self.store.add_full_text(self.p['id'], self.p['revision'], value or article())
        self.p = result['proposal']
        return result['document_id']

    def retain(self, document_id, paragraph_id='p0002'):
        self.p = self.store.get(self.p['id'])
        self.p = self.store.retain_publication_passage(self.p['id'], self.p['revision'], document_id, paragraph_id)
        return next(s for s in self.p['publication_passages'] if s['metadata']['document_id'] == document_id and s['metadata']['paragraph_id'] == paragraph_id)

    def test_reader_and_summary_are_separate_immutable_proposal_scoped_snapshots(self):
        raw = article()
        before = deepcopy(raw)
        document = self.fetch_local(raw)
        raw['paragraphs'][1]['text'] = 'mutated caller'
        summary = self.p['full_texts'][0]
        self.assertEqual(summary['id'], document)
        self.assertEqual(summary['article_type'], 'research-article')
        self.assertNotIn('paragraphs', summary)
        self.assertNotIn('raw_xml_b64', json.dumps(self.p))
        reader = self.store.full_text(self.p['id'], document)
        self.assertEqual(reader['paragraphs'], before['paragraphs'])
        self.assertNotIn('raw_xml_b64', reader)
        reader['paragraphs'][1]['text'] = 'mutated reader'
        self.assertEqual(self.store.full_text(self.p['id'], document)['paragraphs'], before['paragraphs'])
        with self.store.connect() as connection:
            saved = json.loads(connection.execute('SELECT snapshot FROM publication_documents WHERE id=?', (document,)).fetchone()[0])
        self.assertEqual(saved, before)
        other = self.store.create({'title': 'Other', 'question': 'Other fictional question'})
        with self.assertRaises(ValueError):
            self.store.full_text(other['id'], document)
        with self.assertRaises(ValueError):
            self.store.retain_publication_passage(other['id'], other['revision'], document, 'p0002')

    def test_multiple_passages_refresh_and_idempotence_preserve_old_evidence(self):
        document = self.fetch_local()
        first = self.retain(document)
        revision = self.p['revision']
        self.assertEqual(self.retain(document), first)
        self.assertEqual(self.p['revision'], revision)
        abstract = self.retain(document, 'p0001')
        self.assertEqual(abstract['metadata']['scope'], 'abstract')
        self.assertEqual(first['metadata']['scope'], 'full_text_excerpt')
        self.assertEqual(first['metadata']['retained_by'], 'assistant')
        self.assertNotEqual(first['id'], abstract['id'])
        document2 = self.fetch_local(article(version='PMC123456.2', body='A different retained paragraph in a new public version.'))
        changed = self.retain(document2)
        self.assertNotEqual(first['id'], changed['id'])
        self.assertEqual(next(s for s in self.p['publication_passages'] if s['id'] == first['id']), first)
        self.assertEqual(len(self.p['full_texts']), 2)
        self.assertEqual(len(self.p['publication_passages']), 3)
        compiled, _ = prepare_context(self.p, packet(), 'design', {})
        actual = {s['id']: s for s in compiled['sources']}
        for source in (first, abstract, changed):
            self.assertEqual(actual[source['id']], source)

    def test_license_metadata_scope_and_actual_retention_actor_are_preserved(self):
        raw = article()
        raw['license'].update(statement=None, source='metadata_only')
        raw['limitations'].append('No XML license statement was present; terms come from provider metadata only.')
        document = self.fetch_local(raw)
        self.assertEqual(self.store.full_text(self.p['id'], document)['license'], raw['license'])
        p = self.store.retain_publication_passage(self.p['id'], self.p['revision'], document, 'p0002', actor='investigator')
        source = p['publication_passages'][0]
        self.assertEqual(source['metadata']['license'], raw['license'])
        self.assertEqual(source['metadata']['retained_by'], 'investigator')
        self.assertIn(raw['limitations'][-1], source['metadata']['limitations'])
        # A later assistant request cannot rewrite who first retained it.
        p2 = self.store.retain_publication_passage(p['id'], p['revision'], document, 'p0002')
        self.assertEqual(p2['revision'], p['revision'])
        self.assertEqual(p2['publication_passages'], p['publication_passages'])
        with self.assertRaisesRegex(ValueError, 'actually retained'):
            self.store.retain_publication_passage(p['id'], p['revision'], document, 'p0001', actor='automatically verified')

    def test_claim_judgment_can_read_uncited_passage_and_refresh_cannot_replace_it(self):
        document = self.fetch_local()
        source = self.retain(document)
        values = dict(source_id=source['id'], verdict='partial', excerpt=source['text'],
                      locator=source['metadata']['locator'], note='Limited to the exact invented result paragraph.', reviewer='assistant')
        p = self.store.assess_claim(self.p['id'], self.p['revision'], self.a['id'], self.a['claims'][0]['id'], **values)
        review = p['artifacts'][0]['claims'][0]['assessments'][0]
        self.assertEqual(review['source_snapshot'], source)
        self.assertEqual(review['excerpt'], source['text'])
        self.assertEqual(review['scope'], 'full_text_excerpt')
        self.assertTrue(any('retained full-text' in w for w in review['warnings']))
        document2 = self.fetch_local(article(version='PMC123456.2', body='Different results cannot overwrite old judgment.'))
        self.retain(document2)
        self.assertEqual(self.p['artifacts'][0]['claims'][0]['assessments'][0], review)
        with self.assertRaisesRegex(ValueError, 'match'):
            self.store.assess_claim(self.p['id'], self.p['revision'], self.a['id'], self.a['claims'][0]['id'], **{**values, 'excerpt': 'A fabricated improvement.'})
        compiled, views = prepare_context(self.p, packet(), 'revise', {'base_artifact_id': self.a['id']})
        judgment = views['selected_version']['recorded_claim_judgments'][0]
        self.assertEqual(judgment['excerpt'], source['text'])
        self.assertEqual(next(s for s in compiled['sources'] if s['id'] == judgment['source_id'])['text'], source['text'])

    def test_manual_citation_retains_source_and_literal_exports_without_complete_article(self):
        document = self.fetch_local()
        source = self.retain(document)
        original = deepcopy(self.a)
        self.p = self.store.save(self.p['id'], self.p['revision'], self.a['markdown'] + '\nRead this exact paragraph [[' + source['id'] + ']].',
                                 base_artifact_id=self.a['id'], origin='assistant_import')
        new = self.p['artifacts'][0]
        self.assertEqual(self.p['artifacts'][1], original)
        self.assertEqual(new['citation_check']['unknown_ids'], [])
        self.assertEqual(next(s for s in new['context']['sources'] if s['id'] == source['id']), source)
        exported = export_markdown(self.p, new['id'])
        for value in (source['text'], 'CC BY', 'PMC123456.1', 'research-article', source['metadata']['text_sha256']):
            self.assertIn(value, exported)
        self.assertNotIn('UNSELECTED_ARTICLE_PARAGRAPH_CANARY', exported)
        self.assertNotIn('raw_xml_b64', exported)
        compiled, views = prepare_context(self.p, packet(), 'revise', {'base_artifact_id': new['id']})
        prompt = make_prompt(self.p, compiled, 'revise', {'base_artifact_id': new['id']}, versions=views)
        self.assertIn(source['metadata']['text_sha256'], prompt)
        self.assertNotIn('UNSELECTED_ARTICLE_PARAGRAPH_CANARY', prompt)
        self.assertNotIn('raw_xml_b64', prompt)
        from grantlib.docx_export import export_docx
        from xml.etree import ElementTree
        docx = export_docx(self.p, new['id'])
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            tree = ElementTree.fromstring(archive.read('word/document.xml'))
        text = '\n'.join(n.text or '' for n in tree.iter() if n.tag.endswith('}t'))
        self.assertIn('Literal [[not_a_source]]', text)
        self.assertIn('PMC123456.1', text)
        self.assertIn('CC BY', text)
        self.assertNotIn('UNSELECTED_ARTICLE_PARAGRAPH_CANARY', text)

    def test_packet_budget_omits_whole_passages_and_retains_historical_cited_snapshot(self):
        # All paragraphs individually fit retention but together exceed 90k.
        document = self.fetch_local(article(extra=[('Invented paragraph %d. ' % i) + ('z' * 11500) for i in range(9)]))
        sources = [self.retain(document, 'p%04d' % i) for i in range(4, 13)]
        compiled, _ = prepare_context(self.p, {'sources': []}, 'design', {})
        admitted = {s['id']: s for s in compiled['sources']}
        omitted = compiled['coverage']['omitted_publication_passage_ids']
        self.assertTrue(omitted)
        self.assertEqual(set(admitted) | set(omitted), {s['id'] for s in sources})
        for source in sources:
            if source['id'] in admitted:
                self.assertEqual(admitted[source['id']], source)
        self.assertTrue(any('no selected paragraph was shortened' in w for w in compiled['warnings']))
        cited = next(s for s in sources if s['id'] in omitted)
        self.p = self.store.save(self.p['id'], self.p['revision'], self.a['markdown'] + '\nHistorical quote [[' + cited['id'] + ']].', base_artifact_id=self.a['id'])
        compiled, views = prepare_context(self.p, {'sources': []}, 'revise', {'base_artifact_id': self.p['artifacts'][0]['id']})
        self.assertTrue(any(s['text'] == cited['text'] and s['metadata']['text_sha256'] == cited['metadata']['text_sha256'] for s in compiled['sources']))
        self.assertNotIn(cited['id'], compiled['coverage']['omitted_publication_passage_ids'])
        self.assertFalse({s['id'] for s in compiled['sources']} & set(compiled['coverage']['omitted_publication_passage_ids']))
        self.assertIn('Historical quote', views['selected_version']['markdown'])
        # Restoring every initially omitted paragraph must clear the warning too.
        remaining = compiled['coverage']['omitted_publication_passage_ids']
        latest = self.p['artifacts'][0]
        self.p = self.store.save(self.p['id'], self.p['revision'], latest['markdown'] + '\n' +
                                 ' '.join('[[' + identifier + ']]' for identifier in remaining),
                                 base_artifact_id=latest['id'])
        compiled, _ = prepare_context(self.p, {'sources': []}, 'revise', {'base_artifact_id': self.p['artifacts'][0]['id']})
        self.assertEqual(compiled['coverage']['omitted_publication_passage_ids'], [])
        self.assertFalse(any('Some retained publication paragraphs were omitted' in w for w in compiled.get('warnings', [])))

    def test_snapshot_and_retention_bounds_fail_without_truncating_or_mutating(self):
        bad = article()
        bad['paragraphs'][1]['text'] = 'hash mismatch'
        with self.assertRaises(ValueError): self.fetch_local(bad)
        bad = article()
        bad['paragraphs'][1]['id'] = 'p0001'
        with self.assertRaises(ValueError): self.fetch_local(bad)
        bad = article()
        bad['license']['open_access'] = False
        with self.assertRaises(ValueError): self.fetch_local(bad)
        bad = article()
        bad['raw_xml_b64'] = base64.b64encode(b'different XML').decode()
        with self.assertRaises(ValueError): self.fetch_local(bad)
        document = self.fetch_local(article(body='x' * 12001))
        revision = self.p['revision']
        with self.assertRaisesRegex(ValueError, 'no text was truncated'): self.retain(document)
        current = self.store.get(self.p['id'])
        self.assertEqual(current['revision'], revision)
        self.assertEqual(current['publication_passages'], [])
        self.assertEqual(len(self.store.full_text(self.p['id'], document)['paragraphs'][1]['text']), 12001)
        with patch.object(publication_store, 'MAX_DOCUMENTS', 1), self.assertRaises(ValueError): self.fetch_local()

    def test_fetch_sends_only_explicit_public_identifier_and_checks_conflict_twice(self):
        service = Service(self.root / 'library.sqlite3', generate=lambda *a, **k: self.fail('No model allowed'), packet_builder=lambda *a, **k: self.fail('No archive allowed'))
        with patch('grantlib.publication_text.fetch', return_value=article()) as fetch:
            result = service.fetch_full_text(self.p['id'], self.p['revision'], 'PMC123456')
            fetch.assert_called_once_with('PMC123456')
            self.assertEqual(result['proposal']['full_texts'][0]['id'], result['document_id'])
            with self.assertRaises(Conflict): service.fetch_full_text(self.p['id'], self.p['revision'], 'PMC123456')
            self.assertEqual(fetch.call_count, 1)
        self.p = result['proposal']
        before_documents = deepcopy(self.p['full_texts'])
        def changed_during_fetch(pmcid):
            self.assertEqual(pmcid, 'PMC123456')
            self.store.decision(self.p['id'], self.p['revision'], 'A concurrent investigator choice.')
            return article(version='PMC123456.2')
        with patch('grantlib.publication_text.fetch', side_effect=changed_during_fetch), self.assertRaises(Conflict):
            service.fetch_full_text(self.p['id'], self.p['revision'], 'PMC123456')
        self.assertEqual(self.store.get(self.p['id'])['full_texts'], before_documents)

    def test_audit_source_provenance_and_export_keep_full_text_scope(self):
        source = self.retain(self.fetch_local())
        context, candidate = fixture()
        context['evidence_packet']['sources'] = [source]
        report = clean_report()
        report['findings'] = [{'id': 'F1', 'category': 'source_fidelity', 'severity': 'minor', 'resolution': 'investigator_input',
            'summary': 'Check the invented publication scope.', 'explanation': 'The retained text cannot establish general efficacy.',
            'repair': 'Ask the investigator about applicability.', 'before': [],
            'after': [anchor('candidate', 'Imaging will be blinded.')], 'authority': [],
            'sources': [anchor('source', source['text'], identifier=source['id'])]}]
        normalized = revision_check.validate(report, context, candidate)
        metadata = normalized['findings'][0]['sources'][0]['provenance']['metadata']
        for key in ('license', 'provenance', 'article_type', 'canonical', 'coverage', 'publication_notices', 'text_sha256'):
            self.assertEqual(metadata[key], source['metadata'][key])
        exported = revision_check.export_markdown(normalized)
        for value in (source['text'], 'CC BY', 'PMC123456.1', 'full_text_excerpt'):
            self.assertIn(value, exported)

    def test_cli_fetch_reader_and_retain_use_exact_public_identifier(self):
        parser = argparse.ArgumentParser()
        studio_cli.register(parser.add_subparsers(dest='command'))
        config = {'data_dir': str(self.root)}
        def run(values):
            output = io.StringIO()
            with patch('sys.stdout', output): studio_cli.dispatch(parser.parse_args(['proposal'] + values), config)
            return json.loads(output.getvalue())
        with patch('grantlib.publication_text.fetch', return_value=article()) as fetch:
            fetched = run(['fulltext', self.p['id'], '--pmcid', 'PMC123456'])
            fetch.assert_called_once_with('PMC123456')
        document = fetched['document_id']
        reader = run(['fulltext-show', self.p['id'], '--document', document])
        self.assertEqual(reader['paragraphs'], article()['paragraphs'])
        self.assertNotIn('raw_xml_b64', reader)
        kept = run(['retain-passage', self.p['id'], '--document', document, '--paragraph', 'p0002'])
        self.assertEqual(kept['publication_passages'][0]['metadata']['scope'], 'full_text_excerpt')
        self.assertEqual(kept['publication_passages'][0]['metadata']['retained_by'], 'assistant')
        self.assertNotIn('text', kept['publication_passages'][0])


if __name__ == '__main__':
    unittest.main()
