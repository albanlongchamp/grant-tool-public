"""Synthetic generators exercise bounded calls and preservation of valid drafts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from grantlib import audit_context, draft_check, draft_metrics, length_targets
from grantlib.draft_pipeline import check_draft, _digest
from grantlib.model import ModelCancelled
from grantlib.studio import Conflict, OUTPUT_SCHEMA, validate_output


SHORT = ('Conditions are randomized within subject and batch. Imaging remains blinded. '
         'Subjects are the biological units; repeated fields are subsamples.')
OVERCLAIM = 'These measurements will distinguish biological absence from insufficient sensitivity.'
LONG = SHORT + ' ' + OVERCLAIM


def output(text=LONG):
    return {'title': 'Invented aims', 'markdown': text, 'change_summary': '',
            'claims': [], 'directions': [], 'open_questions': []}


def result(value):
    return {'status': 'completed', 'structured': value, 'provider': 'invented',
            'model': 'synthetic-generator', 'usage': {'input_tokens': 3, 'output_tokens': 2}}


def context(target=None):
    return {'brief': {'title': 'Invented study'}, 'investigator_decisions': [],
            'investigator_instruction': 'Develop the proposed aims without inventing missing inputs.',
            'selected_direction': '', 'selected_version': None, 'related_versions': [],
            'evidence_packet': {'sources': [], 'selected_memory': []}, 'length_target': target}


def report(issue=False, *, input_needed=False, quote=OVERCLAIM):
    return {'summary': 'Limited assessment of the supplied invented candidate.',
            'findings': [{'id': 'initial-scientific-finding', 'category': 'interpretation',
                          'severity': 'major',
                          'resolution': 'investigator_input' if input_needed else 'draft_repair',
                          'summary': 'A promised distinction lacks its conditions.',
                          'explanation': 'Sensitivity and precision needed for that distinction are unspecified.',
                          'repair': 'Qualify the unsupported promise and retain the unknown inputs.',
                          'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown',
                                                   'quote': quote}],
                          'authority': [], 'sources': []}] if issue else [], 'limitations': []}


class DraftPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.event = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, responses, *, initial=None, **overrides):
        values = iter(responses)
        def generate(prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            value = next(values)
            if callable(value):
                return value(kwargs)
            if isinstance(value, Exception):
                raise value
            return result(value)
        arguments = dict(action='aims', context=context(), base_prompt='Original invented drafting request.',
                         generate=generate, schema=OUTPUT_SCHEMA,
                         validate_candidate=lambda value: validate_output(value, {'sources': []}, 'aims'),
                         run_dir=self.root, on_event=lambda _: None, cancel_event=self.event,
                         phase=lambda _: None, deadline=time.monotonic() + 900)
        arguments.update(overrides)
        return check_draft(result(initial or output()), **arguments)

    def short_target(self):
        return {'min_words': None, 'max_words': draft_metrics.measure(output(SHORT))['words']}

    def test_clean_check_or_unresolved_input_does_not_repair_without_length_miss(self):
        for audit in [report(), report(True, input_needed=True)]:
            with self.subTest(audit=audit):
                self.calls.clear()
                candidate, metadata = self.run_pipeline([audit])
                self.assertEqual(candidate, output())
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(metadata['draft_check']['status'], 'completed')
                self.assertFalse(metadata['draft_check']['repaired'])
                self.assertIsNone(metadata['draft_check']['selected_artifact_id'])
                self.assertEqual(metadata['length_check']['status'], 'not_requested')
                self.assertEqual(metadata['usage'], {'input_tokens': 6, 'output_tokens': 4})

    def test_major_scientific_issue_gets_one_repair_and_fresh_final_check(self):
        candidate, metadata = self.run_pipeline([report(True), output(SHORT), report()])
        check = metadata['draft_check']
        self.assertEqual(candidate['markdown'], SHORT)
        self.assertTrue(check['repaired'])
        self.assertEqual(check['status'], 'completed')
        self.assertEqual([kwargs['run_dir'].name for _, kwargs in self.calls],
                         ['check-initial', 'repair', 'check-final'])
        self.assertIn('BOUNDED DRAFT REPAIR', self.calls[1][0])
        self.assertIn('initial-scientific-finding', self.calls[1][0])
        self.assertNotIn('initial-scientific-finding', self.calls[2][0])
        self.assertEqual(metadata['usage'], {'input_tokens': 12, 'output_tokens': 8})

    def test_both_audits_use_clean_context_but_repair_retains_original_context(self):
        original = context()
        original['selected_version'] = {'id':'selected-review','kind':'review','markdown':'Keep blinded scoring.',
            'recorded_claim_judgments':[{'explanation':'Keep the human assessment.'}],
            'previous_draft_check':{'report':'OLD_DRAFT_OPINION'}}
        original['related_versions'] = [{'id':'related','kind':'aims','markdown':'Keep paired units.',
            'previous_revision_check':{'report':'OLD_REVISION_OPINION'}}]
        original['evidence_packet']['selected_memory'] = [{'text':'An exact invented memory snapshot.'}]
        snapshot = deepcopy(original)
        cleaned, excluded = audit_context.prepare(original)
        base_prompt = 'Original writer context: ' + json.dumps(original)
        with patch.object(draft_check, 'validate_partial', wraps=draft_check.validate_partial) as validate:
            _, metadata = self.run_pipeline([report(True), output(SHORT), report()],
                                             context=original, base_prompt=base_prompt)
        check = metadata['draft_check']
        self.assertEqual(check['context_sha256'], _digest(snapshot))
        self.assertEqual(check['audit_context_sha256'], _digest(cleaned))
        self.assertEqual(check['audit_excluded_field_paths'], excluded)
        self.assertNotEqual(check['context_sha256'], check['audit_context_sha256'])
        for index in (0,2):
            payload = json.loads(self.calls[index][0].split(draft_check.INPUT_DELIMITER,1)[1])
            self.assertEqual(payload['context'], cleaned)
            self.assertNotIn('OLD_DRAFT_OPINION', self.calls[index][0])
            self.assertNotIn('OLD_REVISION_OPINION', self.calls[index][0])
        final = json.loads(self.calls[2][0].split(draft_check.INPUT_DELIMITER,1)[1])
        self.assertEqual(final['repair_baseline'], draft_check.baseline_snapshot(output()))
        self.assertTrue(self.calls[1][0].startswith(base_prompt))
        self.assertIn('OLD_DRAFT_OPINION', self.calls[1][0])
        self.assertEqual([call.args[1] for call in validate.call_args_list], [cleaned, cleaned])
        for step in check['steps']:
            if step['stage'].startswith('check-'):
                self.assertEqual(step['audit_context_sha256'], check['audit_context_sha256'])
                self.assertEqual(step['audit_excluded_field_paths'], excluded)
            else:
                self.assertNotIn('audit_context_sha256', step)
        self.assertEqual(original, snapshot)

    def test_length_miss_alone_triggers_the_same_single_repair(self):
        target = self.short_target()
        candidate, metadata = self.run_pipeline([report(), output(SHORT), report()], context=context(target))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(candidate['markdown'], SHORT)
        self.assertEqual(metadata['length_check']['status'], 'within_target')
        self.assertEqual(metadata['length_check'], metadata['draft_check']['length_check'])
        self.assertEqual(metadata['draft_check']['steps'][0]['length_check']['status'], 'above_maximum')
        self.assertIn('above_maximum', self.calls[1][0])

    def test_science_and_length_share_one_repair_not_independent_loops(self):
        _, metadata = self.run_pipeline([report(True), output(SHORT), report()], context=context(self.short_target()))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(sum(step['stage'] == 'repair' for step in metadata['draft_check']['steps']), 1)

    def test_failed_initial_audit_cannot_suppress_needed_length_repair(self):
        candidate, metadata = self.run_pipeline([RuntimeError('private provider stderr'), output(SHORT), report()],
                                               context=context(self.short_target()))
        check = metadata['draft_check']
        self.assertEqual(candidate['markdown'], SHORT)
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['failed_stages'], ['check-initial'])
        self.assertEqual(check['report']['state'], 'no_issues_reported')
        self.assertEqual(metadata['length_check']['status'], 'within_target')
        self.assertNotIn('private provider stderr', str(check))
        self.assertIn('"scientific_check_available": false', self.calls[1][0])

    def test_invalid_initial_audit_quote_cannot_drive_science_repair(self):
        invalid = report(True, quote='This sentence is absent from the actual candidate.')
        candidate, metadata = self.run_pipeline([invalid])
        self.assertEqual(candidate, output())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(metadata['draft_check']['report']['state'], 'unassessed')
        self.assertFalse(metadata['draft_check']['report']['repair_needed'])
        self.assertEqual(metadata['draft_check']['partial_stages'], ['check-initial'])
        self.assertEqual(metadata['draft_check']['status'], 'incomplete')

    def test_invalid_repair_keeps_original_candidate_count_and_initial_assessment(self):
        bad = output(SHORT + ' Unsupported statement [[P_missing]].')
        candidate, metadata = self.run_pipeline([report(True), bad], context=context(self.short_target()))
        check = metadata['draft_check']
        self.assertEqual(candidate, output())
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(check['repaired'])
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['report']['state'], 'issues_found')
        self.assertEqual(metadata['length_check']['status'], 'above_maximum')
        self.assertEqual(metadata['length_check'], length_targets.assess(output(), self.short_target()))

    def test_successful_repair_and_failed_final_audit_never_inherit_clean_report(self):
        candidate, metadata = self.run_pipeline([report(), output(SHORT), RuntimeError('unavailable')],
                                               context=context(self.short_target()))
        check = metadata['draft_check']
        self.assertEqual(candidate['markdown'], SHORT)
        self.assertTrue(check['repaired'])
        self.assertIsNone(check['report'])
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['steps'][1]['report']['state'], 'no_issues_reported')

    def test_final_issues_or_word_miss_never_trigger_a_second_repair(self):
        candidate, metadata = self.run_pipeline([report(True), output(), report(True)],
                                               context=context(self.short_target()))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['draft_check']['report']['state'], 'issues_found')
        self.assertEqual(metadata['length_check']['status'], 'above_maximum')
        self.assertTrue(metadata['length_check']['repair_needed'])

    def test_missing_structural_body_field_is_unassessed_and_repaired_for_target(self):
        legacy = output(); legacy.pop('change_summary')
        _, metadata = self.run_pipeline([report(), output(SHORT), report()], initial=legacy,
                                       context=context(self.short_target()))
        self.assertEqual(metadata['draft_check']['steps'][0]['length_check']['status'], 'unassessed')
        self.assertEqual(metadata['length_check']['status'], 'within_target')

    def test_expired_shared_deadline_makes_no_request_and_keeps_valid_candidate(self):
        candidate, metadata = self.run_pipeline([], deadline=time.monotonic() - 1,
                                               context=context(self.short_target()))
        self.assertEqual(self.calls, [])
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['draft_check']['status'], 'incomplete')
        self.assertEqual(metadata['length_check']['status'], 'above_maximum')

    def test_each_request_receives_remaining_shared_deadline(self):
        deadline = time.monotonic() + 20
        self.run_pipeline([report(True), output(SHORT), report()], deadline=deadline)
        budgets = [kwargs['timeout'] for _, kwargs in self.calls]
        self.assertTrue(all(0 < value <= 20 for value in budgets))
        self.assertEqual(budgets, sorted(budgets, reverse=True))

    def test_cancel_before_or_during_any_stage_never_returns_a_candidate(self):
        self.event.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_pipeline([])
        self.assertEqual(self.calls, [])
        for prefix, raw in [([], report()), ([report(True)], output(SHORT)),
                            ([report(True), output(SHORT)], report())]:
            with self.subTest(stage=len(prefix)):
                self.event.clear(); self.calls.clear()
                def cancel(kwargs):
                    self.event.set()
                    return result(raw)
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    self.run_pipeline([*prefix, cancel])
                self.assertEqual(len(self.calls), len(prefix) + 1)

    def test_provider_cancel_and_stale_memory_errors_are_not_ancillary(self):
        for exc in [ModelCancelled('cancelled upstream'), Conflict('Selected memory changed.')]:
            with self.subTest(error=type(exc).__name__):
                self.calls.clear()
                with self.assertRaises(type(exc)):
                    self.run_pipeline([exc], context=context(self.short_target()))
                self.assertEqual(len(self.calls), 1)

    def test_actual_selected_design_is_recorded_without_requiring_other_directions(self):
        value = context()
        value['selected_version'] = {'id': 'actual-selected-design', 'kind': 'design', 'markdown': SHORT,
                                     'claims': [], 'directions': [{'id': 'chosen', 'description': 'Fixed imaging.'},
                                                                 {'id': 'unselected', 'description': 'Conditional live assay.'}]}
        value['selected_direction'] = 'chosen'
        _, metadata = self.run_pipeline([report()], context=value)
        self.assertEqual(metadata['draft_check']['selected_artifact_id'], 'actual-selected-design')
        self.assertEqual(len(self.calls), 1)

    def test_context_candidates_and_reports_are_not_mutated(self):
        values = [report(True), output(SHORT), report()]
        original_context = context(self.short_target())
        before = deepcopy((values, original_context))
        self.run_pipeline(values, context=original_context)
        self.assertEqual((values, original_context), before)

    def test_latest_hashes_and_measurements_belong_to_the_accepted_candidate(self):
        value = context(self.short_target())
        candidate, metadata = self.run_pipeline([report(), output(SHORT), report()], context=value)
        check = metadata['draft_check']
        self.assertEqual(check['candidate_sha256'], _digest(candidate))
        self.assertEqual(check['context_sha256'], _digest(value))
        self.assertEqual(check['steps'][-1]['candidate_sha256'], _digest(candidate))
        self.assertNotEqual(check['steps'][0]['candidate_sha256'], _digest(candidate))
        self.assertEqual(metadata['length_check'], length_targets.assess(candidate, value['length_target']))
        self.assertEqual(check['length_check'], metadata['length_check'])
        self.calls.clear()
        bad = output(SHORT + ' Unsupported [[P_missing]].')
        candidate, metadata = self.run_pipeline([report(), bad], context=value)
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['draft_check']['candidate_sha256'], _digest(output()))
        self.assertEqual(metadata['length_check'], length_targets.assess(output(), value['length_target']))

    def test_stale_memory_during_repair_or_final_check_never_returns_candidate(self):
        for prefix in [[report(True)], [report(True), output(SHORT)]]:
            with self.subTest(stage=len(prefix)):
                self.calls.clear()
                with self.assertRaises(Conflict):
                    self.run_pipeline([*prefix, Conflict('Memory selection changed.')],
                                      context=context(self.short_target()))
                self.assertEqual(len(self.calls), len(prefix) + 1)

    def test_final_audit_receives_exact_initial_candidate_not_previous_findings(self):
        value = context(self.short_target())
        value['selected_version'] = {'id': 'actual-design', 'kind': 'design', 'markdown': SHORT}
        initial = output()
        candidate, metadata = self.run_pipeline([report(True), output(SHORT), report()], initial=initial, context=value)
        initial_payload = json.loads(self.calls[0][0].split(draft_check.INPUT_DELIMITER)[1])
        final_payload = json.loads(self.calls[-1][0].split(draft_check.INPUT_DELIMITER)[1])
        self.assertNotIn('repair_baseline', initial_payload)
        self.assertEqual(final_payload['candidate'], candidate)
        self.assertEqual(final_payload['context'], value)
        self.assertEqual(final_payload['context']['selected_version']['id'], 'actual-design')
        bundle = draft_check.baseline_snapshot(initial)
        self.assertEqual(final_payload['repair_baseline'], bundle)
        self.assertEqual(metadata['draft_check']['repair_baseline'], bundle)
        self.assertEqual(metadata['draft_check']['steps'][-1]['repair_baseline_sha256'], _digest(initial))
        self.assertNotIn('initial-scientific-finding', self.calls[-1][0])
        initial['markdown'] = 'Mutated by the caller after completion.'
        self.assertEqual(metadata['draft_check']['repair_baseline'], bundle)

    def test_baseline_persists_when_final_check_fails_but_not_for_rejected_repair(self):
        _, metadata = self.run_pipeline([report(True), output(SHORT), RuntimeError('Unavailable')])
        self.assertEqual(metadata['draft_check']['repair_baseline'], draft_check.baseline_snapshot(output()))
        self.assertEqual(metadata['draft_check']['status'], 'incomplete')
        self.assertIsNone(metadata['draft_check']['report'])
        self.calls.clear()
        _, metadata = self.run_pipeline([report(True), output(SHORT + ' Unsupported [[missing]].')])
        self.assertNotIn('repair_baseline', metadata['draft_check'])
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        _, metadata = self.run_pipeline([report()])
        self.assertNotIn('repair_baseline', metadata['draft_check'])

    def test_final_check_can_report_material_initial_candidate_loss_without_an_extra_repair(self):
        initial = output(LONG + ' Annotation stability will be assessed.')
        final_report = report(True, quote=SHORT)
        item = final_report['findings'][0]
        item.update(id='material-loss', category='design_commitment',
                    before=[{'kind': 'repair_baseline', 'id': _digest(initial), 'field': 'markdown',
                             'quote': 'Annotation stability will be assessed.'}])
        candidate, metadata = self.run_pipeline([report(True), output(SHORT), final_report], initial=initial)
        self.assertEqual(candidate, output(SHORT))
        self.assertEqual(metadata['draft_check']['report']['state'], 'issues_found')
        self.assertEqual(metadata['draft_check']['report']['repair_baseline_sha256'], _digest(initial))
        self.assertEqual(len(self.calls), 3)

    def test_initial_audit_cannot_claim_a_repair_baseline_it_was_not_given(self):
        invalid = report(True)
        invalid['findings'][0]['before'] = [{'kind': 'repair_baseline', 'id': _digest(output()),
                                           'field': 'markdown', 'quote': LONG}]
        _, metadata = self.run_pipeline([invalid])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(metadata['draft_check']['status'], 'incomplete')
        self.assertEqual(metadata['draft_check']['report']['state'], 'unassessed')
        self.assertFalse(metadata['draft_check']['report']['repair_needed'])
        self.assertNotIn('repair_baseline', metadata['draft_check'])


if __name__ == '__main__':
    unittest.main()
