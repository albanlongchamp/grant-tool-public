"""Review triage uses only fictional snapshots; no model or network requests."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from grantlib.claim_review import review_queue
from grantlib.harness import GrantHarness


SOURCE = {'id': 'P_demo', 'layer': 'personal', 'title': 'Fictional source',
          'text': 'We will compare two lighting schedules. No experiment has been completed.',
          'source_uri': 'urn:fictional:lighting',
          'metadata': {'role': 'ownership_uncertain', 'evidence_status': 'proposed',
                       'locator': 'Fictional plan, paragraph 1'}}
TEXT = 'We propose to compare two lighting schedules using independent trays [[P_demo]].'


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Mock(side_effect=AssertionError('Queue must not invoke a model'))
        self.provider.requested_provider = 'forbidden-test'
        self.provider.requested_model = 'none'
        self.harness = GrantHarness(Path(self.temp.name) / 'library.sqlite3', provider=self.provider)
        self.p = self.harness.create({'title': 'Fictional growth study', 'question': 'How might light affect growth?'})
        job = self.harness.store.queue(self.p['id'], self.p['revision'], 'evidence')
        self.harness.store.complete(job['id'], {'sources': [deepcopy(SOURCE)], 'coverage': {}, 'warnings': []})
        self.p = self.harness.get(self.p['id'])
        self.a = self.save(TEXT)

    def tearDown(self):
        self.harness.close()
        self.temp.cleanup()

    def save(self, markdown, base=None, author='assistant'):
        p = self.harness.get(self.p['id'])
        result = self.harness.save(p['id'], markdown, expected_revision=p['revision'],
                                   title='Fictional version', base_artifact_id=base, author=author)
        return result['artifacts'][0]

    def register(self, artifact, quote=None):
        p = self.harness.get(self.p['id'])
        result = self.harness.add_claim(p['id'], artifact['id'], quote or artifact['markdown'],
                                       expected_revision=p['revision'])
        item = next(a for a in result['artifacts'] if a['id'] == artifact['id'])
        return item['claims'][-1]

    def assess(self, artifact, claim, verdict='supported', **kwargs):
        p = self.harness.get(self.p['id'])
        values = {'source_id': 'P_demo', 'verdict': verdict,
                  'excerpt': 'We will compare two lighting schedules.',
                  'locator': 'Fictional source, first sentence',
                  'note': 'Only a proposed comparison is described.', 'reviewer': 'assistant'}
        values.update(kwargs)
        return self.harness.assess_claim(p['id'], artifact['id'], claim['id'],
                                         expected_revision=p['revision'], **values)

    def queue(self, artifact=None, **kwargs):
        return self.harness.review_queue(self.p['id'], (artifact or self.a)['id'], **kwargs)

    def test_manual_draft_has_exact_prose_candidate_without_registering_it(self):
        before = self.harness.get(self.p['id'])
        result = self.queue()
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['text'], TEXT)
        self.assertEqual(item['draft_spans'], [{'start': 0, 'end': len(TEXT)}])
        self.assertIsNone(item['claim_id'])
        self.assertTrue(item['registration_required'])
        self.assertIn('unreviewed_prose_candidate', item['reasons'])
        self.assertEqual(item['source_ids'], ['P_demo'])
        self.assertEqual(item['sources'][0]['evidence_status'], 'proposed')
        self.assertFalse(item['semantic_support_verified'])
        self.assertEqual(self.harness.get(self.p['id']), before)
        self.provider.assert_not_called()

    def test_registered_claim_replaces_covered_prose_candidate_and_records_current_scope(self):
        claim = self.register(self.a)
        self.assess(self.a, claim, 'partial')
        result = self.queue()
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['claim_id'], claim['id'])
        self.assertFalse(item['registration_required'])
        self.assertEqual(item['priority'], 'disputed')
        review = item['current_assessments'][0]
        self.assertEqual(review['excerpt']['text'], 'We will compare two lighting schedules.')
        self.assertEqual(review['locator']['text'], 'Fictional source, first sentence')
        self.assertEqual(review['scope'], 'source_excerpt')
        self.assertEqual(review['reviewer'], 'assistant')
        self.assertEqual(review['source']['source_uri'], 'urn:fictional:lighting')
        self.assertNotIn('text', review['source'])

    def test_supported_recorded_claim_leaves_queue_without_certifying_the_draft(self):
        claim = self.register(self.a)
        self.assess(self.a, claim)
        result = self.queue()
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['candidate_count'], 1)
        self.assertTrue(any('recorded reviewer opinions' in item for item in result['limitations']))

    def test_partial_coverage_does_not_hide_rest_of_paragraph(self):
        draft = self.save(TEXT + ' An unconfirmed facility is available.', base=self.a['id'])
        claim = self.register(draft, TEXT)
        self.assess(draft, claim)
        result = self.queue(draft)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['origin'], 'prose_candidate')
        self.assertIn('unconfirmed facility', result['items'][0]['text'])

    def test_prior_support_never_transfers_even_for_identical_text(self):
        claim = self.register(self.a)
        self.assess(self.a, claim)
        before_parent = self.harness.artifact(self.p['id'], self.a['id'])
        child = self.save(TEXT, base=self.a['id'])
        item = self.queue(child)['items'][0]
        self.assertEqual(item['current_assessments'], [])
        self.assertIn('parent_judgment_requires_new_review', item['reasons'])
        self.assertEqual(item['parent_comparison']['match'], 'exact_text')
        self.assertEqual(item['parent_comparison']['assessments'][0]['verdict'], 'supported')
        self.assertFalse(item['parent_comparison']['judgments_inherited'])
        self.assertEqual(self.harness.artifact(self.p['id'], self.a['id']), before_parent)

    def test_changed_claim_shows_parent_as_lexical_candidate_not_inherited_verdict(self):
        claim = self.register(self.a)
        self.assess(self.a, claim)
        changed = TEXT.replace('two lighting schedules', 'three lighting schedules')
        child = self.save(changed, base=self.a['id'])
        item = self.queue(child)['items'][0]
        self.assertIn('text_not_exactly_present_in_parent', item['reasons'])
        self.assertIn('parent_judgment_requires_new_review', item['reasons'])
        self.assertEqual(item['parent_comparison']['match'], 'similar_wording_candidate')
        self.assertEqual(item['parent_comparison']['text']['text'], TEXT)
        self.assertFalse(item['parent_comparison']['judgments_inherited'])

    def test_only_latest_per_source_judgment_sets_triage(self):
        claim = self.register(self.a)
        self.assess(self.a, claim, 'contradicted')
        self.assess(self.a, claim, 'unresolved')
        item = self.queue()['items'][0]
        self.assertIn('recorded_unresolved', item['reasons'])
        self.assertNotIn('recorded_contradicted', item['reasons'])
        self.assertEqual(item['current_assessment_count'], 1)
        self.assess(self.a, claim, 'supported')
        self.assertEqual(self.queue()['total'], 0)
        self.assertEqual(len(self.harness.artifact(self.p['id'], self.a['id'])['claims'][0]['assessments']), 3)

    def test_unresolved_judgment_without_source_keeps_reading_scope(self):
        claim = self.register(self.a)
        self.assess(self.a, claim, 'unresolved', source_id='', excerpt='', locator='')
        review = self.queue()['items'][0]['current_assessments'][0]
        self.assertEqual(review['scope'], 'no_source_selected')
        self.assertEqual(review['excerpt']['text'], '')
        self.assertIsNone(review['source']['id'])

    def test_missing_citation_is_observation_not_unsupported_claim(self):
        draft = self.save('We propose a fictional new hypothesis.', base=self.a['id'])
        item = self.queue(draft)['items'][0]
        self.assertEqual(item['citation_observations'], ['no_inline_citation'])
        self.assertNotIn('unsupported', item['reasons'])
        self.assertEqual(item['missing_source_ids'], [])
        draft = self.save('An invented historical import [[P_absent]].', base=draft['id'], author='human')
        item = self.queue(draft)['items'][0]
        self.assertIn('citation_not_in_version_packet', item['reasons'])
        self.assertEqual(item['missing_source_ids'], ['P_absent'])

    def test_pagination_and_priority_are_stable_and_complete(self):
        markdown = '\n\n'.join(f'Fictional planned observation number {i} [[P_demo]].' for i in range(7))
        draft = self.save(markdown, base=self.a['id'])
        last = markdown.split('\n\n')[-1]
        claim = self.register(draft, last)
        self.assess(draft, claim, 'contradicted')
        first = self.queue(draft, limit=2)
        self.assertEqual(first['items'][0]['text'], last)
        self.assertEqual(first['next_offset'], 2)
        all_items = []
        offset = 0
        while offset is not None:
            page = self.queue(draft, offset=offset, limit=2)
            all_items.extend(page['items'])
            offset = page['next_offset']
        self.assertEqual(len(all_items), 7)
        self.assertEqual(len({item['id'] for item in all_items}), 7)
        self.assertEqual(first, self.queue(draft, limit=2))
        self.assertEqual(self.queue(draft, offset=100)['items'], [])

    def test_boundaries_reject_bool_fractional_limits_and_cross_proposal_ids(self):
        for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 21}, {'offset': -1}, {'offset': .5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.queue(**kwargs)
        other = self.harness.create({'title': 'Other fictional study', 'question': 'Other question'})
        with self.assertRaises(ValueError):
            self.harness.review_queue(other['id'], self.a['id'])

    def test_prose_spans_exclude_headings_code_and_blockquotes(self):
        markdown = '# Heading\n\nA fictional paragraph.\n\n```text\nQuoted code.\n```\n\n> Source quote.\n\nAnother paragraph.'
        draft = self.save(markdown, base=self.a['id'])
        items = self.queue(draft)['items']
        self.assertEqual([item['text'] for item in items], ['A fictional paragraph.', 'Another paragraph.'])
        for item in items:
            span = item['draft_spans'][0]
            self.assertEqual(markdown[span['start']:span['end']], item['text'])

    def test_large_unicode_paragraphs_are_exact_bounded_and_paginated(self):
        markdown = ('文献観察 ' * 31000)[:155000].strip()
        draft = self.save(markdown, base=self.a['id'])
        result = self.queue(draft)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 220000)
        self.assertIsNotNone(result['next_offset'])
        for item in result['items']:
            span = item['draft_spans'][0]
            self.assertEqual(draft['markdown'][span['start']:span['end']], item['text'])
            self.assertLessEqual(len(item['text']), 8000)

    def test_unlocated_structured_claim_is_explicit_and_not_given_a_fabricated_span(self):
        proposal = self.harness.get(self.p['id'])
        artifact = proposal['artifacts'][0]
        artifact['claims'] = [{'id': 'synthetic-claim', 'origin': 'model', 'text': 'A paraphrase absent from the draft.',
                               'quote': '', 'source_ids': ['P_demo'], 'assessments': []}]
        result = review_queue(proposal, artifact['id'])
        item = next(item for item in result['items'] if item['claim_id'])
        self.assertEqual(item['draft_spans'], [])
        self.assertIn('claim_not_exactly_located_in_draft', item['reasons'])


if __name__ == '__main__':
    unittest.main()
