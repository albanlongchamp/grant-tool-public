"""Current-task word requests reach real queued drafts and bounded repair.

All providers and research inputs are invented; no external model is invoked.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from grantlib.studio import Service, Store, export_markdown


def candidate(text):
    return {'title': 'Invented paired study', 'markdown': text,
            'change_summary': '', 'claims': [], 'directions': [], 'open_questions': []}


class TaskWordWorkflowTests(unittest.TestCase):
    def test_instruction_target_drives_one_repair_even_when_scientific_check_fails(self):
        instruction = 'Produce an 18–22-word main draft.'
        repaired_text = ('We propose paired subject measurements to estimate within-subject contrasts, retaining uncertainty '
                         'and the planned masked scoring without asserting efficacy.')
        initial = candidate(repaired_text + ' These exploratory observations cannot independently establish causal mechanisms or justify clinical benefit.')
        for audit_failure, repair_succeeds in ((False, True), (True, True), (False, False)):
            with self.subTest(audit_failure=audit_failure, repair_succeeds=repair_succeeds), tempfile.TemporaryDirectory() as tmp:
                calls = []

                def generate(prompt, **kwargs):
                    stage = Path(kwargs['run_dir']).name
                    calls.append((stage, prompt))
                    if stage.startswith('check-'):
                        value = {'summary': 'Injected check.', 'findings': [], 'limitations': []}
                        if audit_failure and stage == 'check-initial':
                            value.pop('findings')
                    else:
                        value = (candidate(repaired_text)
                                 if stage == 'repair' and repair_succeeds else deepcopy(initial))
                    return {'status': 'completed', 'structured': value,
                            'provider': 'injected', 'model': 'invented', 'usage': {},
                            'run_id': 'injected-' + str(len(calls))}

                service = Service(Path(tmp) / 'library.sqlite3', generate=generate,
                                  packet_builder=lambda *args: {'sources': []})
                proposal = service.store.create({'title': 'Invented study', 'question': 'An invented comparison.'})
                job = service.start(proposal_id=proposal['id'], expected_revision=proposal['revision'],
                                    action='aims', instruction=instruction)
                deadline = time.monotonic() + 5
                while job['status'] in ('queued', 'running') and time.monotonic() < deadline:
                    time.sleep(.01)
                    job = service.store.job(job['id'])
                self.assertEqual(job['status'], 'completed', job)
                self.assertEqual([stage for stage, _ in calls], ['draft', 'check-initial', 'repair', 'check-final'])
                saved = service.store.get(proposal['id'])['artifacts'][0]
                request = saved['model_metadata']['generation_request']
                self.assertEqual(request['length_target'], {'min_words': 18, 'max_words': 22})
                resolution = request['length_resolution']
                self.assertEqual(resolution['origin'], 'task_instruction')
                self.assertEqual(resolution, job['length_resolution'])
                self.assertEqual(resolution['instruction_sha256'], hashlib.sha256(instruction.encode()).hexdigest())
                for match in resolution['matches']:
                    self.assertEqual(instruction[match['start']:match['end']], match['text'])
                check = saved['model_metadata']['length_check']
                self.assertEqual(check['request_resolution'], resolution)
                self.assertEqual(check['status'], 'within_target' if repair_succeeds else 'above_maximum')
                self.assertEqual(check['measurement']['words'], 19 if repair_succeeds else 31)
                draft_check = saved['model_metadata']['draft_check']
                self.assertEqual(draft_check['status'], 'incomplete' if audit_failure else 'completed')
                self.assertEqual(draft_check['length_check'], check)
                self.assertIn('Requested bounds: minimum 18; maximum 22 words', export_markdown(service.store.get(proposal['id']), saved['id']))
                # The exact initial candidate remains a separate repair baseline.
                self.assertEqual(draft_check['repair_baseline']['candidate'], initial)

    def test_queue_resolves_only_current_instruction_and_rejects_conflicts_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            proposal = store.create({'title': 'Invented study', 'question': 'An invented comparison.',
                                     'notes': 'Write a 700–900-word main draft.'})
            initial_revision = proposal['revision']
            for instruction, explicit in (
                ('Write a 6–8-word main draft.', {'max_words': 10}),
                ('Write a 6–8-word main draft. Write a 12–14-word main draft.', None),
            ):
                with self.assertRaises(ValueError):
                    store.queue(proposal['id'], initial_revision, 'aims', instruction=instruction, length_target=explicit)
                current = store.get(proposal['id'])
                self.assertEqual(current['jobs'], [])
                self.assertEqual(current['revision'], initial_revision)
            job = store.queue(proposal['id'], initial_revision, 'aims', instruction='Describe the proposed comparison.')
            queued = store.job(job['id'], internal=True)['payload']
            self.assertIsNone(queued['length_target'])
            self.assertEqual(queued['length_resolution']['status'], 'not_requested')
            # Saving cannot accept a provider's false target/provenance or count.
            store.complete(job['id'], {'sources': []}, candidate(
                'Paired observations require cautious interpretation. The exploratory comparison retains subject pairing '
                'and reports uncertainty without establishing a causal mechanism.'),
                           {'length_check': {'target': {'max_words': 2}, 'measurement': {'words': 1}},
                            'generation_request': {'length_resolution': {'origin': 'forged'}}})
            saved = store.get(proposal['id'])['artifacts'][0]
            self.assertIsNone(saved['model_metadata']['length_check']['target'])
            self.assertNotEqual(saved['model_metadata']['generation_request']['length_resolution']['origin'], 'forged')

    def test_advisory_and_review_requests_do_not_create_or_inherit_hard_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            proposal = store.create({'title': 'Invented study', 'question': 'An invented comparison.'})
            job = store.queue(proposal['id'], proposal['revision'], 'aims', instruction='Write about 600 words.')
            self.assertEqual(job['length_resolution']['status'], 'advisory')
            self.assertIsNone(store.job(job['id'], internal=True)['payload']['length_target'])
            store.complete(job['id'], {'sources': []}, candidate(
                'An invented exploratory comparison will retain subject pairing and report uncertainty. '
                'These observations will not independently establish efficacy or a causal mechanism.'))
            proposal = store.get(proposal['id'])
            review = store.queue(proposal['id'], proposal['revision'], 'review',
                                 base_artifact_id=proposal['artifacts'][0]['id'],
                                 instruction='Review this draft in 100–200 words.')
            payload = store.job(review['id'], internal=True)['payload']
            self.assertIsNone(payload['length_target'])
            self.assertIsNone(payload['length_resolution'])


if __name__ == '__main__':
    unittest.main()
