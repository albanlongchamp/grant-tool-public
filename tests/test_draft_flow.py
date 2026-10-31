"""Invented end-to-end drafts retain exact targets, assessment scope and evidence."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from grantlib import audit_context, draft_check, draft_metrics, length_targets
from grantlib.draft_pipeline import _digest
from grantlib.docx_export import export_docx
from grantlib.studio import Service, Store, export_markdown, prepare_context


LONG = ('Subjects are independent units; images are subsamples. Conditions are randomized within '
        'subject. These measurements guarantee distinction between absence of effect and poor sensitivity.')
SHORT = ('Subjects are independent units; images are subsamples. Conditions are randomized within '
         'subject. Interpretation requires validation and adequate precision.')
SOURCE = {'id': 'P_assay', 'layer': 'personal', 'title': 'Invented constraint',
          'text': 'Assay sensitivity is unknown. Literal [[not_a_citation]].',
          'source_uri': 'file:///invented/constraint.txt',
          'metadata': {'role': 'ownership_uncertain', 'evidence_status': 'proposed', 'locator': 'paragraph 1'}}


def draft(text=LONG):
    return {'title': 'Invented draft', 'markdown': text, 'change_summary': '', 'claims': [],
            'directions': [{'id': 'D1', 'title': 'Direction one', 'description': 'First invented direction.'},
                           {'id': 'D2', 'title': 'Direction two', 'description': 'Second invented direction.'}],
            'open_questions': ['What validation and precision are feasible?']}


def report(issue=False):
    return {'summary': 'An attributed invented review.', 'findings': [{
        'id': 'overclaim', 'category': 'interpretation', 'severity': 'major',
        'resolution': 'draft_repair', 'summary': 'The distinction is not supported.',
        'explanation': 'Sensitivity and precision are unknown.', 'repair': 'Qualify the promise.',
        'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown',
                                'quote': 'These measurements guarantee distinction'}],
        'authority': [], 'sources': [{'kind': 'source', 'id': SOURCE['id'], 'field': 'text',
                                     'quote': SOURCE['text']}]}] if issue else [], 'limitations': []}


class DraftFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Invented proposal', 'question': 'What can this assay establish?'})
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, responses, action='aims', **options):
        values = iter(responses)
        def generate(prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            value = next(values)
            if callable(value):
                value = value(kwargs)
            if isinstance(value, Exception):
                raise value
            return {'status': 'completed', 'structured': value, 'provider': 'injected',
                    'usage': {'input_tokens': 1, 'output_tokens': 2}}
        service = Service(self.root / 'library.sqlite3', generate=generate,
                          packet_builder=lambda *args: {'sources': [deepcopy(SOURCE)]})
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], action, **options)
        service._execute(job['id'], threading.Event())
        return self.store.job(job['id']), self.store.get(p['id'])

    def test_each_new_deliverable_receives_fresh_check_and_explicit_target(self):
        for action in ('design', 'aims', 'strategy'):
            self.calls.clear()
            job, p = self.run_job([draft(SHORT), report()], action,
                                 length_target={'min_words': 1, 'max_words': 100})
            self.assertEqual(job['status'], 'completed', job.get('error'))
            saved = p['artifacts'][0]
            self.assertEqual([call[1]['run_dir'].name for call in self.calls], ['draft', 'check-initial'])
            prompt_data = json.loads(self.calls[0][0].split('PROPOSAL AND SOURCE DATA (JSON):\n')[1])
            check_data = json.loads(self.calls[1][0].split(draft_check.INPUT_DELIMITER)[1])
            expected_context, excluded = audit_context.prepare(prompt_data)
            self.assertEqual(expected_context, check_data['context'])
            check = saved['model_metadata']['draft_check']
            self.assertEqual(check['context_sha256'], _digest(prompt_data))
            self.assertEqual(check['audit_context_sha256'], _digest(expected_context))
            self.assertEqual(check['audit_excluded_field_paths'], excluded)
            self.assertEqual(prompt_data['evidence_packet'], check_data['context']['evidence_packet'])
            self.assertEqual(saved['model_metadata']['generation_request']['length_target'], prompt_data['length_target'])
            self.assertEqual(saved['model_metadata']['length_check']['status'], 'within_target')
            self.assertEqual(saved['model_metadata']['draft_check']['status'], 'completed')

    def test_combined_repair_and_final_count_survive_in_saved_artifact_and_exports(self):
        target = {'max_words': draft_metrics.measure(draft(SHORT))['words']}
        job, p = self.run_job([draft(), report(True), draft(SHORT), report()], length_target=target)
        self.assertEqual(job['status'], 'completed', job.get('error'))
        saved = p['artifacts'][0]
        metadata = saved['model_metadata']
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(saved['markdown'], SHORT)
        self.assertTrue(metadata['draft_check']['repaired'])
        expected_length = length_targets.assess(draft(SHORT), target)
        expected_length['request_resolution'] = length_targets.resolve_request('', target)
        self.assertEqual(metadata['length_check'], expected_length)
        self.assertEqual(metadata['usage'], {'input_tokens': 4, 'output_tokens': 8})
        final_input = json.loads(self.calls[-1][0].split(draft_check.INPUT_DELIMITER)[1])
        self.assertNotIn('overclaim', json.dumps(final_input))
        self.assertEqual(final_input['repair_baseline'], draft_check.baseline_snapshot(draft()))
        text = export_markdown(p, saved['id'])
        self.assertIn('Main draft length and task target', text)
        self.assertIn('Automated draft check', text)
        self.assertIn('not investigator approval', text)
        with zipfile.ZipFile(BytesIO(export_docx(p, saved['id']))) as archive:
            xml = archive.read('word/document.xml').decode()
        self.assertIn('Main draft length and task target', xml)
        self.assertIn('Automated draft check', xml)

    def test_failed_audit_still_gets_length_repair_and_failure_stays_visible(self):
        job, p = self.run_job([draft(), ValueError('unusable audit'), draft(SHORT), report()],
                              length_target={'max_words': draft_metrics.measure(draft(SHORT))['words']})
        self.assertEqual(job['status'], 'completed')
        saved = p['artifacts'][0]
        self.assertEqual(saved['model_metadata']['draft_check']['status'], 'incomplete')
        self.assertEqual(saved['model_metadata']['draft_check']['report']['state'], 'no_issues_reported')
        self.assertEqual(saved['model_metadata']['length_check']['status'], 'within_target')
        self.assertIn('Some automated draft-check steps are incomplete', ' '.join(saved['citation_check']['warnings']))

    def test_failed_final_check_has_no_stale_findings_and_measures_saved_repair(self):
        job, p = self.run_job([draft(), report(True), draft(SHORT), ValueError('unusable final')])
        self.assertEqual(job['status'], 'completed')
        metadata = p['artifacts'][0]['model_metadata']
        self.assertIsNone(metadata['draft_check']['report'])
        self.assertEqual(metadata['length_check']['measurement']['words'], draft_metrics.measure(draft(SHORT))['words'])

    def test_source_used_only_in_prior_draft_check_retains_exact_snapshot_and_quotes(self):
        job, p = self.run_job([draft(), report(True), ValueError('repair unavailable')])
        self.assertEqual(job['status'], 'completed')
        saved = p['artifacts'][0]
        changed = deepcopy(SOURCE)
        changed['text'] = 'A newer unrelated source snapshot.'
        packet, versions = prepare_context(p, {'sources': [changed]}, 'revise', {'base_artifact_id': saved['id']})
        anchor = versions['selected_version']['previous_draft_check']['report']['findings'][0]['sources'][0]
        self.assertEqual(anchor['quote'], SOURCE['text'])
        self.assertNotEqual(anchor['id'], SOURCE['id'])
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == anchor['id'])['text'], SOURCE['text'])
        self.assertIn(SOURCE['text'], export_markdown(p, saved['id']))
        _, reviewed = prepare_context(p, {'sources': [changed]}, 'review', {'base_artifact_id': saved['id']})
        self.assertNotIn('previous_draft_check', reviewed['selected_version'])

    def test_save_recomputes_target_and_manual_edit_does_not_inherit_it(self):
        target = {'min_words': 1, 'max_words': 10}
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims', length_target=target)
        target['max_words'] = 40000
        self.store.complete(job['id'], {'sources': []}, draft(SHORT),
                            {'length_check': {'status': 'within_target'},
                             'generation_request': {'length_target': target}})
        p = self.store.get(self.p['id'])
        saved = p['artifacts'][0]
        self.assertEqual(saved['model_metadata']['length_check']['status'], 'above_maximum')
        self.assertEqual(saved['model_metadata']['generation_request']['length_target']['max_words'], 10)
        edited = self.store.save(p['id'], p['revision'], SHORT + ' Edited.', base_artifact_id=saved['id'])['artifacts'][0]
        self.assertEqual(edited['model_metadata']['length_check']['status'], 'not_requested')
        self.assertIsNone(edited['model_metadata']['length_check']['target'])
        self.assertNotIn('draft_check', edited['model_metadata'])
        self.assertNotIn('generation_request', edited['model_metadata'])

    def test_cancellation_during_initial_or_final_check_saves_nothing(self):
        def cancel(options):
            options['cancel_event'].set()
            return report()
        for responses in ([draft(), cancel], [draft(), report(True), draft(SHORT), cancel]):
            job, p = self.run_job(responses)
            self.assertEqual(job['status'], 'cancelled')
            self.assertEqual(p['artifacts'], [])

    def test_review_stays_single_request_and_does_not_reuse_prior_target(self):
        _, p = self.run_job([draft(SHORT), report()], length_target={'max_words': 100})
        self.calls.clear()
        job, p = self.run_job([draft(SHORT)], 'review', base_artifact_id=p['artifacts'][0]['id'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(self.calls), 1)
        metadata = p['artifacts'][0]['model_metadata']
        self.assertNotIn('length_check', metadata)
        self.assertNotIn('draft_check', metadata)
        self.assertIsNone(metadata['generation_request']['length_target'])

    def test_repair_baseline_retains_omitted_source_snapshot_without_rewriting_literal_text(self):
        initial = draft(LONG + ' [[P_assay]]')
        initial['claims'] = [{'text': 'Initial proposed control [[P_assay]].',
                              'basis': 'proposed_experiment', 'source_ids': ['P_assay']}]
        _, p = self.run_job([initial, report(), draft(SHORT), report()],
                            length_target={'max_words': draft_metrics.measure(draft(SHORT))['words']})
        saved = p['artifacts'][0]
        baseline = saved['model_metadata']['draft_check']['repair_baseline']
        self.assertEqual(baseline, draft_check.baseline_snapshot(initial))
        original = deepcopy(saved)
        changed = deepcopy(SOURCE)
        changed['text'] = 'A newer, unrelated statement.'
        packet, views = prepare_context(p, {'sources': [changed]}, 'revise', {'base_artifact_id': saved['id']})
        retained = views['selected_version']['previous_draft_check']['repair_baseline']
        self.assertEqual(retained['candidate'], initial)
        self.assertEqual(retained['candidate_sha256'], baseline['candidate_sha256'])
        alias = retained['source_id_map']['P_assay']
        self.assertNotEqual(alias, 'P_assay')
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == alias)['text'], SOURCE['text'])
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == 'P_assay')['text'], changed['text'])
        self.assertEqual(saved, original)
        fresh_packet, fresh_views = prepare_context(p, {'sources': []}, 'review', {'base_artifact_id': saved['id']})
        self.assertNotIn('previous_draft_check', fresh_views['selected_version'])
        self.assertEqual(fresh_packet['sources'], [])

    def test_baseline_source_map_composes_without_changing_recorded_candidate(self):
        initial = draft(SHORT + ' [[P_assay]]')
        baseline = draft_check.baseline_snapshot(initial)
        baseline['source_id_map'] = {'P_assay': 'P_retained'}
        source = deepcopy(SOURCE)
        source['id'] = 'P_retained'
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], {'sources': [source]}, draft(SHORT), {
            'draft_check': {'status': 'incomplete', 'report': None, 'repaired': True,
                            'repair_baseline': baseline}})
        p = self.store.get(self.p['id'])
        saved = p['artifacts'][0]
        current = deepcopy(source)
        current['text'] = 'A changed retained namespace.'
        packet, views = prepare_context(p, {'sources': [current]}, 'revise', {'base_artifact_id': saved['id']})
        retained = views['selected_version']['previous_draft_check']['repair_baseline']
        self.assertEqual(retained['candidate'], initial)
        self.assertEqual(retained['candidate_sha256'], baseline['candidate_sha256'])
        alias = retained['source_id_map']['P_assay']
        self.assertNotEqual(alias, 'P_retained')
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == alias)['text'], SOURCE['text'])

    def test_failed_final_check_exports_baseline_as_intermediate_history_with_source_scope(self):
        initial = draft(LONG + ' [[P_assay]]')
        job, p = self.run_job([initial, report(), draft(SHORT), ValueError('unusable final')],
                              length_target={'max_words': draft_metrics.measure(draft(SHORT))['words']})
        self.assertEqual(job['status'], 'completed')
        saved = p['artifacts'][0]
        baseline = saved['model_metadata']['draft_check']['repair_baseline']
        # Qualifiers on a baseline-only source must survive export even though
        # the final draft no longer cites it.
        source_metadata = saved['context']['sources'][0]['metadata']
        source_metadata.update(review_after='2031-04-05', selection_note='Initial assay scope only.',
                               truncated=True, scope='abstract', notices={'retracted': True},
                               limitations=['Invented provider coverage is limited.'])
        text = export_markdown(p, saved['id'])
        self.assertTrue(text.startswith(SHORT))
        self.assertIn('Initial candidate before repair', text)
        self.assertIn('not the selected proposal parent', text)
        self.assertIn(initial['markdown'], text)
        self.assertIn('P_assay — Invented constraint', text)
        self.assertIn('ownership_uncertain', text)
        self.assertIn(baseline['candidate_sha256'], text)
        for qualifier in ('2031-04-05', 'Initial assay scope only.', 'Truncated: true',
                          'Retained abstract only', '"retracted": true', 'Invented provider coverage is limited.'):
            self.assertIn(qualifier, text)
        with zipfile.ZipFile(BytesIO(export_docx(p, saved['id']))) as archive:
            xml = archive.read('word/document.xml').decode()
        self.assertIn('Initial candidate before repair', xml)
        self.assertIn('[[P_assay]]', xml)
        self.assertIn(baseline['candidate_sha256'], xml)
        edited = self.store.save(p['id'], p['revision'], SHORT + ' Edit.', base_artifact_id=saved['id'])['artifacts'][0]
        self.assertNotIn('draft_check', edited['model_metadata'])

    def test_corrupted_baseline_or_missing_only_baseline_source_fails_explicitly(self):
        initial = draft(LONG + ' [[P_assay]]')
        _, p = self.run_job([initial, report(), draft(SHORT), report()],
                            length_target={'max_words': draft_metrics.measure(draft(SHORT))['words']})
        saved = p['artifacts'][0]
        corrupt = deepcopy(p)
        corrupt['artifacts'][0]['model_metadata']['draft_check']['repair_baseline']['candidate']['markdown'] += ' Changed.'
        with self.assertRaisesRegex(ValueError, 'recorded hash'):
            prepare_context(corrupt, {'sources': []}, 'revise', {'base_artifact_id': saved['id']})
        missing = deepcopy(p)
        missing['artifacts'][0]['context']['sources'] = []
        with self.assertRaisesRegex(ValueError, 'missing source snapshots'):
            prepare_context(missing, {'sources': []}, 'revise', {'base_artifact_id': saved['id']})


if __name__ == '__main__':
    unittest.main()
