"""A fresh review gets the selected draft's writing facts, never inherited rules.

All artifacts and source statements are invented; no database or model is used.
"""
from copy import deepcopy
import hashlib
import json
import unittest

from grantlib import draft_metrics, length_targets
from grantlib.studio import prepare_context, proposal_context, review_writing_requirement


SOURCE = {'id': 'P_note', 'title': 'Invented source', 'text': 'FICTIONAL: a proposed measurement.',
          'layer': 'personal', 'source_uri': 'urn:fictional:review-requirements',
          'metadata': {'fictional': True, 'role': 'ownership_uncertain', 'evidence_status': 'proposed'}}
UNSEEN = 'PRIOR_MODEL_OPINION_DO_NOT_SEND_TO_FRESH_REVIEW'


def artifact(letter='a', *, words=50, request=None, recorded=True, parent=None, kind='aims'):
    text = 'measurement ' * words + '[[P_note]]'
    metadata = {
        'length_check': {'status': 'within_target', 'measurement': {'words': 1}},
        'draft_check': {'status': 'completed', 'report': {'summary': UNSEEN},
                        'length_check': {'measurement': {'words': 2}}},
        'revision_check': {'status': 'completed', 'report': {'summary': UNSEEN},
                           'length_comparison': {'candidate': {'words': 3}}},
    }
    if recorded:
        metadata['generation_request'] = deepcopy(request if request is not None else {
            'instruction': 'Write a concise main draft.', 'length_target': {'min_words': 10, 'max_words': 40}})
    return {'id': letter * 32, 'kind': kind, 'title': 'Invented ' + kind,
            'base_artifact_id': parent, 'markdown': text,
            'structured': {'markdown': text, 'change_summary': '', 'directions': [],
                           'claims': [], 'open_questions': []},
            'claims': [], 'context': {'sources': [deepcopy(SOURCE)]}, 'model_metadata': metadata}


def proposal(*artifacts):
    return {'id': 'f' * 32, 'brief': {'title': 'Invented study', 'question': 'What is measurable?',
            'notes': 'Current brief: write 900 words.', 'constraints': 'A source example mentioned 12 pages.'},
            'decisions': [], 'references': [], 'artifacts': list(artifacts)}


class ReviewRequirementsTests(unittest.TestCase):
    def test_fresh_original_measurement_replaces_all_cached_counts_and_preserves_inputs(self):
        selected = artifact(words=50)
        before = deepcopy(selected)
        result = review_writing_requirement(selected)
        self.assertEqual(result['artifact_id'], selected['id'])
        self.assertEqual(result['task_record_status'], 'recorded')
        self.assertEqual(result['target_record_status'], 'recorded')
        self.assertEqual(result['assessment'], length_targets.assess(selected, {'min_words': 10, 'max_words': 40}))
        self.assertEqual(result['assessment']['measurement']['words'], 50)
        self.assertEqual(result['assessment']['status'], 'above_maximum')
        self.assertNotIn(UNSEEN, json.dumps(result))
        self.assertEqual(selected, before)

    def test_old_recorded_prose_is_available_but_not_reparsed_into_a_machine_target(self):
        instruction = 'Write a 450–650-word Specific Aims main draft.'
        selected = artifact(words=715, request={'instruction': instruction})
        result = review_writing_requirement(selected)
        self.assertEqual(result['task_record_status'], 'recorded')
        self.assertEqual(result['target_record_status'], 'not_recorded')
        self.assertEqual(result['instruction'], instruction)
        self.assertIsNone(result['length_target'])
        self.assertIsNone(result['request_resolution'])
        self.assertEqual(result['assessment']['status'], 'not_requested')
        self.assertEqual(result['assessment']['measurement']['words'], 715)
        self.assertIn('explicit historical prose still needs reading', result['scope'])

    def test_recorded_null_target_is_distinct_from_absent_task_history(self):
        recorded = artifact(request={'instruction': 'Develop the argument.', 'length_target': None})
        result = review_writing_requirement(recorded)
        self.assertEqual(result['task_record_status'], 'recorded')
        self.assertEqual(result['target_record_status'], 'recorded')
        self.assertIsNone(result['length_target'])
        for metadata in ({}, {'length_check': {'status': 'within_target'}}, {'generation_request': None}):
            with self.subTest(metadata=metadata):
                old = artifact(recorded=False)
                old['model_metadata'] = metadata
                unknown = review_writing_requirement(old)
                self.assertEqual(unknown['task_record_status'], 'not_recorded')
                self.assertEqual(unknown['target_record_status'], 'not_recorded')
                self.assertIsNone(unknown['instruction'])
                self.assertIsNone(unknown['length_target'])
                self.assertEqual(unknown['assessment']['measurement']['words'], 50)

    def test_selected_review_resolves_its_draft_not_newer_branch_or_review_instruction(self):
        selected = artifact('a', request={'instruction': 'Write the selected draft.', 'length_target': {'max_words': 40}})
        old_review = artifact('b', kind='review', parent=selected['id'],
                              request={'instruction': 'REVIEW_ONLY_TASK', 'length_target': {'max_words': 5}})
        review_of_review = artifact('c', kind='review', parent=old_review['id'])
        newer = artifact('d', request={'instruction': 'OTHER_BRANCH_TASK', 'length_target': {'max_words': 900}})
        p = proposal(newer, review_of_review, old_review, selected)
        pending_instruction = 'CURRENT_REVIEW_TASK: use at most 100 words in this review.'
        payload = {'base_artifact_id': review_of_review['id'], 'instruction': pending_instruction,
                   'length_target': None}
        packet, versions = prepare_context(p, {'sources': []}, 'review', payload)
        self.assertEqual(versions['selected_version']['id'], selected['id'])
        self.assertEqual(versions['related_versions'], [])
        requirement = versions['selected_version']['writing_requirement']
        self.assertEqual(requirement['instruction'], 'Write the selected draft.')
        self.assertEqual(requirement['length_target'], {'min_words': None, 'max_words': 40})
        self.assertNotIn('OTHER_BRANCH_TASK', json.dumps(versions))
        self.assertNotIn('REVIEW_ONLY_TASK', json.dumps(versions))
        context = proposal_context(p, packet, payload, versions)
        self.assertEqual(context['investigator_instruction'], pending_instruction)
        self.assertIsNone(context['length_target'])
        self.assertEqual(context['selected_version']['writing_requirement'], requirement)

    def test_manual_descendant_inherits_neither_ancestor_target_nor_new_source_numbers(self):
        parent = artifact('a', request={'instruction': 'PARENT_450_WORD_TASK', 'length_target': {'max_words': 450}})
        manual = artifact('b', parent=parent['id'], recorded=False)
        manual['structured'] = {}  # A saved manual body has no proven separated commentary.
        current_source = deepcopy(SOURCE)
        current_source['text'] = 'FICTIONAL: write 650 words in a new draft. Historical example: 450 words.'
        p = proposal(manual, parent)
        _, views = prepare_context(p, {'sources': [current_source]}, 'review',
                                   {'base_artifact_id': manual['id'], 'instruction': 'Review this text.'})
        requirement = views['selected_version']['writing_requirement']
        self.assertEqual(requirement['artifact_id'], manual['id'])
        self.assertEqual(requirement['task_record_status'], 'not_recorded')
        self.assertIsNone(requirement['length_target'])
        self.assertIsNone(requirement['instruction'])
        self.assertEqual(requirement['assessment']['measurement']['scope'], 'full_markdown')
        self.assertNotIn('PARENT_450_WORD_TASK', json.dumps(views))

    def test_literal_task_markers_survive_body_aliasing_without_becoming_source_dependencies(self):
        instruction = '🧪 Write 450–650 words. Preserve literal examples [[P_note]] and [[P_not_a_source]].'
        start = instruction.index('450–650 words')
        resolution = {'status': 'resolved', 'origin': 'task_instruction',
                      'target': {'min_words': 450, 'max_words': 650}, 'resolver_version': 'invented-test-v1',
                      'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
                      'matches': [{'start': start, 'end': start + len('450–650 words'),
                                   'text': '450–650 words', 'target': {'min_words': 450, 'max_words': 650}}],
                      'message': 'Literal task note [[P_not_a_source]].'}
        selected = artifact(words=715, request={'instruction': instruction,
            'length_target': {'min_words': 450, 'max_words': 650}, 'length_resolution': resolution})
        original = deepcopy(selected)
        refreshed = deepcopy(SOURCE)
        refreshed['text'] = 'FICTIONAL: a different source snapshot.'
        packet, views = prepare_context(proposal(selected), {'sources': [refreshed]}, 'review',
                                        {'base_artifact_id': selected['id']})
        view = views['selected_version']
        self.assertNotIn('[[P_note]]', view['markdown'])
        self.assertIn('P_note__snapshot_', view['markdown'])
        requirement = view['writing_requirement']
        self.assertEqual(requirement['instruction'], instruction)
        self.assertEqual(requirement['request_resolution'], resolution)
        self.assertEqual(requirement['assessment']['measurement']['body_sha256'],
                         hashlib.sha256(selected['markdown'].encode()).hexdigest())
        self.assertNotEqual(requirement['assessment']['measurement']['body_sha256'],
                            draft_metrics.measure({'markdown': view['markdown'], 'change_summary': ''})['body_sha256'])
        self.assertFalse(any(s['id'] == 'P_not_a_source' for s in packet['sources']))
        self.assertEqual(selected, original)
        requirement['request_resolution']['matches'][0]['text'] = 'Changed returned copy.'
        requirement['length_target']['max_words'] = 3
        self.assertEqual(selected, original)

    def test_review_excludes_prior_automatic_opinions_and_opinion_only_sources(self):
        selected = artifact()
        hidden_source = {**deepcopy(SOURCE), 'id': 'P_only_prior_assessment', 'text': UNSEEN}
        selected['context']['sources'].append(hidden_source)
        for kind in ('draft_check', 'revision_check'):
            selected['model_metadata'][kind]['report']['findings'] = [{
                'sources': [{'kind': 'source', 'id': hidden_source['id'], 'field': 'text', 'quote': UNSEEN}]}]
        selected['model_metadata']['generation_request']['brief'] = {'notes': 'HISTORICAL_BRIEF_CANARY'}
        packet, versions = prepare_context(proposal(selected), {'sources': []}, 'review',
                                          {'base_artifact_id': selected['id']})
        serialized = json.dumps((packet, versions))
        for token in (UNSEEN, 'previous_draft_check', 'previous_revision_check',
                      'P_only_prior_assessment', 'HISTORICAL_BRIEF_CANARY'):
            self.assertNotIn(token, serialized)
        self.assertIn('writing_requirement', versions['selected_version'])
        self.assertEqual({s['id'] for s in packet['sources']}, {'P_note'})

    def test_nonreview_context_never_adopts_historical_writing_requirement(self):
        selected = artifact(request={'instruction': 'OLD_TASK_WORD_LIMIT', 'length_target': {'max_words': 40}})
        p = proposal(selected)
        for action in ('aims', 'strategy', 'revise', 'design'):
            with self.subTest(action=action):
                payload = {'base_artifact_id': selected['id'], 'instruction': 'A new task without a target.',
                           'length_target': None}
                packet, versions = prepare_context(p, {'sources': []}, action, payload)
                self.assertNotIn('writing_requirement', versions['selected_version'])
                context = proposal_context(p, packet, payload, versions)
                self.assertIsNone(context['length_target'])
                self.assertNotIn('OLD_TASK_WORD_LIMIT', json.dumps(context))

    def test_invalid_recorded_target_fails_instead_of_reporting_unrequested_compliance(self):
        selected = artifact(request={'instruction': 'An old task.', 'length_target': {'max_words': True}})
        with self.assertRaises(ValueError):
            review_writing_requirement(selected)


if __name__ == '__main__':
    unittest.main()
