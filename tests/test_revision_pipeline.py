"""Bounded repair, cancellation and last-valid-draft recovery use invented text."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from grantlib import audit_context, draft_metrics, length_targets, revision_check
from grantlib.model import ModelCancelled
from grantlib.revision_pipeline import check_revision, digest
from grantlib.studio import Conflict, OUTPUT_SCHEMA, validate_output


CONTROL = 'Conditions will be randomized within each processing batch.'
PARENT = CONTROL + ' Subjects are the independent units; wells are technical replicates. The primary outcome is measured at one fixed time.'
DRAFT = 'Subjects are the independent units; wells are technical replicates. We will compare treated and untreated cells at one fixed time.'
SHORT = ('Subjects remain independent units; randomized treatment comparisons preserve within-batch '
         'allocation, paired contrasts and blinded outcome measurements.')


def output(text=DRAFT):
    return {'title': 'Invented aims', 'markdown': text, 'change_summary': '',
            'claims': [], 'directions': [], 'open_questions': []}


def result(value):
    return {'status': 'completed', 'structured': value, 'provider': 'invented',
            'model': 'test', 'usage': {'input_tokens': 3, 'output_tokens': 2}}


def context():
    return {'brief': {'title': 'Invented study'}, 'investigator_decisions': [],
            'investigator_instruction': 'Shorten the draft without changing the design.',
            'selected_direction': '', 'selected_version': {'id': 'a' * 32, 'kind': 'aims',
            'markdown': PARENT, 'claims': []}, 'related_versions': [], 'evidence_packet': {'sources': []}}


def report(issue=False, *, input_needed=False):
    anchor = {'kind': 'parent', 'id': 'a' * 32, 'field': 'markdown', 'quote': CONTROL}
    return {'summary': 'A design control is omitted.' if issue else 'No issues reported in this limited check.',
            'commitments': [], 'findings': [{
                'id': 'fresh-issue-id', 'category': 'design_commitment', 'severity': 'major',
                'resolution': 'investigator_input' if input_needed else 'draft_repair',
                'summary': 'Allocation across batches is absent.',
                'explanation': 'The parent specifies within-batch allocation and the candidate omits it.',
                'repair': 'Restore the stated allocation control.', 'before': [anchor], 'after': [],
                'authority': [], 'sources': []}] if issue else [],
            'limitations': ['Only supplied invented material was assessed.']}


class RevisionPipelineTests(unittest.TestCase):
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
        arguments = dict(context=context(), base_prompt='Original drafting request.', generate=generate,
                         schema=OUTPUT_SCHEMA, validate_candidate=lambda v: validate_output(v, {'sources': []}, 'aims'),
                         run_dir=self.root, on_event=lambda _: None, cancel_event=self.event,
                         phase=lambda _: None, deadline=time.monotonic() + 900)
        arguments.update(overrides)
        return check_revision(result(initial or output()), **arguments)

    def target_context(self):
        value = context()
        value['length_target'] = {'min_words': None, 'max_words': draft_metrics.measure(output(SHORT))['words']}
        return value

    def test_no_issue_or_investigator_input_does_not_trigger_repair(self):
        for value in (report(), report(True, input_needed=True)):
            with self.subTest(value=value['summary']):
                self.calls.clear()
                candidate, metadata = self.run_pipeline([value])
                self.assertEqual(candidate, output())
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(metadata['revision_check']['status'], 'completed')
                self.assertFalse(metadata['revision_check']['repaired'])
                self.assertEqual(metadata['usage'], {'input_tokens': 6, 'output_tokens': 4})

    def test_material_issue_gets_only_one_repair_even_if_final_check_still_finds_issue(self):
        candidate, metadata = self.run_pipeline([report(True), output(PARENT), report(True)])
        check = metadata['revision_check']
        self.assertEqual(candidate['markdown'], PARENT)
        self.assertEqual([c[1]['run_dir'].name for c in self.calls], ['check-initial', 'repair', 'check-final'])
        self.assertEqual(check['status'], 'completed')
        self.assertTrue(check['repaired'])
        self.assertEqual(check['report']['state'], 'issues_found')
        self.assertEqual(metadata['usage'], {'input_tokens': 12, 'output_tokens': 8})
        self.assertIn('fresh-issue-id', self.calls[1][0])
        self.assertNotIn('fresh-issue-id', self.calls[2][0], 'Final checker was primed with the previous findings')

    def test_historical_checks_are_excluded_from_both_audits_not_repair_or_parent_identity(self):
        original = context()
        original['selected_version']['previous_revision_check'] = {'report':'OLD_REVISION_OPINION'}
        original['selected_version']['recorded_claim_judgments'] = [{'explanation':'Keep this human judgment.'}]
        original['related_versions'] = [{'id':'selected-review','kind':'review','markdown':'An intentional review input.',
            'previous_draft_check':{'report':'OLD_DRAFT_OPINION'}}]
        original['evidence_packet']['selected_memory'] = [{'text':'An exact invented memory snapshot.'}]
        snapshot = deepcopy(original)
        cleaned, excluded = audit_context.prepare(original)
        base_prompt = 'Original writer context: ' + json.dumps(original)
        with patch.object(revision_check, 'validate_partial', wraps=revision_check.validate_partial) as validate:
            _, metadata = self.run_pipeline([report(True), output(PARENT), report()],
                                             context=original, base_prompt=base_prompt)
        check = metadata['revision_check']
        self.assertEqual(check['context_sha256'], digest(snapshot))
        self.assertEqual(check['parent_sha256'], digest(snapshot['selected_version']))
        self.assertEqual(check['audit_context_sha256'], digest(cleaned))
        self.assertEqual(check['audit_excluded_field_paths'], excluded)
        self.assertNotEqual(check['context_sha256'], check['audit_context_sha256'])
        for index in (0,2):
            payload = json.loads(self.calls[index][0].split(revision_check.INPUT_DELIMITER,1)[1])
            self.assertEqual(payload['context'], cleaned)
            self.assertNotIn('OLD_REVISION_OPINION', self.calls[index][0])
            self.assertNotIn('OLD_DRAFT_OPINION', self.calls[index][0])
        self.assertTrue(self.calls[1][0].startswith(base_prompt))
        self.assertIn('OLD_REVISION_OPINION', self.calls[1][0])
        self.assertEqual([call.args[1] for call in validate.call_args_list], [cleaned, cleaned])
        for step in check['steps']:
            if step['stage'].startswith('check-'):
                self.assertEqual(step['audit_context_sha256'], check['audit_context_sha256'])
                self.assertEqual(step['audit_excluded_field_paths'], excluded)
        self.assertEqual(check['report']['state'], 'no_issues_reported')
        self.assertEqual(original, snapshot)

    def test_audit_failure_preserves_candidate_with_explicit_incomplete_status(self):
        candidate, metadata = self.run_pipeline([RuntimeError('secret raw provider stderr')])
        check = metadata['revision_check']
        self.assertEqual(candidate, output())
        self.assertEqual(check['status'], 'incomplete')
        self.assertIsNone(check['report'])
        self.assertNotIn('secret', check['error'])

    def test_invalid_quote_cannot_drive_repair(self):
        invalid = report(True)
        invalid['findings'][0]['before'][0]['quote'] = 'An allocation rule absent from the actual parent.'
        candidate, metadata = self.run_pipeline([invalid])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')

    def test_repair_with_unknown_citation_cannot_replace_valid_draft(self):
        bad = output(PARENT + ' Unsupported statement [[P_missing]].')
        candidate, metadata = self.run_pipeline([report(True), bad])
        check = metadata['revision_check']
        self.assertEqual(candidate, output())
        self.assertFalse(check['repaired'])
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['report']['state'], 'issues_found')
        self.assertEqual(len(self.calls), 2)

    def test_failed_final_check_does_not_attach_old_report_to_repaired_draft(self):
        candidate, metadata = self.run_pipeline([report(True), output(PARENT), RuntimeError('Unavailable')])
        check = metadata['revision_check']
        self.assertEqual(candidate['markdown'], PARENT)
        self.assertTrue(check['repaired'])
        self.assertIsNone(check['report'])
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['steps'][1]['report']['state'], 'issues_found')

    def test_cancel_during_check_never_returns_a_candidate(self):
        def cancel(kwargs):
            self.event.set()
            return result(report())
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_pipeline([cancel])

    def test_expired_shared_deadline_makes_no_additional_request(self):
        candidate, metadata = self.run_pipeline([], deadline=time.monotonic() - 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')

    def test_original_context_and_responses_are_immutable(self):
        original_context = context()
        values = [report(True), output(PARENT), report()]
        before_context, before_values = deepcopy(original_context), deepcopy(values)
        self.run_pipeline(values, context=original_context)
        self.assertEqual(original_context, before_context)
        self.assertEqual(values, before_values)

    def test_length_miss_alone_triggers_one_repair_and_fresh_final_check(self):
        value = self.target_context()
        candidate, metadata = self.run_pipeline([report(), output(SHORT), report()], context=value)
        check = metadata['revision_check']
        self.assertEqual(candidate, output(SHORT))
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(check['repaired'])
        self.assertEqual(check['status'], 'completed')
        self.assertEqual(metadata['length_check']['status'], 'within_target')
        self.assertEqual(metadata['length_check'], check['length_check'])
        self.assertEqual(check['steps'][0]['length_check']['status'], 'above_maximum')
        self.assertIn('above_maximum', self.calls[1][0])

    def test_science_and_length_require_only_one_combined_repair(self):
        _, metadata = self.run_pipeline([report(True), output(SHORT), report()], context=self.target_context())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(sum(step['stage'] == 'repair' for step in metadata['revision_check']['steps']), 1)
        self.assertIn('fresh-issue-id', self.calls[1][0])
        self.assertIn('length_check', self.calls[1][0])
        self.assertIn('length_comparison', self.calls[1][0])

    def test_audit_failure_still_allows_length_repair_but_stays_explicitly_incomplete(self):
        candidate, metadata = self.run_pipeline([RuntimeError('private raw stderr'), output(SHORT), report()],
                                               context=self.target_context())
        check = metadata['revision_check']
        self.assertEqual(candidate, output(SHORT))
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['failed_stages'], ['check-initial'])
        self.assertEqual(check['report']['state'], 'no_issues_reported')
        self.assertNotIn('private raw stderr', str(check))
        self.assertIn('"scientific_check_available": false', self.calls[1][0])
        self.assertEqual(metadata['length_check']['status'], 'within_target')

    def test_invalid_audit_quote_does_not_block_independent_measured_length_repair(self):
        invalid = report(True)
        invalid['findings'][0]['before'][0]['quote'] = 'Absent from the actual parent.'
        _, metadata = self.run_pipeline([invalid, output(SHORT), report()], context=self.target_context())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')
        self.assertEqual(metadata['revision_check']['partial_stages'], ['check-initial'])

    def test_latest_hash_and_exact_parent_comparison_follow_repaired_candidate(self):
        value = self.target_context()
        candidate, metadata = self.run_pipeline([report(), output(SHORT), report()], context=value)
        check = metadata['revision_check']
        self.assertEqual(check['candidate_sha256'], digest(candidate))
        self.assertEqual(check['parent_sha256'], digest(value['selected_version']))
        self.assertEqual(check['context_sha256'], digest(value))
        self.assertEqual(check['length_comparison'], draft_metrics.compare(value['selected_version'], candidate))
        self.assertEqual(check['length_check'], length_targets.assess(candidate, value['length_target']))
        self.assertEqual(check['steps'][-1]['candidate_sha256'], digest(candidate))
        self.assertNotEqual(check['steps'][0]['candidate_sha256'], digest(candidate))

    def test_rejected_repair_keeps_original_hash_count_and_parent_comparison(self):
        value = self.target_context()
        bad = output(SHORT + ' Unsupported [[P_missing]].')
        candidate, metadata = self.run_pipeline([report(), bad], context=value)
        check = metadata['revision_check']
        self.assertEqual(candidate, output())
        self.assertEqual(check['candidate_sha256'], digest(output()))
        self.assertEqual(check['length_comparison'], draft_metrics.compare(value['selected_version'], output()))
        self.assertEqual(metadata['length_check'], length_targets.assess(output(), value['length_target']))
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['report']['state'], 'no_issues_reported')

    def test_final_report_failure_does_not_erase_repaired_length_measurement(self):
        candidate, metadata = self.run_pipeline([report(), output(SHORT), RuntimeError('Unavailable')],
                                               context=self.target_context())
        self.assertEqual(candidate, output(SHORT))
        self.assertIsNone(metadata['revision_check']['report'])
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')
        self.assertEqual(metadata['length_check']['status'], 'within_target')
        self.assertEqual(metadata['revision_check']['candidate_sha256'], digest(candidate))

    def test_final_word_miss_and_science_findings_do_not_trigger_unlimited_retry(self):
        _, metadata = self.run_pipeline([report(True), output(), report(True)], context=self.target_context())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(metadata['length_check']['status'], 'above_maximum')
        self.assertEqual(metadata['revision_check']['report']['state'], 'issues_found')
        self.assertEqual(metadata['revision_check']['status'], 'completed')

    def test_length_compliance_never_overrides_a_remaining_scientific_finding(self):
        _, metadata = self.run_pipeline([report(True), output(SHORT), report(True)], context=self.target_context())
        self.assertEqual(metadata['length_check']['status'], 'within_target')
        self.assertEqual(metadata['revision_check']['report']['state'], 'issues_found')
        self.assertTrue(metadata['revision_check']['report']['repair_needed'])
        self.assertEqual(len(self.calls), 3)

    def test_legacy_unseparated_body_with_explicit_target_is_unassessed_then_repaired(self):
        legacy = output(); legacy.pop('change_summary')
        _, metadata = self.run_pipeline([report(), output(SHORT), report()], initial=legacy,
                                       context=self.target_context())
        self.assertEqual(metadata['revision_check']['steps'][0]['length_check']['status'], 'unassessed')
        self.assertEqual(metadata['length_check']['status'], 'within_target')

    def test_stale_memory_and_provider_cancellation_propagate_at_every_stage(self):
        for prefix in [[], [report(True)], [report(True), output(SHORT)]]:
            for exc in [Conflict('Memory selection is stale.'), ModelCancelled('Cancelled upstream.')]:
                with self.subTest(stage=len(prefix), error=type(exc).__name__):
                    self.calls.clear()
                    with self.assertRaises(type(exc)):
                        self.run_pipeline([*prefix, exc], context=self.target_context())
                    self.assertEqual(len(self.calls), len(prefix) + 1)

    def test_expired_deadline_after_check_does_not_make_repair_request(self):
        with patch('grantlib.revision_pipeline.time.monotonic', side_effect=[100, 101, 121]):
            candidate, metadata = self.run_pipeline([report(True)], deadline=120)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(candidate, output())
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')
        self.assertEqual(metadata['revision_check']['failed_stages'], ['repair'])

    def test_cancellation_before_processing_initial_result_never_returns_candidate(self):
        self.event.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_pipeline([])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
