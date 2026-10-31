import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib.studio import Conflict, Service, Store, export_markdown, prepare_context


def packet():
    return {'sources': [{'id': 'P_plan', 'layer': 'personal', 'title': 'Invented proposal',
                         'text': 'We will measure chlorophyll. No experiment has been completed.',
                         'source_uri': 'urn:fictional:proposal',
                         'metadata': {'role': 'ownership_uncertain', 'evidence_status': 'proposed'}}]}


def response():
    return {'title': 'Invented aims',
            'markdown': 'The earlier work proposed an chlorophyll measurement [[P_plan]]. This future experiment requires an investigator-confirmed intervention, endpoint and sampling plan.',
            'claims': [{'text': 'The earlier work proposed an chlorophyll measurement.', 'basis': 'source_report', 'source_ids': ['P_plan']}],
            'directions': [], 'open_questions': ['Which sampling plan is feasible?']}


def reference(abstract='This invented study measured tissue chlorophyll in an exploratory experiment.'):
    return {'status': 'resolved', 'title': 'Invented public study', 'abstract': abstract,
            'canonical': {'pmid': '123456', 'doi': None}, 'provider': 'synthetic',
            'source_url': 'https://pubmed.ncbi.nlm.nih.gov/123456/',
            'authors': ['Invented A'], 'journal': 'Invented Journal', 'year': '2020',
            'retrieved_at': '2026-01-01T00:00:00+00:00', 'notices': {'coverage': 'unknown'},
            'limitations': ['Fictional metadata for offline testing.']}


class ClaimReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Invented project', 'question': 'An invented research question'})
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], packet(), response())
        self.p = self.store.get(self.p['id'])
        self.a = self.p['artifacts'][0]
        self.claim_id = self.a['claims'][0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def assess(self, **kwargs):
        p = self.store.get(self.p['id'])
        values = {'source_id': 'P_plan', 'verdict': 'supported', 'excerpt': 'We will measure chlorophyll.',
                  'locator': 'Retained proposal excerpt, first sentence',
                  'note': 'Supports a statement about planned work only; it does not establish a result.',
                  'reviewer': 'assistant'}
        values.update(kwargs)
        return self.store.assess_claim(p['id'], p['revision'], self.a['id'], self.claim_id, **values)

    def test_resolution_does_not_certify_support_and_keeps_immutable_snapshots(self):
        raw = reference()
        raw['identifiers'] = raw.pop('canonical')
        raw['publication_notices'] = {'retracted': True, 'complete': False}
        raw.pop('notices')
        p = self.store.add_reference(self.p['id'], self.p['revision'], raw)
        first = p['references'][0]
        self.assertEqual(first['canonical']['pmid'], '123456')
        self.assertTrue(first['notices']['retracted'])
        self.assertFalse(first['verification']['semantic_support'])
        self.assertFalse(first['verification']['full_text'])
        self.assertEqual(p['artifacts'][0]['claims'][0]['assessments'], [])
        changed = reference('A changed abstract retained in a new snapshot.')
        changed['retrieved_at'] = '2026-02-01T00:00:00+00:00'
        p = self.store.add_reference(p['id'], p['revision'], changed)
        self.assertNotEqual(p['references'][0]['id'], first['id'])
        self.assertEqual(p['references'][1], first)
        self.assertNotEqual(p['references'][0]['abstract'], first['abstract'])

    def test_import_can_cite_new_reference_without_changing_earlier_context(self):
        original_context = self.a['context']
        p = self.store.add_reference(self.p['id'], self.p['revision'], reference())
        ref = p['references'][0]
        p = self.store.save(p['id'], p['revision'], self.a['markdown'] + '\nA new abstract informs this question [[' + ref['id'] + ']].',
                            base_artifact_id=self.a['id'], origin='assistant_import')
        new = p['artifacts'][0]
        self.assertEqual(new['citation_check']['unknown_ids'], [])
        self.assertEqual(p['artifacts'][1]['context'], original_context)
        self.assertIn(ref['id'], {s['id'] for s in new['context']['sources']})
        self.assertIn(ref['title'], export_markdown(p, new['id']))
        self.assertNotIn('Unresolved source:', export_markdown(p, new['id']))

    def test_exact_source_quote_scope_author_and_history_survive_reopen(self):
        self.assess()
        p = self.assess(verdict='partial', note='The plan supports an intention, but no intervention is specified.')
        reviews = Store(self.root).get(p['id'])['artifacts'][0]['claims'][0]['assessments']
        self.assertEqual([r['verdict'] for r in reviews], ['partial', 'supported'])
        self.assertEqual(reviews[0]['reviewer'], 'assistant')
        self.assertEqual(reviews[0]['source_snapshot'], packet()['sources'][0])
        self.assertEqual(reviews[0]['scope'], 'source_excerpt')
        self.assertTrue(any('proposed' in w for w in reviews[0]['warnings']))
        self.assertTrue(any('ownership' in w for w in reviews[0]['warnings']))
        self.assertTrue(reviews[0]['source_hash'])

    def test_fabricated_quote_or_missing_support_details_are_rejected(self):
        for values in ({'excerpt': 'We demonstrated improved survival.'}, {'locator': ''},
                       {'note': ''}, {'source_id': ''}, {'source_id': 'P_other'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.assess(**values)
        self.assertEqual(self.store.get(self.p['id'])['artifacts'][0]['claims'][0]['assessments'], [])

    def test_abstract_support_stays_abstract_and_metadata_without_abstract_is_insufficient(self):
        p = self.store.add_reference(self.p['id'], self.p['revision'], reference())
        ref = p['references'][0]
        p = self.assess(source_id=ref['id'], verdict='partial', excerpt=ref['abstract'], locator='Abstract',
                        note='An exploratory abstract informs the measurement only; full-text support remains unchecked.')
        review = p['artifacts'][0]['claims'][0]['assessments'][0]
        self.assertEqual(review['scope'], 'abstract')
        self.assertEqual(review['source_snapshot']['layer'], 'publication')
        self.assertTrue(any('full-text' in w for w in review['warnings']))
        p = self.store.add_reference(p['id'], p['revision'], reference(''))
        with self.assertRaises(ValueError):
            self.assess(source_id=p['references'][0]['id'], excerpt='Invented public study', locator='Title')

    def test_manual_claim_must_be_a_quote_in_this_version_and_is_idempotent(self):
        quote = 'This future experiment requires an investigator-confirmed intervention, endpoint and sampling plan.'
        p = self.store.add_claim(self.p['id'], self.p['revision'], self.a['id'], quote)
        claims = p['artifacts'][0]['claims']
        self.assertEqual(claims[-1]['quote'], quote)
        self.assertEqual(claims[-1]['origin'], 'draft_quote')
        p2 = self.store.add_claim(p['id'], p['revision'], self.a['id'], quote)
        self.assertEqual(p2['revision'], p['revision'])
        self.assertEqual(len(p2['artifacts'][0]['claims']), 2)
        with self.assertRaises(ValueError):
            self.store.add_claim(p['id'], p['revision'], self.a['id'], 'A fabricated sentence not in this version.')

    def test_judgments_never_transfer_to_new_versions_even_with_identical_claim_words(self):
        p = self.assess()
        job = self.store.queue(p['id'], p['revision'], 'revise', base_artifact_id=self.a['id'])
        self.store.complete(job['id'], packet(), response())
        p = self.store.get(p['id'])
        self.assertNotEqual(p['artifacts'][0]['claims'][0]['id'], self.claim_id)
        self.assertEqual(p['artifacts'][0]['claims'][0]['assessments'], [])
        self.assertEqual(len(p['artifacts'][1]['claims'][0]['assessments']), 1)
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.store.assess_claim(p['id'], p['revision'], p['artifacts'][0]['id'], self.claim_id,
                                   verdict='unresolved', note='Wrong version')

    def test_other_proposal_reference_and_artifact_are_rejected(self):
        other = self.store.create({'title': 'Other project', 'question': 'Other question'})
        other = self.store.add_reference(other['id'], other['revision'], reference())
        with self.assertRaises(ValueError):
            self.assess(source_id=other['references'][0]['id'])
        with self.assertRaises(ValueError):
            self.store.add_claim(other['id'], other['revision'], self.a['id'], 'The earlier work proposed')

    def test_conflict_and_active_job_cannot_overwrite_judgments(self):
        self.assess()
        with self.assertRaises(Conflict):
            self.store.add_reference(self.p['id'], self.p['revision'], reference())
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'review', base_artifact_id=self.a['id'])
        with self.assertRaises(Conflict):
            self.assess()
        self.store.terminal(job['id'], 'failed', 'Finished invented job')

    def test_prompt_retains_latest_judgment_scope_and_exact_evidence_without_future_badges(self):
        self.assess()
        p = self.assess(verdict='partial', note='Current judgment limits this to proposed work.')
        source = reference('An invented abstract concerning measurement feasibility.')
        p = self.store.add_reference(p['id'], p['revision'], source)
        p = self.assess(source_id=p['references'][0]['id'], verdict='unresolved', excerpt=source['abstract'],
                        locator='Abstract', note='No direct validation of the new protocol.')
        compiled, views = prepare_context(p, {'sources': []}, 'revise', {'base_artifact_id': self.a['id']})
        judgments = views['selected_version']['recorded_claim_judgments']
        self.assertEqual({r['verdict'] for r in judgments}, {'partial', 'unresolved'})
        self.assertEqual(len(judgments), 2)
        sources = {s['id']: s for s in compiled['sources']}
        for review in judgments:
            self.assertIn(review['excerpt'], sources[review['source_id']]['text'])
        _, independent = prepare_context(p, packet(), 'design', {})
        self.assertIsNone(independent['selected_version'])
        self.assertEqual(independent['related_versions'], [])

    def test_exports_include_unresolved_judgment_and_abstract_scope(self):
        p = self.assess(verdict='unresolved', source_id='', excerpt='', locator='',
                        note='Need primary evidence before treating this as a result.')
        self.assertEqual(p['artifacts'][0]['claims'][0]['assessments'][0]['scope'], 'no_source_selected')
        text = export_markdown(p, self.a['id'])
        self.assertIn('Recorded claim judgments', text)
        self.assertIn('Unresolved', text)
        self.assertIn('Need primary evidence', text)
        self.assertIn('do not certify scientific validity', text)

    def test_prompt_does_not_rewrite_exact_read_excerpt_citation_like_text(self):
        unusual = packet()
        unusual['sources'][0]['text'] = 'We will measure chlorophyll. Literal markers [[P_plan]] and [[not_a_source]] remain unverified.'
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'aims')
        self.store.complete(job['id'], unusual, response())
        p = self.store.get(p['id'])
        a = p['artifacts'][0]
        excerpt = unusual['sources'][0]['text']
        p = self.store.assess_claim(p['id'], p['revision'], a['id'], a['claims'][0]['id'], source_id='P_plan',
                                    verdict='partial', excerpt=excerpt, locator='Whole passage', note='Literal [[P_plan]] in the read text.', reviewer='assistant')
        compiled, views = prepare_context(p, packet(), 'revise', {'base_artifact_id': a['id']})
        judgment = views['selected_version']['recorded_claim_judgments'][0]
        self.assertEqual(judgment['excerpt'], excerpt)
        self.assertEqual(judgment['note'], 'Literal [[P_plan]] in the read text.')
        self.assertNotEqual(judgment['source_id'], 'P_plan')
        source = next(s for s in compiled['sources'] if s['id'] == judgment['source_id'])
        self.assertIn(judgment['excerpt'], source['text'])
        exported = export_markdown(p, a['id'])
        self.assertIn(excerpt, exported)
        self.assertNotIn('Unresolved source: not_a_source', exported)

    def test_review_only_source_exports_keep_ownership_and_evidence_limits(self):
        p = self.assess()
        # The assessment's source need not also be cited in body prose.
        artifact = p['artifacts'][0]
        artifact['markdown'] = artifact['markdown'].replace(' [[P_plan]]', '')
        text = export_markdown(p, artifact['id'])
        self.assertIn('ownership_uncertain', text)
        self.assertIn('proposed', text)
        self.assertIn('does not establish applicant-owned', text)

    def test_multiline_markdown_like_read_excerpt_is_a_literal_fenced_block(self):
        from grantlib.claim_review import export_reviews
        quote = '## A literal source heading\n**unchanged words**\n```not a closing delimiter\n[[not_a_source]]'
        artifact = {'claims': [{'text': 'An invented statement', 'assessments': [{
            'source_id': 'P_literal', 'verdict': 'unresolved', 'scope': 'source_excerpt',
            'reviewer': 'assistant', 'created_at': '2026-01-01', 'note': 'Literal source text only.',
            'excerpt': quote, 'locator': 'Whole retained passage', 'source_snapshot': None, 'warnings': []}]}]}
        rendered = export_reviews(artifact)
        self.assertIn('````text\n' + quote + '\n````', rendered)

    def test_public_lookup_uses_identifier_only_and_conflict_prevents_network(self):
        service = Service(self.root/'library.sqlite3', generate=lambda *a, **k: {}, packet_builder=lambda *a: packet())
        with patch('grantlib.references.resolve', return_value=reference()) as resolve:
            p = service.resolve_reference(self.p['id'], self.p['revision'], 'PMID:123456')
            resolve.assert_called_once_with('PMID:123456')
            self.assertEqual(len(p['references']), 1)
            with self.assertRaises(Conflict):
                service.resolve_reference(self.p['id'], self.p['revision'], 'PMID:123456')
            self.assertEqual(resolve.call_count, 1)


if __name__ == '__main__':
    unittest.main()
