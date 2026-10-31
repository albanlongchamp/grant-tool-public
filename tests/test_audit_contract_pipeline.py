"""Invented responses test routing of the experimental defects-only contract.

These are pipeline integration checks, not model/scientific-quality evidence.
"""
from copy import deepcopy
from functools import partial
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from grantlib import audit_contract, draft_check, draft_metrics
from grantlib.draft_pipeline import check_draft
from grantlib.revision_pipeline import check_revision
from grantlib.studio import OUTPUT_SCHEMA, validate_output
from tests import test_draft_pipeline as dp, test_revision_pipeline as rp
from tests.test_passage_audit_pipeline import wire_report


VERSION = 'scientific_defects_v1'
UNAVAILABLE = 'UNAVAILABLE invented positive finding must not drive a repair.'


def wire(raw, data, anchor_format):
    result = wire_report(raw, data) if anchor_format == 'passages' else deepcopy(raw)
    result['contract_version'] = VERSION
    return result


class AuditContractPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, kind, anchor_format, respond, *, length_miss=False,
                 context=None, initial=None):
        self.calls.clear()
        helper = dp if kind == 'draft' else rp
        context = deepcopy(context if context is not None else helper.context())
        initial = deepcopy(initial if initial is not None else helper.output())
        if length_miss:
            initial['markdown'] += ' ' + 'Additional invented background. ' * 30
            context['length_target'] = {
                'min_words': None,
                'max_words': draft_metrics.measure(helper.output(helper.SHORT))['words'],
            }

        def generate(prompt, **kwargs):
            stage = kwargs['run_dir'].name
            delimiter = (draft_check.INPUT_DELIMITER if stage.startswith('check-') else
                         'CANDIDATE AND ATTRIBUTED MODEL FINDINGS (JSON):\n')
            data = json.loads(prompt.split(delimiter, 1)[1])
            raw = respond(stage, data, helper)
            self.calls.append({'stage': stage, 'prompt': prompt, 'data': data,
                               'wire': deepcopy(raw), 'schema': kwargs['schema']})
            return helper.result(raw)

        prepare = (audit_contract.prepare_draft if kind == 'draft'
                   else audit_contract.prepare_revision)
        kwargs = dict(context=context, base_prompt='Invented writing instructions.',
                      generate=generate, schema=OUTPUT_SCHEMA,
                      validate_candidate=lambda value: validate_output(
                          value, context['evidence_packet'], 'aims'),
                      run_dir=self.root / (kind + '-' + anchor_format),
                      on_event=lambda _: None, cancel_event=threading.Event(),
                      phase=lambda _: None, deadline=time.monotonic() + 60,
                      audit_factory=partial(prepare, anchor_format=anchor_format))
        if kind == 'draft':
            kwargs['action'] = 'aims'
        run = check_draft if kind == 'draft' else check_revision
        candidate, metadata = run(helper.result(initial), **kwargs)
        return candidate, metadata[kind + '_check'], metadata

    def test_authorized_change_without_a_defect_never_requests_repair(self):
        context = rp.context()
        old = 'An exploratory transcript panel is planned if access is confirmed.'
        decision = 'Remove the exploratory transcript panel; preserve the paired functional comparison.'
        new = 'The exploratory transcript panel is removed; the paired functional comparison is retained.'
        context['selected_version']['markdown'] = rp.PARENT + ' ' + old
        context['investigator_instruction'] = decision
        context['investigator_decisions'] = [{'id': 'd_invented', 'text': decision}]
        initial = rp.output(rp.PARENT + ' ' + new)
        raw = rp.report()
        raw['commitments'] = [{
            'id': 'authorized-panel-removal', 'description': 'Exploratory panel removal.',
            'origin': 'prior_proposal', 'disposition': 'changed_with_authority',
            'before': [{'kind': 'parent', 'id': context['selected_version']['id'],
                        'field': 'markdown', 'quote': old}],
            'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': new}],
            'authority': [{'kind': 'decision', 'id': 'd_invented', 'field': 'text', 'quote': decision}],
            'explanation': 'The candidate implements the supplied investigator decision.'}]
        snapshot = deepcopy(raw)
        for fmt in ('quotes', 'passages'):
            with self.subTest(format=fmt):
                candidate, check, _ = self.run_case('revision', fmt,
                    lambda stage, data, helper: wire(raw, data, fmt), context=context, initial=initial)
                self.assertEqual(candidate, initial)
                self.assertEqual([c['stage'] for c in self.calls], ['check-initial'])
                self.assertEqual(check['status'], 'completed')
                self.assertFalse(check['repaired'])
                self.assertFalse(check['report']['repair_needed'])
                self.assertEqual(check['report']['commitments'][0]['disposition'], 'changed_with_authority')
                self.assertEqual(check['report']['contract_version'], VERSION)
        self.assertEqual(raw, snapshot)

    def test_real_defect_still_gets_one_repair_and_fresh_versioned_assessment(self):
        for kind in ('draft', 'revision'):
            for fmt in ('quotes', 'passages'):
                def respond(stage, data, helper):
                    if stage == 'repair':
                        return helper.output(helper.SHORT)
                    return wire(helper.report(stage == 'check-initial'), data, fmt)

                with self.subTest(kind=kind, format=fmt):
                    candidate, check, _ = self.run_case(kind, fmt, respond)
                    helper = dp if kind == 'draft' else rp
                    self.assertEqual(candidate, helper.output(helper.SHORT))
                    self.assertEqual([c['stage'] for c in self.calls],
                                     ['check-initial', 'repair', 'check-final'])
                    self.assertEqual(check['status'], 'completed')
                    self.assertTrue(check['repaired'])
                    feedback = self.calls[1]['data'][kind + '_check']
                    self.assertEqual(feedback['contract_version'], VERSION)
                    expected = hashlib.sha256(json.dumps(self.calls[0]['wire'],
                        ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
                    self.assertEqual(feedback['validation']['raw_report_sha256'], expected)
                    self.assertNotIn(feedback['findings'][0]['id'], self.calls[2]['prompt'])
                    self.assertEqual(check['report']['contract_version'], VERSION)
                    self.assertEqual(check['report']['audit_contract']['anchor_format'], fmt)
                    if kind == 'draft':
                        self.assertEqual(self.calls[2]['data']['repair_baseline'],
                                         draft_check.baseline_snapshot(helper.output()))
                    else:
                        self.assertEqual(self.calls[0]['data']['context']['selected_version'],
                                         self.calls[2]['data']['context']['selected_version'])

    def test_removed_category_rejects_report_without_silently_declaring_no_issues(self):
        for kind in ('draft', 'revision'):
            for fmt in ('quotes', 'passages'):
                def respond(stage, data, helper):
                    raw = helper.report(True)
                    raw['findings'][0].update(category='authorized_change', summary=UNAVAILABLE)
                    return wire(raw, data, fmt)

                with self.subTest(kind=kind, format=fmt):
                    candidate, check, _ = self.run_case(kind, fmt, respond)
                    self.assertEqual(len(self.calls), 1)
                    self.assertEqual(check['status'], 'incomplete')
                    self.assertEqual(check['failed_stages'], ['check-initial'])
                    self.assertIsNone(check['report'])
                    self.assertFalse(check['repaired'])
                    self.assertNotIn(UNAVAILABLE, json.dumps(check))
                    self.assertEqual(candidate, (dp if kind == 'draft' else rp).output())

    def test_bad_contract_preserves_independent_length_repair_and_incomplete_history(self):
        for kind in ('draft', 'revision'):
            for fmt in ('quotes', 'passages'):
                def respond(stage, data, helper):
                    if stage == 'repair':
                        return helper.output(helper.SHORT)
                    value = wire(helper.report(), data, fmt)
                    if stage == 'check-initial':
                        value.update(contract_version='unsupported-version', summary=UNAVAILABLE)
                    return value

                with self.subTest(kind=kind, format=fmt):
                    _, check, metadata = self.run_case(kind, fmt, respond, length_miss=True)
                    self.assertEqual([c['stage'] for c in self.calls],
                                     ['check-initial', 'repair', 'check-final'])
                    self.assertFalse(self.calls[1]['data']['scientific_check_available'])
                    self.assertNotIn(UNAVAILABLE, self.calls[1]['prompt'])
                    self.assertEqual(check['status'], 'incomplete')
                    self.assertEqual(check['failed_stages'], ['check-initial'])
                    self.assertEqual(check['report']['contract_version'], VERSION)
                    self.assertEqual(metadata['length_check']['status'], 'within_target')

    def test_failed_final_contract_retains_repaired_text_without_reusing_initial_verdict(self):
        for kind in ('draft', 'revision'):
            for fmt in ('quotes', 'passages'):
                def respond(stage, data, helper):
                    if stage == 'repair':
                        return helper.output(helper.SHORT)
                    value = wire(helper.report(stage == 'check-initial'), data, fmt)
                    if stage == 'check-final':
                        value['contract_version'] = 'unsupported-version'
                    return value

                with self.subTest(kind=kind, format=fmt):
                    candidate, check, _ = self.run_case(kind, fmt, respond)
                    self.assertEqual(candidate, (dp if kind == 'draft' else rp).output(
                        (dp if kind == 'draft' else rp).SHORT))
                    self.assertEqual(len(self.calls), 3)
                    self.assertTrue(check['repaired'])
                    self.assertEqual(check['status'], 'incomplete')
                    self.assertEqual(check['failed_stages'], ['check-final'])
                    self.assertIsNone(check['report'])
                    self.assertTrue(check['steps'][1]['report']['repair_needed'])


if __name__ == '__main__':
    unittest.main()
