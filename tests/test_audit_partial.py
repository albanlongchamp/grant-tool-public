"""Invented reports test exact whole-item quarantine without trusting criticism."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from grantlib import draft_check, revision_check
from grantlib.draft_pipeline import check_draft
from grantlib.revision_pipeline import check_revision
from grantlib.studio import Conflict, OUTPUT_SCHEMA, validate_output
from grantlib.studio import export_markdown
from grantlib.docx_export import export_docx
from grantlib import audit_validation
from tests import test_draft_check as dc, test_revision_check as rc
from tests import test_draft_pipeline as dp, test_revision_pipeline as rp
from tests import test_docx_export as word


SECRET = 'Excluded invented criticism must never reach repair.'


class PartialValidationTests(unittest.TestCase):
    def cases(self):
        context, candidate = dc.fixture()
        yield draft_check, context, candidate, dc.report(dc.finding()), {'action': 'aims'}
        context, candidate = rc.fixture()
        raw = rc.clean_report(); raw['findings'] = [rc.omission()]
        yield revision_check, context, candidate, raw, {}

    def mixed(self, raw):
        good = raw['findings'][0]
        good['severity'] = 'minor'
        bad = deepcopy(good)
        bad.update(id='private-arbitrary-rejected-id', summary=SECRET,
                   explanation=SECRET, repair=SECRET, severity='major')
        field = 'after' if bad['after'] else 'before'
        # Even one exact location cannot salvage an item with a second invalid one.
        bad[field].append({**bad[field][0], 'quote': SECRET})
        raw['findings'].append(bad)
        raw['summary'] = SECRET; raw['limitations'] = [SECRET]
        return raw

    def test_whole_item_quarantine_is_nonmutating_and_never_leaks_original_prose(self):
        for module, context, candidate, raw, kwargs in self.cases():
            with self.subTest(module=module.__name__):
                self.mixed(raw); snapshot = deepcopy((raw, context, candidate))
                with self.assertRaises(ValueError): module.validate(raw, context, candidate, **kwargs)
                value = module.validate_partial(raw, context, candidate, **kwargs)
                self.assertEqual(snapshot, (raw, context, candidate))
                self.assertEqual(value['findings'], [raw['findings'][0]])
                self.assertFalse(value['repair_needed'])
                self.assertEqual(value['state'], 'issues_found')
                self.assertNotIn(SECRET, json.dumps(value))
                self.assertNotIn('private-arbitrary-rejected-id', json.dumps(value))
                validation = value['validation']
                self.assertEqual(validation['status'], 'partial')
                self.assertEqual(validation['accepted_counts'], {'findings': 1, 'commitments': 0})
                self.assertEqual(validation['excluded_counts'], {'findings': 1, 'commitments': 0})
                self.assertEqual(validation['excluded_items'], [
                    {'collection': 'findings', 'position': 1, 'error_code': 'invalid_anchor'}])
                self.assertEqual(validation['hash_scope'], 'canonical_report_json')
                self.assertEqual(validation['raw_report_sha256'], hashlib.sha256(json.dumps(
                    raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
                exported = module.export_markdown(value)
                self.assertIn('Partial assessment', exported)
                self.assertIn('1 finding(s)', exported)
                self.assertNotIn(SECRET, exported)

    def test_full_valid_output_preserves_strict_report_and_zero_current_counts(self):
        for module, context, candidate, raw, kwargs in self.cases():
            for empty in (False, True):
                if empty: raw['findings'] = []
                with self.subTest(module=module.__name__, empty=empty):
                    strict = module.validate(raw, context, candidate, **kwargs)
                    value = module.validate_partial(raw, context, candidate, **kwargs)
                    validation = value.pop('validation')
                    self.assertEqual(value, strict)
                    self.assertEqual(validation['status'], 'complete')
                    self.assertEqual(validation['excluded_counts'], {'findings': 0, 'commitments': 0})
                    self.assertEqual(validation['excluded_items'], [])

    def test_global_shape_and_duplicate_ids_still_reject_whole_report(self):
        for module, context, candidate, raw, kwargs in self.cases():
            for malformed in ({**raw, 'unexpected': SECRET},
                              {**raw, 'findings': raw['findings'] * 2},
                              {**raw, 'findings': [dict(raw['findings'][0], severity='invented')]}):
                with self.subTest(module=module.__name__), self.assertRaises(ValueError):
                    module.validate_partial(malformed, context, candidate, **kwargs)

    def test_invalid_routing_and_all_rejected_are_unassessed(self):
        for module, context, candidate, raw, kwargs in self.cases():
            raw['findings'][0].update(category='missing_input', resolution='draft_repair')
            value = module.validate_partial(raw, context, candidate, **kwargs)
            self.assertEqual(value['findings'], [])
            self.assertEqual(value['state'], 'unassessed')
            self.assertFalse(value['repair_needed'])
            self.assertEqual(value['validation']['excluded_items'][0]['error_code'], 'item_rule_violation')
            self.assertNotIn('no issues reported', module.export_markdown(value))

    def test_bad_source_or_authority_cannot_be_removed_to_salvage_a_finding(self):
        for module, context, candidate, raw, kwargs in self.cases():
            for group, anchor in [('sources', {'kind': 'source', 'id': 'unknown', 'field': 'text', 'quote': SECRET}),
                                  ('authority', {'kind': 'instruction', 'id': '', 'field': 'text', 'quote': SECRET})]:
                bad = deepcopy(raw); bad['findings'][0][group] = [anchor]
                value = module.validate_partial(bad, context, candidate, **kwargs)
                self.assertEqual(value['findings'], [])
                self.assertEqual(module.source_anchors(value), [])
                self.assertEqual(value['validation']['excluded_items'][0]['error_code'], 'invalid_anchor')

    def test_valid_source_snapshot_and_exact_repair_baseline_survive(self):
        context, candidate = dc.fixture(); initial = deepcopy(candidate)
        item = dc.finding(); source = context['evidence_packet']['sources'][0]
        item['sources'] = [dc.anchor(source['text'], 'source', source['id'], 'text')]
        item['before'] = [dc.anchor(initial['markdown'], 'repair_baseline',
                                   draft_check.baseline_snapshot(initial)['candidate_sha256'])]
        raw = dc.report(item); self.mixed(raw)
        result = draft_check.validate_partial(raw, context, candidate, action='aims', repair_baseline=initial)
        self.assertEqual(result['findings'][0]['sources'][0]['quote'], source['text'])
        self.assertEqual(result['findings'][0]['sources'][0]['provenance']['metadata']['scope'],
                         source['metadata']['scope'])
        self.assertEqual(result['repair_baseline_sha256'], draft_check.baseline_snapshot(initial)['candidate_sha256'])
        self.assertIn(draft_check.BASELINE_LIMITATION, result['limitations'])

    def test_unresolved_commitments_quarantined_when_explanation_link_unavailable(self):
        context, candidate = rc.fixture()
        for findings in ([], [rc.omission()], [rc.omission(), dict(rc.omission(), id='bad')]):
            raw = rc.clean_report(); raw['findings'] = deepcopy(findings)
            if raw['findings']:
                raw['findings'][-1]['before'][0]['quote'] = SECRET
            unresolved = rc.preserved(); unresolved.update(id='C2', disposition='unclear')
            raw['commitments'] = [rc.preserved(), unresolved]
            value = revision_check.validate_partial(raw, context, candidate)
            self.assertEqual([item['id'] for item in value['commitments']], ['C1'])
            self.assertIn({'collection': 'commitments', 'position': 1,
                           'error_code': 'unresolved_commitment_link_unavailable'},
                          value['validation']['excluded_items'])
            if not value['findings']:
                self.assertEqual(value['state'], 'unassessed')
                self.assertFalse(value['repair_needed'])
            self.assertNotIn(SECRET, json.dumps(value))


class PartialPipelineTests(unittest.TestCase):
    def run_case(self, kind, responses, *, target=False, event=None):
        helper = dp if kind == 'draft' else rp
        calls = []; responses = iter(responses); event = event or threading.Event()
        def generate(prompt, **kwargs):
            calls.append((prompt, kwargs))
            response = next(responses)
            if isinstance(response, Exception): raise response
            if callable(response): return response(kwargs)
            return helper.result(response)
        context = helper.context()
        if target: context['length_target'] = {'min_words': None, 'max_words': 10}
        with tempfile.TemporaryDirectory() as directory:
            kwargs = dict(context=context, base_prompt='Invented writer input.', generate=generate,
                          schema=OUTPUT_SCHEMA, validate_candidate=lambda x: validate_output(x, {'sources': []}, 'aims'),
                          run_dir=Path(directory), on_event=lambda _: None, cancel_event=event,
                          phase=lambda _: None, deadline=time.monotonic()+900)
            if kind == 'draft': kwargs['action'] = 'aims'
            candidate, metadata = (check_draft if kind == 'draft' else check_revision)(
                helper.result(helper.output()), **kwargs)
        return candidate, metadata[kind+'_check'], calls

    def partial(self, kind, *, accepted=True):
        helper = dp if kind == 'draft' else rp
        raw = helper.report(True); bad = deepcopy(raw['findings'][0]); bad['id'] = 'rejected-only'
        group = 'after' if bad['after'] else 'before'
        bad[group][0]['quote'] = SECRET; bad['summary'] = SECRET
        raw['findings'] = (raw['findings'] if accepted else []) + [bad]
        raw['summary'] = SECRET; raw['limitations'] = [SECRET]
        return raw

    def test_partial_initial_then_full_final_has_safe_repair_and_historical_counts(self):
        for kind in ('draft', 'revision'):
            helper = dp if kind == 'draft' else rp
            with self.subTest(kind=kind):
                _, wrapper, calls = self.run_case(kind, [self.partial(kind), helper.output(), helper.report()])
                self.assertEqual(len(calls), 3)
                self.assertEqual(wrapper['status'], 'incomplete')
                self.assertEqual(wrapper['partial_stages'], ['check-initial'])
                self.assertEqual(wrapper['steps'][1]['status'], 'partial')
                self.assertEqual(wrapper['steps'][1]['validation']['excluded_counts']['findings'], 1)
                self.assertEqual(wrapper['report']['validation']['status'], 'complete')
                self.assertEqual(wrapper['report']['validation']['excluded_counts']['findings'], 0)
                self.assertEqual(wrapper['report']['findings'], [])
                self.assertNotIn(SECRET, calls[1][0])
                self.assertNotIn('rejected-only', calls[1][0])
                self.assertNotIn('raw_report_sha256', calls[2][0])
                self.assertNotIn('findings', json.loads(calls[2][0].split('\nAUDIT_INPUT_JSON:\n')[1]))

    def test_partial_final_replaces_initial_and_does_not_trigger_another_repair(self):
        for kind in ('draft', 'revision'):
            helper = dp if kind == 'draft' else rp
            _, wrapper, calls = self.run_case(kind, [helper.report(True), helper.output(), self.partial(kind, accepted=False)])
            self.assertEqual(len(calls), 3)
            self.assertEqual(wrapper['partial_stages'], ['check-final'])
            self.assertEqual(wrapper['status'], 'incomplete')
            self.assertEqual(wrapper['report']['state'], 'unassessed')
            self.assertFalse(wrapper['report']['repair_needed'])
            self.assertEqual(wrapper['report']['findings'], [])
            self.assertEqual(wrapper['steps'][1]['report']['validation']['status'], 'complete')

    def test_all_rejected_only_length_can_trigger_repair(self):
        for kind in ('draft', 'revision'):
            helper = dp if kind == 'draft' else rp
            _, wrapper, calls = self.run_case(kind, [self.partial(kind, accepted=False)])
            self.assertEqual(len(calls), 1)
            self.assertFalse(wrapper['repaired'])
            _, wrapper, calls = self.run_case(kind, [self.partial(kind, accepted=False), helper.output(), helper.report()], target=True)
            self.assertEqual(len(calls), 3)
            self.assertIn('"scientific_check_available": false', calls[1][0])
            self.assertNotIn(SECRET, calls[1][0])
            self.assertEqual(wrapper['status'], 'incomplete')

    def test_partial_cannot_swallow_cancellation_or_stale_memory(self):
        for kind in ('draft', 'revision'):
            for failure in (Conflict('Synthetic stale memory.'),):
                with self.subTest(kind=kind), self.assertRaises(Conflict):
                    self.run_case(kind, [self.partial(kind), failure])
            event = threading.Event()
            def cancelled(_kwargs):
                event.set()
                return dp.result(dp.output())
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.run_case(kind, [self.partial(kind), cancelled], event=event)


class PartialExportTests(unittest.TestCase):
    def fixture(self, kind):
        if kind == 'draft':
            context, candidate = dc.fixture()
            raw = dc.report(dc.finding()); kwargs = {'action': 'aims'}
            module = draft_check
        else:
            context, candidate = rc.fixture()
            raw = rc.clean_report(); raw['findings'] = [rc.omission()]; kwargs = {}
            module = revision_check
        bad = deepcopy(raw['findings'][0]); bad['id'] = 'excluded-arbitrary-id'
        bad['summary'] = SECRET
        group = 'after' if bad['after'] else 'before'
        bad[group][0]['quote'] = SECRET
        raw['findings'].append(bad)
        raw['findings'][0]['summary'] = 'HISTORICAL_ACCEPTED_FINDING'
        partial = module.validate_partial(raw, context, candidate, **kwargs)
        raw['findings'] = []; raw['summary'] = 'A fresh full assessment.'
        complete = module.validate_partial(raw, context, candidate, **kwargs)
        proposal = word.proposal(); artifact = proposal['artifacts'][0]
        artifact.update(markdown=candidate['markdown'], structured=candidate,
                        context=context['evidence_packet'])
        wrapper = {'status': 'incomplete', 'repaired': True,
                   'error': 'Some check items were unavailable.', 'report': complete,
                   'partial_stages': ['check-initial'], 'steps': [
                       {'stage': 'draft', 'status': 'completed'},
                       {'stage': 'check-initial', 'status': 'partial',
                        'validation': partial['validation'], 'report': partial},
                       {'stage': 'repair', 'status': 'completed'},
                       {'stage': 'check-final', 'status': 'completed',
                        'validation': complete['validation'], 'report': complete}]}
        artifact['model_metadata'] = {kind + '_check': wrapper}
        return proposal, wrapper, partial

    def exports(self, proposal):
        yield export_markdown(proposal, word.ARTIFACT_ID)
        yield word.texts(word.unpack(export_docx(proposal, word.ARTIFACT_ID))['word/document.xml'])

    def test_initial_partial_full_final_exports_safe_history_in_markdown_and_word(self):
        for kind in ('draft', 'revision'):
            proposal, wrapper, _ = self.fixture(kind)
            snapshot = deepcopy(proposal)
            for text in self.exports(proposal):
                self.assertIn('Partial-check history', text)
                self.assertIn('Initial check: Accepted: 1 finding(s), 0 commitment(s). Excluded: 1 finding(s)', text)
                self.assertIn('an exact input anchor was invalid (1)', text)
                self.assertIn('separately from the current assessment', text)
                self.assertIn('A fresh full assessment.', text)
                self.assertNotIn('HISTORICAL_ACCEPTED_FINDING', text)
                self.assertNotIn(SECRET, text)
                self.assertNotIn('excluded-arbitrary-id', text)
            self.assertEqual(proposal, snapshot)

    def test_current_partial_not_misattributed_to_history_or_exported_twice(self):
        for kind in ('draft', 'revision'):
            proposal, wrapper, partial = self.fixture(kind)
            wrapper['steps'][1] = {'stage': 'check-initial', 'status': 'completed'}
            wrapper['steps'][3] = {'stage': 'check-final', 'status': 'partial', 'report': partial}
            wrapper['report'] = partial; wrapper['partial_stages'] = ['check-final']
            for text in self.exports(proposal):
                self.assertNotIn('Partial-check history', text)
                self.assertIn('Partial assessment:', text)
                self.assertEqual(text.count('an exact input anchor was invalid (1)'), 1)
                self.assertIn('HISTORICAL_ACCEPTED_FINDING', text)  # This is now the current accepted item.
                self.assertNotIn(SECRET, text)

    def test_unavailable_final_preserves_safe_history_errors_and_draft_baseline(self):
        for kind in ('draft', 'revision'):
            proposal, wrapper, _ = self.fixture(kind)
            wrapper['report'] = None
            wrapper['steps'][3] = {'stage': 'check-final', 'status': 'failed'}
            wrapper['error'] = 'The final check was unavailable.'
            if kind == 'draft':
                wrapper['repair_baseline'] = draft_check.baseline_snapshot(proposal['artifacts'][0]['structured'])
            for text in self.exports(proposal):
                self.assertIn('Partial-check history', text)
                self.assertIn('Initial check: Accepted:', text)
                self.assertIn('The final check was unavailable.', text)
                self.assertNotIn('HISTORICAL_ACCEPTED_FINDING', text)
                self.assertNotIn(SECRET, text)
                if kind == 'draft': self.assertIn('Initial candidate before repair', text)

    def test_identical_partial_hash_omits_only_last_current_step_and_ignores_raw_diagnostics(self):
        proposal, wrapper, partial = self.fixture('draft')
        wrapper['report'] = partial
        wrapper['steps'][3] = {'stage': 'check-final', 'status': 'partial', 'validation': partial['validation']}
        wrapper['partial_stages'] = ['check-initial', 'check-final']
        text = audit_validation.export_history(wrapper)
        self.assertIn('Initial check:', text)
        self.assertNotIn('Final check:', text)
        unsafe = deepcopy(partial['validation'])
        unsafe['excluded_items'][0].update(error_code=SECRET, id=SECRET, quote=SECRET)
        unsafe['excluded_counts']['findings'] = SECRET
        safe = audit_validation.validation_summary(unsafe)
        self.assertNotIn(SECRET, safe)
        self.assertIn('unavailable', safe)


if __name__ == '__main__':
    unittest.main()
