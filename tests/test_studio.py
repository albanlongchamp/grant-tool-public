"""Proposal lineage, evidence isolation, cancellation and lost-update checks."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from grantlib.studio import Conflict, Service, Store, export_markdown, make_prompt, prepare_context


def packet(text='We will test seedling irrigation in mice.'):
    return {'version': 1, 'sources': [{'id': 'P_example', 'layer': 'personal',
        'title': 'Prior proposed study', 'text': text, 'source_uri': 'file:///synthetic/aims.docx',
        'metadata': {'document_id': 'synthetic', 'locator': 'paragraph 3',
                     'role': 'ownership_uncertain', 'evidence_status': 'proposed'}}]}


def output():
    return {'title': 'Test design', 'markdown': ('The earlier source proposes a mouse experiment [[P_example]]. '
        'A new study could examine seedling irrigation, with the primary endpoint and feasibility still requiring investigator confirmation.'),
        'claims': [{'text': 'The earlier experiment was proposed.', 'basis': 'source_report', 'source_ids': ['P_example']}],
        'directions': [{'id': 'mechanism', 'title': 'Mechanism', 'description': 'Test a mechanism'},
                       {'id': 'prediction', 'title': 'Prediction', 'description': 'Evaluate prediction'}],
        'open_questions': ['What is the primary endpoint?']}


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Synthetic proposal', 'question': 'How does seedling irrigation affect injury?'})

    def tearDown(self):
        self.tmp.cleanup()

    def finish(self, action='design', **kwargs):
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], action, **kwargs)
        self.store.complete(job['id'], packet(), output())
        return self.store.get(p['id'])

    def wait_job(self, service, job):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            result = service.store.job(job['id'])
            if result['status'] not in ('running', 'queued'):
                return result
            time.sleep(.01)
        self.fail('Synthetic job did not reach a terminal state')

    def record(self, action, title, *, base=None, source=None, response=None):
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], action, base_artifact_id=base)
        value = response or output()
        value['title'] = title
        self.store.complete(job['id'], source or packet(), value)
        return self.store.get(p['id'])['artifacts'][0]

    def test_lost_updates_rejected_and_user_decisions_persist(self):
        changed = self.store.decision(self.p['id'], 1, 'Use paired plots as the experimental unit.')
        self.assertEqual(changed['revision'], 2)
        with self.assertRaises(Conflict):
            self.store.update(self.p['id'], 1, self.p['brief'])
        reopened = Store(self.root).get(self.p['id'])
        self.assertIn('paired plots', reopened['decisions'][0]['text'])

    def test_saved_task_inputs_come_from_queued_request_not_model_metadata(self):
        instruction = 'Compare mechanisms only; do not draft aims yet. Literal [[not_a_source]].'
        job = self.store.queue(self.p['id'], self.p['revision'], 'design', instruction=instruction)
        supplied = {'generation_request': {'action': 'aims', 'instruction': 'A false request.'}}
        self.store.complete(job['id'], packet(), output(), supplied)
        saved = self.store.get(self.p['id'])['artifacts'][0]
        self.assertEqual(saved['model_metadata']['generation_request'], {
            'action': 'design', 'instruction': instruction, 'selected_direction': '', 'base_artifact_id': None,
            'review_artifact_id': None, 'length_target': None, 'length_resolution': job['length_resolution'],
            'proposal_revision': self.p['revision'],
            'brief': self.p['brief'], 'investigator_decisions': []})
        self.assertEqual(supplied['generation_request']['instruction'], 'A false request.')
        self.assertEqual(saved['citation_check']['unknown_ids'], [])
        self.assertEqual(saved['model_metadata']['selected_memory'], [])

    def test_prior_task_instruction_is_history_not_inherited_instruction_or_manual_provenance(self):
        design = self.record('design', 'Design A')
        p = self.store.get(self.p['id'])
        instruction = 'Draft aims from the second direction only.'
        job = self.store.queue(p['id'], p['revision'], 'aims', instruction=instruction,
                               selected_direction='prediction', base_artifact_id=design['id'])
        self.store.complete(job['id'], packet(), output())
        p = self.store.get(p['id'])
        saved = p['artifacts'][0]
        request = saved['model_metadata']['generation_request']
        self.assertEqual(request['selected_direction'], 'prediction')
        self.assertEqual(request['base_artifact_id'], design['id'])
        _, versions = prepare_context(p, packet(), 'review', {'base_artifact_id': saved['id'], 'instruction': ''})
        self.assertNotIn('generation_request', versions['selected_version'])
        edited = self.store.save(p['id'], p['revision'], saved['markdown'] + '\nA human edit.',
                                 base_artifact_id=saved['id'])
        self.assertNotIn('generation_request', edited['artifacts'][0]['model_metadata'])
        self.assertNotIn('selected_memory', edited['artifacts'][0]['model_metadata'])
        self.assertEqual(edited['artifacts'][1]['model_metadata']['generation_request'], request)

    def test_task_history_preserves_the_prior_brief_decisions_and_selected_review(self):
        design = self.record('design', 'Design A')
        review = self.record('review', 'Review A', base=design['id'])
        p = self.store.get(self.p['id'])
        p = self.store.decision(p['id'], p['revision'], 'Use the subject pair as the unit.')
        job = self.store.queue(p['id'], p['revision'], 'revise', base_artifact_id=review['id'])
        self.store.complete(job['id'], packet(), output())
        saved = self.store.get(p['id'])['artifacts'][0]
        request = saved['model_metadata']['generation_request']
        self.assertEqual(request['base_artifact_id'], design['id'])
        self.assertEqual(request['review_artifact_id'], review['id'])
        self.assertEqual(request['investigator_decisions'], p['decisions'])
        self.assertEqual(request['brief'], p['brief'])
        latest = self.store.get(p['id'])
        brief = {**latest['brief'], 'question': 'A different current question?'}
        latest = self.store.update(p['id'], latest['revision'], brief)
        latest = self.store.decision(p['id'], latest['revision'], 'A later choice for a different revision.')
        history = next(a for a in latest['artifacts'] if a['id'] == saved['id'])
        self.assertEqual(history['model_metadata']['generation_request'], request)
        self.assertNotEqual(history['model_metadata']['generation_request']['brief'], latest['brief'])

    def test_active_job_excludes_parallel_mutations_and_duplicate_runs(self):
        job = self.store.queue(self.p['id'], 1, 'design')
        with self.assertRaises(Conflict):
            self.store.queue(self.p['id'], 1, 'aims')
        with self.assertRaises(Conflict):
            self.store.decision(self.p['id'], 1, 'A change while drafting')
        self.store.terminal(job['id'], 'failed', 'Synthetic failure')
        self.assertEqual(self.store.decision(self.p['id'], 1, 'Now editable')['revision'], 2)

    def test_versions_keep_immutable_source_context_after_refresh(self):
        p = self.finish()
        first = p['artifacts'][0]
        job = self.store.queue(p['id'], p['revision'], 'evidence')
        self.store.complete(job['id'], packet('A changed source snapshot.'))
        p = self.store.get(p['id'])
        self.assertEqual(p['context']['sources'][0]['text'], 'A changed source snapshot.')
        self.assertEqual(p['artifacts'][0]['context']['sources'][0]['text'], packet()['sources'][0]['text'])
        revised = self.store.save(p['id'], p['revision'], first['markdown'] + '\nA human edit.', 'My revision', first['id'])
        self.assertEqual(len(revised['artifacts']), 2)
        self.assertEqual(revised['artifacts'][0]['context']['sources'][0]['text'], packet()['sources'][0]['text'])
        self.assertEqual(revised['artifacts'][0]['origin'], 'human')
        exported = export_markdown(revised, revised['artifacts'][0]['id'])
        self.assertIn('paragraph 3', exported)
        self.assertIn('proposed', exported)
        self.assertNotIn('[[P_example]]', exported)
        self.assertIn('Unresolved investigator inputs', exported)
        self.assertIn('What is the primary endpoint?', exported)
        self.assertIn('carried from the prior version', exported)

    def test_revision_of_selected_review_targets_its_draft_and_exact_review(self):
        draft = self.record('aims', 'Draft A')
        first = self.record('review', 'Chosen review', base=draft['id'])
        self.record('review', 'A newer review', base=draft['id'])
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'revise', base_artifact_id=first['id'])
        payload = self.store.job(job['id'], True)['payload']
        self.assertEqual(payload['base_artifact_id'], draft['id'])
        self.assertEqual(payload['review_artifact_id'], first['id'])
        _, versions = prepare_context(p, packet(), 'revise', payload)
        self.assertEqual(versions['selected_version']['id'], draft['id'])
        self.assertEqual([a['id'] for a in versions['related_versions']], [first['id']])

    def test_branches_do_not_import_unrelated_drafts_or_reviews(self):
        design_a = self.record('design', 'Design A')
        draft_a = self.record('aims', 'Draft A', base=design_a['id'])
        review_a = self.record('review', 'Review A', base=draft_a['id'])
        design_b = self.record('design', 'Design B')
        draft_b = self.record('aims', 'Draft B', base=design_b['id'])
        self.record('review', 'Review B', base=draft_b['id'])
        p = self.store.get(self.p['id'])
        _, versions = prepare_context(p, packet(), 'revise', {'base_artifact_id': draft_a['id']})
        self.assertEqual({a['id'] for a in versions['related_versions']}, {design_a['id'], review_a['id']})
        _, independent = prepare_context(p, packet(), 'review', {'base_artifact_id': draft_a['id']})
        self.assertEqual(independent['related_versions'], [])
        _, fresh = prepare_context(p, packet(), 'design', {})
        self.assertIsNone(fresh['selected_version'])
        self.assertEqual(fresh['related_versions'], [])

    def test_all_prompt_versions_retain_exact_cited_sources_without_replacing_current(self):
        design = self.record('design', 'Old design', source=packet('Old proposed mouse protocol.'))
        draft = self.record('aims', 'Later aims', base=design['id'], source=packet('Later proposed human protocol.'))
        review = self.record('review', 'Review of aims', base=draft['id'], source=packet('Reviewer source snapshot.'))
        p = self.store.get(self.p['id'])
        refreshed = packet('Current source, different population.')
        compiled, versions = prepare_context(p, refreshed, 'revise', {'base_artifact_id': draft['id']})
        sources = {s['id']: s for s in compiled['sources']}
        self.assertEqual(sources['P_example']['text'], 'Current source, different population.')
        self.assertEqual(len(sources), 4)
        self.assertEqual(compiled['coverage']['source_count'], 4)
        self.assertEqual(compiled['coverage']['input_packet_source_count'], 1)
        views = [versions['selected_version'], *versions['related_versions']]
        for view in views:
            original = next(a for a in p['artifacts'] if a['id'] == view['id'])
            citation_id = view['claims'][0]['source_ids'][0]
            self.assertEqual(sources[citation_id]['text'], original['context']['sources'][0]['text'])
            self.assertIn('[[' + citation_id + ']]', view['markdown'])
        self.assertEqual(self.store.get(p['id'])['artifacts'], p['artifacts'])
        self.assertEqual(refreshed['sources'][0]['id'], 'P_example')
        self.assertEqual(len(compiled['coverage']['prior_citation_aliases']), 3)

    def test_cited_related_evidence_absent_from_fresh_search_is_retained(self):
        old_source = packet()
        old_source['sources'][0]['id'] = 'P_older'
        old_output = output()
        old_output['markdown'] = old_output['markdown'].replace('P_example', 'P_older')
        old_output['claims'][0]['source_ids'] = ['P_older']
        design = self.record('design', 'Design with older source', source=old_source, response=old_output)
        draft = self.record('aims', 'Current aims', base=design['id'])
        compiled, _ = prepare_context(self.store.get(self.p['id']), packet(), 'revise', {'base_artifact_id': draft['id']})
        self.assertEqual({s['id'] for s in compiled['sources']}, {'P_older', 'P_example'})

    def test_directions_are_validated_against_selected_branch(self):
        first = self.record('design', 'First design')
        different = output()
        different['directions'][0]['id'] = 'new_direction'
        self.record('design', 'Other design', response=different)
        p = self.store.get(self.p['id'])
        with self.assertRaisesRegex(ValueError, 'direction from the design'):
            self.store.queue(p['id'], p['revision'], 'aims', selected_direction='new_direction', base_artifact_id=first['id'])
        job = self.store.queue(p['id'], p['revision'], 'aims', selected_direction='mechanism', base_artifact_id=first['id'])
        self.assertEqual(self.store.job(job['id'], True)['payload']['base_artifact_id'], first['id'])

    def test_edited_design_keeps_selected_direction_mapping_from_ancestor(self):
        design = self.record('design', 'Original directions')
        p = self.store.get(self.p['id'])
        p = self.store.save(p['id'], p['revision'], design['markdown'] + '\nA revised design constraint.', base_artifact_id=design['id'])
        edited = p['artifacts'][0]
        job = self.store.queue(p['id'], p['revision'], 'aims', base_artifact_id=edited['id'], selected_direction='mechanism')
        _, versions = prepare_context(p, packet(), 'aims', self.store.job(job['id'], True)['payload'])
        self.assertEqual(versions['selected_version']['id'], edited['id'])
        self.assertEqual(versions['related_versions'][0]['directions'][0]['id'], 'mechanism')

    def test_revised_design_has_updated_direction_choices_and_keeps_its_kind(self):
        design = self.record('design', 'Original directions')
        new = output()
        new['directions'][0]['id'] = 'new_mechanism'
        revised = self.record('revise', 'Revised design', base=design['id'], response=new)
        self.assertEqual(revised['kind'], 'design')
        self.assertEqual(revised['model_metadata']['action'], 'revise')
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'aims', base_artifact_id=revised['id'], selected_direction='new_mechanism')
        self.assertEqual(self.store.job(job['id'], True)['payload']['base_artifact_id'], revised['id'])

    def test_evidence_refresh_does_not_require_or_use_direction(self):
        job = self.store.queue(self.p['id'], 1, 'evidence', selected_direction='old_direction')
        self.assertEqual(self.store.job(job['id'], True)['payload']['selected_direction'], '')

    def test_missing_source_in_human_version_fails_before_model_call(self):
        p = self.store.save(self.p['id'], 1, 'An edited draft cites a source not yet available [[P_missing]].')
        with self.assertRaisesRegex(ValueError, 'missing source snapshots'):
            make_prompt(p, packet(), 'review', {'base_artifact_id': p['artifacts'][0]['id']})

    def test_unknown_citations_in_questions_are_not_silently_exported(self):
        bad = output()
        bad['open_questions'] = ['Does [[P_missing]] resolve this?']
        job = self.store.queue(self.p['id'], 1, 'aims')
        with self.assertRaisesRegex(ValueError, 'outside its evidence packet'):
            self.store.complete(job['id'], packet(), bad)

    def test_unknown_source_citation_rolls_back_result_without_losing_prior_work(self):
        p = self.finish()
        bad = output()
        bad['markdown'] += ' Invented support [[P_missing]].'
        job = self.store.queue(p['id'], p['revision'], 'aims')
        with self.assertRaisesRegex(ValueError, 'outside its evidence packet'):
            self.store.complete(job['id'], packet(), bad)
        self.assertEqual(len(self.store.get(p['id'])['artifacts']), 1)
        self.assertEqual(self.store.get(p['id'])['revision'], p['revision'])

    def test_cross_proposal_version_and_path_identifiers_rejected(self):
        p = self.finish()
        other = self.store.create({'title': 'Other proposal', 'question': 'Another question'})
        with self.assertRaises(ValueError):
            self.store.queue(other['id'], 1, 'review', base_artifact_id=p['artifacts'][0]['id'])
        with self.assertRaises(ValueError):
            self.store.get('../../outside')

    def test_cancelled_generation_never_commits_late_result(self):
        entered = threading.Event()
        def generate(prompt, **kwargs):
            entered.set()
            kwargs['cancel_event'].wait(2)
            return {'status': 'completed', 'structured': output()}
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: packet())
        job = service.start(proposal_id=self.p['id'], expected_revision=1, action='design')
        self.assertTrue(entered.wait(2))
        service.cancel(job['id'])
        self.assertEqual(self.wait_job(service, job)['status'], 'cancelled')
        self.assertEqual(self.store.get(self.p['id'])['artifacts'], [])

    def test_other_service_cancels_silent_generation_without_callbacks(self):
        entered, observed = threading.Event(), threading.Event()
        def generate(prompt, **kwargs):
            entered.set()
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if kwargs['cancel_event'].is_set():
                    observed.set()
                    break
                time.sleep(.02)
            return {'status': 'completed', 'structured': output()}
        owner = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: packet())
        remote = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: packet())
        job = owner.start(proposal_id=self.p['id'], expected_revision=1, action='design')
        self.assertTrue(entered.wait(2))
        remote.cancel(job['id'])
        self.assertTrue(observed.wait(1.5), 'Silent generation did not observe cross-service cancellation')
        self.assertEqual(self.wait_job(owner, job)['status'], 'cancelled')
        self.assertEqual(self.store.get(self.p['id'])['artifacts'], [])

    def test_evidence_generation_and_revision_use_sources_and_recorded_decisions(self):
        captured = []
        def generate(prompt, **kwargs):
            captured.append(prompt)
            return {'status': 'completed', 'structured': output(), 'provider': 'synthetic'}
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: packet())
        evidence = service.start(proposal_id=self.p['id'], expected_revision=1, action='evidence')
        self.assertEqual(self.wait_job(service, evidence)['status'], 'completed')
        self.assertEqual(captured, [])
        p = self.store.get(self.p['id'])
        p = self.store.decision(p['id'], p['revision'], 'No access to harvest outcomes; revise the endpoint.')
        job = service.start(proposal_id=p['id'], expected_revision=p['revision'], action='design')
        self.assertEqual(self.wait_job(service, job)['status'], 'completed')
        self.assertIn('No access to harvest outcomes', captured[0])
        self.assertIn('We will test seedling irrigation in mice.', captured[0])
        artifact = self.store.get(p['id'])['artifacts'][0]
        self.assertEqual(artifact['model_metadata']['provider'], 'synthetic')
        self.assertTrue(artifact['citation_check']['warnings'])

    def test_failed_model_task_is_observed_not_marked_complete(self):
        def generate(prompt, **kwargs):
            raise RuntimeError('Synthetic provider unavailable')
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: packet())
        job = service.start(proposal_id=self.p['id'], expected_revision=1, action='design')
        result = self.wait_job(service, job)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.get(self.p['id'])['artifacts'], [])

    def test_recovery_requires_evidence_owner_process_is_gone(self):
        job = self.store.queue(self.p['id'], 1, 'design')
        with patch('grantlib.studio.os.kill', return_value=None):
            self.store.recover()
        self.assertEqual(self.store.job(job['id'])['status'], 'queued')
        with patch('grantlib.studio.os.kill', side_effect=ProcessLookupError):
            self.store.recover()
        self.assertEqual(self.store.job(job['id'])['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
