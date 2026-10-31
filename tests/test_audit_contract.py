"""Defects-only audit contracts use invented inputs and no model service."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from grantlib import audit_contract, audit_passages, audit_requests, draft_check, revision_check
from grantlib.model import _check_schema, _validate
from tests.test_audit_requests import wire as passage_wire


CONTRACT = 'scientific_defects_v1'
PARENT_ID = 'invented-selected-parent'
OLD_ENDPOINT = 'The primary endpoint will be live flux.'
NEW_ENDPOINT = 'The primary endpoint will be fixed-image morphology.'
OLD_ANALYSIS = 'The primary model will estimate live-flux change.'
DECISION = 'Replace live flux with fixed-image morphology. Keep subject pairing and masked scoring.'
REJECTED = 'EXCLUDED CRITICISM about an invented defect.'


def fixture():
    context = {
        'brief': {'title': 'Invented resource change',
                  'question': 'What morphology contrast can the available fixed images support?',
                  'constraints': 'Live flux access is unavailable for this proposal.'},
        'investigator_instruction': 'Apply the current resource decision without inventing a substitute flux assay.',
        'investigator_decisions': [{'id': 'decision-1', 'text': DECISION}],
        'selected_direction': '',
        'selected_version': {'id': PARENT_ID, 'kind': 'aims',
                             'markdown': OLD_ENDPOINT + ' ' + OLD_ANALYSIS +
                             ' Subjects will be paired. Scoring will be masked.',
                             'claims': [], 'change_summary': ''},
        'related_versions': [],
        'evidence_packet': {'selected_memory': [], 'sources': [{
            'id': 'P_resource', 'layer': 'personal', 'title': 'Invented investigator resource statement',
            'text': 'FICTIONAL: Fixed images are available. Live flux access is unavailable. Images do not measure flux.',
            'source_uri': 'urn:fictional:audit-contract:resources',
            'metadata': {'role': 'investigator_statement', 'evidence_status': 'recorded_resources',
                         'scope': 'This invented proposal only.', 'locator': 'Invented complete note'}
        }]},
    }
    candidate = {
        'title': 'Invented narrowed aims',
        'markdown': NEW_ENDPOINT + ' Subjects will be paired. Scoring will be masked. '
                    'No live-flux measurement or inference is promised.',
        'change_summary': 'The endpoint is narrowed under the recorded decision; pairing and masking remain.',
        'claims': [], 'directions': [], 'open_questions': [],
    }
    return context, candidate


def anchor(kind, quote, field=None, identifier=None):
    return {'kind': kind,
            'id': identifier if identifier is not None else {
                'parent': PARENT_ID, 'decision': 'decision-1', 'source': 'P_resource'}.get(kind, ''),
            'field': field or ('markdown' if kind in ('parent', 'candidate', 'repair_baseline') else 'text'),
            'quote': quote}


def changed_commitment():
    return {'id': 'C1', 'description': 'Replace the unavailable endpoint under current authority.',
            'origin': 'investigator_decision', 'disposition': 'changed_with_authority',
            'before': [anchor('parent', OLD_ENDPOINT)],
            'after': [anchor('candidate', NEW_ENDPOINT)],
            'authority': [anchor('decision', DECISION)],
            'explanation': 'The recorded decision authorizes the narrower endpoint.'}


def finding(quote, *, category='design_commitment'):
    return {'id': 'F1', 'category': category, 'severity': 'major', 'resolution': 'draft_repair',
            'summary': 'The candidate still promises an unavailable outcome.',
            'explanation': 'That promise conflicts with the exact current decision and resource statement.',
            'repair': 'Keep the available morphology endpoint and remove the unsupported flux promise.',
            'before': [], 'after': [anchor('candidate', quote)],
            'authority': [anchor('decision', DECISION)],
            'sources': [anchor('source', 'Live flux access is unavailable.')]}


def report(kind, findings=(), commitments=()):
    value = {'summary': 'Limited assessment of invented scientific inputs.',
             'findings': deepcopy(list(findings)), 'limitations': []}
    if kind == 'revision':
        value['commitments'] = deepcopy(list(commitments))
    return value


def prepare(kind, context, candidate, anchor_format, baseline=None):
    if kind == 'draft':
        return audit_contract.prepare_draft(context, candidate, action='aims',
                                             repair_baseline=baseline, anchor_format=anchor_format)
    return audit_contract.prepare_revision(context, candidate, anchor_format=anchor_format)


def wire(request, raw, anchor_format):
    value = passage_wire(request, raw) if anchor_format == 'passages' else deepcopy(raw)
    value['contract_version'] = CONTRACT
    return value


class AuditContractTests(unittest.TestCase):
    def combinations(self):
        for kind in ('draft', 'revision'):
            for anchor_format in ('quotes', 'passages'):
                yield kind, anchor_format

    def validate(self, request, value, context, candidate, baseline=None):
        return request.validate_partial(value, context=context, candidate=candidate,
                                        repair_baseline=baseline)

    def test_authorized_change_confirmation_does_not_trigger_repair(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                request = prepare(kind, context, candidate, fmt)
                raw = report(kind, commitments=[changed_commitment()])
                value = wire(request, raw, fmt)
                _check_schema(request.schema)
                _validate(value, request.schema, request.schema)
                result = self.validate(request, value, context, candidate)
                self.assertEqual(result['state'], 'no_issues_reported')
                self.assertFalse(result['repair_needed'])
                self.assertEqual(result['findings'], [])
                if kind == 'revision':
                    self.assertEqual(result['commitments'][0]['disposition'], 'changed_with_authority')
                else:
                    self.assertNotIn('commitments', result)
                self.assertEqual(result['contract_version'], CONTRACT)

    def test_real_authority_violation_remains_actionable_in_both_formats(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                violation = 'We will also acquire live-flux measurements.'
                candidate['markdown'] += ' ' + violation
                request = prepare(kind, context, candidate, fmt)
                result = self.validate(request, wire(request, report(kind, [finding(violation)]), fmt),
                                       context, candidate)
                self.assertTrue(result['repair_needed'])
                self.assertEqual(result['state'], 'issues_found')
                item = result['findings'][0]
                self.assertEqual(item['authority'][0]['id'], 'decision-1')
                self.assertEqual(item['sources'][0]['provenance']['metadata']['evidence_status'],
                                 'recorded_resources')
                self.assertIn(item['after'][0]['quote'], candidate['markdown'])

    def test_authorized_endpoint_change_does_not_suppress_stale_analysis_defect(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                candidate['markdown'] += ' ' + OLD_ANALYSIS
                request = prepare(kind, context, candidate, fmt)
                defect = finding(OLD_ANALYSIS, category='estimand')
                defect.update(summary='The analysis still targets the removed endpoint.',
                              explanation='The endpoint change is authorized but the analysis has not followed it.',
                              repair='Specify the morphology contrast using the supplied design.')
                value = wire(request, report(kind, [defect], [changed_commitment()]), fmt)
                result = self.validate(request, value, context, candidate)
                self.assertTrue(result['repair_needed'])
                self.assertEqual(result['findings'][0]['category'], 'estimand')
                if kind == 'revision':
                    self.assertEqual(result['commitments'][0]['disposition'], 'changed_with_authority')

    def test_old_ambiguous_category_remains_reproducible_but_new_contract_rejects_it_globally(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                confirmation = finding(NEW_ENDPOINT, category='authorized_change')
                confirmation.update(summary='The change is authorized.',
                                    explanation='The candidate obeys the recorded decision.',
                                    repair='No se requiere reparación.')
                raw = report(kind, [confirmation], [changed_commitment()])
                original = deepcopy(raw)
                old = (draft_check.validate(raw, context, candidate, action='aims') if kind == 'draft'
                       else revision_check.validate(raw, context, candidate))
                self.assertTrue(old['repair_needed'])  # Historical behavior, not a scientific endorsement.
                if fmt == 'passages':
                    legacy = (audit_requests.prepare_draft(context, candidate, action='aims') if kind == 'draft'
                              else audit_requests.prepare_revision(context, candidate))
                    legacy_result = legacy.validate_partial(passage_wire(legacy, raw),
                                                             context=context, candidate=candidate)
                    self.assertTrue(legacy_result['repair_needed'])
                request = prepare(kind, context, candidate, fmt)
                enum = request.schema['properties']['findings']['items']['properties']['category']['enum']
                self.assertNotIn('authorized_change', enum)
                # Do not silently turn this report into a clean or partial result.
                with self.assertRaises(ValueError):
                    self.validate(request, wire(request, raw, fmt), context, candidate)
                self.assertEqual(raw, original)

    def test_missing_input_routing_and_required_authority_remain_enforced(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                unknown = 'The morphology scoring threshold remains to be prespecified.'
                candidate['markdown'] += ' ' + unknown
                request = prepare(kind, context, candidate, fmt)
                item = finding(unknown, category='missing_input')
                item['resolution'] = 'investigator_input'
                good = self.validate(request, wire(request, report(kind, [item]), fmt), context, candidate)
                self.assertFalse(good['repair_needed'])
                self.assertEqual(good['validation']['status'], 'complete')
                item['resolution'] = 'draft_repair'
                bad = self.validate(request, wire(request, report(kind, [item]), fmt), context, candidate)
                self.assertEqual(bad['state'], 'unassessed')
                self.assertEqual(bad['validation']['excluded_counts']['findings'], 1)
                self.assertFalse(bad['repair_needed'])
                if kind == 'revision':
                    commitment = changed_commitment(); commitment['authority'] = []
                    bad = self.validate(request, wire(request, report(kind, commitments=[commitment]), fmt),
                                        context, candidate)
                    self.assertEqual(bad['commitments'], [])
                    self.assertEqual(bad['validation']['excluded_counts']['commitments'], 1)

    def test_raw_wire_hash_binds_contract_version_and_normalized_metadata_is_stable(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                request = prepare(kind, context, candidate, fmt)
                value = wire(request, report(kind, commitments=[changed_commitment()]), fmt)
                snapshot = deepcopy(value)
                result = self.validate(request, value, context, candidate)
                expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                     allow_nan=False).encode()).hexdigest()
                self.assertEqual(result['validation']['raw_report_sha256'], expected)
                self.assertEqual(result['validation']['hash_scope'], 'canonical_report_json')
                without_version = deepcopy(value); without_version.pop('contract_version')
                old_hash = hashlib.sha256(json.dumps(without_version, ensure_ascii=False,
                                                    sort_keys=True, allow_nan=False).encode()).hexdigest()
                self.assertNotEqual(expected, old_hash)
                self.assertEqual(result['audit_contract']['anchor_format'], fmt)
                self.assertEqual(result['audit_contract']['hash_scope'], 'canonical_audit_input_json')
                self.assertEqual(len(result['audit_contract']['input_sha256']), 64)
                self.assertEqual(result, self.validate(request, value, context, candidate))
                self.assertEqual(value, snapshot)
                for invalid in (None, 'another_contract'):
                    changed = deepcopy(value)
                    if invalid is None:
                        changed.pop('contract_version')
                    else:
                        changed['contract_version'] = invalid
                    with self.assertRaises(ValueError):
                        self.validate(request, changed, context, candidate)

    def test_changed_candidate_decision_source_or_parent_cannot_reuse_prepared_request(self):
        for kind, fmt in self.combinations():
            for field in ('candidate', 'decision', 'source_scope', 'parent'):
                with self.subTest(kind=kind, anchor_format=fmt, field=field):
                    context, candidate = fixture()
                    request = prepare(kind, context, candidate, fmt)
                    value = wire(request, report(kind), fmt)
                    if field == 'candidate':
                        candidate['markdown'] += ' A changed scientific statement.'
                    elif field == 'decision':
                        context['investigator_decisions'][0]['text'] += ' Changed authority.'
                    elif field == 'source_scope':
                        context['evidence_packet']['sources'][0]['metadata']['scope'] = 'Different scope.'
                    else:
                        context['selected_version']['markdown'] += ' A new parent qualification.'
                    with self.assertRaises(ValueError):
                        self.validate(request, value, context, candidate)

    def test_draft_repair_baseline_is_bound_and_cannot_be_promoted_to_authority(self):
        for fmt in ('quotes', 'passages'):
            with self.subTest(anchor_format=fmt):
                context, candidate = fixture()
                baseline = deepcopy(candidate)
                request = prepare('draft', context, candidate, fmt, baseline)
                digest = draft_check.baseline_snapshot(baseline)['candidate_sha256']
                item = finding(NEW_ENDPOINT)
                item['authority'] = [anchor('repair_baseline', NEW_ENDPOINT, identifier=digest)]
                value = wire(request, report('draft', [item]), fmt)
                if fmt == 'quotes':
                    # The quote schema names kinds directly; repair_baseline is
                    # legal only in before. IDs defer role checks until lookup.
                    with self.assertRaises(ValueError):
                        self.validate(request, value, context, candidate, baseline)
                else:
                    bad = self.validate(request, value, context, candidate, baseline)
                    self.assertEqual(bad['state'], 'unassessed')
                    self.assertFalse(bad['repair_needed'])
                baseline['markdown'] += ' A changed intermediate candidate.'
                with self.assertRaises(ValueError):
                    self.validate(request, wire(request, report('draft'), fmt), context, candidate, baseline)

    def test_partial_quarantine_never_leaks_invalid_item_or_original_summary_into_feedback(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                violation = 'We will acquire live-flux data despite the withdrawal.'
                candidate['markdown'] += ' ' + violation
                request = prepare(kind, context, candidate, fmt)
                raw = report(kind, [finding(violation)])
                raw['summary'] = 'ORIGINAL summary repeats excluded criticism.'
                raw['limitations'] = ['ORIGINAL model limitation repeats excluded criticism.']
                value = wire(request, raw, fmt)
                bad = deepcopy(value['findings'][0]); bad['id'] = 'bad-item'
                bad.update(summary=REJECTED, explanation=REJECTED, repair=REJECTED)
                bad['after'] = ([{'passage_id': 'unknown-passage'}] if fmt == 'passages'
                                else [anchor('candidate', 'Absent invented quotation.')])
                value['findings'].append(bad)
                snapshot = deepcopy(value)
                result = self.validate(request, value, context, candidate)
                self.assertEqual(result['validation']['status'], 'partial')
                self.assertEqual([f['id'] for f in result['findings']], ['F1'])
                self.assertTrue(result['repair_needed'])
                rendered = json.dumps(result)
                for excluded in (REJECTED, 'ORIGINAL summary', 'ORIGINAL model limitation', 'bad-item'):
                    self.assertNotIn(excluded, rendered)
                self.assertEqual(result['contract_version'], CONTRACT)
                self.assertEqual(value, snapshot)

    def test_prepared_schema_payload_and_input_objects_are_not_mutated_by_callers_or_validation(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                original = deepcopy((context, candidate))
                request = prepare(kind, context, candidate, fmt)
                schema, scientific = request.schema, request.payload
                saved_schema, saved_payload = deepcopy(schema), deepcopy(scientific)
                schema['properties']['contract_version']['enum'] = ['FORGED']
                scientific['candidate']['markdown'] = 'FORGED'
                value = wire(request, report(kind, commitments=[changed_commitment()]), fmt)
                self.validate(request, value, context, candidate)
                self.assertEqual(request.schema, saved_schema)
                self.assertEqual(request.payload, saved_payload)
                self.assertEqual((context, candidate), original)
                with self.assertRaises((AttributeError, TypeError)):
                    request.prompt = 'FORGED'

    def test_quote_format_never_builds_registry_or_inherits_passage_only_limits(self):
        for kind in ('draft', 'revision'):
            with self.subTest(kind=kind):
                context, candidate = fixture()
                with patch.object(audit_passages.Registry, 'build', side_effect=AssertionError('No registry in quote path')):
                    with patch.object(audit_passages, 'MAX_REGISTRY_BYTES', 1), \
                            patch.object(audit_requests, 'MAX_PROMPT_BYTES', 1):
                        request = prepare(kind, context, candidate, 'quotes')
                        result = self.validate(request, wire(request, report(kind), 'quotes'), context, candidate)
                self.assertNotIn('passage_registry', request.prompt)
                self.assertNotIn('registry_sha256', request.schema['properties'])
                self.assertNotIn('anchor_registry', result)
                self.assertEqual(result['audit_contract']['anchor_format'], 'quotes')

    def test_quote_validation_uses_snapshot_if_caller_changes_after_binding_check(self):
        for kind, with_baseline in (('draft', False), ('draft', True), ('revision', False)):
            with self.subTest(kind=kind, baseline=with_baseline):
                context, candidate = fixture()
                baseline = deepcopy(candidate) if with_baseline else None
                request = prepare(kind, context, candidate, 'quotes', baseline)
                expected = deepcopy((context, candidate, baseline))
                value = wire(request, report(kind, [finding(NEW_ENDPOINT)]), 'quotes')
                module = draft_check if kind == 'draft' else revision_check
                original_validate = module.validate_partial

                def mutate_caller(raw, bound_context, bound_candidate, **kwargs):
                    context['investigator_decisions'][0]['text'] = 'Changed caller authority.'
                    context['evidence_packet']['sources'][0]['metadata']['scope'] = 'Changed caller scope.'
                    candidate['markdown'] = 'Changed caller candidate.'
                    if baseline is not None:
                        baseline['markdown'] = 'Changed caller baseline.'
                    self.assertEqual((bound_context, bound_candidate, kwargs.get('repair_baseline')), expected)
                    return original_validate(raw, bound_context, bound_candidate, **kwargs)

                with patch.object(module, 'validate_partial', side_effect=mutate_caller):
                    result = self.validate(request, value, context, candidate, baseline)
                self.assertEqual(result['validation']['status'], 'complete')
                self.assertEqual(result['findings'][0]['sources'][0]['provenance']['metadata']['scope'],
                                 expected[0]['evidence_packet']['sources'][0]['metadata']['scope'])
                self.assertEqual(request.payload['candidate'], expected[1])

    def test_closed_schema_limits_remain_global_failures_in_both_formats(self):
        for kind, fmt in self.combinations():
            with self.subTest(kind=kind, anchor_format=fmt):
                context, candidate = fixture()
                request = prepare(kind, context, candidate, fmt)
                raw = report(kind, [finding(NEW_ENDPOINT)])
                valid = wire(request, raw, fmt)
                _check_schema(request.schema)
                _validate(valid, request.schema, request.schema)
                variants = []
                changed = deepcopy(valid); changed['repair_needed'] = False; variants.append(changed)
                changed = deepcopy(valid); changed['findings'][0]['severity'] = 'unrecognized'; variants.append(changed)
                changed = deepcopy(valid); changed['findings'].append(deepcopy(changed['findings'][0])); variants.append(changed)
                changed = deepcopy(valid)
                changed['findings'] = [{**deepcopy(valid['findings'][0]), 'id': f'F{i}'} for i in range(41)]
                variants.append(changed)
                for value in variants:
                    with self.assertRaises(ValueError):
                        self.validate(request, value, context, candidate)
                with self.assertRaises(ValueError):
                    prepare(kind, context, candidate, 'unrecognized-format')


if __name__ == '__main__':
    unittest.main()
