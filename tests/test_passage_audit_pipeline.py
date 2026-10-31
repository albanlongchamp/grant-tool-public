"""Experimental passage requests use only invented, injected model responses."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from grantlib import audit_context, audit_requests, draft_check, draft_metrics
from grantlib.draft_pipeline import check_draft
from grantlib.revision_pipeline import check_revision
from grantlib.model import ModelCancelled
from grantlib.studio import Conflict, OUTPUT_SCHEMA, validate_output
from tests import test_draft_pipeline as dp, test_revision_pipeline as rp


REJECTED = 'EXCLUDED invented criticism must not enter repair.'
ORIGINAL_SUMMARY = 'ORIGINAL mixed-report summary must not enter repair.'
SOURCE = {
    'id': 'P_invented_plan', 'layer': 'personal', 'title': 'Invented collaborator plan',
    'text': 'FICTIONAL: The collaborator proposes masked imaging. No results are reported.',
    'source_uri': 'urn:fictional:passage-pipeline:plan',
    'metadata': {'role': 'collaborator_or_template', 'evidence_status': 'proposed',
                 'locator': 'Invented paragraph 1', 'scope': 'Proposed work only.'},
}


def payload(prompt):
    return json.loads(prompt.split(draft_check.INPUT_DELIMITER, 1)[1])


def wire_report(raw, request_payload):
    """Select exact supplied passages for existing synthetic legacy fixtures."""
    value = deepcopy(raw)
    registry = request_payload['passage_registry']
    value['registry_sha256'] = registry['registry_sha256']
    for collection in ('findings', 'commitments'):
        for item in value.get(collection, []):
            for field in ('before', 'after', 'authority', 'sources'):
                if field not in item:
                    continue
                selected = []
                for anchor in item[field]:
                    matches = [p for p in registry['passages']
                               if p['kind'] == anchor['kind'] and p['id'] == anchor['id']
                               and p['field'] == anchor['field'] and anchor['quote'] in p['text']]
                    if not matches:
                        raise AssertionError('The invented fixture quote needs a supplied passage.')
                    selected.append({'passage_id': matches[0]['passage_id']})
                item[field] = selected
    return value


class PassageAuditPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.requests = []
        self.event = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, kind, *, target=False):
        helper = dp if kind == 'draft' else rp
        context = helper.context()
        context['evidence_packet']['sources'] = [deepcopy(SOURCE)]
        if target:
            context['length_target'] = {
                'min_words': None,
                'max_words': draft_metrics.measure(helper.output(helper.SHORT))['words'],
            }
        return helper, context

    def issue(self, kind, *, final=False):
        helper = dp if kind == 'draft' else rp
        if kind == 'draft':
            report = helper.report(True, quote=helper.SHORT if final else helper.OVERCLAIM)
        else:
            report = helper.report(True)
        report['findings'][0]['sources'] = [{
            'kind': 'source', 'id': SOURCE['id'], 'field': 'text',
            'quote': 'The collaborator proposes masked imaging.',
        }]
        return report

    def mixed(self, kind, request_payload, *, minor=False, all_rejected=False):
        raw = self.issue(kind)
        raw['summary'] = ORIGINAL_SUMMARY
        raw['limitations'] = ['ORIGINAL limitations describing excluded criticism.']
        raw['findings'][0]['id'] = 'retained-finding'
        if minor:
            raw['findings'][0]['severity'] = 'minor'
        wire = wire_report(raw, request_payload)
        bad = deepcopy(wire['findings'][0])
        bad['id'] = 'excluded-finding'
        bad['summary'] = REJECTED
        group = 'after' if bad['after'] else 'before'
        bad[group] = [{'passage_id': 'no-such-passage-in-this-registry'}]
        wire['findings'] = [bad] if all_rejected else [wire['findings'][0], bad]
        return wire

    def run_pipeline(self, kind, responder, *, target=False, context=None,
                     legacy=False, deadline=None):
        self.calls.clear()
        self.requests.clear()
        helper, default_context = self.fixture(kind, target=target)
        context = default_context if context is None else context
        prepare = (audit_requests.prepare_draft if kind == 'draft'
                   else audit_requests.prepare_revision)

        def factory(current_context, candidate, **kwargs):
            request = prepare(current_context, candidate, **kwargs)
            self.requests.append({'request': request, 'context': current_context,
                                  'candidate': candidate, 'baseline': kwargs.get('repair_baseline')})
            return request

        def generate(prompt, **kwargs):
            stage = kwargs['run_dir'].name
            data = payload(prompt) if stage.startswith('check-') else json.loads(
                prompt.split('CANDIDATE AND ATTRIBUTED MODEL FINDINGS (JSON):\n', 1)[1])
            self.calls.append({'stage': stage, 'prompt': prompt, 'payload': data, 'kwargs': kwargs})
            return helper.result(responder(stage, data, kwargs))

        arguments = dict(context=context, base_prompt='Original invented writing instructions.',
                         generate=generate, schema=OUTPUT_SCHEMA,
                         validate_candidate=lambda v: validate_output(v, context['evidence_packet'], 'aims'),
                         run_dir=self.root / kind, on_event=lambda _: None,
                         cancel_event=self.event, phase=lambda _: None,
                         deadline=time.monotonic() + 60 if deadline is None else deadline)
        if not legacy:
            arguments['audit_factory'] = factory
        if kind == 'draft':
            arguments['action'] = 'aims'
        run = check_draft if kind == 'draft' else check_revision
        return run(helper.result(helper.output()), **arguments)

    def test_default_path_stays_legacy_and_clean_experimental_check_makes_one_call(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)
            with self.subTest(kind=kind, legacy=True):
                _, metadata = self.run_pipeline(kind, lambda *_: helper.report(), legacy=True)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.requests, [])
                self.assertNotIn('passage_registry', self.calls[0]['payload'])
                self.assertEqual(metadata[kind + '_check']['status'], 'completed')
            with self.subTest(kind=kind, legacy=False):
                _, metadata = self.run_pipeline(
                    kind, lambda _stage, data, _kw: wire_report(helper.report(), data))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(metadata[kind + '_check']['status'], 'completed')
                self.assertFalse(metadata[kind + '_check']['repaired'])
                schema = self.calls[0]['kwargs']['schema']
                anchor = schema['properties']['findings']['items']['properties']['after']['items']
                self.assertEqual(set(anchor['properties']), {'passage_id'})
                self.assertEqual(schema['properties']['registry_sha256']['enum'],
                                 [self.calls[0]['payload']['passage_registry']['registry_sha256']])

    def test_one_repair_and_fresh_final_request_bind_exact_candidate_parent_and_baseline(self):
        for kind in ('draft', 'revision'):
            helper, context = self.fixture(kind)
            original_context = deepcopy(context)

            def respond(stage, data, _kw):
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                # A remaining major finding cannot start a second repair.
                return wire_report(self.issue(kind, final=stage == 'check-final'), data)

            with self.subTest(kind=kind):
                candidate, metadata = self.run_pipeline(kind, respond, context=context)
                check = metadata[kind + '_check']
                self.assertEqual([c['stage'] for c in self.calls], ['check-initial', 'repair', 'check-final'])
                self.assertEqual(candidate, helper.output(helper.SHORT))
                first, final = self.calls[0]['payload'], self.calls[2]['payload']
                self.assertEqual(first['candidate'], helper.output())
                self.assertEqual(final['candidate'], candidate)
                self.assertNotEqual(first['passage_registry']['registry_sha256'],
                                    final['passage_registry']['registry_sha256'])
                self.assertEqual(first['context'], final['context'])
                self.assertEqual(context, original_context)
                if kind == 'draft':
                    self.assertNotIn('repair_baseline', first)
                    self.assertEqual(final['repair_baseline'], draft_check.baseline_snapshot(helper.output()))
                    self.assertTrue(any(p['kind'] == 'repair_baseline'
                                        for p in final['passage_registry']['passages']))
                else:
                    self.assertEqual(final['context']['selected_version'], context['selected_version'])
                    self.assertNotIn('repair_baseline', final)
                self.assertTrue(check['repaired'])
                self.assertTrue(check['report']['repair_needed'])
                self.assertEqual(check['status'], 'completed')
                source = check['report']['findings'][0]['sources'][0]
                self.assertEqual(source['kind'], 'source')
                self.assertEqual(source['provenance']['metadata']['role'], 'collaborator_or_template')
                self.assertEqual(source['provenance']['metadata']['evidence_status'], 'proposed')
                self.assertIn(source['quote'], SOURCE['text'])
                self.assertIn('passage_binding', source)

    def test_partial_initial_feedback_is_resolved_without_excluded_prose_or_extra_repair(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)
            raw_first = []

            def respond(stage, data, _kw):
                if stage == 'check-initial':
                    wire = self.mixed(kind, data)
                    raw_first.append(deepcopy(wire))
                    return wire
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                return wire_report(helper.report(), data)

            with self.subTest(kind=kind):
                _, metadata = self.run_pipeline(kind, respond)
                check = metadata[kind + '_check']
                self.assertEqual(len(self.calls), 3)
                self.assertEqual(check['status'], 'incomplete')
                self.assertEqual(check['partial_stages'], ['check-initial'])
                self.assertEqual(check['report']['validation']['status'], 'complete')
                repair = self.calls[1]['payload'][kind + '_check']
                self.assertEqual([f['id'] for f in repair['findings']], ['retained-finding'])
                for text in (REJECTED, ORIGINAL_SUMMARY, 'ORIGINAL limitations', 'excluded-finding'):
                    self.assertNotIn(text, self.calls[1]['prompt'])
                self.assertNotIn('retained-finding', self.calls[2]['prompt'])
                expected = hashlib.sha256(json.dumps(raw_first[0], ensure_ascii=False,
                                                     sort_keys=True, allow_nan=False).encode()).hexdigest()
                self.assertEqual(repair['validation']['raw_report_sha256'], expected)
                anchor = repair['findings'][0]['after' if kind == 'draft' else 'before'][0]
                self.assertIn('quote', anchor)
                self.assertIn('passage_binding', anchor)

    def test_minor_or_all_rejected_findings_do_not_repair_without_length_miss(self):
        for kind in ('draft', 'revision'):
            for rejected in (False, True):
                with self.subTest(kind=kind, all_rejected=rejected):
                    _, metadata = self.run_pipeline(kind, lambda _s, data, _kw:
                        self.mixed(kind, data, minor=True, all_rejected=rejected))
                    check = metadata[kind + '_check']
                    self.assertEqual(len(self.calls), 1)
                    self.assertFalse(check['repaired'])
                    self.assertFalse(check['report']['repair_needed'])
                    self.assertEqual(check['report']['state'], 'unassessed' if rejected else 'issues_found')
                    self.assertEqual(check['status'], 'incomplete')

    def test_all_rejected_still_allows_same_length_repair_without_scientific_clearance(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)

            def respond(stage, data, _kw):
                if stage == 'check-initial':
                    return self.mixed(kind, data, all_rejected=True)
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                return wire_report(helper.report(), data)

            with self.subTest(kind=kind):
                _, metadata = self.run_pipeline(kind, respond, target=True)
                self.assertEqual(len(self.calls), 3)
                self.assertFalse(self.calls[1]['payload']['scientific_check_available'])
                self.assertNotIn(REJECTED, self.calls[1]['prompt'])
                self.assertEqual(metadata['length_check']['status'], 'within_target')
                self.assertEqual(metadata[kind + '_check']['status'], 'incomplete')

    def test_final_all_rejected_replaces_initial_report_and_stops_after_one_repair(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)

            def respond(stage, data, _kw):
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                raw = wire_report(self.issue(kind, final=stage == 'check-final'), data)
                if stage == 'check-final':
                    group = 'after' if kind == 'draft' else 'before'
                    raw['findings'][0][group] = [{'passage_id': 'absent-final-passage'}]
                return raw

            with self.subTest(kind=kind):
                _, metadata = self.run_pipeline(kind, respond)
                check = metadata[kind + '_check']
                self.assertEqual(len(self.calls), 3)
                self.assertTrue(check['repaired'])
                self.assertEqual(check['partial_stages'], ['check-final'])
                self.assertEqual(check['report']['state'], 'unassessed')
                self.assertEqual(check['report']['findings'], [])

    def test_wrong_role_passage_rejects_whole_item_even_with_valid_source_identity(self):
        for kind in ('draft', 'revision'):
            def respond(_stage, data, _kw):
                raw = wire_report(self.issue(kind), data)
                source_id = next(p['passage_id'] for p in data['passage_registry']['passages']
                                 if p['kind'] == 'source')
                raw['findings'][0]['authority'] = [{'passage_id': source_id}]
                return raw

            with self.subTest(kind=kind):
                _, metadata = self.run_pipeline(kind, respond)
                check = metadata[kind + '_check']
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(check['report']['findings'], [])
                self.assertEqual(check['report']['validation']['excluded_counts']['findings'], 1)
                self.assertFalse(check['repaired'])

    def test_wrong_registry_is_fatal_to_report_not_partial_item_acceptance(self):
        for kind in ('draft', 'revision'):
            def respond(_stage, data, _kw):
                raw = wire_report(self.issue(kind), data)
                raw['registry_sha256'] = '0' * 64
                return raw

            with self.subTest(kind=kind):
                _, metadata = self.run_pipeline(kind, respond)
                check = metadata[kind + '_check']
                self.assertEqual(len(self.calls), 1)
                self.assertIsNone(check['report'])
                self.assertEqual(check['failed_stages'], ['check-initial'])
                self.assertNotIn('partial_stages', check)
                self.assertFalse(check['repaired'])

    def test_input_mutation_after_request_is_rejected_before_accepting_even_clean_report(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)
            for changed in ('candidate', 'source_provenance', 'baseline'):
                if changed == 'baseline' and kind == 'revision':
                    continue

                def respond(stage, data, _kw):
                    if changed == 'baseline' and stage == 'check-initial':
                        return wire_report(self.issue(kind), data)
                    if stage == 'repair':
                        return helper.output(helper.SHORT)
                    raw = wire_report(helper.report(), data)
                    captured = self.requests[-1]
                    if changed == 'candidate':
                        captured['candidate']['markdown'] += ' Invented changed wording.'
                    elif changed == 'source_provenance':
                        captured['context']['evidence_packet']['sources'][0]['metadata']['scope'] = 'Changed scope.'
                    else:
                        captured['baseline']['markdown'] += ' Changed initial snapshot.'
                    return raw

                with self.subTest(kind=kind, changed=changed):
                    _, metadata = self.run_pipeline(kind, respond)
                    check = metadata[kind + '_check']
                    stage = 'check-final' if changed == 'baseline' else 'check-initial'
                    self.assertEqual(len(self.calls), 3 if changed == 'baseline' else 1)
                    self.assertIsNone(check['report'])
                    self.assertEqual(check['failed_stages'], [stage])
                    self.assertEqual(check['status'], 'incomplete')

    def test_audits_keep_clean_context_and_final_does_not_receive_earlier_findings(self):
        for kind in ('draft', 'revision'):
            helper, context = self.fixture(kind)
            if kind == 'draft':
                context['selected_version'] = deepcopy(rp.context()['selected_version'])
            context['selected_version']['previous_draft_check'] = {'report': 'OLD AUTOMATIC OPINION'}
            original = deepcopy(context)
            cleaned, _ = audit_context.prepare(context)

            def respond(stage, data, _kw):
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                return wire_report(self.issue(kind) if stage == 'check-initial' else helper.report(), data)

            with self.subTest(kind=kind):
                self.run_pipeline(kind, respond, context=context)
                for index in (0, 2):
                    self.assertEqual(self.calls[index]['payload']['context'], cleaned)
                    self.assertNotIn('OLD AUTOMATIC OPINION', self.calls[index]['prompt'])
                self.assertNotIn(self.issue(kind)['findings'][0]['id'], self.calls[2]['prompt'])
                self.assertEqual(context, original)

    def test_cancellation_and_conflict_propagate_without_additional_request(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)
            for stop_stage in ('check-initial', 'repair', 'check-final'):
                for failure in ('event', 'provider_cancel', 'conflict'):
                    self.event.clear()

                    def respond(stage, data, _kw):
                        if stage == stop_stage:
                            if failure == 'conflict':
                                raise Conflict('Invented selected memory changed.')
                            if failure == 'provider_cancel':
                                raise ModelCancelled()
                            self.event.set()
                        if stage == 'repair':
                            return helper.output(helper.SHORT)
                        return wire_report(self.issue(kind) if stage == 'check-initial' else helper.report(), data)

                    with self.subTest(kind=kind, stage=stop_stage, failure=failure):
                        with self.assertRaises((RuntimeError, ModelCancelled, Conflict)):
                            self.run_pipeline(kind, respond)
                        self.assertEqual(len(self.calls),
                                         {'check-initial': 1, 'repair': 2, 'check-final': 3}[stop_stage])
            self.event.clear()

    def test_shared_deadline_is_bounded_and_expired_request_never_calls_generator(self):
        for kind in ('draft', 'revision'):
            helper, _ = self.fixture(kind)

            def respond(stage, data, _kw):
                if stage == 'repair':
                    return helper.output(helper.SHORT)
                return wire_report(self.issue(kind) if stage == 'check-initial' else helper.report(), data)

            with self.subTest(kind=kind, expired=False):
                self.run_pipeline(kind, respond, deadline=time.monotonic() + 10)
                budgets = [c['kwargs']['timeout'] for c in self.calls]
                self.assertEqual(len(budgets), 3)
                self.assertTrue(all(0 < value <= 10 for value in budgets))
                self.assertEqual(budgets, sorted(budgets, reverse=True))
            with self.subTest(kind=kind, expired=True):
                _, metadata = self.run_pipeline(kind, respond, deadline=time.monotonic() - 1)
                self.assertEqual(self.calls, [])
                self.assertEqual(metadata[kind + '_check']['status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
