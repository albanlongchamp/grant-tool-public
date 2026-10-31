"""Public headless workflows with invented sources and injected Python providers.

No external model, authentication service, archive or application database is
used. These tests check orchestration and provenance, not scientific quality.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from grantlib.harness import GrantHarness
from grantlib.model import ModelCancelled, ModelTimeout
from grantlib.studio import Conflict


SOURCE = {'id': 'P_headless', 'layer': 'personal', 'title': 'Invented planned study',
          'text': 'FICTIONAL: six subjects may provide paired material. Precision is unknown.',
          'source_uri': 'urn:fictional:headless:source',
          'metadata': {'fictional': True, 'role': 'collaborator_or_template',
                       'evidence_status': 'proposed', 'locator': 'Invented paragraph 1'}}
BODY = ('Six fictional subjects provide the proposed independent units; repeated images are subsamples. '
        'The paired comparison remains conditional on access and adequate precision. '
        'An imprecise estimate does not establish absence of an effect. [[P_headless]]')
OVERCLAIM = 'These measurements guarantee the distinction.'


def packet(*_args):
    return {'version': 1, 'sources': [deepcopy(SOURCE)], 'coverage': {}, 'warnings': []}


def candidate(*, repair=False, overclaim=False):
    return {'title': 'Invented headless proposal',
            'markdown': BODY + (' ' + OVERCLAIM if overclaim else ''),
            'change_summary': 'Qualified the interpretation.' if repair else '',
            'directions': [{'id': 'D1', 'title': 'Paired contrast', 'description': 'Estimate a within-subject contrast.'},
                           {'id': 'D2', 'title': 'Repeatability', 'description': 'Characterize measurement repeatability.'}],
            'claims': [{'text': 'The experiment remains proposed.', 'basis': 'proposed_experiment',
                        'source_ids': [SOURCE['id']]}],
            'open_questions': ['Which precision is attainable?']}


class FictionalProvider:
    requested_provider = 'fixture-python'
    requested_model = 'requested-fictional-model'

    def __init__(self, *, text_only=False, repair=False, malformed=False, hook=None):
        self.text_only, self.repair, self.malformed, self.hook = text_only, repair, malformed, hook
        self.calls = []

    def __call__(self, prompt, **options):
        stage = Path(options['run_dir']).name
        schema = options['schema']
        self.calls.append({'prompt': prompt, 'schema': deepcopy(schema), 'stage': stage,
                           'timeout': options['timeout']})
        if self.hook:
            self.hook(prompt, options, len(self.calls))
        if 'findings' in schema['properties']:
            value = {'summary': 'A synthetic assessment of supplied text.', 'findings': [], 'limitations': []}
            if 'commitments' in schema['properties']:
                value['commitments'] = []
            if self.repair and stage == 'check-initial':
                value['findings'] = [{'id': 'F1', 'category': 'interpretation', 'severity': 'major',
                    'resolution': 'draft_repair', 'summary': 'An unsupported guarantee.',
                    'explanation': 'The source leaves precision unknown.',
                    'repair': 'Remove the guarantee while retaining the paired comparison.',
                    'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': OVERCLAIM}],
                    'authority': [], 'sources': [{'kind': 'source', 'id': SOURCE['id'], 'field': 'text',
                                                'quote': SOURCE['text']}]}]
        else:
            value = candidate(repair=stage == 'repair', overclaim=self.repair and stage != 'repair')
            if self.malformed:
                # This used to pass the lighter legacy studio.validate_output.
                value['claims'][0]['basis'] = 'invented_unchecked_basis'
        result = {'status': 'completed', 'provider': 'reported-fictional-provider',
                  'model': 'reported-fictional-model', 'usage': {'input_tokens': 2, 'output_tokens': 3},
                  'run_id': 'fictional-run-' + str(len(self.calls))}
        result['text' if self.text_only else 'structured'] = json.dumps(value) if self.text_only else value
        return result


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def harness(self, provider=None):
        result = GrantHarness(self.root / 'library.sqlite3', provider=provider or FictionalProvider(),
                              packet_builder=packet)
        self.addCleanup(result.close)
        return result

    def proposal(self, harness):
        return harness.create({'title': 'Wholly fictional headless proposal',
                               'question': 'Which invented paired contrast is informative?',
                               'language': 'English', 'notes': 'FICTIONAL TEST ONLY'})

    def run_action(self, harness, pid, action, **kwargs):
        p = harness.get(pid)
        job = harness.run(pid, action, expected_revision=p['revision'], **kwargs)
        self.assertEqual(job['status'], 'completed', job.get('error'))
        return harness.artifact(pid, job['result_artifact_id']) if job.get('result_artifact_id') else job

    def test_two_provider_wire_forms_complete_same_branch_aware_workflow(self):
        for text_only in (False, True):
            with self.subTest(text_only=text_only):
                provider = FictionalProvider(text_only=text_only)
                h = self.harness(provider)
                p = self.proposal(h)
                design = self.run_action(h, p['id'], 'design')
                aims = self.run_action(h, p['id'], 'aims', base_artifact_id=design['id'], selected_direction='D1')
                before = deepcopy(aims)
                review = self.run_action(h, p['id'], 'review', base_artifact_id=aims['id'])
                current = h.get(p['id'])
                decided = h.decide(p['id'], 'The investigator requests a fictional resource change.',
                                   expected_revision=current['revision'])
                revised = self.run_action(h, p['id'], 'revise', base_artifact_id=review['id'],
                                          instruction='Apply the recorded choice without inventing resources.')
                self.assertEqual(len(provider.calls), 7)
                self.assertEqual(aims['base_artifact_id'], design['id'])
                self.assertEqual(review['base_artifact_id'], aims['id'])
                self.assertEqual(revised['base_artifact_id'], aims['id'])
                request = revised['model_metadata']['generation_request']
                self.assertEqual(request['review_artifact_id'], review['id'])
                self.assertEqual(request['investigator_decisions'], decided['decisions'])
                self.assertEqual(h.artifact(p['id'], aims['id']), before)
                self.assertEqual(revised['context']['sources'][0], SOURCE)
                for artifact in (design, aims, review, revised):
                    metadata = artifact['model_metadata']
                    self.assertEqual(metadata['requested_provider'], provider.requested_provider)
                    self.assertEqual(metadata['requested_model'], provider.requested_model)
                    self.assertEqual(metadata['provider'], 'reported-fictional-provider')
                    self.assertEqual(metadata['model'], 'reported-fictional-model')
                    self.assertEqual(artifact['citation_check']['unknown_ids'], [])
                self.assertNotIn('draft_check', review['model_metadata'])
                self.assertNotIn('revision_check', review['model_metadata'])
                review_input = json.loads(provider.calls[4]['prompt'].split('PROPOSAL AND SOURCE DATA (JSON):\n')[1])
                self.assertEqual(review_input['selected_version']['id'], aims['id'])
                self.assertNotIn('previous_draft_check', review_input['selected_version'])
                self.assertNotIn('previous_revision_check', review_input['selected_version'])
                self.assertEqual(revised['model_metadata']['revision_check']['status'], 'completed')
                self.assertIn('Source notes', h.export(p['id'], revised['id']))
                self.assertTrue(h.export(p['id'], revised['id'], format='docx').startswith(b'PK'))
                h.close()

    def test_one_combined_repair_retains_exact_baseline_and_provider_metadata(self):
        provider = FictionalProvider(repair=True)
        h = self.harness(provider)
        p = self.proposal(h)
        saved = self.run_action(h, p['id'], 'aims', min_words=1, max_words=100)
        self.assertEqual([x['stage'] for x in provider.calls], ['draft', 'check-initial', 'repair', 'check-final'])
        check = saved['model_metadata']['draft_check']
        self.assertTrue(check['repaired'])
        self.assertEqual(check['repair_baseline']['candidate'], candidate(overclaim=True))
        self.assertEqual(saved['markdown'], BODY)
        self.assertEqual(saved['model_metadata']['length_check']['status'], 'within_target')
        for step in check['steps']:
            self.assertEqual(step['model']['requested_provider'], provider.requested_provider)
            self.assertEqual(step['model']['requested_model'], provider.requested_model)
        final_payload = json.loads(provider.calls[-1]['prompt'].split('\nAUDIT_INPUT_JSON:\n')[1])
        self.assertEqual(final_payload['repair_baseline']['candidate'], candidate(overclaim=True))
        self.assertEqual(final_payload['candidate'], candidate(repair=True))

    def test_new_adapter_cannot_bypass_nested_schema_validation(self):
        provider = FictionalProvider(malformed=True)
        h = self.harness(provider)
        p = self.proposal(h)
        job = h.run(p['id'], 'design', expected_revision=p['revision'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])
        self.assertNotIn('invented_unchecked_basis', job.get('error') or '')

    def test_invalid_audit_retains_draft_with_incomplete_status_not_false_clearance(self):
        class InvalidAuditProvider(FictionalProvider):
            def __call__(self, prompt, **options):
                result = super().__call__(prompt, **options)
                if 'findings' in options['schema']['properties']:
                    result['structured']['findings'] = 'Not an assessment array.'
                return result
        provider = InvalidAuditProvider()
        h = self.harness(provider)
        p = self.proposal(h)
        saved = self.run_action(h, p['id'], 'aims')
        check = saved['model_metadata']['draft_check']
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(check['status'], 'incomplete')
        self.assertIsNone(check['report'])
        self.assertFalse(check['repaired'])
        self.assertEqual(saved['markdown'], BODY)

    def test_initial_provider_timeout_is_terminal_without_retry_or_saved_draft(self):
        def expired(_prompt, _options, _count):
            raise ModelTimeout('UNTRUSTED_PROVIDER_DIAGNOSTIC')
        provider = FictionalProvider(hook=expired)
        h = self.harness(provider)
        p = self.proposal(h)
        job = h.run(p['id'], 'aims', expected_revision=p['revision'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])
        self.assertNotIn('UNTRUSTED_PROVIDER_DIAGNOSTIC', job.get('error') or '')

    def test_explicit_branch_and_revision_guards_run_before_provider(self):
        provider = FictionalProvider()
        h = self.harness(provider)
        p = self.proposal(h)
        for action in ('review', 'revise'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                h.start(p['id'], action, expected_revision=p['revision'])
        design = self.run_action(h, p['id'], 'design')
        current = h.get(p['id'])
        for action, values in [('aims', {}), ('strategy', {}), ('aims', {'selected_direction': 'D1'})]:
            with self.subTest(action=action, values=values), self.assertRaises(ValueError):
                h.start(p['id'], action, expected_revision=current['revision'], **values)
        with self.assertRaises(Conflict):
            h.start(p['id'], 'aims', expected_revision=p['revision'], base_artifact_id=design['id'])
        other = self.proposal(h)
        with self.assertRaises(ValueError):
            h.start(other['id'], 'aims', expected_revision=other['revision'], base_artifact_id=design['id'])
        self.assertEqual(len(provider.calls), 2)

    def test_observation_timeout_does_not_restart_or_cancel_and_other_owner_close_is_safe(self):
        entered, release = threading.Event(), threading.Event()
        def block(_prompt, options, count):
            if count == 1:
                entered.set()
                while not release.wait(.01):
                    if options['cancel_event'].is_set():
                        raise ModelCancelled('Synthetic cancellation.')
        provider = FictionalProvider(hook=block)
        h = self.harness(provider)
        p = self.proposal(h)
        job = h.start(p['id'], 'aims', expected_revision=p['revision'])
        self.assertTrue(entered.wait(2))
        with self.assertRaises(TimeoutError) as caught:
            h.wait(job['id'], timeout=.01)
        self.assertIn(job['id'], str(caught.exception))
        observer = self.harness()
        observer.close()
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(h.job(job['id'])['cancel_requested'])
        release.set()
        result = h.wait(job['id'], timeout=3)
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(provider.calls), 2)

    def test_cross_harness_cancellation_saves_nothing_and_calls_no_audit(self):
        entered = threading.Event()
        def block(_prompt, options, _count):
            entered.set()
            options['cancel_event'].wait(3)
            raise ModelCancelled('Synthetic cancellation.')
        provider = FictionalProvider(hook=block)
        h = self.harness(provider)
        p = self.proposal(h)
        job = h.start(p['id'], 'design', expected_revision=p['revision'])
        self.assertTrue(entered.wait(2))
        observer = self.harness()
        observer.cancel(job['id'])
        result = h.wait(job['id'], timeout=3)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])

    def test_public_job_recovers_missing_owner_without_restart(self):
        provider = FictionalProvider()
        h = self.harness(provider)
        p = self.proposal(h)
        # Queue only: no worker or provider request is started by this fixture.
        job = h.store.queue(p['id'], p['revision'], 'aims')
        with patch('grantlib.studio.os.kill', side_effect=ProcessLookupError):
            terminal = h.job(job['id'])
        self.assertEqual(terminal['id'], job['id'])
        self.assertEqual(terminal['status'], 'failed')
        self.assertIn('process running this task stopped', terminal['error'])
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(h.get(p['id'])['jobs']), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])

    def test_wait_rechecks_owner_at_bounded_interval_without_changing_live_job(self):
        provider = FictionalProvider()
        h = self.harness(provider)
        p = self.proposal(h)
        job = h.store.queue(p['id'], p['revision'], 'aims')
        clock, recover_times = [0.0], []
        original_recover = h.store.recover
        def recover():
            recover_times.append(clock[0])
            original_recover()
        def sleep(seconds):
            clock[0] += seconds
            if clock[0] < 1:
                self.assertEqual(h.store.job(job['id'])['status'], 'queued')
                self.assertFalse(h.store.job(job['id'])['cancel_requested'])
        with (patch('grantlib.harness.time.monotonic', side_effect=lambda: clock[0]),
              patch('grantlib.harness.time.sleep', side_effect=sleep),
              patch.object(h.store, 'recover', side_effect=recover),
              patch('grantlib.studio.os.kill', side_effect=[None, ProcessLookupError])):
            terminal = h.wait(job['id'], timeout=3)
        self.assertEqual(terminal['id'], job['id'])
        self.assertEqual(terminal['status'], 'failed')
        self.assertEqual(len(recover_times), 2)
        self.assertGreaterEqual(recover_times[1] - recover_times[0], 1)
        self.assertLess(recover_times[1] - recover_times[0], 1.2)
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(h.get(p['id'])['jobs']), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])

    def test_changed_selected_memory_stops_before_audit_and_saves_nothing(self):
        h = self.harness()
        p = self.proposal(h)
        data = {'category': 'constraint', 'text': 'Only fictional fixed images are available.',
                'scope': 'This invented study.', 'provenance_note': 'Synthetic investigator statement.',
                'status': 'confirmed'}
        catalog = h.store.create_memory(h.store.memory_catalog()['revision'], data, actor='investigator')
        selected = catalog['entries'][0]
        p = h.store.select_memory(p['id'], p['revision'], catalog['revision'],
                                 [{'entry_id': selected['id'], 'version_id': selected['version_id'], 'note': ''}])
        def change(_prompt, _options, count):
            if count == 1:
                h.store.revise_memory(selected['id'], h.store.memory_catalog()['revision'],
                                      {**data, 'text': 'A changed fictional constraint.'}, actor='investigator')
        provider = FictionalProvider(hook=change)
        second = self.harness(provider)
        job = second.run(p['id'], 'aims', expected_revision=p['revision'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(h.get(p['id'])['artifacts'], [])

    def test_construction_and_import_require_no_browser_auth_or_model(self):
        source_dir = self.root / 'empty-archive'
        source_dir.mkdir()
        config = self.root / 'config.json'
        config.write_text(json.dumps({'sources': [{'name': 'fictional', 'path': str(source_dir)}],
                                      'data_dir': str(self.root / 'state')}))
        with (patch('grantlib.model.availability', side_effect=AssertionError('No auth probe')),
              patch('grantlib.model.generate', side_effect=AssertionError('No model call'))):
            h = GrantHarness.from_config(config)
            h.close()
        result = subprocess.run([sys.executable, '-c',
                                 "import sys; import grantlib.harness; assert 'grantlib.web' not in sys.modules"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
