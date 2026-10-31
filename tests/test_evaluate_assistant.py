"""Offline evaluation-harness checks; every generated response is injected."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_assistant as harness
from scripts import prepare_revision_cases as heldout


class InjectedGenerator:
    def __init__(self, *, repair=False, fail_stage=None, persistent_issue=False,
                 draft_repair=False, fail_action='revise'):
        self.repair, self.fail_stage, self.persistent_issue = repair, fail_stage, persistent_issue
        self.draft_repair, self.fail_action = draft_repair, fail_action
        self.calls = []
        self.observed = []

    def __call__(self, prompt, **kwargs):
        stage = Path(kwargs['run_dir']).name
        self.calls.append(stage)
        payload = harness.prompt_data(prompt)
        context = payload.get('context', payload)
        is_draft_audit = prompt.startswith('DRAFT_SCIENTIFIC_AUDIT_V1')
        if is_draft_audit:
            action = payload['action']
        elif prompt.startswith('REVISION_SCIENTIFIC_AUDIT_V1') or context['investigator_decisions']:
            action = 'revise'
        elif context['selected_version'] is None:
            action = 'design'
        elif context['selected_version']['kind'] == 'design':
            action = 'aims'
        else:
            action = 'review'
        self.observed.append((action, stage))
        if stage == self.fail_stage and action == self.fail_action:
            raise RuntimeError('Injected stage failure')
        if stage.startswith('check-'):
            parent = context['selected_version']
            needs_repair = (self.draft_repair if is_draft_audit else self.repair) and (
                stage == 'check-initial' or self.persistent_issue)
            findings = []
            if needs_repair:
                findings = [{'id': 'preserve_pairing', 'category': 'design_commitment',
                    'severity': 'major', 'resolution': 'draft_repair',
                    'summary': 'Injected concern for pipeline routing.',
                    'explanation': 'This injected comparison tests one bounded repair, not scientific judgment.',
                    'repair': 'Retain the supplied paired-design commitment.',
                    'before': [] if is_draft_audit else [{'kind': 'parent', 'id': parent['id'], 'field': 'markdown', 'quote': 'Preserve subject pairing.'}],
                    'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown',
                               'quote': 'A fictional paired-subject experiment remains planned, not completed.'}]
                              if is_draft_audit else [], 'authority': [], 'sources': []}]
            output = {'summary': 'Injected audit response.',
                      'findings': findings, 'limitations': ['Injected test, not model evidence or scientific verification.']}
            if not is_draft_audit:
                output['commitments'] = []
        else:
            current = 'P_resources_v2' if context['investigator_decisions'] else 'P_resources_v1'
            pairing = '' if action == 'revise' and stage == 'draft' and self.repair else ' Preserve subject pairing.'
            output = {'title': 'Injected fictional draft',
                'markdown': ('A fictional paired-subject experiment remains planned, not completed. '
                             'The resource note sets a feasibility ceiling and leaves scientific inputs unresolved. '
                             + pairing + ' [[' + current + ']]'),
                'claims': [{'text': 'The fictional resource note describes a planning constraint.',
                            'basis': 'investigator_statement', 'source_ids': [current]}],
                'directions': [{'id': 'first', 'title': 'First direction', 'description': 'An invented comparison'},
                               {'id': 'second', 'title': 'Second direction', 'description': 'Another invented comparison'}]
                              if context['selected_version'] is None else [],
                'open_questions': ['Which intervention remains to be specified?'], 'change_summary': ''}
        return {'status': 'completed', 'structured': output, 'provider': 'injected', 'model': 'synthetic',
                'usage': {'input_tokens': 1, 'output_tokens': 2}, 'run_id': 'injected-' + str(len(self.calls))}


class EvaluationHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, **options):
        directory = harness.prepare(self.root / ('run-' + str(len(list(self.root.iterdir())))))
        generate = InjectedGenerator(**options)
        original_connect = sqlite3.connect
        def connect(path, *args, **kwargs):
            self.assertEqual(Path(path).resolve(), directory / 'studio.sqlite3')
            return original_connect(path, *args, **kwargs)
        with patch('sqlite3.connect', connect), redirect_stdout(StringIO()):
            result = harness.run_evaluation(directory, generate=generate, timeout=5)
        return directory, generate, result

    def test_default_cli_prepares_without_importing_runtime_or_model(self):
        program = ('import sys; from scripts.evaluate_assistant import main; '
                   'assert main(["--output-dir",sys.argv[1]])==0; '
                   'assert "grantlib.model" not in sys.modules; '
                   'assert "grantlib.studio" not in sys.modules')
        result = subprocess.run([sys.executable, '-c', program, str(self.root / 'prepare')],
                                cwd=harness.REPO, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['model_calls'], 0)
        self.assertEqual((self.root / 'prepare').stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            harness.prepare(self.root / 'prepare')

    def test_clean_workflow_observes_seven_calls_including_automatic_draft_checks(self):
        directory, generate, result = self.run_case()
        self.assertEqual(result['model_calls'], 7)
        self.assertEqual(result['expected_call_range'], [7, 13])
        self.assertEqual(generate.observed[:4], [('design', 'draft'), ('design', 'check-initial'),
                                               ('aims', 'draft'), ('aims', 'check-initial')])
        self.assertEqual(generate.calls[-2:], ['draft', 'check-initial'])
        self.assertTrue(result['call_plan_satisfied'])
        self.assertTrue(result['earlier_artifacts_unchanged'])
        self.assertTrue(result['investigator_decisions_unchanged'])
        self.assertTrue(result['investigator_brief_unchanged'])
        self.assertTrue(result['revision_targets_reviewed_aims'])
        self.assertEqual(result['revision_check']['status'], 'completed')
        self.assertEqual(result['total_usage'], {'input_tokens': 7, 'output_tokens': 14})
        self.assertEqual(len(list((directory / 'calls').glob('*/input.txt'))), 7)
        self.assertEqual(len(list((directory / 'calls').glob('*/schema.json'))), 7)
        self.assertEqual([c['protocol'] for c in result['calls']],
                         ['generation', 'draft_scientific_audit', 'generation', 'draft_scientific_audit',
                          'generation', 'generation', 'revision_scientific_audit'])
        for job in result['jobs'][:2]:
            self.assertEqual(job['model_metadata']['draft_check']['status'], 'completed')
            self.assertEqual(job['model_metadata']['draft_check']['report']['state'], 'no_issues_reported')
        self.assertTrue(all(j['status'] == 'completed' for j in result['jobs']))
        with self.assertRaises(ValueError):
            harness.run_evaluation(directory, generate=generate)

    def test_repair_and_fresh_final_check_are_recorded_separately(self):
        directory, generate, result = self.run_case(repair=True)
        self.assertEqual(result['model_calls'], 9)
        self.assertEqual(generate.calls[-4:], list(harness.REVISION_STAGES))
        self.assertTrue(result['revision_check']['repaired'])
        self.assertEqual(result['total_usage'], {'input_tokens': 9, 'output_tokens': 18})
        repair = json.loads((directory / result['calls'][7]['files'] / 'input.json').read_text())
        self.assertIn('repair', repair)
        self.assertIn('revision_check', repair['repair'])
        final_check = json.loads((directory / result['calls'][8]['files'] / 'input.json').read_text())
        self.assertNotIn('repair', final_check)
        self.assertNotIn('revision_check', final_check)
        self.assertIn('Preserve subject pairing.', final_check['candidate']['markdown'])

    def test_repair_is_never_repeated_for_remaining_findings(self):
        _, generate, result = self.run_case(repair=True, persistent_issue=True)
        self.assertEqual(result['model_calls'], 9)
        self.assertEqual(generate.calls.count('repair'), 1)
        self.assertEqual(result['revision_check']['report']['state'], 'issues_found')

    def test_failed_ancillary_stages_retain_explicit_incomplete_state(self):
        for stage, count in [('check-initial', 7), ('repair', 8), ('check-final', 9)]:
            with self.subTest(stage=stage):
                directory, _, result = self.run_case(repair=True, fail_stage=stage)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['model_calls'], count)
                self.assertEqual(result['calls'][-1]['status'], 'failed')
                self.assertEqual(result['revision_check']['status'], 'incomplete')
                self.assertTrue((directory / result['calls'][-1]['files'] / 'observation.json').exists())
                self.assertEqual(result['total_usage']['input_tokens'], count - 1)

    def test_all_three_drafting_jobs_can_each_use_one_repair_with_thirteen_call_cap(self):
        directory, generate, result = self.run_case(repair=True, draft_repair=True, persistent_issue=True)
        self.assertEqual(result['model_calls'], harness.MAX_CALLS)
        self.assertEqual(result['model_calls'], 13)
        self.assertEqual(generate.calls.count('repair'), 3)
        self.assertTrue(result['call_plan_satisfied'])
        for action in ('design', 'aims', 'revise'):
            observed = [stage for recorded_action, stage in generate.observed if recorded_action == action]
            self.assertEqual(observed, list(harness.REVISION_STAGES))
        for job in result['jobs'][:2]:
            check = job['model_metadata']['draft_check']
            self.assertTrue(check['repaired'])
            self.assertEqual(check['report']['state'], 'issues_found')
        repair_call = next(c for c in result['calls'] if c['action'] == 'design' and c['stage'] == 'repair')
        payload = json.loads((directory / repair_call['files'] / 'input.json').read_text())
        self.assertIn('draft_check', payload['repair'])
        self.assertTrue(payload['repair']['scientific_check_available'])
        final_call = next(c for c in result['calls'] if c['action'] == 'design' and c['stage'] == 'check-final')
        final_payload = json.loads((directory / final_call['files'] / 'input.json').read_text())
        self.assertEqual(final_payload['action'], 'design')
        self.assertNotIn('repair', final_payload)
        self.assertNotIn('draft_check', final_payload)
        from grantlib.draft_check import baseline_snapshot
        for action in ('design', 'aims'):
            calls = {c['stage']: c for c in result['calls'] if c['action'] == action}
            initial_output = json.loads((directory / calls['draft']['files'] / 'output.json').read_text())['structured']
            initial_check = json.loads((directory / calls['check-initial']['files'] / 'input.json').read_text())
            final_check = json.loads((directory / calls['check-final']['files'] / 'input.json').read_text())
            self.assertNotIn('repair_baseline', initial_check)
            self.assertEqual(final_check['repair_baseline'], baseline_snapshot(initial_output))

    def test_failed_new_draft_audit_is_recorded_without_hiding_the_automatic_attempt(self):
        directory, _, result = self.run_case(fail_stage='check-initial', fail_action='design')
        self.assertEqual(result['model_calls'], 7)
        self.assertEqual(result['calls'][1]['action'], 'design')
        self.assertEqual(result['calls'][1]['stage'], 'check-initial')
        self.assertEqual(result['calls'][1]['status'], 'failed')
        check = result['jobs'][0]['model_metadata']['draft_check']
        self.assertEqual(check['status'], 'incomplete')
        self.assertIsNone(check['report'])
        self.assertTrue((directory / result['calls'][1]['files'] / 'observation.json').exists())
        self.assertEqual(result['total_usage']['input_tokens'], 6)

    def test_nonfictional_audit_evidence_is_rejected_before_generation(self):
        payload = {'context': {'evidence_packet': {'sources': [{'metadata': {'fictional': False},
                    'source_uri': 'urn:fictional-benchmark:bad'}]}}, 'candidate': {}}
        for marker in harness.AUDIT_MARKERS:
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'non-fictional'):
                harness.prompt_data(marker + '\nAUDIT_INPUT_JSON:\n' + json.dumps(payload))

    def test_new_draft_audit_and_repair_preserve_context_and_guard_every_source(self):
        source = json.loads(harness.FIXTURE.read_text())['initial_sources'][0]
        context = {'investigator_decisions': [], 'evidence_packet': {'sources': [source]}}
        candidate = {'markdown': 'An entirely invented draft.'}
        payload = {'action': 'aims', 'context': context, 'candidate': candidate}
        audit_prompt = 'DRAFT_SCIENTIFIC_AUDIT_V1\nAUDIT_INPUT_JSON:\n'
        self.assertEqual(harness.prompt_data(audit_prompt + json.dumps(payload)), payload)
        repair = {'candidate': candidate, 'draft_check': None,
                  'scientific_check_available': False,
                  'length_check': {'status': 'above_maximum'}}
        def prompt():
            return ('Original drafting instructions.\nPROPOSAL AND SOURCE DATA (JSON):\n'
                    + json.dumps(context) + '\n\nBOUNDED DRAFT REPAIR:\n'
                    'A bounded instruction; no extra data source.\n'
                    'CANDIDATE AND ATTRIBUTED MODEL FINDINGS (JSON):\n' + json.dumps(repair))
        self.assertEqual(harness.prompt_data(prompt()), {'context': context, 'repair': repair})
        source['text'] = 'An unapproved source cannot become fictional through metadata alone.'
        with self.assertRaisesRegex(ValueError, 'non-fictional'):
            harness.prompt_data(prompt())

    def test_draft_audit_guard_requires_exact_generated_baseline_and_repair(self):
        from grantlib.draft_check import baseline_snapshot
        initial = {'markdown': 'Entirely invented original candidate.', 'claims': [{'text': 'An invented plan.'}]}
        repaired = {'markdown': 'Entirely invented repaired candidate.'}
        payload = {'candidate': repaired, 'repair_baseline': baseline_snapshot(initial)}
        before = deepcopy((initial, repaired, payload))
        harness.guard_draft_audit(payload, stage='check-final', initial_candidate=initial, repaired_candidate=repaired)
        self.assertEqual((initial, repaired, payload), before)
        variants = [deepcopy(payload) for _ in range(5)]
        variants[0].pop('repair_baseline')
        variants[1]['repair_baseline']['candidate']['markdown'] += ' Externally substituted material.'
        variants[2]['repair_baseline']['candidate_sha256'] = '0' * 64
        variants[3]['repair_baseline']['extra_payload'] = 'External data must not be smuggled into the baseline.'
        variants[4]['candidate'] = {'markdown': 'Unrecorded replacement.'}
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                harness.guard_draft_audit(invalid, stage='check-final', initial_candidate=initial,
                                          repaired_candidate=repaired)

    def test_initial_audit_guard_forbids_even_null_baseline_or_changed_candidate(self):
        initial = {'markdown': 'An invented original candidate.'}
        harness.guard_draft_audit({'candidate': initial}, stage='check-initial', initial_candidate=initial)
        for payload in [{'candidate': initial, 'repair_baseline': None},
                        {'candidate': {'markdown': 'An unrecorded external candidate.'}}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                harness.guard_draft_audit(payload, stage='check-initial', initial_candidate=initial)

    def test_audit_context_guard_requires_only_designated_opinion_exclusions(self):
        from grantlib.audit_context import prepare
        original = {'selected_version':{'id':'selected','markdown':'Actual selected review text.',
            'recorded_claim_judgments':[{'explanation':'Keep this judgment.'}],
            'previous_draft_check':{'report':'Historical automatic opinion.'}},
            'related_versions':[{'id':'related','markdown':'Scientific context.',
                'previous_revision_check':{'report':'Historical automatic opinion.'}}],
            'evidence_packet':{'sources':[{'text':'Exact retained source.'}]}}
        snapshot = deepcopy(original)
        cleaned, _ = prepare(original)
        harness.guard_audit_context({'context':cleaned}, generation_context=original)
        invalids = [original, deepcopy(cleaned), deepcopy(cleaned)]
        invalids[1]['evidence_packet']['sources'] = []
        invalids[2]['selected_version']['markdown'] += ' Changed text.'
        for invalid in invalids:
            with self.subTest(context=invalid), self.assertRaises(ValueError):
                harness.guard_audit_context({'context':invalid}, generation_context=original)
        self.assertEqual(original, snapshot)

    def test_final_baseline_tampering_is_rejected_before_injected_generation(self):
        from grantlib import draft_check
        original = draft_check.make_prompt
        seen = []
        def altered(context, candidate, *, action, repair_baseline=None):
            prompt = original(context, candidate, action=action, repair_baseline=repair_baseline)
            if repair_baseline is not None:
                prefix, raw = prompt.split(draft_check.INPUT_DELIMITER, 1)
                payload = json.loads(raw)
                payload['repair_baseline']['candidate']['markdown'] += ' Substituted data.'
                seen.append(action)
                return prefix + draft_check.INPUT_DELIMITER + json.dumps(payload)
            return prompt
        with patch('grantlib.draft_check.make_prompt', altered):
            _, generate, result = self.run_case(draft_repair=True)
        self.assertEqual(seen, ['design', 'aims'])
        self.assertFalse(any(stage == 'check-final' and action in ('design', 'aims')
                             for action, stage in generate.observed))
        for job in result['jobs'][:2]:
            self.assertEqual(job['model_metadata']['draft_check']['status'], 'incomplete')
        self.assertLessEqual(result['model_calls'], 13)

    def test_decision_sources_require_exact_known_invented_text_and_metadata(self):
        from grantlib.studio import decision_source
        decision = {'id': 'a' * 32, 'proposal_id': 'b' * 32, 'created_at': '2026-01-01T00:00:00Z',
                    'text': json.loads(harness.FIXTURE.read_text())['change']['decision']}
        source = decision_source(decision['proposal_id'], decision)
        payload = {'context': {'investigator_decisions': [decision], 'evidence_packet': {'sources': [source]}}, 'candidate': {}}
        prefix = 'REVISION_SCIENTIFIC_AUDIT_V1\nAUDIT_INPUT_JSON:\n'
        self.assertEqual(harness.prompt_data(prefix + json.dumps(payload)), payload)
        source['metadata']['evidence_status'] = 'published_result_verified'
        with self.assertRaisesRegex(ValueError, 'altered decision'):
            harness.prompt_data(prefix + json.dumps(payload))
        decision['text'] = 'An unapproved statement outside the known fixture.'
        payload['context']['evidence_packet']['sources'] = [decision_source(decision['proposal_id'], decision)]
        with self.assertRaisesRegex(ValueError, 'non-fictional'):
            harness.prompt_data(prefix + json.dumps(payload))

    def test_both_heldout_cases_keep_expected_findings_out_of_inputs(self):
        for case_id, case in heldout.cases().items():
            path = heldout.prepare_case(self.root / case_id, case_id)
            payload = json.loads((path / 'audit-input.json').read_text())
            self.assertEqual(payload['candidate'], case['candidate'])
            self.assertNotIn('reviewer_expectations', json.dumps(payload))
            self.assertNotIn('expected_concern', (path / 'audit-input.txt').read_text())
            self.assertNotIn('reviewer_expectations', (path / 'revision-seed.json').read_text())
            self.assertEqual(json.loads((path / 'summary.json').read_text())['model_calls'], 0)
            self.assertTrue((path / 'reviewer-expectations.json').exists())

    def test_heldout_pipeline_injects_candidate_without_calling_draft_generator(self):
        directory = heldout.prepare_case(self.root / 'heldout-run', 'cell_culture_units')
        calls = []
        def generate(prompt, **kwargs):
            calls.append(Path(kwargs['run_dir']).name)
            payload = harness.prompt_data(prompt)
            self.assertEqual(payload['candidate'], heldout.cases()['cell_culture_units']['candidate'])
            return {'status': 'completed', 'provider': 'injected', 'model': 'synthetic',
                    'structured': {'summary': 'Injected test report.', 'commitments': [], 'findings': [],
                                   'limitations': ['No scientific assessment is made by this injected response.']},
                    'usage': {'input_tokens': 1, 'output_tokens': 2}, 'run_id': 'injected-audit'}
        with redirect_stdout(StringIO()):
            result = heldout.run_case(directory, generate=generate, timeout=5)
        self.assertEqual(calls, ['check-initial'])
        self.assertEqual(result['model_calls'], 1)
        self.assertTrue(result['original_parent_unchanged'])
        self.assertTrue(result['original_candidate_unchanged'])
        self.assertTrue((directory / 'revision.docx').exists())
        with self.assertRaises(ValueError):
            heldout.run_case(directory, generate=generate)


if __name__ == '__main__':
    unittest.main()
